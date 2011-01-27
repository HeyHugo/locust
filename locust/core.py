import gevent
from gevent import monkey

monkey.patch_all(thread=False)

from time import time
import random
import socket
from hashlib import md5
from hotqueue import HotQueue

import web
from clients import HTTPClient, HttpBrowser
from stats import RequestStats


def require_once(required_func):
    """
    @require_once decorator is used on a locust task in order to make sure another locust 
    task (the argument to require_once) is run once (per client) before the decorated 
    task.
    
    The require_once decorator respects the wait time of the Locust class, by inserting the
    locust tasks at the beginning of the task execution queue.
    
    Example::
    
        def login(l):
            l.client.post("/login", {"username":"joe_hill", "password":"organize"})
        
        @require_once(login)
        def inbox(l):
            l.client.get("/inbox")
    """

    def decorator_func(func):
        def wrapper(l):
            if not "_required_once" in l.__dict__:
                l.__dict__["_required_once"] = {}

            if not str(required_func) in l._required_once:
                # when the required task has not been run in the current client, we schedule it to
                # be the next task in queue, and we also reschedule the original task to be run
                # immediately after the required task
                l._required_once[str(required_func)] = True
                l.schedule_task(func, first=True)
                l.schedule_task(required_func, first=True)
                return

            return func(l)

        return wrapper

    return decorator_func


class LocustMeta(type):
    """
    Meta class for the main Locust class. It's used to allow Locust classes to specify task execution 
    ratio using an {int:task} dict.
    """

    def __new__(meta, classname, bases, classDict):
        if "tasks" in classDict and isinstance(classDict["tasks"], dict):
            tasks = []
            for count, task in classDict["tasks"].iteritems():
                for i in xrange(0, count):
                    tasks.append(task)
            classDict["tasks"] = tasks

        return type.__new__(meta, classname, bases, classDict)


class Locust(object):
    """
    Locust base class defining a locust user/client.
    """

    """Minimum waiting time between two execution of locust tasks"""
    min_wait = 1000

    """Maximum waiting time between the execution of locust tasks"""
    max_wait = 1000

    """Base hostname to swarm. i.e: http://127.0.0.1:1234"""
    host = None

    """Number of seconds after which the Locust will die. If None it won't timeout."""
    stop_timeout = None

    __metaclass__ = LocustMeta

    def __init__(self):
        self.client = HttpBrowser(self.host)
        self._task_queue = []
        self._time_start = time()

    def __call__(self):
        while True:
            if (
                self.stop_timeout is not None
                and time() - self._time_start > self.stop_timeout
            ):
                return

            if not self._task_queue:
                self.schedule_task(self.get_next_task())
            self._task_queue.pop(0)(self)
            self.wait()

    def schedule_task(self, task, first=False):
        if first:
            self._task_queue.insert(0, task)
        else:
            self._task_queue.append(task)

    def get_next_task(self):
        return random.choice(self.tasks)

    def wait(self):
        gevent.sleep(random.randint(self.min_wait, self.max_wait) / 1000.0)


locusts = []
locust_runner = None


def hatch(locust, hatch_rate, num_clients, host=None, stop_timeout=None):
    if host is not None:
        locust.host = host
    if stop_timeout is not None:
        locust.stop_timeout = stop_timeout

    print "Hatching and swarming %i clients at the rate %i clients/s..." % (
        num_clients,
        hatch_rate,
    )
    while True:
        for i in range(0, hatch_rate):
            if len(locusts) >= num_clients:
                print "All locusts hatched"
                gevent.joinall(locusts)
                return
            new_locust = gevent.spawn(locust())
            new_locust.link(on_death)
            locusts.append(new_locust)
        print "%i locusts hatched" % (len(locusts))
        gevent.sleep(1)


def on_death(locust):
    locusts.remove(locust)
    if len(locusts) == 0:
        print "All locusts dead"


class LocustRunner(object):
    def __init__(self, locust_class, hatch_rate, num_clients, host=None):
        self.locust_class = locust_class
        self.hatch_rate = hatch_rate
        self.num_clients = num_clients
        self.host = host

    @property
    def request_stats(self):
        return RequestStats.requests


class LocalLocustRunner(LocustRunner):
    def start_hatching(self):
        hatch_greenlet = gevent.spawn(
            hatch, self.locust_class, self.hatch_rate, self.num_clients, self.host
        )


class DistributedLocustRunner(LocustRunner):
    def __init__(
        self,
        locust_class,
        hatch_rate,
        num_clients,
        host=None,
        redis_host="localhost",
        redis_port=6379,
    ):
        super(DistributedLocustRunner, self).__init__(
            locust_class, hatch_rate, num_clients, host
        )

        # set up the redis queus that will be used to communicate between master and slaves
        self.work_queue = HotQueue(
            "locust_work_queue", host=redis_host, port=redis_port, db=0
        )
        self.client_report_queue = HotQueue(
            "locust_client_report_queue", host=redis_host, port=redis_port, db=0
        )
        self.stats_report_queue = HotQueue(
            "locust_stats_report_queue", host=redis_host, port=redis_port, db=0
        )


class MasterLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(MasterLocustRunner, self).__init__(*args, **kwargs)
        self.ready_clients = []
        self.client_stats = {}
        gevent.spawn(self.client_tracker)
        gevent.spawn(self.stats_aggregator)

    def start_hatching(self):
        print "starting to hatch..", self.ready_clients
        for client in self.ready_clients:
            self.work_queue.put(
                {
                    "hatch_rate": self.hatch_rate,
                    "num_clients": self.num_clients,
                    "host": self.host,
                    "stop_timeout": 30,
                }
            )

    def client_tracker(self):
        for client in self.client_report_queue.consume():
            self.ready_clients.append(client)
            print "Client %r reported as ready. Currently %i clients ready to swarm." % (
                client,
                len(self.ready_clients),
            )

    def stats_aggregator(self):
        for report in self.stats_report_queue.consume():
            if not report["stats"]:
                continue
            # print "stats report recieved from %s:" % report["client_id"], report["stats"]
            self.client_stats[report["client_id"]] = report["stats"]

    @property
    def request_stats(self):
        stats = {}
        for client_id, client_stats in self.client_stats.iteritems():
            for entry_name, entry in client_stats.iteritems():
                stats[entry_name] = (
                    stats.setdefault(entry_name, RequestStats(entry_name)) + entry
                )
        return stats


class SlaveLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(SlaveLocustRunner, self).__init__(*args, **kwargs)
        self.client_id = (
            socket.gethostname()
            + "_"
            + md5(str(time() + random.randint(0, 10000))).hexdigest()
        )
        self.client_report_queue.put(self.client_id)
        gevent.spawn(self.worker)
        gevent.spawn(self.stats_reporter)

    def start_hatching(self):
        raise Exception("Should never be called for a slave process")

    def worker(self):
        for job in self.work_queue.consume():
            print "job recieved: %r" % job
            hatch(
                self.locust_class,
                job["hatch_rate"],
                job["num_clients"],
                job["host"],
                stop_timeout=job["stop_timeout"],
            )

    def stats_reporter(self):
        while True:
            self.stats_report_queue.put(
                {"client_id": self.client_id, "stats": self.request_stats,}
            )
            gevent.sleep(5)

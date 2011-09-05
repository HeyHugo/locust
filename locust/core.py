import gevent
from gevent import monkey, GreenletExit
from gevent.pool import Group

monkey.patch_all(thread=False)

from time import time
import sys
import random
import socket
import warnings
import traceback
from hashlib import md5

from locust.stats import print_percentile_stats
from clients import HttpBrowser
from stats import RequestStats, print_stats
import events

try:
    import zmqrpc
except ImportError:
    print "WARNING: Using pure Python socket RPC implementation instead of zmq."
    import socketrpc as zmqrpc

from exception import LocustError, InterruptLocust, RescheduleTaskImmediately


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
                required_func(l)
                return

            return func(l)

        return wrapper

    return decorator_func


def task(weight_or_func=1):
    def decorator_func(func):
        func.locust_task_weight = weight_or_func
        return func

    """
    Check if task was used without parentheses (not called), like this::
    
        @task
        def my_task()
            pass
    """
    if callable(weight_or_func):
        func = weight_or_func
        weight_or_func = 1
        return decorator_func(func)
    else:
        return decorator_func


class LocustMeta(type):
    """
    Meta class for the main Locust class. It's used to allow Locust classes to specify task execution 
    ratio using an {task:int} dict, or a [(task0,int), ..., (taskN,int)] list.
    """

    def __new__(meta, classname, bases, classDict):
        new_tasks = []
        for base in bases:
            if hasattr(base, "tasks") and base.tasks:
                new_tasks += base.tasks

        if "tasks" in classDict and classDict["tasks"] is not None:
            tasks = classDict["tasks"]
            if isinstance(tasks, dict):
                tasks = list(tasks.iteritems())

            for task in tasks:
                if isinstance(task, tuple):
                    task, count = task
                    for i in xrange(0, count):
                        new_tasks.append(task)
                else:
                    new_tasks.append(task)

        for item in classDict.itervalues():
            if hasattr(item, "locust_task_weight"):
                for i in xrange(0, item.locust_task_weight):
                    new_tasks.append(item)

        classDict["tasks"] = new_tasks

        return type.__new__(meta, classname, bases, classDict)


class LocustBase(object):
    """
    Locust base class defining a locust user/client.
    """

    tasks = []
    """
    List with python callables that represents a locust user task.

    If tasks is a list, the task to be performed will be picked randomly.

    If tasks is a *(callable,int)* list of two-tuples, the task to be performed will be picked randomly, but each task will be
    weighted according to it's corresponding int value. So in the following case *task1* will be three times more
    likely to be picked than *task2*::

        class User(Locust):
            tasks = [(task1, 3), (task2, 1)]
    """

    host = None
    """Base hostname to swarm. i.e: http://127.0.0.1:1234"""

    min_wait = 1000
    """Minimum waiting time between two execution of locust tasks"""

    max_wait = 1000
    """Maximum waiting time between the execution of locust tasks"""

    avg_wait = None
    """Average waiting time wanted between the execution of locust tasks"""

    stop_timeout = None
    """Number of seconds after which the Locust will die. If None it won't timeout."""

    weight = 10
    """Probability of locust beeing choosen. The higher the weight, the greater is the chance of it beeing chosen."""

    __metaclass__ = LocustMeta

    def __init__(self):
        self._avg_wait = 0
        self._avg_wait_ctr = 0
        self._task_queue = []
        self._time_start = time()

    def __call__(self):
        if hasattr(self, "on_start"):
            self.on_start()
        while True:
            try:
                if (
                    self.stop_timeout is not None
                    and time() - self._time_start > self.stop_timeout
                ):
                    return

                if not self._task_queue:
                    self.schedule_task(self.get_next_task())

                try:
                    self.execute_next_task()
                except RescheduleTaskImmediately:
                    pass
                else:
                    self.wait()
            except InterruptLocust, e:
                if e.reschedule:
                    raise RescheduleTaskImmediately()
                return
            except GreenletExit:
                raise
            except Exception, e:
                events.locust_error.fire(self, e)
                sys.stderr.write("\n" + traceback.format_exc())

    def execute_next_task(self):
        task = self._task_queue.pop(0)
        self.execute_task(task["callable"], *task["args"])

    def execute_task(self, task, *args):
        # check if the function is a method bound to the current locust, and if so, don't pass self as first argument
        if hasattr(task, "im_self") and task.im_self == self:
            task(*args)
        else:
            task(self, *args)

    def schedule_task(self, task_callable, *args, **kwargs):
        """
        Add a task to the Locust's task execution queue.
        
        *Arguments*:
        
        * task_callable: Locust task to schedule
        * first: Optional keyword argument. If True, the task will be put first in the queue.
        * All other non keyword arguments will be passed to the task callable.
        """
        task = {"callable": task_callable, "args": args}
        if "first" in kwargs:
            self._task_queue.insert(0, task)
        else:
            self._task_queue.append(task)

    def get_next_task(self):
        return random.choice(self.tasks)

    def wait(self):
        if self.avg_wait:
            # Handle (strive for) average wait time
            if self._avg_wait:
                if self._avg_wait >= self.avg_wait:
                    # Want shorter wait
                    millis = random.randint(self.min_wait, self.avg_wait)
                else:
                    # Want longer wait
                    millis = random.randint(self.avg_wait, self.max_wait)
                self._avg_wait = ((self._avg_wait * self._avg_wait_ctr) + millis) / (
                    self._avg_wait_ctr + 1.0
                )
            else:
                # Average specified but is first run
                radius = min(
                    self.avg_wait - self.min_wait, self.max_wait - self.avg_wait
                )
                millis = random.randint(self.avg_wait - radius, self.avg_wait + radius)
                self._avg_wait = millis
            self._avg_wait_ctr += 1
        else:
            # Ignore average wait
            millis = random.randint(self.min_wait, self.max_wait)

        seconds = millis / 1000.0
        self._sleep(seconds)

    def _sleep(self, seconds):
        gevent.sleep(seconds)


class Locust(LocustBase):
    """
    Locust class that inherits from Locust and creates a *client* attribute on instantiation. 
    
    The *client* attribute is a simple HTTP client with support for keeping a user session between requests.
    """

    client = None
    """
    Instance of HttpBrowser that is created upon instantiation of WebLocust. 
    The client support cookies, and therefore keeps the session between HTTP requests.
    """

    gzip = False
    """
    If set to True the HTTP client will set headers for accepting gzip, and decode gzip data
    that is sent back from the server. This attribute is set from the command line.
    """

    def __init__(self):
        super(Locust, self).__init__()
        if self.host is None:
            raise LocustError(
                "You must specify the base host. Either in the host attribute in the Locust class, or on the command line using the --host option."
            )

        self.client = HttpBrowser(self.host, self.gzip)


class WebLocust(Locust):
    def __init__(self, *args, **kwargs):
        warnings.warn("WebLocust class has been, deprecated. Use Locust class instead.")
        super(WebLocust, self).__init__(*args, **kwargs)


class SubLocust(LocustBase):
    """
    Class for making a sub Locust that can be included as a task inside of a normal Locust/WebLocus,
    as well as inside another sub locust. 
    
    When the parent locust enters the sub locust, it will not
    continue executing it's tasks until a task in the sub locust has called the interrupt() function.
    """

    def __init__(self, parent):
        super(SubLocust, self).__init__()

        self.parent = parent
        if isinstance(parent, WebLocust):
            self.client = parent.client

        self()

    def interrupt(self, reschedule=True):
        """
        Interrupt the SubLocust and hand over execution control back to the parent Locust.
        
        If *reschedule* is True (default), the parent Locust will immediately re-schedule,
        and execute, a new task
        """
        raise InterruptLocust(reschedule)


locust_runner = None
STATE_INIT, STATE_HATCHING, STATE_RUNNING, STATE_STOPPED = [
    "ready",
    "hatching",
    "running",
    "stopped",
]


class LocustRunner(object):
    def __init__(
        self, locust_classes, hatch_rate, num_clients, num_requests=None, host=None
    ):
        self.locust_classes = locust_classes
        self.hatch_rate = hatch_rate
        self.num_clients = num_clients
        self.num_requests = num_requests
        self.host = host
        self.locusts = Group()
        self.state = STATE_INIT
        self.hatching_greenlet = None

        # register listener that resets stats when hatching is complete
        def on_hatch_complete(count):
            self.state = STATE_RUNNING
            print "Resetting stats\n"
            RequestStats.reset_all()

        events.hatch_complete += on_hatch_complete

    @property
    def request_stats(self):
        return RequestStats.requests

    @property
    def errors(self):
        return RequestStats.errors

    @property
    def user_count(self):
        return len(self.locusts)

    def hatch(self, stop_timeout=None):
        if self.num_requests is not None:
            RequestStats.global_max_requests = self.num_requests

        bucket = []
        weight_sum = sum((locust.weight for locust in self.locust_classes))
        for locust in self.locust_classes:
            if not locust.tasks:
                warnings.warn(
                    "Notice: Found locust (%s) got no tasks. Skipping..."
                    % locust.__name__
                )
                continue

            if self.host is not None:
                locust.host = self.host
            if stop_timeout is not None:
                locust.stop_timeout = stop_timeout

            # create locusts depending on weight
            percent = locust.weight / float(weight_sum)
            num_locusts = int(round(self.num_clients * percent))
            bucket.extend([locust for x in xrange(0, num_locusts)])

        print "\nHatching and swarming %i clients at the rate %g clients/s...\n" % (
            self.num_clients,
            self.hatch_rate,
        )
        occurence_count = dict([(l.__name__, 0) for l in self.locust_classes])
        total_locust_count = len(bucket)

        def spawn_locusts():
            sleep_time = 1.0 / self.hatch_rate
            while True:
                if not bucket:
                    print "All locusts hatched: %s" % ", ".join(
                        [
                            "%s: %d" % (name, count)
                            for name, count in occurence_count.iteritems()
                        ]
                    )
                    events.hatch_complete.fire(total_locust_count)
                    return

                locust = bucket.pop(random.randint(0, len(bucket) - 1))
                occurence_count[locust.__name__] += 1

                def start_locust():
                    try:
                        locust()()
                    except RescheduleTaskImmediately:
                        pass
                    except GreenletExit:
                        pass

                new_locust = self.locusts.spawn(start_locust)
                if len(self.locusts) % 10 == 0:
                    print "%i locusts hatched" % len(self.locusts)
                gevent.sleep(sleep_time)

        spawn_locusts()
        self.locusts.join()
        print "All locusts dead\n"
        print_stats(self.request_stats)
        print_percentile_stats(
            self.request_stats
        )  # TODO use an event listener, or such, for this?

    def start_hatching(self, locust_count=None, hatch_rate=None):
        if locust_count:
            self.num_clients = locust_count
        if hatch_rate:
            self.hatch_rate = hatch_rate

        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            RequestStats.clear_all()

        RequestStats.global_start_time = time()
        self.state = STATE_HATCHING
        self.hatch()

    def stop(self):
        # if we are currently hatching locusts we need to kill the hatching greenlet first
        if self.hatching_greenlet and not self.hatching_greenlet.ready():
            self.hatching_greenlet.kill(block=True)
        self.locusts.kill(block=True)
        self.state = STATE_STOPPED


class LocalLocustRunner(LocustRunner):
    def start_hatching(self, locust_count=None, hatch_rate=None):
        self.hatching_greenlet = gevent.spawn(
            lambda: super(LocalLocustRunner, self).start_hatching(
                locust_count, hatch_rate
            )
        )
        self.greenlet = self.hatching_greenlet


class DistributedLocustRunner(LocustRunner):
    def __init__(
        self,
        locust_classes,
        hatch_rate,
        num_clients,
        num_requests,
        host=None,
        master_host="localhost",
    ):
        super(DistributedLocustRunner, self).__init__(
            locust_classes, hatch_rate, num_clients, num_requests, host
        )
        self.master_host = master_host


class SlaveNode(object):
    def __init__(self, id, state=STATE_INIT):
        self.id = id
        self.state = state
        self.user_count = 0


class MasterLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(MasterLocustRunner, self).__init__(*args, **kwargs)

        class SlaveNodesDict(dict):
            def get_by_state(self, state):
                return [c for c in self.itervalues() if c.state == state]

            @property
            def ready(self):
                return self.get_by_state(STATE_INIT)

            @property
            def hatching(self):
                return self.get_by_state(STATE_HATCHING)

            @property
            def running(self):
                return self.get_by_state(STATE_RUNNING)

        self.clients = SlaveNodesDict()

        self.client_stats = {}
        self.client_errors = {}
        self._request_stats = {}

        self.server = zmqrpc.Server()
        self.greenlet = Group()
        self.greenlet.spawn(self.client_listener).link_exception()

        # listener that gathers info on how many locust users the slaves has spawned
        def on_slave_report(client_id, data):
            self.clients[client_id].user_count = data["user_count"]

        events.slave_report += on_slave_report

    @property
    def user_count(self):
        return sum([c.user_count for c in self.clients.itervalues()])

    def start_hatching(self, locust_count=None, hatch_rate=None):
        if locust_count:
            self.num_clients = locust_count / (len(self.clients.ready) or 1)
        if hatch_rate:
            self.hatch_rate = float(hatch_rate) / (len(self.clients.ready) or 1)

        print "Sending hatch jobs to %i ready clients" % len(self.clients.ready)
        if not len(self.clients.ready):
            print "WARNING: You are running in distributed mode but have no slave servers connected."
            print "Please connect slaves prior to swarming."

        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            RequestStats.clear_all()

        for client in self.clients.itervalues():
            msg = {
                "hatch_rate": self.hatch_rate,
                "num_clients": self.num_clients,
                "num_requests": self.num_requests,
                "host": self.host,
                "stop_timeout": None,
            }
            self.server.send({"type": "start", "data": msg})

        RequestStats.global_start_time = time()
        self.state = STATE_HATCHING

    def stop(self):
        for client in self.clients.hatching + self.clients.running:
            self.server.send({"type": "stop", "data": {}})

    def client_listener(self):
        while True:
            msg = self.server.recv()
            if msg["type"] == "client_ready":
                id = msg["data"]
                self.clients[id] = SlaveNode(id)
                print "Client %r reported as ready. Currently %i clients ready to swarm." % (
                    id,
                    len(self.clients.ready),
                )
            elif msg["type"] == "client_stopped":
                del self.clients[msg["data"]]
                if len(self.clients.hatching + self.clients.running) == 0:
                    self.state = STATE_STOPPED
                print "Removing %s client from running clients" % (msg["data"])
            elif msg["type"] == "stats":
                report = msg["data"]
                events.slave_report.fire(report["client_id"], report["data"])
            elif msg["type"] == "running":
                id = msg["data"]
                self.clients[id].state = STATE_HATCHING
            elif msg["type"] == "hatch_complete":
                id = msg["data"]["client_id"]
                self.clients[id].state = STATE_RUNNING
                self.clients[id].user_count = msg["data"]["count"]
                if len(self.clients.hatching) == 0:
                    count = sum(c.user_count for c in self.clients.itervalues())
                    events.hatch_complete.fire(count)

    @property
    def slave_count(self):
        return (
            len(self.clients.ready)
            + len(self.clients.hatching)
            + len(self.clients.running)
        )


class SlaveLocustRunner(DistributedLocustRunner):
    def __init__(self, *args, **kwargs):
        super(SlaveLocustRunner, self).__init__(*args, **kwargs)
        self.client_id = (
            socket.gethostname()
            + "_"
            + md5(str(time() + random.randint(0, 10000))).hexdigest()
        )

        self.client = zmqrpc.Client(self.master_host)
        self.greenlet = Group()
        self.greenlet.spawn(self.worker).link_exception()
        self.client.send({"type": "client_ready", "data": self.client_id})
        self.greenlet.spawn(self.stats_reporter).link_exception()

        # register listener for when all locust users have hatched, and report it to the master node
        def on_hatch_complete(count):
            self.client.send(
                {
                    "type": "hatch_complete",
                    "data": {"client_id": self.client_id, "count": count},
                }
            )

        events.hatch_complete += on_hatch_complete

        # register listener that adds the current number of spawned locusts to the report that is sent to the master node
        def on_report_to_master(client_id, data):
            data["user_count"] = self.user_count

        events.report_to_master += on_report_to_master

    def worker(self):
        while True:
            msg = self.client.recv()
            if msg["type"] == "start":
                self.client.send({"type": "running", "data": self.client_id})
                job = msg["data"]
                self.hatch_rate = job["hatch_rate"]
                self.num_clients = job["num_clients"]
                self.num_requests = job["num_requests"]
                self.host = job["host"]
                self.hatching_greenlet = gevent.spawn(lambda: self.start_hatching())
            elif msg["type"] == "stop":
                self.stop()
                self.client.send({"type": "client_stopped", "data": self.client_id})
                self.client.send({"type": "client_ready", "data": self.client_id})

    def stats_reporter(self):
        while True:
            data = {}
            events.report_to_master.fire(self.client_id, data)
            report = {
                "client_id": self.client_id,
                "data": data,
            }
            try:
                self.client.send({"type": "stats", "data": report})
            except:
                print "Connection lost to master server. Aborting..."
                break

            gevent.sleep(1)

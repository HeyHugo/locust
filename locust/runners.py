import socket
import warnings
import random
import logging
from time import time
from hashlib import md5

import gevent
from gevent import GreenletExit
from gevent.pool import Group

import events
from locust.stats import print_percentile_stats
from stats import RequestStats, print_stats
from exception import RescheduleTaskImmediately

from rpc import rpc, Message

logger = logging.getLogger(__name__)

# global locust runner singleton
locust_runner = None

STATE_INIT, STATE_HATCHING, STATE_RUNNING, STATE_STOPPED = [
    "ready",
    "hatching",
    "running",
    "stopped",
]
SLAVE_REPORT_INTERVAL = 3.0


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
            logger.info("Resetting stats\n")
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

    def weight_locusts(self, amount, stop_timeout=None):
        """
        Distributes the amount of locusts for each WebLocust-class according to it's weight
        returns a list "bucket" with the weighted locusts
        """
        bucket = []
        weight_sum = sum(
            (locust.weight for locust in self.locust_classes if locust.tasks)
        )
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
            num_locusts = int(round(amount * percent))
            bucket.extend([locust for x in xrange(0, num_locusts)])
        return bucket

    def spawn_locusts(self, spawn_count=None, stop_timeout=None, wait=False):
        if spawn_count is None:
            spawn_count = self.num_clients

        if self.num_requests is not None:
            RequestStats.global_max_requests = self.num_requests

        bucket = self.weight_locusts(spawn_count, stop_timeout)
        spawn_count = len(bucket)
        if self.state == STATE_INIT or self.state == STATE_STOPPED:
            self.state = STATE_HATCHING
            self.num_clients = spawn_count
        else:
            self.num_clients += spawn_count

        logger.info(
            "Hatching and swarming %i clients at the rate %g clients/s..."
            % (spawn_count, self.hatch_rate)
        )
        occurence_count = dict([(l.__name__, 0) for l in self.locust_classes])

        def hatch():
            sleep_time = 1.0 / self.hatch_rate
            while True:
                if not bucket:
                    logger.info(
                        "All locusts hatched: %s"
                        % ", ".join(
                            [
                                "%s: %d" % (name, count)
                                for name, count in occurence_count.iteritems()
                            ]
                        )
                    )
                    events.hatch_complete.fire(self.num_clients)
                    return

                locust = bucket.pop(random.randint(0, len(bucket) - 1))
                occurence_count[locust.__name__] += 1

                def start_locust(_):
                    try:
                        locust()()
                    except RescheduleTaskImmediately:
                        pass
                    except GreenletExit:
                        pass

                new_locust = self.locusts.spawn(start_locust, locust)
                if len(self.locusts) % 10 == 0:
                    logger.debug("%i locusts hatched" % len(self.locusts))
                gevent.sleep(sleep_time)

        hatch()
        if wait:
            self.locusts.join()
            logger.info("All locusts dead\n")
            print_stats(self.request_stats)
            print_percentile_stats(
                self.request_stats
            )  # TODO use an event listener, or such, for this?

    def kill_locusts(self, kill_count):
        """
        Kill a kill_count of weighted locusts from the Group() object in self.locusts
        """
        bucket = self.weight_locusts(kill_count)
        kill_count = len(bucket)
        self.num_clients -= kill_count
        logger.info("Killing %i locusts" % kill_count)
        dying = []
        for g in self.locusts:
            for l in bucket:
                if l == g.args[0]:
                    dying.append(g)
                    bucket.remove(l)
                    break
        for g in dying:
            self.locusts.killone(g)
        events.hatch_complete.fire(self.num_clients)

    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False):
        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            RequestStats.clear_all()
            RequestStats.global_start_time = time()
        # Dynamically changing the locust count
        if self.state != STATE_INIT and self.state != STATE_STOPPED:
            self.state = STATE_HATCHING
            if self.num_clients > locust_count:
                # Kill some locusts
                kill_count = self.num_clients - locust_count
                self.kill_locusts(kill_count)
            elif self.num_clients < locust_count:
                # Spawn some locusts
                if hatch_rate:
                    self.hatch_rate = hatch_rate
                spawn_count = locust_count - self.num_clients
                self.spawn_locusts(spawn_count=spawn_count)
        else:
            if hatch_rate:
                self.hatch_rate = hatch_rate
            if locust_count:
                self.spawn_locusts(locust_count, wait=wait)
            else:
                self.spawn_locusts(wait=wait)

    def stop(self):
        # if we are currently hatching locusts we need to kill the hatching greenlet first
        if self.hatching_greenlet and not self.hatching_greenlet.ready():
            self.hatching_greenlet.kill(block=True)
        self.locusts.kill(block=True)
        self.state = STATE_STOPPED


class LocalLocustRunner(LocustRunner):
    def start_hatching(self, locust_count=None, hatch_rate=None, wait=False):
        self.hatching_greenlet = gevent.spawn(
            lambda: super(LocalLocustRunner, self).start_hatching(
                locust_count, hatch_rate, wait=wait
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

        self.server = rpc.Server()
        self.greenlet = Group()
        self.greenlet.spawn(self.client_listener).link_exception()

        # listener that gathers info on how many locust users the slaves has spawned
        def on_slave_report(client_id, data):
            self.clients[client_id].user_count = data["user_count"]

        events.slave_report += on_slave_report

    @property
    def user_count(self):
        return sum([c.user_count for c in self.clients.itervalues()])

    def start_hatching(self, locust_count, hatch_rate):
        self.num_clients = locust_count
        slave_num_clients = locust_count / (
            (len(self.clients.ready) + len(self.clients.running)) or 1
        )
        slave_hatch_rate = float(hatch_rate) / (
            (len(self.clients.ready) + len(self.clients.running)) or 1
        )

        logger.info(
            "Sending hatch jobs to %i ready clients"
            % (len(self.clients.ready) + len(self.clients.running))
        )
        if not (len(self.clients.ready) + len(self.clients.running)):
            logger.warning(
                "You are running in distributed mode but have no slave servers connected. Please connect slaves prior to swarming."
            )
            return

        if self.state != STATE_RUNNING and self.state != STATE_HATCHING:
            RequestStats.clear_all()

        for client in self.clients.itervalues():
            data = {
                "hatch_rate": slave_hatch_rate,
                "num_clients": slave_num_clients,
                "num_requests": self.num_requests,
                "host": self.host,
                "stop_timeout": None,
            }
            self.server.send(Message("hatch", data, None))

        RequestStats.global_start_time = time()
        self.state = STATE_HATCHING

    def stop(self):
        for client in self.clients.hatching + self.clients.running:
            self.server.send(Message("stop", None, None))

    def client_listener(self):
        while True:
            msg = self.server.recv()
            if msg.type == "client_ready":
                id = msg.node_id
                self.clients[id] = SlaveNode(id)
                logger.info(
                    "Client %r reported as ready. Currently %i clients ready to swarm."
                    % (id, len(self.clients.ready))
                )
            elif msg.type == "client_stopped":
                del self.clients[msg.node_id]
                if len(self.clients.hatching + self.clients.running) == 0:
                    self.state = STATE_STOPPED
                logger.info("Removing %s client from running clients" % (msg.node_id))
            elif msg.type == "stats":
                events.slave_report.fire(msg.node_id, msg.data)
            elif msg.type == "hatching":
                self.clients[msg.node_id].state = STATE_HATCHING
            elif msg.type == "hatch_complete":
                self.clients[msg.node_id].state = STATE_RUNNING
                self.clients[msg.node_id].user_count = msg.data["count"]
                if len(self.clients.hatching) == 0:
                    count = sum(c.user_count for c in self.clients.itervalues())
                    events.hatch_complete.fire(count)
            elif msg.type == "quit":
                if msg.node_id in self.clients:
                    del self.clients[msg.node_id]
                    logger.info(
                        "Client %r quit. Currently %i clients connected."
                        % (msg.node_id, len(self.clients.ready))
                    )

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

        self.client = rpc.Client(self.master_host)
        self.greenlet = Group()
        self.greenlet.spawn(self.worker).link_exception()
        self.client.send(Message("client_ready", None, self.client_id))
        self.greenlet.spawn(self.stats_reporter).link_exception()

        # register listener for when all locust users have hatched, and report it to the master node
        def on_hatch_complete(count):
            self.client.send(
                Message("hatch_complete", {"count": count}, self.client_id)
            )

        events.hatch_complete += on_hatch_complete

        # register listener that adds the current number of spawned locusts to the report that is sent to the master node
        def on_report_to_master(client_id, data):
            data["user_count"] = self.user_count

        events.report_to_master += on_report_to_master

        # register listener that sends quit message to master
        def on_quitting():
            self.client.send(Message("quit", None, self.client_id))

        events.quitting += on_quitting

    def worker(self):
        while True:
            msg = self.client.recv()
            if msg.type == "hatch":
                self.client.send(Message("hatching", None, self.client_id))
                job = msg.data
                self.hatch_rate = job["hatch_rate"]
                # self.num_clients = job["num_clients"]
                self.num_requests = job["num_requests"]
                self.host = job["host"]
                self.hatching_greenlet = gevent.spawn(
                    lambda: self.start_hatching(
                        locust_count=job["num_clients"], hatch_rate=job["hatch_rate"]
                    )
                )
            elif msg.type == "stop":
                self.stop()
                self.client.send(Message("client_stopped", None, self.client_id))
                self.client.send(Message("client_ready", None, self.client_id))

    def stats_reporter(self):
        while True:
            data = {}
            events.report_to_master.fire(self.client_id, data)
            try:
                self.client.send(Message("stats", data, self.client_id))
            except:
                logger.error("Connection lost to master server. Aborting...")
                break

            gevent.sleep(SLAVE_REPORT_INTERVAL)

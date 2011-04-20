import sys
import time
import gevent
from copy import copy
import math
import functools

from urllib2 import URLError
from httplib import BadStatusLine
import socket

from exception import InterruptLocust
from collections import deque


class RequestStatsAdditionError(Exception):
    pass


class RequestStats(object):
    requests = {}
    request_observers = []
    total_num_requests = 0
    global_max_requests = None
    global_last_request_timestamp = None
    global_start_time = None
    errors = {}

    def __init__(self, name):
        self.name = name
        self.num_reqs_per_sec = {}
        self.last_request_timestamp = 0
        self.reset()

    @classmethod
    def reset_all(cls):
        cls.total_num_requests = 0
        for name, stats in cls.requests.iteritems():
            stats.reset()

    def reset(self):
        self.start_time = time.time()
        self.num_reqs = 0
        self.num_failures = 0
        self.total_response_time = 0
        self.response_times = {}
        self.min_response_time = None
        self.max_response_time = 0
        self._latest_requests = deque(maxlen=1000)
        self.last_request_timestamp = int(time.time())
        self.num_reqs_per_sec = {}

    def log(self, response_time):
        RequestStats.total_num_requests += 1

        self.num_reqs += 1
        self.total_response_time += response_time

        t = int(time.time())
        self.num_reqs_per_sec[t] = self.num_reqs_per_sec.setdefault(t, 0) + 1
        self.last_request_timestamp = t
        RequestStats.global_last_request_timestamp = t

        if self.min_response_time is None:
            self.min_response_time = response_time

        self.min_response_time = min(self.min_response_time, response_time)
        self.max_response_time = max(self.max_response_time, response_time)

        # to avoid to much data that has to be transfered to the master node when
        # running in distributed mode, we save the response time rounded in a dict
        # so that 147 becomes 150, 3432 becomes 3400 and 58760 becomes 59000
        if response_time < 100:
            rounded_response_time = response_time
        elif response_time < 1000:
            rounded_response_time = int(round(response_time, -1))
        elif response_time < 10000:
            rounded_response_time = int(round(response_time, -2))
        else:
            rounded_response_time = int(round(response_time, -3))
        # increase request count for the rounded key in response time dict
        self.response_times.setdefault(rounded_response_time, 0)
        self.response_times[rounded_response_time] += 1

        self._latest_requests.appendleft(response_time)

    def log_error(self, error):
        self.num_failures += 1
        key = "%r: %s" % (error, error)
        RequestStats.errors.setdefault(key, 0)
        RequestStats.errors[key] += 1

    @property
    def avg_response_time(self):
        try:
            return float(self.total_response_time) / self.num_reqs
        except ZeroDivisionError:
            return 0

    @property
    def median_response_time(self):
        if not self.response_times:
            return 0

        def median(total, count):
            """
            total is the number of requests made
            count is a dict {response_time: count}
            """
            pos = (total - 1) / 2
            for k in sorted(count.iterkeys()):
                if pos < count[k]:
                    return k
                pos -= count[k]

        return median(self.num_reqs, self.response_times)

    @property
    def current_rps(self):
        slice_start_time = max(self.last_request_timestamp - 10, int(self.start_time))

        reqs = [
            self.num_reqs_per_sec.get(t, 0)
            for t in range(slice_start_time, self.last_request_timestamp)
        ]
        return avg(reqs)

    @property
    def total_rps(self):
        if not RequestStats.global_last_request_timestamp:
            return 0.0

        return self.num_reqs / max(
            RequestStats.global_last_request_timestamp - RequestStats.global_start_time,
            1,
        )

    def __add__(self, other):
        # if self.name != other.name:
        #    raise RequestStatsAdditionError("Trying to add two RequestStats objects of different names (%s and %s)" % (self.name, other.name))

        new = RequestStats(self.name)
        new.last_request_timestamp = max(
            self.last_request_timestamp, other.last_request_timestamp
        )
        new.start_time = min(self.start_time, other.start_time)

        new.num_reqs = self.num_reqs + other.num_reqs
        new.num_failures = self.num_failures + other.num_failures
        new.total_response_time = self.total_response_time + other.total_response_time
        new.max_response_time = max(self.max_response_time, other.max_response_time)
        new.min_response_time = (
            min(self.min_response_time, other.min_response_time)
            or other.min_response_time
        )

        def merge_dict_add(d1, d2):
            """Merge two dicts by adding the values from each dict"""
            merged = {}
            for key in set(d1.keys() + d2.keys()):
                merged[key] = d1.get(key, 0) + d2.get(key, 0)
            return merged

        new.num_reqs_per_sec = merge_dict_add(
            self.num_reqs_per_sec, other.num_reqs_per_sec
        )
        new.response_times = merge_dict_add(self.response_times, other.response_times)
        return new

    def get_stripped_report(self):
        report = copy(self)
        # report.response_times = {report.median_response_time:report.num_reqs}

        # report.num_reqs_per_sec = {}
        # slice_start_time = max(self.last_request_timestamp - 10, int(self.start_time))
        # for t in range(slice_start_time, self.last_request_timestamp):
        #    report.num_reqs_per_sec[t] = self.num_reqs_per_sec[t]

        self.reset()
        return report

    def to_dict(self):
        return {
            "num_reqs": self.num_reqs,
            "num_failures": self.num_failures,
            "avg": self.avg_response_time,
            "min": self.min_response_time,
            "max": self.max_response_time,
            "current_req_per_sec": self.current_rps,
        }

    def __str__(self):
        try:
            fail_percent = (self.num_failures / float(self.num_reqs)) * 100
        except ZeroDivisionError:
            fail_percent = 0

        return " %-40s %7d %12s %7d %7d %7d  | %7d %7.2f" % (
            self.name,
            self.num_reqs,
            "%d(%.2f%%)" % (self.num_failures, fail_percent),
            self.avg_response_time,
            self.min_response_time or 0,
            self.max_response_time,
            self.median_response_time or 0,
            self.current_rps or 0,
        )

    def create_response_times_list(self):
        inflated_list = []
        for response_time, count in self.response_times.iteritems():
            inflated_list.extend([response_time for x in xrange(0, count)])
        inflated_list.sort()

        return inflated_list

    def percentile(self, tpl=" %-40s %8d %6d %6d %6d %6d %6d %6d %6d %6d %6d"):
        inflated_list = self.create_response_times_list()
        return tpl % (
            self.name,
            self.num_reqs,
            percentile(inflated_list, 0.5),
            percentile(inflated_list, 0.66),
            percentile(inflated_list, 0.75),
            percentile(inflated_list, 0.80),
            percentile(inflated_list, 0.90),
            percentile(inflated_list, 0.95),
            percentile(inflated_list, 0.98),
            percentile(inflated_list, 0.99),
            percentile(inflated_list, 1.0),
        )

    @classmethod
    def get(cls, name):
        request = cls.requests.get(name, None)
        if not request:
            request = RequestStats(name)
            cls.requests[name] = request
        return request

    @classmethod
    def sum_stats(cls, request_stats, name="Total"):
        stats = RequestStats(name)
        for s in request_stats:
            stats += s
        return stats


def avg(values):
    return sum(values, 0.0) / max(len(values), 1)


# TODO Use interpolation or not?
def percentile(N, percent, key=lambda x: x):
    """
    Find the percentile of a list of values.

    @parameter N - is a list of values. Note N MUST BE already sorted.
    @parameter percent - a float value from 0.0 to 1.0.
    @parameter key - optional key function to compute value from each element of N.

    @return - the percentile of the values
    """
    if not N:
        return 0
    k = (len(N) - 1) * percent
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return key(N[int(k)])
    d0 = key(N[int(f)]) * (c - k)
    d1 = key(N[int(c)]) * (k - f)
    return d0 + d1


def log_request(f):
    # hack to preserve the function spec when generating sphinx documentation
    # TODO: If sphinx is imported in locustfile, things will not function! Need a better way to check if
    # sphinx is actually running documentation generation
    if "sphinx" in sys.modules:
        import warnings

        warnings.warn(
            "Sphinx detected, @log_request decorator will have no effect to preserve function spec"
        )
        return f

    def _wrapper(*args, **kwargs):
        name = kwargs.get("name", args[1]) or args[1]
        try:
            if (
                RequestStats.global_max_requests is not None
                and RequestStats.total_num_requests >= RequestStats.global_max_requests
            ):
                raise InterruptLocust("Maximum number of requests reached")
            start = time.time()
            retval = f(*args, **kwargs)
            response_time = int((time.time() - start) * 1000)
            RequestStats.get(name).log(response_time)
            return retval
        except (URLError, BadStatusLine, socket.error), e:
            RequestStats.get(name).log_error(e)

    return _wrapper


def print_stats(stats):
    print " %-40s %7s %12s %7s %7s %7s  | %7s %7s" % (
        "Name",
        "# reqs",
        "# fails",
        "Avg",
        "Min",
        "Max",
        "Median",
        "req/s",
    )
    print "-" * 120
    total_rps = 0
    total_reqs = 0
    total_failures = 0
    for r in stats.itervalues():
        total_rps += r.current_rps
        total_reqs += r.num_reqs
        total_failures += r.num_failures
        print r
    print "-" * 120

    try:
        fail_percent = (total_failures / float(total_reqs)) * 100
    except ZeroDivisionError:
        fail_percent = 0

    print " %-40s %7d %12s %42.2f" % (
        "Total",
        total_reqs,
        "%d(%.2f%%)" % (total_failures, fail_percent),
        total_rps,
    )
    print ""


def print_percentile_stats(stats):
    print "Percentage of the requests completed within given times"
    print " %-40s %8s %6s %6s %6s %6s %6s %6s %6s %6s %6s" % (
        "Name",
        "# reqs",
        "50%",
        "66%",
        "75%",
        "80%",
        "90%",
        "95%",
        "98%",
        "99%",
        "100%",
    )
    print "-" * 120
    complete_list = []
    for r in stats.itervalues():
        if r.response_times:
            print r.percentile()
            complete_list.extend(r.create_response_times_list())
    print "-" * 120
    complete_list.sort()
    if complete_list:
        print " %-40s %8s %6d %6d %6d %6d %6d %6d %6d %6d %6d" % (
            "Total",
            str(len(complete_list)),
            percentile(complete_list, 0.5),
            percentile(complete_list, 0.66),
            percentile(complete_list, 0.75),
            percentile(complete_list, 0.8),
            percentile(complete_list, 0.9),
            percentile(complete_list, 0.95),
            percentile(complete_list, 0.98),
            percentile(complete_list, 0.99),
            complete_list[-1],
        )
    print ""


def print_error_report():
    print "Error report"
    print " %-18s %-100s" % ("# occurences", "Error")
    print "-" * 120
    for error, count in RequestStats.errors.iteritems():
        print " %-18i %-100s" % (count, error)
    print "-" * 120
    print


def stats_printer():
    from core import locust_runner

    while True:
        print_stats(locust_runner.request_stats)
        gevent.sleep(2)

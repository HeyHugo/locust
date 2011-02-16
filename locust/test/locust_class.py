from locust.core import Locust, require_once, WebLocust
import unittest
from testcases import WebserverTestCase


class TestLocustClass(unittest.TestCase):
    def test_task_ratio(self):
        t1 = lambda l: None
        t2 = lambda l: None

        class MyLocust(Locust):
            tasks = {t1: 5, t2: 2}

        l = MyLocust()
        self.assertEqual(l.tasks, [t1, t1, t1, t1, t1, t2, t2])

    def test_require_once(self):
        self.t1_executed = False
        self.t2_executed = False

        def t1(l):
            self.t1_executed = True

        @require_once(t1)
        def t2(l):
            self.t2_executed = True

        class MyLocust(Locust):
            tasks = [t2]

        l = MyLocust()
        l.schedule_task(l.get_next_task())
        l._task_queue.pop(0)["callable"](l)
        self.assertTrue(self.t1_executed)
        self.assertFalse(self.t2_executed)
        l._task_queue.pop(0)["callable"](l)
        self.assertTrue(self.t2_executed)


class TestWebLocustClass(WebserverTestCase):
    def test_get_request(self):
        self.response = ""

        def t1(l):
            self.response = l.client.get("/ultra_fast")

        class MyLocust(WebLocust):
            tasks = [t1]
            host = "http://127.0.0.1:%i" % self.port

        my_locust = MyLocust()
        t1(my_locust)
        self.assertEqual(self.response, "This is an ultra fast response")

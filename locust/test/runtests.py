import unittest
from locust_class import TestLocustClass, TestSubLocust, TestWebLocustClass
from test_stats import (
    TestRequestStats,
    TestRequestStatsWithWebserver,
    TestInspectLocust,
)

if __name__ == "__main__":
    unittest.main()

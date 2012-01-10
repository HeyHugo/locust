import inspect

from core import LocustBase, SubLocust
from log import console_logger


def print_task_ratio(locusts, total=False, level=0, parent_ratio=1.0):
    """
	Output table with task execution ratio info to console_logger
	"""
    ratio = {}
    for locust in locusts:
        ratio.setdefault(locust, 0)
        ratio[locust] += 1

    # get percentage
    ratio_percent = dict(
        map(
            lambda x: (x[0], float(x[1]) / len(locusts) * parent_ratio),
            ratio.iteritems(),
        )
    )

    for locust, ratio in ratio_percent.iteritems():
        # print " %-10.2f %-50s" % (ratio*100, "  "*level + locust.__name__)
        console_logger.info(
            " %-10s %-50s"
            % ("  " * level + "%-6.1f" % (ratio * 100), "  " * level + locust.__name__)
        )
        if inspect.isclass(locust) and issubclass(locust, LocustBase):
            if total:
                print_task_ratio(locust.tasks, total, level + 1, ratio)
            else:
                print_task_ratio(locust.tasks, total, level + 1)

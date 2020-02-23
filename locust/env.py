from .events import Events
from .stats import RequestStats


class Environment:
    locust_classes = None
    """The locust user classes that is to be run"""

    events = None
    """Event hooks used by Locust internally, as well as """

    stats = None
    """Instance of RequestStats which holds the request statistics for this Locust test"""

    options = None
    """Other environment options"""

    runner = None
    """Reference to the runner instance"""

    web_ui = None
    """Reference to the WebUI instance"""

    def __init__(self, locust_classes=None, options=None):
        self.events = Events()
        self.stats = RequestStats()
        self.locust_classes = locust_classes
        self.options = options

        # set up event listeners for recording requests
        def on_request_success(
            request_type, name, response_time, response_length, **kwargs
        ):
            self.stats.log_request(request_type, name, response_time, response_length)

        def on_request_failure(
            request_type, name, response_time, response_length, exception, **kwargs
        ):
            self.stats.log_request(request_type, name, response_time, response_length)
            self.stats.log_error(request_type, name, exception)

        self.events.request_success.add_listener(on_request_success)
        self.events.request_failure.add_listener(on_request_failure)

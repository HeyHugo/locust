import urllib2
import urllib
from stats import log_request


class HTTPClient(object):
    def __init__(self, base_url):
        self.base_url = base_url

    @log_request
    def get(self, url, name=None):
        return urllib2.urlopen(self.base_url + url).read()


class HttpBrowser(object):
    """
    Class for performing web requests and holding session cookie between requests (in order
    to be able to log in to websites). 
    
    Logs each request so that locust can display statistics.
    """

    def __init__(self, base_url):
        self.base_url = base_url
        self.opener = urllib2.build_opener(urllib2.HTTPCookieProcessor())
        urllib2.install_opener(self.opener)

    @log_request
    def get(self, path, headers=None, name=None):
        """
        Make an HTTP GET request.
        
        Arguments:
        
        * *path* is the relative path to request.
        * *headers* is an optional dict with HTTP request headers
        """
        request = urllib2.Request(self.base_url + path, None, headers)
        f = self.opener.open(request)
        data = f.read()
        f.close()
        return data

    @log_request
    def post(self, path, data, headers=None, name=None):
        """
        Make an HTTP POST request.
        
        Arguments:
        
        * *path* is the relative path to request.
        * *data* dict with the data that will be sent in the body of the POST request
        * *headers* is an optional dict with HTTP request headers
        
        Example::
        
            client = HttpBrowser("http://example.com")
            client.post("/post", {"user":"joe_hill"})
        """
        request = urllib2.Request(self.base_url + path, urllib.urlencode(data), headers)
        f = self.opener.open(request)
        data = f.read()
        f.close()
        return data

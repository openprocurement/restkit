# -*- coding: utf-8 -
#
# This file is part of restkit released under the MIT license. 
# See the NOTICE for more information.

import copy
import errno
import logging
import mimetypes
import os
import threading
import time
import socket
import types
import urlparse
import uuid

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

try:
    import ssl # python 2.6
    have_ssl = True
except ImportError:
    have_ssl = False

from . import __version__ 
from .datastructures import MultiDict
from .errors import *
from .filters import Filters
from .forms import multipart_form_encode, form_encode
from .globals import _manager
from . import http

from .sock import close, send, sendfile, sendlines, send_chunk
from .tee import TeeInput
from .util import parse_netloc, to_bytestring, rewrite_location

MAX_CLIENT_TIMEOUT=300
MAX_CLIENT_CONNECTIONS = 5
MAX_CLIENT_TRIES = 5
CLIENT_WAIT_TRIES = 1.0
MAX_FOLLOW_REDIRECTS = 5
USER_AGENT = "restkit/%s" % __version__

log = logging.getLogger(__name__)


class BodyWrapper(object):

    def __init__(self, resp, client):
        self.resp = resp
        self.body = resp._body
        self.client = client
        self._sock = client._sock
        self._sock_key = copy.copy(client._sock_key)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, traceback):
        self.close() 

    def close(self):
        """ release connection """ 
        self.client.release_connection(self._sock_key, 
                self._sock, self.resp.should_close)
    
    def __iter__(self):
        return self

    def next(self):
        try:
            return self.body.next()
        except StopIteration:
            self.close() 
            raise

    def read(self, size=None):
        data = self.body.read(size=size)
        if not data:
            self.close()
        return data

    def readline(self, size=None):
        line = self.body.readline(size=size)
        if not line: 
            self.close()
        return line

    def readlines(self, size=None):
        lines = self.body.readlines(size=size)
        if self.body.close:
            self.close()
        return line


class ClientResponse(object):

    charset = "utf8"
    unicode_errors = 'strict'

    def __init__(self, client, resp):
        self.client = client
        self._sock = client._sock
        self._sock_key = copy.copy(client._sock_key)
        self._body = resp.body
        
        # response infos
        self.headers = resp.headers
        self.status = resp.status
        self.status_int = resp.status_int
        self.version = resp.version
        self.headerslist = resp.headers.items()
        self.location = resp.headers.iget('location')
        self.final_url = client.url
        self.should_close = resp.should_close()


        self._closed = False
        self._already_read = False

        if client.method == "HEAD":
            """ no body on HEAD, release the connection now """
            self._body = StringIO()
            self.client.release_connection(self._sock_key, self._sock,
                    resp.should_close())

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            pass
        return self.headers.iget(key)
    
    def __contains__(self, key):
        return (self.headers.iget(key) is not None)

    def __iter__(self):
        return self.headers.iteritems()

    def release_connection(self):
        """ release the connection in the client or pool """
        self.client.release_connection(self._sock_key, 
                self._sock, self.should_close)
        self._closed = True

    def can_read(self):
        return not self._closed and not self._already_read

    def body_string(self, charset=None, unicode_errors="strict"):
        """ return body string, by default in bytestring """
       
        if not self.can_read():
            raise AlreadyRead() 

        body = self._body.read()
        self._already_read = True
        
        # release connection
        self.release_connection()

        if charset is not None:
            try:
                body = body.decode(charset, unicode_errors)
            except UnicodeDecodeError:
                pass
        return body

    def body_stream(self):
        """ stream body """ 
        if not self.can_read():
            raise AlreadyRead()

        self._already_read = True

        return BodyWrapper(self, self.client) 

    def tee(self):
        """ copy response input to standard output or a file if length >
        sock.MAX_BODY. This make possible to reuse it in your
        appplication. When all the input has been read, connection is
        released """

        return TeeInput(self, self.client)


class Client(object):

    version = (1, 1)
    response_class=ClientResponse

    def __init__(self,
            follow_redirect=False,
            force_follow_redirect=False,
            max_follow_redirect=MAX_FOLLOW_REDIRECTS,
            filters=None, 
            decompress=True, 
            manager=None,
            response_class=None,
            max_conn=MAX_CLIENT_CONNECTIONS,
            timeout=MAX_CLIENT_TIMEOUT,
            force_dns=False,
            max_tries=5,
            wait_tries=1.0,
            **ssl_args):
        
        self.follow_redirect = follow_redirect
        self.force_follow_redirect = force_follow_redirect
        self.max_follow_redirect = max_follow_redirect 
        self.filters = Filters(filters)
        self.decompress = decompress
        
        # set manager
        if manager is None:
            manager = _manager
        self._manager = manager

        # change default response class 
        if response_class is not None:
            self.response_class = response_class

        self.max_conn = max_conn
        self.max_tries = max_tries
        self.wait_tries = wait_tries
        self.timeout = timeout

        self._nb_redirections = self.max_follow_redirect
        self._connections = {}
        self._url = None
        self._initial_url = None
        self._write_cb = None
        self._headers = None 
        self._sock_key = None
        self._sock = None
        self._original = None

        self.method = 'GET'
        self.body = None

        self.ssl_args = ssl_args or {}
        self._lock = threading.Lock()
        

    def _headers__get(self):
        if not isinstance(self._headers, MultiDict):
            self._headers = MultiDict(self._headers or [])
        return self._headers
    def _headers__set(self, value):
        self._headers = MultiDict(value)
    headers = property(_headers__get, _headers__set, doc=_headers__get.__doc__)
    
    
    def write_callback(self, cb):
        if not callable(cb):
            raise ValueError("%s isn't a callable" % str(cb))
        self._write_cb = cb

    def _url__get(self):
        if self._url is None:
            raise ValueError("url isn't set")
        return urlparse.urlunparse(self._url)
    def _url__set(self, string):
        self._url = urlparse.urlparse(string)
    url = property(_url__get, _url__set, doc="current url to request")

    def _parsed_url__get(self):
        if self._url is None:
            raise ValueError("url isn't set")
        return self._url
    parsed_url = property(_parsed_url__get)

    def _host__get(self):
        try:
            h = self.parsed_url.netloc.encode('ascii')
        except UnicodeEncodeError:
            h = self.parsed_url.netloc.encode('idna')
        
        hdr_host = self.headers.iget("host")
        if not hdr_host:
            return h
        return hdr_host
    host = property(_host__get)

    def _path__get(self):
        path = self.parsed_url.path or '/'

        return urlparse.urlunparse(('','', path, self._url.params, 
            self._url.query, self._url.fragment))
    path = property(_path__get, doc="request path")

    def req_is_chunked(self):
        te = self.headers.iget("transfer-encoding")
        return (te is not None and te.lower() == "chunked")

    def req_is_ssl(self):
        return self.parsed_url.scheme == "ssl"

    def connect(self, addr, ssl):
        for res in socket.getaddrinfo(addr[0], addr[1], 0, 
                socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res

            try:
                sock = socket.socket(af, socktype, proto)
        
                sock.settimeout(self.timeout)
                sock.connect(sa)
                if ssl:
                    if not have_ssl:
                        raise ValueError("https isn't supported.  On python 2.5x,"
                                        + " https support requires ssl module "
                                        + "(http://pypi.python.org/pypi/ssl) "
                                        + "to be intalled.")
                    validate_ssl_args(self.ssl_args)
                    return ssl.wrap_socket(sock, **self.ssl_args)
                return sock
            except socket.error:
                close(sock)
        raise socket.error, "getaddrinfo returns an empty list" 

    def get_connection(self):
        addr = parse_netloc(self.parsed_url)
        
        ssl = self.req_is_ssl()
        self._sock_key = (addr, ssl)
    
        self._lock.acquire()
        try:
            try:
                sock = self._connections.pop(self._sock_key)
            except KeyError:
                sock = self._manager.find_socket(addr, ssl)

            if sock is None:
                sock = self.connect(addr, ssl)
            return sock
        finally:
            self._lock.release()

    def release_connection(self, key, sck, should_close=False):
        if should_close:
            log.debug("close connection")
            close(sck)
            return

        log.debug("release connection")
        self._lock.acquire()
        try:
            if key in self._connections or \
                    len(self._connections) > self.max_conn: 
                self._manager.store_socket(sck, key[0], key[1])
            else:
                self._connections[key] = sck
        finally:
            self._lock.release()

    def close_connection(self):
        close(self._sock)
        self._sock = None

    def parse_body(self):
        if not self.body:
            if self.method in ('POST', 'PUT',):
                self.headers['Content-Length'] = 0
            return

        ctype = self.headers.iget('content-type')
        clen = self.headers.iget('content-length')
       
        if isinstance(self.body, dict):
            if ctype is not None and \
                    ctype.startswith("multipart/form-data"):
                type_, opts = cgi.parse_header(ctype)
                boundary = opts.get('boundary', uuid.uuid4().hex)
                self.body, self.headers = multipart_form_encode(body, 
                                            self.headers, boundary)
            else:
                ctype = "application/x-www-form-urlencoded; charset=utf-8"
                self.body = form_encode(self.body)
        elif hasattr(self.body, "boundary"):
            ctype = "multipart/form-data; boundary=%s" % self.body.boundary
            clen = self.body.get_size()

        if not ctype:
            ctype = 'application/octet-stream'
            if hasattr(self.body, 'name'):
                ctype =  mimetypes.guess_type(self.body.name)[0]
        
        if not clen:
            if hasattr(self.body, 'fileno'):
                try:
                    self.body.flush()
                except IOError:
                    pass
                try:
                    fno = self.body.fileno()
                    clen = str(os.fstat(fno)[6])
                except  IOError:
                    if not self.req_is_chunked():
                        clen = len(self.body.read())
            elif hasattr(self.body, 'getvalue') and not \
                    self.req_is_chunked():
                clen = len(self.body.getvalue())
            elif isinstance(self.body, types.StringTypes):
                self.body = to_bytestring(self.body)
                clen = len(self.body)

        if clen is not None:
            self.headers['Content-Length'] = clen
        elif not self.req_is_chunked():
            raise RequestError("Can't determine content length and " +
                    "Transfer-Encoding header is not chunked")

        if ctype is not None:
            self.headers['Content-Type'] = ctype

    def make_headers_string(self):
        if self.version == (1,1):
            httpver = "HTTP/1.1"
        else:
            httpver = "HTTP/1.0"

        ua = self.headers.iget('user_agent')
        host = self.host
        accept_encoding = self.headers.iget('accept-encoding')

        headers = [
            "%s %s %s\r\n" % (self.method, self.path, httpver),
            "Host: %s\r\n" % host,
            "User-Agent: %s\r\n" % ua or USER_AGENT,
            "Accept-Encoding: %s\r\n" % accept_encoding or 'identity'
        ]

        headers.extend(["%s: %s\r\n" % (k, str(v)) for k, v in \
                self.headers.items() if k.lower() not in \
                ('user-agent', 'host', 'accept-encoding',)])

        log.debug("Send headers: %s" % headers)
        return "%s\r\n" % "".join(headers)

    def reset_request(self):
        if self._original is None:
            return
        
        self.url = self._original["url"] 
        self.method = self._original["method"]
        self.body = self._original["body"]
        self.headers = self._original["headers"]
        self._nb_redirections = self.max_follow_redirect 
        
    def perform(self):
        if not self.url:
            raise RequestError("url isn't set")

        log.debug("Start to perform request: %s %s %s" % (self.method,
            self.host, self.path))

        self._original = dict( 
                url = self.url,
                method = self.method,
                body = self.body,
                headers = self.headers
       ) 

        tries = self.max_tries
        wait = self.wait_tries
        while tries > 0:
            try:
                # generate final body
                self.parse_body()
                
                # get or create a connection to the remote host
                self._sock = self.get_connection()
                
                # set socket timeout in case default has changed
                self._sock.settimeout(self.timeout)
                
                # apply on_request filters
                self.filters.apply("on_request", self)
                
                # send headers
                headers_str = self.make_headers_string()
                self._sock.sendall(headers_str)
                
                # send body
                if self.body is not None:
                    chunked = self.req_is_chunked()
                    log.debug("send body (chunked: %s) %s" % (chunked,
                        type(self.body)))

                    if hasattr(self.body, 'read'):
                        if hasattr(self.body, 'seek'): self.body.seek(0)
                        sendfile(self._sock, self.body, chunked)
                    elif isinstance(self.body, types.StringTypes):
                        send(self._sock, self.body, chunked)
                    else:
                        sendlines(self._sock, self.body, chunked)
                    if chunked:
                        send_chunk(self._sock, "")
                
                return self.get_response()
            except socket.gaierror, e:
                self.close_connection()
                raise RequestError(str(e))
            except socket.timeout, e:
                self.close_connection()
                if tries <= 0:
                    raise RequestTimeout(str(e))
            except socket.error, e:
                self.close_connection()

                if e[0] not in (errno.EAGAIN, errno.ECONNABORTED, 
                        errno.EPIPE, errno.ECONNREFUSED, 
                        errno.ECONNRESET) or tries <= 0:
                    raise RequestError(str(e))
            except (KeyboardInterrupt, SystemExit):
                break
            except Exception, e:
                # unkown error
                self.close_connection()
                raise

            # time until we retry.
            time.sleep(wait)
            wait = wait * 2
            tries = tries - 1

            # reset request
            self.reset_request()

    def request(self, url, method='GET', body=None, headers=None):
        self.url = url
        self.method = method
        self.body = body
        self.headers = copy.copy(headers) or []
        self._nb_redirections = self.max_follow_redirect
        return self.perform()

    def redirect(self, resp, location, method=None):
        if self._nb_redirections <= 0:
            raise RedirectLimit("Redirection limit is reached")

        if self._initial_url is None:
            self._initial_url = self.url

        # discard response body and reset request informations
        resp.body.discard()
        self.reset_request()

        # make sure location follow rfc2616
        location = rewrite_location(self.url, location)
        
        log.debug("Redirect to %s" % location)

        # change request url and method if needed
        self.url = location
        if method is not None:
            self.method = "GET"

        self._nb_redirections -= 1
        return self.perform()

    def get_response(self):
        unreader = http.Unreader(self._sock)

        log.info("Start to parse response")
        while True:
            resp = http.Request(unreader)
            if resp.status_int != 100:
                break

            log.debug("Go 100-Continue header")
        
        location = resp.headers.iget('location')

        if self.follow_redirect:
            if resp.status_int in (301, 302, 307,):
                if self.method in ('GET', 'HEAD',) or \
                        self.force_follow_redirect:
                    if hasattr(self.body, 'read'):
                        try:
                            self.body.seek(0)
                        except AttributeError:
                            raise RequestError("Can't redirect %s to %s "
                                    "because body has already been read"
                                    % (self.url, location))
                    return self.redirect(resp, location)

            elif resp.status_int == 303 and self.method == "POST":
                return self.redirect(resp, location, method="GET")
       
        # apply final response
        self.filters.apply("on_response", self, resp)
        
        # reset request
        self.reset_request()

        log.debug("return response class")
        return self.response_class(self, resp)

#!/usr/bin/python -u
# Copyright (c) 2010-2011 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from errno import EEXIST, ENOENT
from hashlib import md5
from optparse import OptionParser
from os import environ, listdir, makedirs, utime
from os.path import basename, dirname, getmtime, getsize, isdir, join
from Queue import Empty, Queue
from sys import argv, exit, stderr, stdout
from threading import enumerate as threading_enumerate, Thread
from time import sleep


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# Inclusion of swift.common.client for convenience of single file distribution

import socket
from cStringIO import StringIO
from httplib import HTTPException, HTTPSConnection
from re import compile, DOTALL
from tokenize import generate_tokens, STRING, NAME, OP
from urllib import quote as _quote, unquote
from urlparse import urlparse, urlunparse

try:
    from eventlet import sleep
except Exception:
    from time import sleep

try:
    from swift.common.bufferedhttp \
        import BufferedHTTPConnection as HTTPConnection
except Exception:
    from httplib import HTTPConnection


def quote(value, safe='/'):
    """
    Patched version of urllib.quote that encodes utf8 strings before quoting
    """
    if isinstance(value, unicode):
        value = value.encode('utf8')
    return _quote(value, safe)


# look for a real json parser first
try:
    # simplejson is popular and pretty good
    from simplejson import loads as json_loads
except ImportError:
    try:
        # 2.6 will have a json module in the stdlib
        from json import loads as json_loads
    except ImportError:
        # fall back on local parser otherwise
        comments = compile(r'/\*.*\*/|//[^\r\n]*', DOTALL)

        def json_loads(string):
            '''
            Fairly competent json parser exploiting the python tokenizer and
            eval(). -- From python-cloudfiles

            _loads(serialized_json) -> object
            '''
            try:
                res = []
                consts = {'true': True, 'false': False, 'null': None}
                string = '(' + comments.sub('', string) + ')'
                for type, val, _junk, _junk, _junk in \
                        generate_tokens(StringIO(string).readline):
                    if (type == OP and val not in '[]{}:,()-') or \
                            (type == NAME and val not in consts):
                        raise AttributeError()
                    elif type == STRING:
                        res.append('u')
                        res.append(val.replace('\\/', '/'))
                    else:
                        res.append(val)
                return eval(''.join(res), {}, consts)
            except Exception:
                raise AttributeError()


class ClientException(Exception):

    def __init__(self, msg, http_scheme='', http_host='', http_port='',
                 http_path='', http_query='', http_status=0, http_reason='',
                 http_device=''):
        Exception.__init__(self, msg)
        self.msg = msg
        self.http_scheme = http_scheme
        self.http_host = http_host
        self.http_port = http_port
        self.http_path = http_path
        self.http_query = http_query
        self.http_status = http_status
        self.http_reason = http_reason
        self.http_device = http_device

    def __str__(self):
        a = self.msg
        b = ''
        if self.http_scheme:
            b += '%s://' % self.http_scheme
        if self.http_host:
            b += self.http_host
        if self.http_port:
            b += ':%s' % self.http_port
        if self.http_path:
            b += self.http_path
        if self.http_query:
            b += '?%s' % self.http_query
        if self.http_status:
            if b:
                b = '%s %s' % (b, self.http_status)
            else:
                b = str(self.http_status)
        if self.http_reason:
            if b:
                b = '%s %s' % (b, self.http_reason)
            else:
                b = '- %s' % self.http_reason
        if self.http_device:
            if b:
                b = '%s: device %s' % (b, self.http_device)
            else:
                b = 'device %s' % self.http_device
        return b and '%s: %s' % (a, b) or a


def http_connection(url):
    """
    Make an HTTPConnection or HTTPSConnection

    :param url: url to connect to
    :returns: tuple of (parsed url, connection object)
    :raises ClientException: Unable to handle protocol scheme
    """
    parsed = urlparse(url)
    if parsed.scheme == 'http':
        conn = HTTPConnection(parsed.netloc)
    elif parsed.scheme == 'https':
        conn = HTTPSConnection(parsed.netloc)
    else:
        raise ClientException('Cannot handle protocol scheme %s for url %s' %
                              (parsed.scheme, repr(url)))
    return parsed, conn


def get_auth(url, user, key, snet=False):
    """
    Get authentication/authorization credentials.

    The snet parameter is used for Rackspace's ServiceNet internal network
    implementation. In this function, it simply adds *snet-* to the beginning
    of the host name for the returned storage URL. With Rackspace Cloud Files,
    use of this network path causes no bandwidth charges but requires the
    client to be running on Rackspace's ServiceNet network.

    :param url: authentication/authorization URL
    :param user: user to authenticate as
    :param key: key or password for authorization
    :param snet: use SERVICENET internal network (see above), default is False
    :returns: tuple of (storage URL, auth token)
    :raises ClientException: HTTP GET request to auth URL failed
    """
    parsed, conn = http_connection(url)
    conn.request('GET', parsed.path, '',
                 {'X-Auth-User': user, 'X-Auth-Key': key})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Auth GET failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port,
                http_path=parsed.path, http_status=resp.status,
                http_reason=resp.reason)
    url = resp.getheader('x-storage-url')
    if snet:
        parsed = list(urlparse(url))
        # Second item in the list is the netloc
        parsed[1] = 'snet-' + parsed[1]
        url = urlunparse(parsed)
    return url, resp.getheader('x-storage-token',
                                                resp.getheader('x-auth-token'))


def get_account(url, token, marker=None, limit=None, prefix=None,
                http_conn=None, full_listing=False):
    """
    Get a listing of containers for the account.

    :param url: storage URL
    :param token: auth token
    :param marker: marker query
    :param limit: limit query
    :param prefix: prefix query
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param full_listing: if True, return a full listing, else returns a max
                         of 10000 listings
    :returns: a tuple of (response headers, a list of containers) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if not http_conn:
        http_conn = http_connection(url)
    if full_listing:
        rv = get_account(url, token, marker, limit, prefix, http_conn)
        listing = rv[1]
        while listing:
            marker = listing[-1]['name']
            listing = \
                get_account(url, token, marker, limit, prefix, http_conn)[1]
            if listing:
                rv.extend(listing)
        return rv
    parsed, conn = http_conn
    qs = 'format=json'
    if marker:
        qs += '&marker=%s' % quote(marker)
    if limit:
        qs += '&limit=%d' % limit
    if prefix:
        qs += '&prefix=%s' % quote(prefix)
    conn.request('GET', '%s?%s' % (parsed.path, qs), '',
                 {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException('Account GET failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port,
                http_path=parsed.path, http_query=qs, http_status=resp.status,
                http_reason=resp.reason)
    if resp.status == 204:
        resp.read()
        return resp_headers, []
    return resp_headers, json_loads(resp.read())


def head_account(url, token, http_conn=None):
    """
    Get account stats.

    :param url: storage URL
    :param token: auth token
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    conn.request('HEAD', parsed.path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Account HEAD failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port,
                http_path=parsed.path, http_status=resp.status,
                http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def post_account(url, token, headers, http_conn=None):
    """
    Update an account's metadata.

    :param url: storage URL
    :param token: auth token
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    headers['X-Auth-Token'] = token
    conn.request('POST', parsed.path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Account POST failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)


def get_container(url, token, container, marker=None, limit=None,
                  prefix=None, delimiter=None, http_conn=None,
                  full_listing=False):
    """
    Get a listing of objects for the container.

    :param url: storage URL
    :param token: auth token
    :param container: container name to get a listing for
    :param marker: marker query
    :param limit: limit query
    :param prefix: prefix query
    :param delimeter: string to delimit the queries on
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param full_listing: if True, return a full listing, else returns a max
                         of 10000 listings
    :returns: a tuple of (response headers, a list of objects) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if not http_conn:
        http_conn = http_connection(url)
    if full_listing:
        rv = get_container(url, token, container, marker, limit, prefix,
                           delimiter, http_conn)
        listing = rv[1]
        while listing:
            if not delimiter:
                marker = listing[-1]['name']
            else:
                marker = listing[-1].get('name', listing[-1].get('subdir'))
            listing = get_container(url, token, container, marker, limit,
                                    prefix, delimiter, http_conn)[1]
            if listing:
                rv[1].extend(listing)
        return rv
    parsed, conn = http_conn
    path = '%s/%s' % (parsed.path, quote(container))
    qs = 'format=json'
    if marker:
        qs += '&marker=%s' % quote(marker)
    if limit:
        qs += '&limit=%d' % limit
    if prefix:
        qs += '&prefix=%s' % quote(prefix)
    if delimiter:
        qs += '&delimiter=%s' % quote(delimiter)
    conn.request('GET', '%s?%s' % (path, qs), '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException('Container GET failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_query=qs,
                http_status=resp.status, http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    if resp.status == 204:
        resp.read()
        return resp_headers, []
    return resp_headers, json_loads(resp.read())


def head_container(url, token, container, http_conn=None):
    """
    Get container stats.

    :param url: storage URL
    :param token: auth token
    :param container: container name to get stats for
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    conn.request('HEAD', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Container HEAD failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def put_container(url, token, container, headers=None, http_conn=None):
    """
    Create a container

    :param url: storage URL
    :param token: auth token
    :param container: container name to create
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP PUT request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    if not headers:
        headers = {}
    headers['X-Auth-Token'] = token
    conn.request('PUT', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Container PUT failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)


def post_container(url, token, container, headers, http_conn=None):
    """
    Update a container's metadata.

    :param url: storage URL
    :param token: auth token
    :param container: container name to update
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    headers['X-Auth-Token'] = token
    conn.request('POST', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Container POST failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)


def delete_container(url, token, container, http_conn=None):
    """
    Delete a container

    :param url: storage URL
    :param token: auth token
    :param container: container name to delete
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP DELETE request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s' % (parsed.path, quote(container))
    conn.request('DELETE', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Container DELETE failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)


def get_object(url, token, container, name, http_conn=None,
               resp_chunk_size=None):
    """
    Get an object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to get
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :param resp_chunk_size: if defined, chunk size of data to read. NOTE: If
                            you specify a resp_chunk_size you must fully read
                            the object's contents before making another
                            request.
    :returns: a tuple of (response headers, the object's contents) The response
              headers will be a dict and all header names will be lowercase.
    :raises ClientException: HTTP GET request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    conn.request('GET', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    if resp.status < 200 or resp.status >= 300:
        resp.read()
        raise ClientException('Object GET failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port, http_path=path,
                http_status=resp.status, http_reason=resp.reason)
    if resp_chunk_size:

        def _object_body():
            buf = resp.read(resp_chunk_size)
            while buf:
                yield buf
                buf = resp.read(resp_chunk_size)
        object_body = _object_body()
    else:
        object_body = resp.read()
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers, object_body


def head_object(url, token, container, name, http_conn=None):
    """
    Get object info

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to get info for
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: a dict containing the response's headers (all header names will
              be lowercase)
    :raises ClientException: HTTP HEAD request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    conn.request('HEAD', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Object HEAD failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port, http_path=path,
                http_status=resp.status, http_reason=resp.reason)
    resp_headers = {}
    for header, value in resp.getheaders():
        resp_headers[header.lower()] = value
    return resp_headers


def put_object(url, token, container, name, contents, content_length=None,
               etag=None, chunk_size=65536, content_type=None, headers=None,
               http_conn=None):
    """
    Put an object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to put
    :param contents: a string or a file like object to read object data from
    :param content_length: value to send as content-length header; also limits
                           the amount read from contents
    :param etag: etag of contents
    :param chunk_size: chunk size of data to write
    :param content_type: value to send as content-type header
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :returns: etag from server response
    :raises ClientException: HTTP PUT request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    if not headers:
        headers = {}
    headers['X-Auth-Token'] = token
    if etag:
        headers['ETag'] = etag.strip('"')
    if content_length is not None:
        headers['Content-Length'] = str(content_length)
    if content_type is not None:
        headers['Content-Type'] = content_type
    if not contents:
        headers['Content-Length'] = '0'
    if hasattr(contents, 'read'):
        conn.putrequest('PUT', path)
        for header, value in headers.iteritems():
            conn.putheader(header, value)
        if content_length is None:
            conn.putheader('Transfer-Encoding', 'chunked')
            conn.endheaders()
            chunk = contents.read(chunk_size)
            while chunk:
                conn.send('%x\r\n%s\r\n' % (len(chunk), chunk))
                chunk = contents.read(chunk_size)
            conn.send('0\r\n\r\n')
        else:
            conn.endheaders()
            left = content_length
            while left > 0:
                size = chunk_size
                if size > left:
                    size = left
                chunk = contents.read(size)
                conn.send(chunk)
                left -= len(chunk)
    else:
        conn.request('PUT', path, contents, headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Object PUT failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port, http_path=path,
                http_status=resp.status, http_reason=resp.reason)
    return resp.getheader('etag').strip('"')


def post_object(url, token, container, name, headers, http_conn=None):
    """
    Update object metadata

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: name of the object to update
    :param headers: additional headers to include in the request
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP POST request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    headers['X-Auth-Token'] = token
    conn.request('POST', path, '', headers)
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Object POST failed', http_scheme=parsed.scheme,
                http_host=conn.host, http_port=conn.port, http_path=path,
                http_status=resp.status, http_reason=resp.reason)


def delete_object(url, token, container, name, http_conn=None):
    """
    Delete object

    :param url: storage URL
    :param token: auth token
    :param container: container name that the object is in
    :param name: object name to delete
    :param http_conn: HTTP connection object (If None, it will create the
                      conn object)
    :raises ClientException: HTTP DELETE request failed
    """
    if http_conn:
        parsed, conn = http_conn
    else:
        parsed, conn = http_connection(url)
    path = '%s/%s/%s' % (parsed.path, quote(container), quote(name))
    conn.request('DELETE', path, '', {'X-Auth-Token': token})
    resp = conn.getresponse()
    resp.read()
    if resp.status < 200 or resp.status >= 300:
        raise ClientException('Object DELETE failed',
                http_scheme=parsed.scheme, http_host=conn.host,
                http_port=conn.port, http_path=path, http_status=resp.status,
                http_reason=resp.reason)


class Connection(object):
    """Convenience class to make requests that will also retry the request"""

    def __init__(self, authurl, user, key, retries=5, preauthurl=None,
                 preauthtoken=None, snet=False):
        """
        :param authurl: authenitcation URL
        :param user: user name to authenticate as
        :param key: key/password to authenticate with
        :param retries: Number of times to retry the request before failing
        :param preauthurl: storage URL (if you have already authenticated)
        :param preauthtoken: authentication token (if you have already
                             authenticated)
        :param snet: use SERVICENET internal network default is False
        """
        self.authurl = authurl
        self.user = user
        self.key = key
        self.retries = retries
        self.http_conn = None
        self.url = preauthurl
        self.token = preauthtoken
        self.attempts = 0
        self.snet = snet

    def get_auth(self):
        return get_auth(self.authurl, self.user, self.key, snet=self.snet)

    def http_connection(self):
        return http_connection(self.url)

    def _retry(self, func, *args, **kwargs):
        self.attempts = 0
        backoff = 1
        while self.attempts <= self.retries:
            self.attempts += 1
            try:
                if not self.url or not self.token:
                    self.url, self.token = self.get_auth()
                    self.http_conn = None
                if not self.http_conn:
                    self.http_conn = self.http_connection()
                kwargs['http_conn'] = self.http_conn
                rv = func(self.url, self.token, *args, **kwargs)
                return rv
            except (socket.error, HTTPException):
                if self.attempts > self.retries:
                    raise
                self.http_conn = None
            except ClientException, err:
                if self.attempts > self.retries:
                    raise
                if err.http_status == 401:
                    self.url = self.token = None
                    if self.attempts > 1:
                        raise
                elif 500 <= err.http_status <= 599:
                    pass
                else:
                    raise
            sleep(backoff)
            backoff *= 2

    def head_account(self):
        """Wrapper for :func:`head_account`"""
        return self._retry(head_account)

    def get_account(self, marker=None, limit=None, prefix=None,
                    full_listing=False):
        """Wrapper for :func:`get_account`"""
        # TODO(unknown): With full_listing=True this will restart the entire
        # listing with each retry. Need to make a better version that just
        # retries where it left off.
        return self._retry(get_account, marker=marker, limit=limit,
                           prefix=prefix, full_listing=full_listing)

    def post_account(self, headers):
        """Wrapper for :func:`post_account`"""
        return self._retry(post_account, headers)

    def head_container(self, container):
        """Wrapper for :func:`head_container`"""
        return self._retry(head_container, container)

    def get_container(self, container, marker=None, limit=None, prefix=None,
                      delimiter=None, full_listing=False):
        """Wrapper for :func:`get_container`"""
        # TODO(unknown): With full_listing=True this will restart the entire
        # listing with each retry. Need to make a better version that just
        # retries where it left off.
        return self._retry(get_container, container, marker=marker,
                           limit=limit, prefix=prefix, delimiter=delimiter,
                           full_listing=full_listing)

    def put_container(self, container, headers=None):
        """Wrapper for :func:`put_container`"""
        return self._retry(put_container, container, headers=headers)

    def post_container(self, container, headers):
        """Wrapper for :func:`post_container`"""
        return self._retry(post_container, container, headers)

    def delete_container(self, container):
        """Wrapper for :func:`delete_container`"""
        return self._retry(delete_container, container)

    def head_object(self, container, obj):
        """Wrapper for :func:`head_object`"""
        return self._retry(head_object, container, obj)

    def get_object(self, container, obj, resp_chunk_size=None):
        """Wrapper for :func:`get_object`"""
        return self._retry(get_object, container, obj,
                           resp_chunk_size=resp_chunk_size)

    def put_object(self, container, obj, contents, content_length=None,
                   etag=None, chunk_size=65536, content_type=None,
                   headers=None):
        """Wrapper for :func:`put_object`"""
        return self._retry(put_object, container, obj, contents,
            content_length=content_length, etag=etag, chunk_size=chunk_size,
            content_type=content_type, headers=headers)

    def post_object(self, container, obj, headers):
        """Wrapper for :func:`post_object`"""
        return self._retry(post_object, container, obj, headers)

    def delete_object(self, container, obj):
        """Wrapper for :func:`delete_object`"""
        return self._retry(delete_object, container, obj)

# End inclusion of swift.common.client
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #


def mkdirs(path):
    try:
        makedirs(path)
    except OSError, err:
        if err.errno != EEXIST:
            raise


class QueueFunctionThread(Thread):

    def __init__(self, queue, func, *args, **kwargs):
        """ Calls func for each item in queue; func is called with a queued
            item as the first arg followed by *args and **kwargs. Use the abort
            attribute to have the thread empty the queue (without processing)
            and exit. """
        Thread.__init__(self)
        self.abort = False
        self.queue = queue
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        while True:
            try:
                item = self.queue.get_nowait()
                if not self.abort:
                    self.func(item, *self.args, **self.kwargs)
                self.queue.task_done()
            except Empty:
                if self.abort:
                    break
                sleep(0.01)


st_delete_help = '''
delete --all OR delete container [--leave-segments] [object] [object] ...
    Deletes everything in the account (with --all), or everything in a
    container, or a list of objects depending on the args given. Segments of
    manifest objects will be deleted as well, unless you specify the
    --leave-segments option.'''.strip('\n')


def st_delete(parser, args, print_queue, error_queue):
    parser.add_option('-a', '--all', action='store_true', dest='yes_all',
        default=False, help='Indicates that you really want to delete '
        'everything in the account')
    parser.add_option('', '--leave-segments', action='store_true',
        dest='leave_segments', default=False, help='Indicates that you want '
        'the segments of manifest objects left alone')
    (options, args) = parse_args(parser, args)
    args = args[1:]
    if (not args and not options.yes_all) or (args and options.yes_all):
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_delete_help))
        return

    def _delete_segment((container, obj), conn):
        conn.delete_object(container, obj)
        if options.verbose:
            print_queue.put('%s/%s' % (container, obj))

    object_queue = Queue(10000)

    def _delete_object((container, obj), conn):
        try:
            old_manifest = None
            if not options.leave_segments:
                try:
                    old_manifest = conn.head_object(container, obj).get(
                        'x-object-manifest')
                except ClientException, err:
                    if err.http_status != 404:
                        raise
            conn.delete_object(container, obj)
            if old_manifest:
                segment_queue = Queue(10000)
                scontainer, sprefix = old_manifest.split('/', 1)
                for delobj in conn.get_container(scontainer,
                                                 prefix=sprefix)[1]:
                    segment_queue.put((scontainer, delobj['name']))
                if not segment_queue.empty():
                    segment_threads = [QueueFunctionThread(segment_queue,
                        _delete_segment, create_connection()) for _junk in
                        xrange(10)]
                    for thread in segment_threads:
                        thread.start()
                    while not segment_queue.empty():
                        sleep(0.01)
                    for thread in segment_threads:
                        thread.abort = True
                        while thread.isAlive():
                            thread.join(0.01)
            if options.verbose:
                path = options.yes_all and join(container, obj) or obj
                if path[:1] in ('/', '\\'):
                    path = path[1:]
                print_queue.put(path)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Object %s not found' %
                            repr('%s/%s' % (container, obj)))

    container_queue = Queue(10000)

    def _delete_container(container, conn):
        try:
            marker = ''
            while True:
                objects = [o['name'] for o in
                           conn.get_container(container, marker=marker)[1]]
                if not objects:
                    break
                for obj in objects:
                    object_queue.put((container, obj))
                marker = objects[-1]
            while not object_queue.empty():
                sleep(0.01)
            attempts = 1
            while True:
                try:
                    conn.delete_container(container)
                    break
                except ClientException, err:
                    if err.http_status != 409:
                        raise
                    if attempts > 10:
                        raise
                    attempts += 1
                    sleep(1)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Container %s not found' % repr(container))

    url, token = get_auth(options.auth, options.user, options.key,
        snet=options.snet)
    create_connection = lambda: Connection(options.auth, options.user,
        options.key, preauthurl=url, preauthtoken=token, snet=options.snet)
    object_threads = [QueueFunctionThread(object_queue, _delete_object,
        create_connection()) for _junk in xrange(10)]
    for thread in object_threads:
        thread.start()
    container_threads = [QueueFunctionThread(container_queue,
        _delete_container, create_connection()) for _junk in xrange(10)]
    for thread in container_threads:
        thread.start()
    if not args:
        conn = create_connection()
        try:
            marker = ''
            while True:
                containers = \
                    [c['name'] for c in conn.get_account(marker=marker)[1]]
                if not containers:
                    break
                for container in containers:
                    container_queue.put(container)
                marker = containers[-1]
            while not container_queue.empty():
                sleep(0.01)
            while not object_queue.empty():
                sleep(0.01)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Account not found')
    elif len(args) == 1:
        if '/' in args[0]:
            print >> stderr, 'WARNING: / in container name; you might have ' \
                             'meant %r instead of %r.' % \
                             (args[0].replace('/', ' ', 1), args[0])
        conn = create_connection()
        _delete_container(args[0], conn)
    else:
        for obj in args[1:]:
            object_queue.put((args[0], obj))
    while not container_queue.empty():
        sleep(0.01)
    for thread in container_threads:
        thread.abort = True
        while thread.isAlive():
            thread.join(0.01)
    while not object_queue.empty():
        sleep(0.01)
    for thread in object_threads:
        thread.abort = True
        while thread.isAlive():
            thread.join(0.01)


st_download_help = '''
download --all OR download container [options] [object] [object] ...
    Downloads everything in the account (with --all), or everything in a
    container, or a list of objects depending on the args given. For a single
    object download, you may use the -o [--output] <filename> option to
    redirect the output to a specific file or if "-" then just redirect to
    stdout.'''.strip('\n')


def st_download(options, args, print_queue, error_queue):
    parser.add_option('-a', '--all', action='store_true', dest='yes_all',
        default=False, help='Indicates that you really want to download '
        'everything in the account')
    parser.add_option('-o', '--output', dest='out_file', help='For a single '
        'file download, stream the output to an alternate location ')
    (options, args) = parse_args(parser, args)
    args = args[1:]
    if options.out_file == '-':
        options.verbose = 0
    if options.out_file and len(args) != 2:
        exit('-o option only allowed for single file downloads')
    if (not args and not options.yes_all) or (args and options.yes_all):
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_download_help))
        return

    object_queue = Queue(10000)

    def _download_object(queue_arg, conn):
        if len(queue_arg) == 2:
            container, obj = queue_arg
            out_file = None
        elif len(queue_arg) == 3:
            container, obj, out_file = queue_arg
        else:
            raise Exception("Invalid queue_arg length of %s" % len(queue_arg))
        try:
            headers, body = \
                conn.get_object(container, obj, resp_chunk_size=65536)
            content_type = headers.get('content-type')
            if 'content-length' in headers:
                content_length = int(headers.get('content-length'))
            else:
                content_length = None
            etag = headers.get('etag')
            path = options.yes_all and join(container, obj) or obj
            if path[:1] in ('/', '\\'):
                path = path[1:]
            md5sum = None
            make_dir = out_file != "-"
            if content_type.split(';', 1)[0] == 'text/directory':
                if make_dir and not isdir(path):
                    mkdirs(path)
                read_length = 0
                if 'x-object-manifest' not in headers:
                    md5sum = md5()
                for chunk in body:
                    read_length += len(chunk)
                    if md5sum:
                        md5sum.update(chunk)
            else:
                dirpath = dirname(path)
                if make_dir and dirpath and not isdir(dirpath):
                    mkdirs(dirpath)
                if out_file == "-":
                    fp = stdout
                elif out_file:
                    fp = open(out_file, 'wb')
                else:
                    fp = open(path, 'wb')
                read_length = 0
                if 'x-object-manifest' not in headers:
                    md5sum = md5()
                for chunk in body:
                    fp.write(chunk)
                    read_length += len(chunk)
                    if md5sum:
                        md5sum.update(chunk)
                fp.close()
            if md5sum and md5sum.hexdigest() != etag:
                error_queue.put('%s: md5sum != etag, %s != %s' %
                                (path, md5sum.hexdigest(), etag))
            if content_length is not None and read_length != content_length:
                error_queue.put('%s: read_length != content_length, %d != %d' %
                                (path, read_length, content_length))
            if 'x-object-meta-mtime' in headers and not options.out_file:
                mtime = float(headers['x-object-meta-mtime'])
                utime(path, (mtime, mtime))
            if options.verbose:
                print_queue.put(path)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Object %s not found' %
                            repr('%s/%s' % (container, obj)))

    container_queue = Queue(10000)

    def _download_container(container, conn):
        try:
            marker = ''
            while True:
                objects = [o['name'] for o in
                           conn.get_container(container, marker=marker)[1]]
                if not objects:
                    break
                for obj in objects:
                    object_queue.put((container, obj))
                marker = objects[-1]
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Container %s not found' % repr(container))

    url, token = get_auth(options.auth, options.user, options.key,
        snet=options.snet)
    create_connection = lambda: Connection(options.auth, options.user,
        options.key, preauthurl=url, preauthtoken=token, snet=options.snet)
    object_threads = [QueueFunctionThread(object_queue, _download_object,
        create_connection()) for _junk in xrange(10)]
    for thread in object_threads:
        thread.start()
    container_threads = [QueueFunctionThread(container_queue,
        _download_container, create_connection()) for _junk in xrange(10)]
    for thread in container_threads:
        thread.start()
    if not args:
        conn = create_connection()
        try:
            marker = ''
            while True:
                containers = [c['name']
                              for c in conn.get_account(marker=marker)[1]]
                if not containers:
                    break
                for container in containers:
                    container_queue.put(container)
                marker = containers[-1]
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Account not found')
    elif len(args) == 1:
        if '/' in args[0]:
            print >> stderr, 'WARNING: / in container name; you might have ' \
                             'meant %r instead of %r.' % \
                             (args[0].replace('/', ' ', 1), args[0])
        _download_container(args[0], create_connection())
    else:
        if len(args) == 2:
            obj = args[1]
            object_queue.put((args[0], obj, options.out_file))
        else:
            for obj in args[1:]:
                object_queue.put((args[0], obj))
    while not container_queue.empty():
        sleep(0.01)
    for thread in container_threads:
        thread.abort = True
        while thread.isAlive():
            thread.join(0.01)
    while not object_queue.empty():
        sleep(0.01)
    for thread in object_threads:
        thread.abort = True
        while thread.isAlive():
            thread.join(0.01)


st_list_help = '''
list [options] [container]
    Lists the containers for the account or the objects for a container. -p or
    --prefix is an option that will only list items beginning with that prefix.
    -d or --delimiter is option (for container listings only) that will roll up
    items with the given delimiter (see Cloud Files general documentation for
    what this means).
'''.strip('\n')


def st_list(options, args, print_queue, error_queue):
    parser.add_option('-p', '--prefix', dest='prefix', help='Will only list '
        'items beginning with the prefix')
    parser.add_option('-d', '--delimiter', dest='delimiter', help='Will roll '
        'up items with the given delimiter (see Cloud Files general '
        'documentation for what this means)')
    (options, args) = parse_args(parser, args)
    args = args[1:]
    if options.delimiter and not args:
        exit('-d option only allowed for container listings')
    if len(args) > 1:
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_list_help))
        return
    conn = Connection(options.auth, options.user, options.key,
        snet=options.snet)
    try:
        marker = ''
        while True:
            if not args:
                items = \
                    conn.get_account(marker=marker, prefix=options.prefix)[1]
            else:
                items = conn.get_container(args[0], marker=marker,
                    prefix=options.prefix, delimiter=options.delimiter)[1]
            if not items:
                break
            for item in items:
                print_queue.put(item.get('name', item.get('subdir')))
            marker = items[-1].get('name', items[-1].get('subdir'))
    except ClientException, err:
        if err.http_status != 404:
            raise
        if not args:
            error_queue.put('Account not found')
        else:
            error_queue.put('Container %s not found' % repr(args[0]))


st_stat_help = '''
stat [container] [object]
    Displays information for the account, container, or object depending on the
    args given (if any).'''.strip('\n')


def st_stat(options, args, print_queue, error_queue):
    (options, args) = parse_args(parser, args)
    args = args[1:]
    conn = Connection(options.auth, options.user, options.key)
    if not args:
        try:
            headers = conn.head_account()
            if options.verbose > 1:
                print_queue.put('''
StorageURL: %s
Auth Token: %s
'''.strip('\n') % (conn.url, conn.token))
            container_count = int(headers.get('x-account-container-count', 0))
            object_count = int(headers.get('x-account-object-count', 0))
            bytes_used = int(headers.get('x-account-bytes-used', 0))
            print_queue.put('''
   Account: %s
Containers: %d
   Objects: %d
     Bytes: %d'''.strip('\n') % (conn.url.rsplit('/', 1)[-1], container_count,
                                 object_count, bytes_used))
            for key, value in headers.items():
                if key.startswith('x-account-meta-'):
                    print_queue.put('%10s: %s' % ('Meta %s' %
                        key[len('x-account-meta-'):].title(), value))
            for key, value in headers.items():
                if not key.startswith('x-account-meta-') and key not in (
                        'content-length', 'date', 'x-account-container-count',
                        'x-account-object-count', 'x-account-bytes-used'):
                    print_queue.put(
                        '%10s: %s' % (key.title(), value))
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Account not found')
    elif len(args) == 1:
        if '/' in args[0]:
            print >> stderr, 'WARNING: / in container name; you might have ' \
                             'meant %r instead of %r.' % \
                             (args[0].replace('/', ' ', 1), args[0])
        try:
            headers = conn.head_container(args[0])
            object_count = int(headers.get('x-container-object-count', 0))
            bytes_used = int(headers.get('x-container-bytes-used', 0))
            print_queue.put('''
  Account: %s
Container: %s
  Objects: %d
    Bytes: %d
 Read ACL: %s
Write ACL: %s'''.strip('\n') % (conn.url.rsplit('/', 1)[-1], args[0],
                                object_count, bytes_used,
                                headers.get('x-container-read', ''),
                                headers.get('x-container-write', '')))
            for key, value in headers.items():
                if key.startswith('x-container-meta-'):
                    print_queue.put('%9s: %s' % ('Meta %s' %
                        key[len('x-container-meta-'):].title(), value))
            for key, value in headers.items():
                if not key.startswith('x-container-meta-') and key not in (
                        'content-length', 'date', 'x-container-object-count',
                        'x-container-bytes-used', 'x-container-read',
                        'x-container-write'):
                    print_queue.put(
                        '%9s: %s' % (key.title(), value))
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Container %s not found' % repr(args[0]))
    elif len(args) == 2:
        try:
            headers = conn.head_object(args[0], args[1])
            print_queue.put('''
       Account: %s
     Container: %s
        Object: %s
  Content Type: %s'''.strip('\n') % (conn.url.rsplit('/', 1)[-1], args[0],
                                     args[1], headers.get('content-type')))
            if 'content-length' in headers:
                print_queue.put('Content Length: %s' %
                                headers['content-length'])
            if 'last-modified' in headers:
                print_queue.put(' Last Modified: %s' %
                                headers['last-modified'])
            if 'etag' in headers:
                print_queue.put('          ETag: %s' % headers['etag'])
            if 'x-object-manifest' in headers:
                print_queue.put('      Manifest: %s' %
                                headers['x-object-manifest'])
            for key, value in headers.items():
                if key.startswith('x-object-meta-'):
                    print_queue.put('%14s: %s' % ('Meta %s' %
                        key[len('x-object-meta-'):].title(), value))
            for key, value in headers.items():
                if not key.startswith('x-object-meta-') and key not in (
                        'content-type', 'content-length', 'last-modified',
                        'etag', 'date', 'x-object-manifest'):
                    print_queue.put(
                        '%14s: %s' % (key.title(), value))
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Object %s not found' %
                            repr('%s/%s' % (args[0], args[1])))
    else:
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_stat_help))


st_post_help = '''
post [options] [container] [object]
    Updates meta information for the account, container, or object depending on
    the args given. If the container is not found, it will be created
    automatically; but this is not true for accounts and objects. Containers
    also allow the -r (or --read-acl) and -w (or --write-acl) options. The -m
    or --meta option is allowed on all and used to define the user meta data
    items to set in the form Name:Value. This option can be repeated. Example:
    post -m Color:Blue -m Size:Large'''.strip('\n')


def st_post(options, args, print_queue, error_queue):
    parser.add_option('-r', '--read-acl', dest='read_acl', help='Sets the '
        'Read ACL for containers. Quick summary of ACL syntax: .r:*, '
        '.r:-.example.com, .r:www.example.com, account1, account2:user2')
    parser.add_option('-w', '--write-acl', dest='write_acl', help='Sets the '
        'Write ACL for containers. Quick summary of ACL syntax: account1, '
        'account2:user2')
    parser.add_option('-m', '--meta', action='append', dest='meta', default=[],
        help='Sets a meta data item with the syntax name:value. This option '
        'may be repeated. Example: -m Color:Blue -m Size:Large')
    (options, args) = parse_args(parser, args)
    args = args[1:]
    if (options.read_acl or options.write_acl) and not args:
        exit('-r and -w options only allowed for containers')
    conn = Connection(options.auth, options.user, options.key)
    if not args:
        headers = {}
        for item in options.meta:
            split_item = item.split(':')
            headers['X-Account-Meta-' + split_item[0]] = \
                len(split_item) > 1 and split_item[1]
        try:
            conn.post_account(headers=headers)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Account not found')
    elif len(args) == 1:
        if '/' in args[0]:
            print >> stderr, 'WARNING: / in container name; you might have ' \
                             'meant %r instead of %r.' % \
                             (args[0].replace('/', ' ', 1), args[0])
        headers = {}
        for item in options.meta:
            split_item = item.split(':')
            headers['X-Container-Meta-' + split_item[0]] = \
                len(split_item) > 1 and split_item[1]
        if options.read_acl is not None:
            headers['X-Container-Read'] = options.read_acl
        if options.write_acl is not None:
            headers['X-Container-Write'] = options.write_acl
        try:
            conn.post_container(args[0], headers=headers)
        except ClientException, err:
            if err.http_status != 404:
                raise
            conn.put_container(args[0], headers=headers)
    elif len(args) == 2:
        headers = {}
        for item in options.meta:
            split_item = item.split(':')
            headers['X-Object-Meta-' + split_item[0]] = \
                len(split_item) > 1 and split_item[1]
        try:
            conn.post_object(args[0], args[1], headers=headers)
        except ClientException, err:
            if err.http_status != 404:
                raise
            error_queue.put('Object %s not found' %
                            repr('%s/%s' % (args[0], args[1])))
    else:
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_post_help))


st_upload_help = '''
upload [options] container file_or_directory [file_or_directory] [...]
    Uploads to the given container the files and directories specified by the
    remaining args. -c or --changed is an option that will only upload files
    that have changed since the last upload. -S <size> or --segment-size <size>
    and --leave-segments are options as well (see --help for more).
'''.strip('\n')


def st_upload(options, args, print_queue, error_queue):
    parser.add_option('-c', '--changed', action='store_true', dest='changed',
        default=False, help='Will only upload files that have changed since '
        'the last upload')
    parser.add_option('-S', '--segment-size', dest='segment_size', help='Will '
        'upload files in segments no larger than <size> and then create a '
        '"manifest" file that will download all the segments as if it were '
        'the original file. The segments will be uploaded to a '
        '<container>_segments container so as to not pollute the main '
        '<container> listings.')
    parser.add_option('', '--leave-segments', action='store_true',
        dest='leave_segments', default=False, help='Indicates that you want '
        'the older segments of manifest objects left alone (in the case of '
        'overwrites)')
    (options, args) = parse_args(parser, args)
    args = args[1:]
    if len(args) < 2:
        error_queue.put('Usage: %s [options] %s' %
                        (basename(argv[0]), st_upload_help))
        return
    object_queue = Queue(10000)

    def _segment_job(job, conn):
        if job.get('delete', False):
            conn.delete_object(job['container'], job['obj'])
        else:
            fp = open(job['path'], 'rb')
            fp.seek(job['segment_start'])
            conn.put_object(job.get('container', args[0] + '_segments'),
                job['obj'], fp, content_length=job['segment_size'])
        if options.verbose and 'log_line' in job:
            print_queue.put(job['log_line'])

    def _object_job(job, conn):
        path = job['path']
        container = job.get('container', args[0])
        dir_marker = job.get('dir_marker', False)
        try:
            obj = path
            if obj.startswith('./') or obj.startswith('.\\'):
                obj = obj[2:]
            put_headers = {'x-object-meta-mtime': str(getmtime(path))}
            if dir_marker:
                if options.changed:
                    try:
                        headers = conn.head_object(container, obj)
                        ct = headers.get('content-type')
                        cl = int(headers.get('content-length'))
                        et = headers.get('etag')
                        mt = headers.get('x-object-meta-mtime')
                        if ct.split(';', 1)[0] == 'text/directory' and \
                                cl == 0 and \
                                et == 'd41d8cd98f00b204e9800998ecf8427e' and \
                                mt == put_headers['x-object-meta-mtime']:
                            return
                    except ClientException, err:
                        if err.http_status != 404:
                            raise
                conn.put_object(container, obj, '', content_length=0,
                                content_type='text/directory',
                                headers=put_headers)
            else:
                # We need to HEAD all objects now in case we're overwriting a
                # manifest object and need to delete the old segments
                # ourselves.
                old_manifest = None
                if options.changed or not options.leave_segments:
                    try:
                        headers = conn.head_object(container, obj)
                        cl = int(headers.get('content-length'))
                        mt = headers.get('x-object-meta-mtime')
                        if options.changed and cl == getsize(path) and \
                                mt == put_headers['x-object-meta-mtime']:
                            return
                        if not options.leave_segments:
                            old_manifest = headers.get('x-object-manifest')
                    except ClientException, err:
                        if err.http_status != 404:
                            raise
                if options.segment_size and \
                        getsize(path) < options.segment_size:
                    full_size = getsize(path)
                    segment_queue = Queue(10000)
                    segment_threads = [QueueFunctionThread(segment_queue,
                        _segment_job, create_connection()) for _junk in
                        xrange(10)]
                    for thread in segment_threads:
                        thread.start()
                    segment = 0
                    segment_start = 0
                    while segment_start < full_size:
                        segment_size = int(options.segment_size)
                        if segment_start + segment_size > full_size:
                            segment_size = full_size - segment_start
                        segment_queue.put({'path': path,
                            'obj': '%s/%s/%s/%08d' % (obj,
                                put_headers['x-object-meta-mtime'], full_size,
                                segment),
                            'segment_start': segment_start,
                            'segment_size': segment_size,
                            'log_line': '%s segment %s' % (obj, segment)})
                        segment += 1
                        segment_start += segment_size
                    while not segment_queue.empty():
                        sleep(0.01)
                    for thread in segment_threads:
                        thread.abort = True
                        while thread.isAlive():
                            thread.join(0.01)
                    new_object_manifest = '%s_segments/%s/%s/%s/' % (
                        container, obj, put_headers['x-object-meta-mtime'],
                        full_size)
                    if old_manifest == new_object_manifest:
                        old_manifest = None
                    put_headers['x-object-manifest'] = new_object_manifest
                    conn.put_object(container, obj, '', content_length=0,
                                    headers=put_headers)
                else:
                    conn.put_object(container, obj, open(path, 'rb'),
                        content_length=getsize(path), headers=put_headers)
                if old_manifest:
                    segment_queue = Queue(10000)
                    scontainer, sprefix = old_manifest.split('/', 1)
                    for delobj in conn.get_container(scontainer,
                                                     prefix=sprefix)[1]:
                        segment_queue.put({'delete': True,
                            'container': scontainer, 'obj': delobj['name']})
                    if not segment_queue.empty():
                        segment_threads = [QueueFunctionThread(segment_queue,
                            _segment_job, create_connection()) for _junk in
                            xrange(10)]
                        for thread in segment_threads:
                            thread.start()
                        while not segment_queue.empty():
                            sleep(0.01)
                        for thread in segment_threads:
                            thread.abort = True
                            while thread.isAlive():
                                thread.join(0.01)
            if options.verbose:
                print_queue.put(obj)
        except OSError, err:
            if err.errno != ENOENT:
                raise
            error_queue.put('Local file %s not found' % repr(path))

    def _upload_dir(path):
        names = listdir(path)
        if not names:
            object_queue.put({'path': path, 'dir_marker': True})
        else:
            for name in listdir(path):
                subpath = join(path, name)
                if isdir(subpath):
                    _upload_dir(subpath)
                else:
                    object_queue.put({'path': subpath})

    url, token = get_auth(options.auth, options.user, options.key,
        snet=options.snet)
    create_connection = lambda: Connection(options.auth, options.user,
        options.key, preauthurl=url, preauthtoken=token, snet=options.snet)
    object_threads = [QueueFunctionThread(object_queue, _object_job,
        create_connection()) for _junk in xrange(10)]
    for thread in object_threads:
        thread.start()
    conn = create_connection()
    # Try to create the container, just in case it doesn't exist. If this
    # fails, it might just be because the user doesn't have container PUT
    # permissions, so we'll ignore any error. If there's really a problem,
    # it'll surface on the first object PUT.
    try:
        conn.put_container(args[0])
        if options.segment_size is not None:
            conn.put_container(args[0] + '_segments')
    except Exception:
        pass
    try:
        for arg in args[1:]:
            if isdir(arg):
                _upload_dir(arg)
            else:
                object_queue.put({'path': arg})
        while not object_queue.empty():
            sleep(0.01)
        for thread in object_threads:
            thread.abort = True
            while thread.isAlive():
                thread.join(0.01)
    except ClientException, err:
        if err.http_status != 404:
            raise
        error_queue.put('Account not found')


def parse_args(parser, args, enforce_requires=True):
    if not args:
        args = ['-h']
    (options, args) = parser.parse_args(args)
    if enforce_requires and \
            not (options.auth and options.user and options.key):
        exit('''
Requires ST_AUTH, ST_USER, and ST_KEY environment variables be set or
overridden with -A, -U, or -K.'''.strip('\n'))
    return options, args


if __name__ == '__main__':
    parser = OptionParser(version='%prog 1.0', usage='''
Usage: %%prog <command> [options] [args]

Commands:
  %(st_stat_help)s
  %(st_list_help)s
  %(st_upload_help)s
  %(st_post_help)s
  %(st_download_help)s
  %(st_delete_help)s

Example:
  %%prog -A https://auth.api.rackspacecloud.com/v1.0 -U user -K key stat
'''.strip('\n') % globals())
    parser.add_option('-s', '--snet', action='store_true', dest='snet',
                      default=False, help='Use SERVICENET internal network')
    parser.add_option('-v', '--verbose', action='count', dest='verbose',
                      default=1, help='Print more info')
    parser.add_option('-q', '--quiet', action='store_const', dest='verbose',
                      const=0, default=1, help='Suppress status output')
    parser.add_option('-A', '--auth', dest='auth',
                      default=environ.get('ST_AUTH'),
                      help='URL for obtaining an auth token')
    parser.add_option('-U', '--user', dest='user',
                      default=environ.get('ST_USER'),
                      help='User name for obtaining an auth token')
    parser.add_option('-K', '--key', dest='key',
                      default=environ.get('ST_KEY'),
                      help='Key for obtaining an auth token')
    parser.disable_interspersed_args()
    (options, args) = parse_args(parser, argv[1:], enforce_requires=False)
    parser.enable_interspersed_args()

    commands = ('delete', 'download', 'list', 'post', 'stat', 'upload')
    if not args or args[0] not in commands:
        parser.print_usage()
        if args:
            exit('no such command: %s' % args[0])
        exit()

    print_queue = Queue(10000)

    def _print(item):
        if isinstance(item, unicode):
            item = item.encode('utf8')
        print item

    print_thread = QueueFunctionThread(print_queue, _print)
    print_thread.start()

    error_queue = Queue(10000)

    def _error(item):
        if isinstance(item, unicode):
            item = item.encode('utf8')
        print >> stderr, item

    error_thread = QueueFunctionThread(error_queue, _error)
    error_thread.start()

    try:
        parser.usage = globals()['st_%s_help' % args[0]]
        globals()['st_%s' % args[0]](parser, argv[1:], print_queue,
                                     error_queue)
        while not print_queue.empty():
            sleep(0.01)
        print_thread.abort = True
        while print_thread.isAlive():
            print_thread.join(0.01)
        while not error_queue.empty():
            sleep(0.01)
        error_thread.abort = True
        while error_thread.isAlive():
            error_thread.join(0.01)
    except Exception:
        for thread in threading_enumerate():
            thread.abort = True
        raise

# Copyright (c) 2012 Ian C. Good
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

"""Implements an |Edge| that receives messages with the HTTP protocol.

"""

from __future__ import absolute_import

import sys
import re
from base64 import b64decode
from wsgiref.headers import Headers

import gevent
from gevent.pywsgi import WSGIServer
from gevent import monkey; monkey.patch_all()
from dns import resolver, reversename
from dns.exception import DNSException

from slimta import logging
from slimta.envelope import Envelope
from slimta.smtp.reply import Reply
from slimta.queue import QueueError
from slimta.relay import RelayError
from . import Edge

__all__ = ['WsgiEdge']

log = logging.getWSGILogger(__name__)


class WSGIResponse(Exception):

    def __init__(self, status, headers=None, data=None):
        super(WSGIResponse, self).__init__(status)
        self.status = status
        self.headers = headers or []
        self.data = data or []


def _header_name_to_cgi(name):
    return 'HTTP_{0}'.format(name.upper().replace('-', '_'))


def _build_http_response(smtp_reply):
    code = smtp_reply.code
    headers = []
    Headers(headers).add_header('X-Smtp-Reply', code,
                                message=smtp_reply.message)
    if code.startswith('2'):
        return WSGIResponse('200 OK', headers)
    elif code.startswith('4'):
        return WSGIResponse('503 Service Unavailable', headers)
    elif code == '535':
        return WSGIResponse('401 Unauthorized', headers)
    else:
        return WSGIResponse('500 Internal Server Error', headers)


class WsgiEdge(Edge):
    """This class is intended to be instantiated and used as an app on top of a
    WSGI server engine such as :class:`gevent.pywsgi.WSGIServer`. It will only
    acccept ``POST`` requests that provide a ``message/rfc822`` payload.

    :param queue: |Queue| object used by :meth:`.handoff()` to ensure the
                  envelope is properly queued before acknowledged by the edge
                  service.
    :param hostname: String identifying the local machine. See |Edge| for more
                     details.
    :param uri_pattern: If given, only URI paths that match the given pattern
                        will be allowed.
    :type uri_pattern: :py:class:`~re.RegexObject` or string
    :param sender_header: The header name that clients will use to provide the
                          envelope sender address.
    :param rcpt_header: The header name that clients will use to provide the
                        envelope recipient addresses. This header may be given
                        multiple times, for each recipient.
    :param ehlo_header: The header name that clients will use to provide the
                        EHLO identifier string, as in an SMTP session.

    """

    split_pattern = re.compile(r'\s*[,;]\s*')

    def __init__(self, queue, hostname=None, uri_pattern=None,
                 sender_header='X-Envelope-Sender',
                 rcpt_header='X-Envelope-Recipient', ehlo_header='X-Ehlo'):
        super(WsgiEdge, self).__init__(queue, hostname)
        self.uri_pattern = uri_pattern
        self.sender_header = _header_name_to_cgi(sender_header)
        self.rcpt_header = _header_name_to_cgi(rcpt_header)
        self.ehlo_header = _header_name_to_cgi(ehlo_header)

    def build_server(self, listener, pool=None, tls=None):
        """Constructs and returns a WSGI server engine, configured to use the
        current object as its application.

        :param listener: Usually a ``(ip, port)`` tuple defining the interface
                         and port upon which to listen for connections.
        :param pool: If given, defines a specific :class:`gevent.pool.Pool` to
                     use for new greenlets.
        :param tls: Optional dictionary of TLS settings passed directly as
                    keyword arguments to :class:`gevent.ssl.SSLSocket`.
        :rtype: :class:`gevent.pywsgi.WSGIServer`

        """
        spawn = pool or 'default'
        tls = tls or {}
        return WSGIServer(listener, self, log=sys.stdout, **tls)

    def __call__(self, environ, start_response):
        log.request(environ)
        self._trigger_ptr_lookup(environ)
        try:
            self._validate_request(environ)
            env = self._get_envelope(environ)
            self._add_envelope_extras(environ, env)
            self._enqueue_envelope(env)
        except WSGIResponse as res:
            start_response(res.status, res.headers)
            log.response(environ, res.status, res.headers)
            return res.data
        except Exception as exc:
            logging.log_exception(__name__)
            msg = '{0!s}\n'.format(exc)
            headers = [('Content-Length', len(msg)),
                       ('Content-Type', 'text/plain')]
            start_response('500 Internal Server Error', headers)
            return [msg]
        finally:
            environ['slimta.ptr_lookup_thread'].kill(block=False)

    def _validate_request(self, environ):
        if self.uri_pattern:
            path = environ.get('PATH_INFO', '')
            if not re.match(self.uri_pattern, path):
                raise WSGIResponse('404 Not Found')
        method = environ['REQUEST_METHOD'].upper()
        if method != 'POST':
            headers = [('Allow', 'POST')]
            raise WSGIResponse('405 Method Not Allowed', headers)
        ctype = environ.get('CONTENT_TYPE', 'message/rfc822')
        if ctype != 'message/rfc822':
            raise WSGIResponse('415 Unsupported Media Type')

    def _ptr_lookup(self, environ):
        ip = environ.get('REMOTE_ADDR', '0.0.0.0')
        ptraddr = reversename.from_address(ip)
        try:
            answers = resolver.query(ptraddr, 'PTR')
        except DNSException:
            answers = []
        try:
            environ['slimta.reverse_address'] = str(answers[0])
        except IndexError:
            pass

    def _trigger_ptr_lookup(self, environ):
        thread = gevent.spawn(self._ptr_lookup, environ)
        environ['slimta.ptr_lookup_thread'] = thread

    def _get_sender(self, environ):
        return b64decode(environ.get(self.sender_header, ''))

    def _get_recipients(self, environ):
        rcpts_raw = environ.get(self.rcpt_header, None)
        if not rcpts_raw:
            return []
        rcpts_split = self.split_pattern.split(rcpts_raw)
        return [b64decode(rcpt_b64) for rcpt_b64 in rcpts_split]

    def _get_ehlo(self, environ):
        default = '[{0}]'.format(environ.get('REMOTE_ADDR', 'unknown'))
        return environ.get(self.ehlo_header, default)

    def _get_envelope(self, environ):
        sender = self._get_sender(environ)
        recipients = self._get_recipients(environ)
        env = Envelope(sender, recipients)

        content_length = int(environ.get('CONTENT_LENGTH', 0))
        data = environ['wsgi.input'].read(content_length)
        env.parse(data)
        return env

    def _add_envelope_extras(self, environ, env):
        env.client['ip'] = environ.get('REMOTE_ADDR', 'unknown')
        env.client['host'] = environ.get('slimta.reverse_address', None)
        env.client['name'] = self._get_ehlo(environ)
        env.client['protocol'] = environ.get('wsgi.url_scheme', 'http').upper()

    def _enqueue_envelope(self, env):
        results = self.handoff(env)
        if isinstance(results[0][1], QueueError):
            reply = Reply('550', '5.6.0 Error queuing message')
            raise _build_http_response(reply)
        elif isinstance(results[0][1], RelayError):
            relay_reply = results[0][1].reply
            raise _build_http_response(relay_reply)
        reply = Reply('250', '2.6.0 Message accepted for delivery')
        raise _build_http_response(reply)


# vim:et:fdm=marker:sts=4:sw=4:ts=4
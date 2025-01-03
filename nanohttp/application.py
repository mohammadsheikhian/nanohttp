import sys
import types
import logging
import traceback
from collections.abc import Iterable

import ujson

from nanohttp.contexts import Context, context
from nanohttp.exceptions import HTTPStatus
from nanohttp.configuration import settings
from nanohttp.constants import NO_CONTENT_STATUSES


logger = logging.getLogger('nanohttp')


class Application:
    """Application main handler
    """

    #: Application logger based on python builtin logging module
    __logger__ = logger

    #: The root controller
    __root__ = None

    def __init__(self, root=None):
        """Initialize application and calling ``app_init`` hook.

        .. note:: ``__root__`` attribute will set by ``root`` parameter.

        :param root: The root controller
        """
        if root is not None:
            self.__root__ = root

        self._hook('app_init')

    def _hook(self, name, *args, **kwargs):
        """Call the hook

        :param name: Hook name
        :param args: Pass to the hook positional arguments
        :param kwargs: Pass to the hook keyword arguments
        """
        if hasattr(self, name):
            return getattr(self, name)(*args, **kwargs)

    def _handle_exception(self, ex, start_response):

        response_headers = [("content-type", "text/plain")]
        stack_trace = traceback.format_exc()
        if isinstance(ex, HTTPStatus):
            exc_info = None
            status = ex.status
            code, text = ex.status_format()
            if hasattr(ex, 'custom_status_code'):
                code = ex.custom_status_code

            response_body = dict(
                statusCode=code,
                message=text,
                stackTrace=None,
                messageFa=ex.message_fa,
            )
            if ex.headers:
                response_headers = ex.headers

        else:
            exc_info = sys.exc_info()
            status = '500 Internal Server Error'
            response_body = dict(
                statusCode=500,
                message='Internal Server Error',
                stackTrace=None,
                messageFa='خطای داخلی سرور',
            )

        if settings.debug:
            response_body['stackTrace'] = stack_trace

        if response_body['statusCode'] in ['400', '500', 400, 500]:
            extra_information = None
            if hasattr(ex, 'extra_information'):
                extra_information = ex.extra_information

            _log_response_body = dict(
                statusCode=response_body['statusCode'],
                message=f"{response_body['message']}: {extra_information}",
                stackTrace=stack_trace,
            )
            self.__logger__.error(ujson.dumps(
                _log_response_body,
                escape_forward_slashes=True,
                reject_bytes=False,
            ))

        if hasattr(context, 'identity') and context.identity:
            response_headers.append(('X-Identity', str(context.identity.id)))

        response_body = ujson.dumps(response_body, reject_bytes=False)
        start_response(
            status,
            response_headers,
            exc_info
        )
        self._hook('end_response')
        context.__exit__(*sys.exc_info())

        # Sometimes don't need to transfer any body, for example the 304 case.
        if status[:3] in NO_CONTENT_STATUSES:
            return []

        return [response_body.encode()]

    def __call__(self, environ, start_response):
        """Method that
        `WSGI <https://www.python.org/dev/peps/pep-0333/#id15>`_ server calls
        """
        # Entering the context
        context_ = Context(environ, self)
        context_.__enter__()

        # Preparing some variables
        status = '200 OK'
        buffer = None
        response_iterable = None

        try:
            self._hook('begin_request')

            # Removing the trailing slash in-place, if exists
            context_.path = context_.path.rstrip('/')

            # Removing the heading slash, and query string anyway
            path = context_.path[1:].split('?')[0]

            # Splitting the path by slash(es) if any
            remaining_paths = path.split('/') if path else []

            # Calling the controller, actually this will be serve our request
            response_body = self.__root__(*remaining_paths)

            if response_body:
                # The goal is to yield an iterable, to encode and iter over it
                # at the end of this method.

                if isinstance(response_body, (str, bytes)):
                    # Mocking the body inside an iterable to prevent
                    # the iteration over the str character by character
                    # For more info check the pull-request
                    # #34, https://github.com/pylover/nanohttp/pull/34
                    response_iterable = (response_body, )

                elif isinstance(response_body, types.GeneratorType):
                    # Generators are iterable !
                    response_iterable = response_body

                    # Trying to get at least one element from the generator,
                    # to force the method call till the second
                    # `yield` statement
                    buffer = next(response_iterable)

                elif isinstance(response_body, Iterable):
                    # Creating an iterator from iterable!
                    response_iterable = iter(response_body)

                else:
                    raise ValueError(
                        'Controller\'s action/handler response must be '
                        'generator and or iterable'
                    )

        except Exception as ex:
            return self._handle_exception(ex, start_response)

        self._hook('begin_response')

        # Setting cookies in response headers, if any
        cookie = context_.cookies.output()
        if cookie:
            for line in cookie.split('\r\n'):
                context_.response_headers.add_header(*line.split(': ', 1))

        start_response(
            status,
            context_.response_headers.items(),
        )

        # It seems we have to transfer a body, so this function should yield
        # a generator of the body chunks.
        def _response():
            try:
                if buffer is not None:
                    yield context_.encode_response(buffer)

                if response_iterable:
                    # noinspection PyTypeChecker
                    for chunk in response_iterable:
                        yield context_.encode_response(chunk)
                else:
                    yield b''
            except Exception as ex_:
                self.__logger__.exception(
                    'Exception while serving the response.'
                )
                if settings.debug:
                    yield str(ex_).encode()
                raise ex_

            finally:
                self._hook('end_response')
                context.__exit__(*sys.exc_info())

        return _response()

    def shutdown(self):  # pragma: nocover
        pass


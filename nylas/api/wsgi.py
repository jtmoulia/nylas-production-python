import socket
import errno

from gevent.pywsgi import WSGIHandler, WSGIServer

from gunicorn.workers.ggevent import GeventWorker
import gunicorn.glogging

from nylas.util.debug import Tracer
from nylas.logging import get_logger, configure_logging
log = get_logger()

# Monkeypatch with values from your app's config file to change.
# Set to 0 to disable altogether.
MAX_BLOCKING_TIME = 1.

# Same deal here (with monkeypatching).
LOGLEVEL = 10


class NylasWSGIHandler(WSGIHandler):
    """Custom WSGI handler class to customize request logging. Based on
    gunicorn.workers.ggevent.PyWSGIHandler."""
    def log_request(self):
        # gevent.pywsgi tries to call log.write(), but Python logger objects
        # implement log.debug(), log.info(), etc., so we need to monkey-patch
        # log_request(). See
        # http://stackoverflow.com/questions/9444405/gunicorn-and-websockets
        log = self.server.log
        length = self.response_length
        if self.time_finish:
            request_time = round(self.time_finish - self.time_start, 6)
        if isinstance(self.client_address, tuple):
            client_address = self.client_address[0]
        else:
            client_address = self.client_address

        # client_address is '' when requests are forwarded from nginx via
        # Unix socket. In that case, replace with a meaningful value
        if client_address == '':
            client_address = self.headers.get('X-Forward-For')
        status = getattr(self, 'code', None)
        requestline = getattr(self, 'requestline', None)

        # To use this, generate a unique ID at your termination proxy (e.g.
        # haproxy or nginx) and set it as a header on the request
        request_uid = self.headers.get('X-Unique-Id')

        additional_context = self.environ.get('log_context') or {}

        # Since not all users may implement this, don't log null values
        if request_uid is not None:
            additional_context['request_uid'] = request_uid

        # 'prod', 'staging', 'dev' ...
        env = self.environ.get('NYLAS_ENV')
        if env is not None:
            additional_context['env'] = env

        log.info('request handled',
                 response_bytes=length,
                 request_time=request_time,
                 remote_addr=client_address,
                 http_status=status,
                 http_request=requestline,
                 **additional_context)

    def get_environ(self):
        env = super(NylasWSGIHandler, self).get_environ()
        env['gunicorn.sock'] = self.socket
        env['RAW_URI'] = self.path
        return env

    def handle_error(self, type, value, tb):
        # Suppress tracebacks when e.g. a client disconnects from the streaming
        # API.
        if (issubclass(type, socket.error) and value.args[0] == errno.EPIPE and
                self.response_length):
            self.server.log.info('Socket error', exc=value)
            self.close_connection = True
        else:
            super(NylasWSGIHandler, self).handle_error(type, value, tb)


class NylasWSGIWorker(GeventWorker):
    """Custom worker class for gunicorn. Based on
    gunicorn.workers.ggevent.GeventPyWSGIWorker."""
    server_class = WSGIServer
    wsgi_handler = NylasWSGIHandler

    def init_process(self):
        if MAX_BLOCKING_TIME:
            self.tracer = Tracer(max_blocking_time=MAX_BLOCKING_TIME)
            self.tracer.start()
        super(NylasWSGIWorker, self).init_process()


class NylasGunicornLogger(gunicorn.glogging.Logger):
    def __init__(self, cfg):
        gunicorn.glogging.Logger.__init__(self, cfg)
        configure_logging(log_level=LOGLEVEL)
        self.error_log = log

import os
import sys
import types
import inspect
import datetime
import pytest
import tornado
import tornado.gen
import tornado.testing
import tornado.httpserver
import tornado.httpclient

if sys.version_info[:2] >= (3, 5):
    iscoroutinefunction = inspect.iscoroutinefunction
else:
    iscoroutinefunction = lambda f: False


def _get_async_test_timeout():
    try:
        return float(os.environ.get('ASYNC_TEST_TIMEOUT'))
    except (ValueError, TypeError):
        return 5


def pytest_addoption(parser):
    parser.addoption('--async-test-timeout', type=float,
                     default=_get_async_test_timeout(),
                     help='timeout in seconds before failing the test')
    parser.addoption('--app-fixture', default='app',
                     help='fixture name returning a tornado application')


def pytest_configure(config):
    config.addinivalue_line("markers",
                            "gen_test(timeout=None): "
                            "mark the test as asynchronous, it will be "
                            "run using tornado's event loop")


def _argnames(func):
    spec = inspect.getargspec(func)
    if spec.defaults:
        return spec.args[:-len(spec.defaults)]
    if isinstance(func, types.FunctionType):
        return spec.args
    # Func is a bound method, skip "self"
    return spec.args[1:]


def _timeout(item):
    default_timeout = item.config.getoption('async_test_timeout')
    gen_test = item.get_marker('gen_test')
    if gen_test:
        return gen_test.kwargs.get('timeout', default_timeout)
    return default_timeout


@pytest.mark.tryfirst
def pytest_pycollect_makeitem(collector, name, obj):
    if collector.funcnamefilter(name) and inspect.isgeneratorfunction(obj):
        item = pytest.Function(name, parent=collector)
        if 'gen_test' in item.keywords:
            return list(collector._genfunctions(name, obj))


def pytest_runtest_setup(item):
    if 'gen_test' in item.keywords and 'io_loop' not in item.fixturenames:
        # inject an event loop fixture for all async tests
        item.fixturenames.append('io_loop')


@pytest.mark.tryfirst
def pytest_pyfunc_call(pyfuncitem):
    gen_test_mark = pyfuncitem.keywords.get('gen_test')
    if gen_test_mark:
        io_loop = pyfuncitem.funcargs.get('io_loop')
        run_sync = gen_test_mark.kwargs.get('run_sync', True)

        funcargs = dict((arg, pyfuncitem.funcargs[arg])
                        for arg in _argnames(pyfuncitem.obj))
        if iscoroutinefunction(pyfuncitem.obj):
            coroutine = pyfuncitem.obj
            future = tornado.gen.convert_yielded(coroutine(**funcargs))
        else:
            coroutine = tornado.gen.coroutine(pyfuncitem.obj)
            future = coroutine(**funcargs)
        if run_sync:
            io_loop.run_sync(lambda: future, timeout=_timeout(pyfuncitem))
        else:
            # Run this test function as a coroutine, until the timeout. When completed, stop the IOLoop
            # and reraise any exceptions

            future_with_timeout = tornado.gen.with_timeout(
                    datetime.timedelta(seconds=_timeout(pyfuncitem)),
                    future)
            io_loop.add_future(future_with_timeout, lambda f: io_loop.stop())
            io_loop.start()

            # This will reraise any exceptions that occurred.
            future_with_timeout.result()

        # prevent other pyfunc calls from executing
        return True


@pytest.fixture
def io_loop(request):
    """Create an instance of the `tornado.ioloop.IOLoop` for each test case.
    """
    io_loop = tornado.ioloop.IOLoop()
    io_loop.make_current()

    def _close():
        io_loop.clear_current()
        io_loop.close(all_fds=True)

    request.addfinalizer(_close)
    return io_loop


@pytest.fixture
def _unused_port():
    return tornado.testing.bind_unused_port()


@pytest.fixture
def http_port(_unused_port):
    """Get a port used by the test server.
    """
    return _unused_port[1]


@pytest.fixture
def base_url(http_port):
    """Create an absolute base url (scheme://host:port)
    """
    return 'http://localhost:%s' % http_port


@pytest.fixture
def http_server(request, io_loop, _unused_port):
    """Start a tornado HTTP server.

    You must create an `app` fixture, which returns
    the `tornado.web.Application` to be tested.

    Raises:
        FixtureLookupError: tornado application fixture not found
    """
    http_app = request.getfuncargvalue(request.config.option.app_fixture)
    server = tornado.httpserver.HTTPServer(http_app)
    server.add_socket(_unused_port[0])

    def _stop():
        server.stop()

        if hasattr(server, 'close_all_connections'):
            io_loop.run_sync(server.close_all_connections,
                             timeout=request.config.option.async_test_timeout)

    request.addfinalizer(_stop)
    return server


@pytest.fixture
def http_client(request, http_server):
    """Get an asynchronous HTTP client.
    """
    client = tornado.httpclient.AsyncHTTPClient()

    def _close():
        client.close()

    request.addfinalizer(_close)
    return client

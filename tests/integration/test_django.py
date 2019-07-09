# coding=utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import datetime as dt
import sys
from contextlib import contextmanager

import django
import pytest
from django.apps import apps
from django.conf import settings
from django.core.wsgi import get_wsgi_application
from django.http import HttpResponse
from django.test.utils import override_settings
from webtest import TestApp

from scout_apm.api import Config
from scout_apm.compat import datetime_to_timestamp
from tests.compat import mock
from tests.integration import django_app  # noqa  # force import to configure
from tests.integration.util import (
    parametrize_queue_time_header_name,
    parametrize_user_ip_headers,
)

skip_unless_new_style_middleware = pytest.mark.skipif(
    django.VERSION < (1, 10), reason="new-style middleware was added in Django 1.10"
)

skip_unless_old_style_middleware = pytest.mark.skipif(
    django.VERSION >= (1, 10), reason="new-style middleware was added in Django 1.10"
)


@pytest.fixture(autouse=True)
def ensure_no_django_config_applied_after_tests():
    """
    Prevent state leaking into the non-Django tests. All config needs to be set
    with @override_settings so that the on_setting_changed handler removes
    them from the dictionary afterwards.
    """
    yield
    assert all(
        (key != "BASE_DIR" and not key.startswith("SCOUT_")) for key in dir(settings)
    )


@contextmanager
def app_with_scout(**settings):
    """
    Context manager that simply overrides settings. Unlike the other web
    frameworks, Django is a singleton application, so we can't smoothly
    uninstall and reinstall scout per test.
    """
    settings.setdefault("SCOUT_MONITOR", True)
    settings["SCOUT_CORE_AGENT_LAUNCH"] = False
    with override_settings(**settings):
        # Have to create a new WSGI app each time because the middleware stack
        # within it is static
        yield get_wsgi_application()


def test_on_setting_changed_application_root():
    with app_with_scout(BASE_DIR="/tmp/foobar"):
        assert Config().value("application_root") == "/tmp/foobar"
    assert Config().value("application_root") == ""


def test_on_setting_changed_monitor():
    with app_with_scout(SCOUT_MONITOR=True):
        assert Config().value("monitor") is True
    assert Config().value("monitor") is False


def test_home(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert response.text == "Welcome home."
    assert len(tracked_requests) == 1
    tracked_request = tracked_requests[0]
    assert tracked_request.tags["path"] == "/"
    assert tracked_request.tags["user_ip"] is None
    spans = tracked_requests[0].complete_spans
    assert [s.operation for s in spans] == [
        "Controller/tests.integration.django_app.home",
        "Middleware",
    ]


def test_home_ignored(tracked_requests):
    with app_with_scout(SCOUT_IGNORE="/") as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert response.text == "Welcome home."
    assert tracked_requests == []


@parametrize_user_ip_headers
def test_user_ip(headers, extra_environ, expected, tracked_requests):
    if sys.version_info[0] == 2:
        # Required for WebTest lint
        headers = {str(k): str(v) for k, v in headers.items()}
        extra_environ = {str(k): str(v) for k, v in extra_environ.items()}

    with app_with_scout() as app:
        TestApp(app).get("/", headers=headers, extra_environ=extra_environ)

    tracked_request = tracked_requests[0]
    assert tracked_request.tags["user_ip"] == expected


def test_hello(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/hello/")
        assert response.status_int == 200

    assert len(tracked_requests) == 1
    spans = tracked_requests[0].complete_spans
    assert [s.operation for s in spans] == [
        "Controller/tests.integration.django_app.hello",
        "Middleware",
    ]


def test_not_found(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/not-found/", expect_errors=True)

    assert response.status_int == 404
    assert len(tracked_requests) == 0


def test_server_error(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/crash/", expect_errors=True)

    assert response.status_int == 500
    assert len(tracked_requests) == 1
    tracked_request = tracked_requests[0]
    assert tracked_request.tags["error"] == "true"
    spans = tracked_requests[0].complete_spans
    operations = [s.operation for s in spans]
    if django.VERSION >= (1, 9):
        # Changed in Django 1.9 or later (we only test 1.8 and 1.11 at time of
        # writing)
        expected_operations = [
            "Template/Compile/<Unknown Template>",
            "Template/Render/<Unknown Template>",
            "Controller/tests.integration.django_app.crash",
            "Middleware",
        ]
    else:
        expected_operations = [
            "Controller/tests.integration.django_app.crash",
            "Middleware",
        ]
    assert operations == expected_operations


def test_sql(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/sql/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    spans = tracked_requests[0].complete_spans
    assert [s.operation for s in spans] == [
        "SQL/Query",
        "SQL/Many",
        "SQL/Query",
        "Controller/tests.integration.django_app.sql",
        "Middleware",
    ]


# Monkey patch should_capture_backtrace in order to keep the test fast.
@mock.patch(
    "scout_apm.core.n_plus_one_call_set.NPlusOneCallSetItem.should_capture_backtrace"
)
def test_sql_capture_backtrace(should_capture_backtrace, tracked_requests):
    should_capture_backtrace.return_value = True
    with app_with_scout() as app:
        response = TestApp(app).get("/sql/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    spans = tracked_requests[0].complete_spans
    assert [s.operation for s in spans] == [
        "SQL/Query",
        "SQL/Many",
        "SQL/Query",
        "Controller/tests.integration.django_app.sql",
        "Middleware",
    ]


def test_template(tracked_requests):
    with app_with_scout() as app:
        response = TestApp(app).get("/template/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    spans = tracked_requests[0].complete_spans
    print([s.operation for s in spans])
    assert [s.operation for s in spans] == [
        "Template/Compile/<Unknown Template>",
        "Block/Render/name",
        "Template/Render/<Unknown Template>",
        "Controller/tests.integration.django_app.template",
        "Middleware",
    ]


@pytest.mark.xfail(reason="Test setup doesn't reset state fully at the moment.")
def test_no_monitor(tracked_requests):
    with app_with_scout(SCOUT_MONITOR=False) as app:
        response = TestApp(app).get("/hello/")

    assert response.status_int == 200
    assert len(tracked_requests) == 0


def fake_authentication_middleware(get_response):
    def middleware(request):
        # Mock the User instance to avoid a dependency on django.contrib.auth.
        request.user = mock.Mock()
        request.user.get_username.return_value = "scout"
        return get_response(request)

    return middleware


@skip_unless_new_style_middleware
def test_username(tracked_requests):
    new_middleware = (
        settings.MIDDLEWARE[:1]
        + [__name__ + ".fake_authentication_middleware"]
        + settings.MIDDLEWARE[1:]
    )
    with app_with_scout(MIDDLEWARE=new_middleware) as app:
        response = TestApp(app).get("/hello/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    tr = tracked_requests[0]
    assert tr.tags["username"] == "scout"


def crashy_authentication_middleware(get_response):
    def middleware(request):
        # Mock the User instance to avoid a dependency on django.contrib.auth.
        request.user = mock.Mock()
        request.user.get_username.side_effect = ValueError
        return get_response(request)

    return middleware


@skip_unless_new_style_middleware
def test_username_exception(tracked_requests):
    new_middleware = (
        settings.MIDDLEWARE[:1]
        + [__name__ + ".crashy_authentication_middleware"]
        + settings.MIDDLEWARE[1:]
    )
    with app_with_scout(MIDDLEWARE=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    tr = tracked_requests[0]
    assert "username" not in tr.tags


class FakeAuthenticationMiddleware(object):
    def process_request(self, request):
        # Mock the User instance to avoid a dependency on django.contrib.auth.
        request.user = mock.Mock()
        request.user.get_username.return_value = "scout"


@skip_unless_old_style_middleware
def test_old_style_username(tracked_requests):
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:1]
        + [__name__ + ".FakeAuthenticationMiddleware"]
        + settings.MIDDLEWARE_CLASSES[1:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    tr = tracked_requests[0]
    print(tr.tags)
    assert tr.tags["username"] == "scout"


class CrashyAuthenticationMiddleware(object):
    def process_request(self, request):
        # Mock the User instance to avoid a dependency on django.contrib.auth.
        request.user = mock.Mock()
        request.user.get_username.side_effect = ValueError


@skip_unless_old_style_middleware
def test_old_style_username_exception(tracked_requests):
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:1]
        + [__name__ + ".CrashyAuthenticationMiddleware"]
        + settings.MIDDLEWARE_CLASSES[1:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    tr = tracked_requests[0]
    assert "username" not in tr.tags


@parametrize_queue_time_header_name
def test_queue_time(header_name, tracked_requests):
    # Not testing floats due to Python 2/3 rounding differences
    queue_start = int(datetime_to_timestamp(dt.datetime.utcnow()) - 2)
    with app_with_scout() as app:
        response = TestApp(app).get(
            "/", headers={header_name: str("t=") + str(queue_start)}
        )

    assert response.status_int == 200
    assert len(tracked_requests) == 1
    queue_time_ns = tracked_requests[0].tags["scout.queue_time_ns"]
    # Upper bound assumes we didn't take more than 2s to run this test...
    assert queue_time_ns >= 2000000000 and queue_time_ns < 4000000000


@skip_unless_old_style_middleware
@pytest.mark.parametrize("list_or_tuple", [list, tuple])
@pytest.mark.parametrize("preinstalled", [True, False])
def test_install_middleware_old_style(list_or_tuple, preinstalled):
    if preinstalled:
        middleware = list_or_tuple(
            [
                "scout_apm.django.middleware.OldStyleMiddlewareTimingMiddleware",
                "django.middleware.common.CommonMiddleware",
                "scout_apm.django.middleware.OldStyleViewMiddleware",
            ]
        )
    else:
        middleware = list_or_tuple(["django.middleware.common.CommonMiddleware"])

    with override_settings(MIDDLEWARE_CLASSES=middleware):
        apps.get_app_config("scout_apm").install_middleware()

        assert settings.MIDDLEWARE_CLASSES == list_or_tuple(
            [
                "scout_apm.django.middleware.OldStyleMiddlewareTimingMiddleware",
                "django.middleware.common.CommonMiddleware",
                "scout_apm.django.middleware.OldStyleViewMiddleware",
            ]
        )


@skip_unless_new_style_middleware
@pytest.mark.parametrize("list_or_tuple", [list, tuple])
@pytest.mark.parametrize("preinstalled", [True, False])
def test_install_middleware_new_style(list_or_tuple, preinstalled):
    if preinstalled:
        middleware = list_or_tuple(
            [
                "scout_apm.django.middleware.MiddlewareTimingMiddleware",
                "django.middleware.common.CommonMiddleware",
                "scout_apm.django.middleware.ViewTimingMiddleware",
            ]
        )
    else:
        middleware = list_or_tuple(["django.middleware.common.CommonMiddleware"])

    with override_settings(MIDDLEWARE=middleware):
        apps.get_app_config("scout_apm").install_middleware()

        assert settings.MIDDLEWARE == list_or_tuple(
            [
                "scout_apm.django.middleware.MiddlewareTimingMiddleware",
                "django.middleware.common.CommonMiddleware",
                "scout_apm.django.middleware.ViewTimingMiddleware",
            ]
        )


class OldStyleOnRequestResponseMiddleware:
    def process_request(self, request):
        return HttpResponse("on_request response!")


@skip_unless_old_style_middleware
@pytest.mark.parametrize("middleware_index", [0, 1, 2])
def test_old_style_on_request_response_middleware(middleware_index, tracked_requests):
    """
    Test the case that a middleware got added/injected that generates a
    response in its process_request, triggering Django's middleware shortcut
    path. This will not be counted as a real request because it doesn't reach a
    view.
    """
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:middleware_index]
        + [__name__ + "." + OldStyleOnRequestResponseMiddleware.__name__]
        + settings.MIDDLEWARE_CLASSES[middleware_index:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert response.text == "on_request response!"
    assert len(tracked_requests) == 0


class OldStyleOnResponseResponseMiddleware:
    def process_response(self, request, response):
        return HttpResponse("process_response response!")


@skip_unless_old_style_middleware
@pytest.mark.parametrize("middleware_index", [0, 1, 2])
def test_old_style_on_response_response_middleware(middleware_index, tracked_requests):
    """
    Test the case that a middleware got added/injected that generates a fresh
    response in its process_response. This will count as a real request because
    it reaches the view, but then the view's response gets replaced on the way
    out.
    """
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:middleware_index]
        + [__name__ + "." + OldStyleOnResponseResponseMiddleware.__name__]
        + settings.MIDDLEWARE_CLASSES[middleware_index:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert response.text == "process_response response!"
    assert len(tracked_requests) == 1


class OldStyleOnViewResponseMiddleware:
    def process_view(self, request, view_func, view_func_args, view_func_kwargs):
        return HttpResponse("process_view response!")


@skip_unless_old_style_middleware
@pytest.mark.parametrize("middleware_index", [0, 1, 2])
def test_old_style_on_view_response_middleware(middleware_index, tracked_requests):
    """
    Test the case that a middleware got added/injected that generates a fresh
    response in its process_response. This will count as a real request because
    it reaches the view, but then the view's response gets replaced on the way
    out.
    """
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:middleware_index]
        + [__name__ + "." + OldStyleOnViewResponseMiddleware.__name__]
        + settings.MIDDLEWARE_CLASSES[middleware_index:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert response.text == "process_view response!"
    # If the middleware is before OldStyleViewMiddleware, its process_view
    # won't be called and we won't know to mark the request as real, so it
    # won't be tracked.
    if middleware_index < 2:
        assert len(tracked_requests) == 0
    else:
        assert len(tracked_requests) == 1


class OldStyleOnExceptionResponseMiddleware:
    def process_exception(self, request, exception):
        return HttpResponse("process_exception response!")


@skip_unless_old_style_middleware
@pytest.mark.parametrize("middleware_index", [0, 1, 2])
def test_old_style_on_exception_response_middleware(middleware_index, tracked_requests):
    """
    Test the case that a middleware got added/injected that generates a
    response in its process_exception. This should follow basically the same
    path as normal view exception, since Django applies process_response from
    middleware on the outgoing response.
    """
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:middleware_index]
        + [__name__ + "." + OldStyleOnExceptionResponseMiddleware.__name__]
        + settings.MIDDLEWARE_CLASSES[middleware_index:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/crash/")

    assert response.status_int == 200
    assert response.text == "process_exception response!"
    assert len(tracked_requests) == 1

    # In the case that the middleware is added after OldStyleViewMiddleware,
    # its process_exception won't be called so we won't know it's an error.
    # Nothing we can do there - but it's a rare case, since we programatically
    # add our middleware at the end of the stack.
    if middleware_index != 2:
        assert tracked_requests[0].tags["error"] == "true"


class OldStyleExceptionOnRequestMiddleware:
    def process_request(self, request):
        return ValueError("Woops!")


@skip_unless_old_style_middleware
@pytest.mark.parametrize("middleware_index", [0, 1, 2])
def test_old_style_exception_on_request_middleware(middleware_index, tracked_requests):
    """
    Test the case that a middleware got added/injected that raises an exception
    in its process_request.
    """
    new_middleware = (
        settings.MIDDLEWARE_CLASSES[:middleware_index]
        + [__name__ + "." + OldStyleExceptionOnRequestMiddleware.__name__]
        + settings.MIDDLEWARE_CLASSES[middleware_index:]
    )
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/", expect_errors=True)

    assert response.status_int == 500
    assert len(tracked_requests) == 0


@skip_unless_old_style_middleware
@pytest.mark.parametrize("url,expected_status", [("/", 200), ("/crash/", 500)])
def test_old_style_timing_middleware_deleted(url, expected_status, tracked_requests):
    """
    Test the case that some adversarial thing fiddled with the settings
    after app.ready() (like we do!) in order to remove the
    OldStyleMiddlewareTimingMiddleware. The tracked request won't be started
    but OldStyleViewMiddleware defends against this.
    """
    new_middleware = settings.MIDDLEWARE_CLASSES[1:]
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get(url, expect_errors=True)

    assert response.status_int == expected_status
    assert len(tracked_requests) == 0


@skip_unless_old_style_middleware
def test_old_style_view_middleware_deleted(tracked_requests):
    """
    Test the case that some adversarial thing fiddled with the settings
    after app.ready() (like we do!) in order to remove the
    OldStyleViewMiddleware. The tracked request won't be marked as real since
    its process_view won't have run.
    """
    new_middleware = settings.MIDDLEWARE_CLASSES[:1]
    with app_with_scout(MIDDLEWARE_CLASSES=new_middleware) as app:
        response = TestApp(app).get("/")

    assert response.status_int == 200
    assert len(tracked_requests) == 0

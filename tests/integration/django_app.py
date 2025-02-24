# coding: utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import sys

import django
import wrapt
from django.conf import settings
from django.template.response import TemplateResponse

from tests.compat import suppress

config = {
    "ALLOWED_HOSTS": ["*"],
    "DATABASES": {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    },
    "DEBUG_PROPAGATE_EXCEPTIONS": True,
    "ROOT_URLCONF": __name__,
    "SECRET_KEY": "********",
    "TEMPLATES": [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                ]
            },
        }
    ],
    "TIME_ZONE": "America/Chicago",
    # Setup as per https://docs.scoutapm.com/#django but *without* the settings
    # - these are temporarily set by app_with_scout() to avoid state leak
    "INSTALLED_APPS": [
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.messages",
        "django.contrib.sessions",
        "huey.contrib.djhuey",
        "scout_apm.django",
    ],
    # https://huey.readthedocs.io/en/latest/django.html
    "HUEY": {"backend_class": "huey.MemoryHuey", "immediate": True},
}

if django.VERSION > (1, 10):
    config["MIDDLEWARE"] = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ]
else:
    config["MIDDLEWARE_CLASSES"] = [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ]


settings.configure(**config)

if True:
    # Old versions of Django, at least 1.8, need settings configured before
    # other bits are imported such as Admin. Hence do the imports here, under
    # an 'if True' to appease isort.
    from django.contrib import admin
    from django.db import connection
    from django.http import HttpResponse
    from django.template import engines
    from django.utils.functional import SimpleLazyObject
    from django.views.generic import View


def home(request):
    return HttpResponse("Welcome home.")


def hello(request):
    return HttpResponse("Hello World!")


def set_session(request):
    request.session["session_var"] = 1
    return HttpResponse("Set session")


def crash(request):
    raise ValueError("BØØM!")  # non-ASCII


def return_error(request):
    return HttpResponse("Something went wrong", status=503)


class CbvView(View):
    def get(self, request):
        return HttpResponse("Hello getter")


def get_username(request):
    return HttpResponse(request.user.username)


def sql(request):
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS test(item)")
        cursor.executemany(
            "INSERT INTO test(item) VALUES(%s)", [("Hello",), ("World!",)]
        )
        cursor.execute("SELECT item from test")
        result = " ".join(item for (item,) in cursor.fetchall())
    return HttpResponse(result)


def sql_kwargs(request):
    with connection.cursor() as cursor:
        cursor.execute(sql="CREATE TABLE IF NOT EXISTS test(item)")
        cursor.executemany(
            sql="INSERT INTO test(item) VALUES(%s)",
            param_list=[("Hello",), ("World!",)],
        )
    return HttpResponse("Okay")


def sql_type_errors(request):
    with connection.cursor() as cursor:
        with suppress(TypeError):
            cursor.execute()

        if sys.version_info >= (3, 9):
            exc_type = TypeError
        else:
            exc_type = ValueError

        with suppress(exc_type):
            cursor.execute(sql=None)

        with suppress(TypeError):
            cursor.executemany()

        with suppress(TypeError):
            cursor.executemany(sql=None, param_list=[(1,)])
    return HttpResponse("Done")


def template(request):
    template = engines["django"].from_string(
        "Hello {% block name %}{{ name }}{% endblock %}!"
    )
    context = {"name": "World"}
    return HttpResponse(template.render(context))


@wrapt.decorator
def exclaimify_template_response_name(wrapped, instance, args, kwargs):
    response = wrapped(*args, **kwargs)
    response.context_data["name"] = response.context_data["name"] + "!!"
    return response


@exclaimify_template_response_name
def template_response(request):
    template = engines["django"].from_string(
        "Hello {% block name %}{{ name }}{% endblock %}!"
    )
    context = {"name": "World"}
    return TemplateResponse(request, template, context)


@SimpleLazyObject
def drf_router():
    """
    DRF Router as a lazy object because it needs to import User model which
    can't be done until after django.setup()
    """
    from django.contrib.auth.models import User
    from rest_framework import routers, serializers, viewsets

    class UserSerializer(serializers.Serializer):
        id = serializers.IntegerField(label="ID", read_only=True)
        username = serializers.CharField(max_length=200)

    class UserViewSet(viewsets.ModelViewSet):
        queryset = User.objects.all()
        serializer_class = UserSerializer

    class ErrorViewSet(viewsets.ModelViewSet):
        queryset = User.objects.all()

        def get_queryset(self, *args, **kwargs):
            raise ValueError("BØØM!")

    router = routers.SimpleRouter()
    router.register(r"users", UserViewSet)
    router.register(r"crash", ErrorViewSet)

    return router


@SimpleLazyObject
def tastypie_api():
    """
    Tastypie API as a lazy object because it needs to import User model which
    can't be done until after django.setup()
    """
    from django.contrib.auth.models import User

    try:
        from tastypie.api import Api as TastypieApi
        from tastypie.resources import ModelResource as TastypieModelResource
    except ImportError:
        return None

    class UserResource(TastypieModelResource):
        class Meta:
            queryset = User.objects.all()
            allowed_methods = ["get"]

    class CrashResource(TastypieModelResource):
        class Meta:
            queryset = User.objects.all()
            allowed_methods = ["get"]

        def build_filters(self, *args, **kwargs):
            raise ValueError("BØØM!")

    api = TastypieApi(api_name="v1")
    api.register(UserResource())
    api.register(CrashResource())
    return api


@SimpleLazyObject
def urlpatterns():
    """
    URL's as a lazy object because they touch admin.site.urls and that isn't
    ready until django.setup() has been called
    """
    if django.VERSION >= (2, 0):
        from django.urls import include, path

        patterns = [
            path("", home),
            path("hello/", hello),
            path("crash/", crash),
            path("set-session/", set_session),
            path("return-error/", return_error),
            path("cbv/", CbvView.as_view()),
            path("get-username/", get_username),
            path("sql/", sql),
            path("sql-kwargs/", sql_kwargs),
            path("sql-type-errors/", sql_type_errors),
            path("template/", template),
            path("template-response/", template_response),
            path("admin/", admin.site.urls),
            path("drf-router/", include(drf_router.urls)),
        ]
        if tastypie_api:
            patterns.append(path("tastypie-api/", include(tastypie_api.urls)))
        return patterns

    else:
        from django.conf.urls import include, url

        patterns = [
            url(r"^$", home),
            url(r"^hello/$", hello),
            url(r"^crash/$", crash),
            url(r"^set-session/$", set_session),
            url(r"^return-error/$", return_error),
            url(r"^cbv/$", CbvView.as_view()),
            url(r"^get-username/$", get_username),
            url(r"^sql/$", sql),
            url(r"^sql-kwargs/$", sql_kwargs),
            url(r"^sql-type-errors/$", sql_type_errors),
            url(r"^template/$", template),
            url(r"^template-response/$", template_response),
            url(r"^admin/", admin.site.urls),
            url(r"^drf-router/", include(drf_router.urls)),
        ]
        if tastypie_api:
            patterns.append(url(r"^tastypie-api/", include(tastypie_api.urls)))
        return patterns

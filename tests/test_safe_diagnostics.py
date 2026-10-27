"""Exception diagnostics expose categories and numeric fields, not values."""

import errno
import json
from types import SimpleNamespace

import pytest
import requests
import yaml

from app.logging_utils import safe_exception_details


SECRET = "SYNTHETIC_PRIVATE_VALUE"


@pytest.mark.parametrize("error,category", [
    (requests.Timeout(SECRET), "Timeout"),
    (requests.ConnectionError(SECRET), "ConnectionError"),
    (requests.HTTPError(SECRET), "HTTPError"),
    (requests.exceptions.InvalidURL(SECRET), "InvalidURL"),
    (requests.exceptions.InvalidSchema(SECRET), "InvalidSchema"),
    (requests.exceptions.MissingSchema(SECRET), "MissingSchema"),
    (requests.exceptions.SSLError(SECRET), "SSLError"),
    (ValueError(SECRET), "ValueError"),
    (KeyError(SECRET), "KeyError"),
    (TypeError(SECRET), "TypeError"),
    (RuntimeError(SECRET), "RuntimeError"),
    (json.JSONDecodeError(SECRET, SECRET, 0), "JSONDecodeError"),
    (AttributeError(SECRET), "AttributeError"),
    (Exception(SECRET), "Exception"),
])
def test_safe_diagnostics_use_known_categories(error, category):
    result = safe_exception_details(error)
    assert json.loads(result) == {"category": category}
    assert SECRET not in result


def test_safe_diagnostics_keep_numeric_status_and_errno():
    response = requests.Response()
    response.status_code = 503  # A failed response is falsey; still retain its status.
    response._content = SECRET.encode()
    error = requests.HTTPError(SECRET, response=response)
    error.errno = errno.EACCES
    assert json.loads(safe_exception_details(error)) == {
        "category": "HTTPError", "http_status": 503, "errno": errno.EACCES,
    }


@pytest.mark.parametrize("mark_name", ["problem_mark", "context_mark"])
def test_safe_diagnostics_keep_only_yaml_coordinates(mark_name):
    mark = yaml.error.Mark(SECRET, 0, 2, 4, SECRET, 0)
    error = yaml.error.MarkedYAMLError(
        context=SECRET, problem=SECRET, note=SECRET, **{mark_name: mark}
    )
    result = safe_exception_details(error)
    assert json.loads(result) == {"category": "YAMLError", "line": 3, "column": 5}
    assert SECRET not in result


@pytest.mark.parametrize("value", [SECRET, True, -1, None])
def test_safe_diagnostics_ignore_unsafe_metadata(value):
    error = yaml.YAMLError(SECRET)
    error.response = SimpleNamespace(status_code=value)
    error.errno = value
    error.problem_mark = SimpleNamespace(line=value, column=value, name=SECRET)
    assert json.loads(safe_exception_details(error)) == {"category": "YAMLError"}


def test_safe_diagnostics_do_not_format_int_subclasses():
    class PrivateInt(int):
        def __str__(self):
            return SECRET

    value = PrivateInt(503)
    error = OSError(SECRET)
    error.response = SimpleNamespace(status_code=value)
    error.errno = value
    assert json.loads(safe_exception_details(error)) == {"category": "OSError"}


def test_safe_diagnostics_never_stringify_exception_or_use_custom_class_name():
    class PrivateError(Exception):
        def __str__(self):
            pytest.fail("Exception text must never be formatted")

        @property
        def response(self):
            raise RuntimeError(SECRET)

        @property
        def errno(self):
            raise RuntimeError(SECRET)

    PrivateError.__name__ = SECRET
    assert json.loads(safe_exception_details(PrivateError())) == {"category": "Exception"}

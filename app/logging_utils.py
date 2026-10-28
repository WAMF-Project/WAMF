"""Helpers for including untrusted diagnostic values in plain-text logs."""

import json
import sqlite3

import requests
import yaml


def sanitize_log_value(value):
    """Preserve a value as a quoted ASCII string with escaped control characters."""
    return json.dumps(str(value), ensure_ascii=True)


def _diagnostic_attribute(value, name):
    try:
        return getattr(value, name, None)
    except Exception:
        # An unusual exception/property must not break the error handler.
        return None


def safe_exception_details(error):
    """Return only known categories/numeric metadata, never exception text."""
    categories = (
        (yaml.YAMLError, "YAMLError"),
        (requests.exceptions.SSLError, "SSLError"),
        (requests.Timeout, "Timeout"),
        (requests.ConnectionError, "ConnectionError"),
        (requests.HTTPError, "HTTPError"),
        (requests.exceptions.InvalidURL, "InvalidURL"),
        (requests.exceptions.InvalidSchema, "InvalidSchema"),
        (requests.exceptions.MissingSchema, "MissingSchema"),
        (requests.RequestException, "RequestException"),
        (sqlite3.Error, "SQLiteError"),
        (OSError, "OSError"),
        (json.JSONDecodeError, "JSONDecodeError"),
        (ValueError, "ValueError"),
        (TypeError, "TypeError"),
        (KeyError, "KeyError"),
        (AttributeError, "AttributeError"),
        (RuntimeError, "RuntimeError"),
    )
    details = {"category": next(
        (label for kind, label in categories if isinstance(error, kind)), "Exception"
    )}
    status = _diagnostic_attribute(_diagnostic_attribute(error, "response"), "status_code")
    if type(status) is int and 100 <= status <= 599:
        details["http_status"] = status
    error_number = _diagnostic_attribute(error, "errno")
    if type(error_number) is int and error_number >= 0:
        details["errno"] = error_number
    if isinstance(error, yaml.YAMLError):
        mark = _diagnostic_attribute(error, "problem_mark")
        if mark is None:
            mark = _diagnostic_attribute(error, "context_mark")
        line = _diagnostic_attribute(mark, "line")
        column = _diagnostic_attribute(mark, "column")
        if type(line) is int and type(column) is int and line >= 0 and column >= 0:
            details.update(line=line + 1, column=column + 1)
    return json.dumps(details, sort_keys=True)

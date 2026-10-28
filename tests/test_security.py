"""Redirect destination validation, independent of the login flow."""

import pytest

from app.security import is_safe_next_url


@pytest.mark.parametrize("destination", [
    None, "", b"/admin", 123,
    "https://evil.example/admin", "http://localhost/admin",
    "https:/evil.example", "https:///evil.example", "javascript:alert(1)",
    "//evil.example/admin", "///evil.example/admin", "//[invalid",
    "admin", "?next=/admin", "#admin", " /admin",
    "\\\\evil.example", "/\\evil.example", "/admin\\..\\evil.example",
    "/%2fevil.example", "/%2F%2Fevil.example", "/%5cevil.example",
    "/admin?value=\\evil.example",
    "/%09/evil.example", "/%0d%0aLocation:%20https://evil.example",
])
def test_safe_next_url_rejects_unsafe_destinations(destination):
    assert is_safe_next_url(destination) is False


@pytest.mark.parametrize("control", [chr(value) for value in range(32)] + [chr(127), chr(133), chr(159)])
@pytest.mark.parametrize("template", ["{control}/admin", "/{control}/evil.example", "/admin?q={control}"])
def test_safe_next_url_rejects_controls_before_parsing(control, template):
    assert is_safe_next_url(template.format(control=control)) is False


@pytest.mark.parametrize("destination", [
    "/", "/admin", "/admin/api/health?check=1", "/admin#settings",
    "/admin?tag=one&tag=two&empty=&flag",
    "/admin?q=blue+tit&name=blue%20tit&literal=%252F",
    "/admin?next=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1",
    "/admin?next=https://example.com/a?x=1",
    "/admin?next=//example.com", "/admin?q=%23tag%26value%3D1",
    "/media/snapshots/blue%20tit.jpg", "/species/Grünfink",
])
def test_safe_next_url_accepts_internal_destinations(destination):
    assert is_safe_next_url(destination) is True


@pytest.mark.parametrize("depth", [1, 2, 3, 5])
@pytest.mark.parametrize("suffix", [
    "/evil.example", "\\evil.example", "\t/evil.example", "\r\n/evil.example",
    "admin\x00", "admin\x7f", "admin\x85",
])
def test_safe_next_url_rejects_repeatedly_encoded_unsafe_paths(depth, suffix):
    from urllib.parse import quote

    for _ in range(depth):
        suffix = quote(suffix, safe="")
    assert is_safe_next_url("/" + suffix) is False


def test_safe_next_url_rejects_mixed_encoding_layers():
    assert is_safe_next_url("/%25%32%46evil.example") is False


@pytest.mark.parametrize("destination", [
    "/admin?value=%5Cevil.example", "/admin?value=%255Cevil.example",
    "/admin?value=%25255Cevil.example", "/admin?value=%00",
    "/admin?value=%7f", "/admin?value=%C2%85",
    "/admin?value=%2509%250d%250a", "/admin?value=%252509%25250d%25250a",
    "/admin?next=%252F%252Fevil.example", "/admin?next=%25252F%25252Fevil.example",
    "/admin?tag=one&tag=two&empty=&flag&literal=%25&invalid=%zz",
    "/media/blue%2520tit.jpg", "/media/nested%252Fclip.mp4",
    "/admin#value=%25255C",
])
def test_safe_next_url_preserves_encoded_data(destination):
    assert is_safe_next_url(destination) is True

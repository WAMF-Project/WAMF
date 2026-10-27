"""Wikipedia summary requests must stay within one fixed HTTPS API endpoint."""

from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests

from app import metadata_tasks, species_metadata


SUMMARY = {
    "extract": "A bird summary.",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/American_robin"}},
    "thumbnail": {"source": "https://upload.wikimedia.org/example.jpg"},
}


def _response(status=200, location=None):
    response = Mock(status_code=status, headers={"Location": location})
    response.json.return_value = SUMMARY
    return response


@pytest.fixture
def wikipedia(monkeypatch):
    response = _response()
    get = Mock(return_value=response)
    monkeypatch.setattr(species_metadata.requests, "get", get)
    return get, response


@pytest.mark.parametrize("name,segment", [
    ("Turdus migratorius", "Turdus_migratorius"),
    ("Turdus_migratorius", "Turdus_migratorius"),
    ("Anas sp.", "Anas_sp."),
    ("Turdus migratorius (Linnaeus, 1766)", "Turdus_migratorius_%28Linnaeus%2C_1766%29"),
    ("O'Connor’s bird", "O%27Connor%E2%80%99s_bird"),
    ("Anas × Éider", "Anas_%C3%97_%C3%89ider"),
    ("Anas E\u0301ider", "Anas_%C3%89ider"),
    ("Anas + Corvus & Pica", "Anas_%2B_Corvus_%26_Pica"),
])
def test_metadata_names_are_encoded_as_one_segment(wikipedia, name, segment):
    get, response = wikipedia
    result = species_metadata.fetch_species_metadata(name)
    url = species_metadata.SUMMARY_BASE_URL + segment
    get.assert_called_once_with(
        url, headers={"User-Agent": "WAMF/1.0 (Bird Wildlife Monitoring)"},
        timeout=10, allow_redirects=False,
    )
    prepared = requests.Request("GET", url).prepare().url
    assert prepared == url
    parsed = urlsplit(prepared)
    assert (parsed.scheme, parsed.netloc) == ("https", "en.wikipedia.org")
    assert parsed.path == species_metadata.SUMMARY_PATH + segment
    assert parsed.query == parsed.fragment == ""
    assert result == {
        "scientific_name": name, "description": SUMMARY["extract"],
        "wikipedia_url": SUMMARY["content_urls"]["desktop"]["page"],
        "thumbnail_url": SUMMARY["thumbnail"]["source"],
    }
    response.close.assert_called_once_with()


@pytest.mark.parametrize("name", [
    None, 123, b"Turdus", [], {}, "", " ", ".", "..", "...", "_", "123",
    " Turdus", "Turdus ", "../Turdus", "Turdus/merula", "Turdus\\merula",
    "../../../../w/index.php?title=Example", "https://evil.example/",
    "//evil.example", "Turdus?redirect=false", "Turdus#fragment",
    "Turdus%3Fredirect=false", "Turdus%23fragment",
    "Turdus%2Fmerula", "Turdus%5cmerula", "%2e%2e", "%252e%252e",
    "Turdus%252Fmerula", "Turdus%25252Fmerula", "Turdus%250d%250a",
    "Turdus\ud800", "Turdus\u2028merula", "Turdus\u2029merula", "Turdus\u202emerula",
] + ["Turdus" + chr(code) + "merula" for code in list(range(32)) + list(range(127, 160))])
def test_metadata_rejects_unsafe_names_before_request(wikipedia, name):
    get, _ = wikipedia
    with pytest.raises(ValueError, match="Invalid scientific name"):
        species_metadata.fetch_species_metadata(name)
    get.assert_not_called()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [
    "https://en.wikipedia.org/api/rest_v1/page/summary/American_robin",
    "/api/rest_v1/page/summary/American_robin", "American_robin",
    "/api/rest_v1/page/summary/American%20robin",
])
def test_metadata_follows_only_rebuilt_summary_redirects(wikipedia, status, location):
    get, final = wikipedia
    redirect = _response(status, location)
    get.side_effect = [redirect, final]
    result = species_metadata.fetch_species_metadata("Turdus migratorius")
    assert result["scientific_name"] == "Turdus migratorius"
    assert result["description"] == SUMMARY["extract"]
    assert [call.args[0] for call in get.call_args_list] == [
        species_metadata.SUMMARY_BASE_URL + "Turdus_migratorius",
        species_metadata.SUMMARY_BASE_URL + "American_robin",
    ]
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)
    redirect.close.assert_called_once_with()
    final.close.assert_called_once_with()


@pytest.mark.parametrize("location", [
    None, "", "https://evil.example/api/rest_v1/page/summary/Turdus",
    "//evil.example/api/rest_v1/page/summary/Turdus",
    "http://en.wikipedia.org/api/rest_v1/page/summary/Turdus",
    "https://en.wikipedia.org.evil.example/api/rest_v1/page/summary/Turdus",
    "https://user:password@en.wikipedia.org/api/rest_v1/page/summary/Turdus",
    "https://en.wikipedia.org:444/api/rest_v1/page/summary/Turdus",
    "https://en.wikipedia.org/wiki/Turdus", "../../../../w/index.php",
    "/api/rest_v1/page/summary-evil/Turdus", "/api/rest_v1/page/summary/..",
    "/api/rest_v1/page/summary/Turdus/merula",
    "/api/rest_v1/page/summary/Turdus%2Fmerula",
    "/api/rest_v1/page/summary/Turdus%252Fmerula",
    "/api/rest_v1/page/summary/Turdus%25252Fmerula",
    "/api/rest_v1/page/summary/Turdus%5cmerula",
    "/api/rest_v1/page/summary/Turdus%0d%0aFORGED",
    "/api/rest_v1/page/summary/Turdus%FF",
    "/api/rest_v1/page/summary/Turdus%3Fredirect=false",
    "/api/rest_v1/page/summary/Turdus%23fragment",
    "https://[invalid/api/rest_v1/page/summary/Turdus",
    "/api/rest_v1/page/summary/Turdus?next=https://evil.example",
    "/api/rest_v1/page/summary/Turdus#fragment",
    "/api/rest_v1/page/summary/Turdus\\merula",
    "/api/rest_v1/page/summary/Turdus\r\nFORGED",
    "/api/rest_v1/page/summary/Turdus\x1b[2J",
])
def test_metadata_rejects_unsafe_redirect_without_second_request(wikipedia, location):
    get, response = wikipedia
    response.status_code = 302
    response.headers["Location"] = location
    with pytest.raises(ValueError):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    get.assert_called_once()
    response.close.assert_called_once_with()
    response.json.assert_not_called()


def test_metadata_bounds_redirect_chains(wikipedia):
    get, _ = wikipedia
    responses = [_response(302, "Turdus") for _ in range(6)]
    get.side_effect = responses
    with pytest.raises(requests.TooManyRedirects):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    assert get.call_count == 6
    for response in responses:
        response.close.assert_called_once_with()


@pytest.mark.parametrize("error_type", [requests.Timeout, requests.ConnectionError])
def test_metadata_request_failures_propagate(wikipedia, error_type):
    get, _ = wikipedia
    get.side_effect = error_type("Request failed")
    with pytest.raises(error_type):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    get.assert_called_once()


@pytest.mark.parametrize("status", [404, 429, 500])
def test_metadata_http_errors_propagate_and_close(wikipedia, status):
    _, response = wikipedia
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError("Request failed")
    with pytest.raises(requests.HTTPError):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    response.close.assert_called_once_with()
    response.json.assert_not_called()


@pytest.mark.parametrize("status", [201, 300, 304])
def test_metadata_other_statuses_are_not_parsed_as_summaries(wikipedia, status):
    _, response = wikipedia
    response.status_code = status
    with pytest.raises(requests.HTTPError):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    response.close.assert_called_once_with()
    response.json.assert_not_called()


def test_metadata_json_failure_closes_response(wikipedia):
    _, response = wikipedia
    response.json.side_effect = ValueError("Invalid JSON")
    with pytest.raises(ValueError, match="Invalid JSON"):
        species_metadata.fetch_species_metadata("Turdus migratorius")
    response.close.assert_called_once_with()


def test_metadata_without_thumbnail_still_updates_database(wikipedia, monkeypatch):
    _, response = wikipedia
    response.json.return_value = {key: value for key, value in SUMMARY.items() if key != "thumbnail"}
    save = Mock()
    common_name = Mock(return_value="American Robin")
    monkeypatch.setattr(metadata_tasks, "save_species_info", save)
    monkeypatch.setattr(metadata_tasks, "get_common_name", common_name)
    metadata_tasks.refresh_species_metadata("Turdus migratorius")
    common_name.assert_called_once_with("Turdus migratorius")
    save.assert_called_once_with(
        scientific_name="Turdus migratorius", common_name="American Robin",
        description=SUMMARY["extract"],
        wikipedia_url=SUMMARY["content_urls"]["desktop"]["page"], thumbnail_url=None,
    )


def test_worker_rejects_unsafe_name_and_continues_without_database_update(wikipedia, monkeypatch):
    get, _ = wikipedia
    invalid = "../../../../w/index.php?title=Example"
    valid = "Turdus migratorius"
    work_queue = Mock()
    work_queue.get.side_effect = [invalid, valid, StopIteration]
    queued = {invalid, valid}
    save = Mock()
    monkeypatch.setattr(metadata_tasks, "_metadata_queue", work_queue)
    monkeypatch.setattr(metadata_tasks, "_queued_species", queued)
    monkeypatch.setattr(metadata_tasks, "get_species_info", Mock(return_value=None))
    monkeypatch.setattr(metadata_tasks, "get_common_name", Mock(return_value="American Robin"))
    monkeypatch.setattr(metadata_tasks, "save_species_info", save)
    monkeypatch.setattr(metadata_tasks, "log_system_event", Mock())
    with pytest.raises(StopIteration):
        metadata_tasks._metadata_worker()
    get.assert_called_once()
    assert save.call_args.kwargs["scientific_name"] == valid
    save.assert_called_once()
    assert work_queue.task_done.call_count == 2
    assert queued == set()

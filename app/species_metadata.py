import re
from unicodedata import normalize
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests


SUMMARY_PATH = "/api/rest_v1/page/summary/"
SUMMARY_BASE_URL = "https://en.wikipedia.org" + SUMMARY_PATH
MAX_SUMMARY_REDIRECTS = 5


def _summary_url(scientific_name):
    # Accept raw taxonomic names, not URLs or already encoded path segments.
    if not isinstance(scientific_name, str):
        raise ValueError("Invalid scientific name for Wikipedia metadata.")
    title = normalize("NFC", scientific_name)
    if (
        not re.fullmatch(r"[\w .,'‘’()\-×+&]+", title)
        or title != title.strip()
        or not any(char.isalpha() for char in title)
    ):
        raise ValueError("Invalid scientific name for Wikipedia metadata.")
    return SUMMARY_BASE_URL + quote(title.replace(" ", "_"), safe="")


def _redirect_summary_url(current_url, location):
    # Inspect before parsing: URL parsers can silently strip literal controls.
    if (
        not isinstance(location, str)
        or not location
        or "\\" in location
        or not location.isprintable()
    ):
        raise ValueError("Unsafe Wikipedia summary redirect.")
    target = urlsplit(urljoin(current_url, location))
    if (
        target.scheme != "https"
        or target.netloc != "en.wikipedia.org"
        or not target.path.startswith(SUMMARY_PATH)
        or target.query
        or target.fragment
    ):
        raise ValueError("Unsafe Wikipedia summary redirect.")
    # Decode once, validate, and rebuild under the constant origin/API path.
    # Remaining percent escapes (including nested separators) fail validation.
    return _summary_url(unquote(target.path[len(SUMMARY_PATH):], errors="strict"))


def fetch_species_metadata(scientific_name):
    summary_url = _summary_url(scientific_name)
    for redirects in range(MAX_SUMMARY_REDIRECTS + 1):
        summary_response = requests.get(
            summary_url,
            headers={"User-Agent": "WAMF/1.0 (Bird Wildlife Monitoring)"},
            timeout=10,
            allow_redirects=False,
        )
        try:
            # Wikipedia uses 301 for normalization and 302 for article aliases.
            # Follow only validated summary targets, with automatic redirects off.
            if summary_response.status_code in {301, 302, 303, 307, 308}:
                if redirects == MAX_SUMMARY_REDIRECTS:
                    raise requests.TooManyRedirects("Too many Wikipedia summary redirects.")
                summary_url = _redirect_summary_url(
                    summary_url, summary_response.headers.get("Location")
                )
                continue
            summary_response.raise_for_status()
            if summary_response.status_code != 200:
                raise requests.HTTPError("Wikipedia did not return a metadata summary.")
            summary_data = summary_response.json()
            break
        finally:
            summary_response.close()

    return {
        "scientific_name": scientific_name,
        "description": summary_data["extract"],
        "wikipedia_url": summary_data["content_urls"]["desktop"]["page"],
        "thumbnail_url": (
            summary_data["thumbnail"]["source"] if "thumbnail" in summary_data else None
        ),
    }

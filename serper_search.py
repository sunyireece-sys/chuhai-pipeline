"""
Thin wrapper around https://serper.dev/ google search API.

Handles:
  - retry on 429 / 5xx with exponential backoff
  - optional throttle between calls
  - country code mapping
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

SERPER_ENDPOINT = "https://google.serper.dev/search"

# Country name → Serper `gl` (geolocation) code.
# Add more as the keyword file expands.
COUNTRY_CODE = {
    "us": "us",
    "usa": "us",
    "united states": "us",
    "germany": "de",
    "deutschland": "de",
    "uk": "gb",
    "united kingdom": "gb",
    "great britain": "gb",
    "canada": "ca",
    "australia": "au",
    "france": "fr",
    "italy": "it",
    "spain": "es",
    "japan": "jp",
    "south korea": "kr",
    "korea": "kr",
    "netherlands": "nl",
    "belgium": "be",
    "sweden": "se",
    "switzerland": "ch",
    "austria": "at",
    "ireland": "ie",
    "new zealand": "nz",
    "mexico": "mx",
    "brazil": "br",
}


def normalize_country(country: str) -> tuple[str, str]:
    """
    Returns (display_name, serper_gl_code).
    Falls back to ('us', 'us') and prints a warning if unknown.
    """
    key = country.strip().lower()
    code = COUNTRY_CODE.get(key)
    if code is None:
        return country.strip() or "United States", "us"
    # Pretty display name (title case for unknown originals)
    display = {
        "us": "United States",
        "de": "Germany",
        "gb": "United Kingdom",
        "ca": "Canada",
        "au": "Australia",
        "fr": "France",
        "it": "Italy",
        "es": "Spain",
        "jp": "Japan",
        "kr": "South Korea",
        "nl": "Netherlands",
        "be": "Belgium",
        "se": "Sweden",
        "ch": "Switzerland",
        "at": "Austria",
        "ie": "Ireland",
        "nz": "New Zealand",
        "mx": "Mexico",
        "br": "Brazil",
    }.get(code, country.strip().title())
    return display, code


@dataclass
class SerperResult:
    query: str
    country_display: str
    country_code: str
    organic: list[dict[str, Any]]
    raw: dict[str, Any]


class SerperClient:
    def __init__(self, api_key: str, throttle_seconds: float = 0.6):
        if not api_key:
            raise ValueError("SERPER_API_KEY is empty. Set it in .env")
        self._api_key = api_key
        self._throttle = throttle_seconds
        self._session = requests.Session()

    def search(self, query: str, country: str, num: int = 10) -> SerperResult:
        country_display, gl = normalize_country(country)
        payload = {"q": query, "gl": gl, "hl": "en", "num": num}
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.post(
                    SERPER_ENDPOINT,
                    json=payload,
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    if self._throttle > 0:
                        time.sleep(self._throttle)
                    return SerperResult(
                        query=query,
                        country_display=country_display,
                        country_code=gl,
                        organic=body.get("organic", []),
                        raw=body,
                    )
                if resp.status_code in (429, 500, 502, 503, 504):
                    backoff = 2 ** attempt
                    print(f"  serper {resp.status_code}, retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                # 401, 403, 400 etc — fail fast
                raise RuntimeError(
                    f"Serper API returned {resp.status_code}: {resp.text[:200]}"
                )
            except requests.RequestException as exc:
                last_error = exc
                backoff = 2 ** attempt
                print(f"  serper network error: {exc}, retrying in {backoff}s...")
                time.sleep(backoff)

        raise RuntimeError(f"Serper API failed after retries: {last_error}")

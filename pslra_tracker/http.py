"""One polite HTTP session shared by every source."""

from __future__ import annotations

import os
import time

import requests

# GlobeNewswire stalls requests that send no Accept headers, so always send them.
USER_AGENT = os.environ.get("PSLRA_USER_AGENT", "pslra-tracker/0.2 (open-source securities case tracker)")
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
POLITE_DELAY = 0.6

_session = requests.Session()
_session.headers.update(HEADERS)


def get(url: str, *, timeout: int = 30, headers: dict | None = None, params: dict | None = None) -> requests.Response:
    r = _session.get(url, timeout=timeout, headers=headers, params=params)
    r.raise_for_status()
    return r


def pause(seconds: float = POLITE_DELAY) -> None:
    time.sleep(seconds)

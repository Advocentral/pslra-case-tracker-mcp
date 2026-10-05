"""Where cases come from.

Two kinds of source, all public, free and keyless:

  * the global newswires   PR Newswire, Business Wire, GlobeNewswire
  * the federal court record, through CourtListener (see `fetch_dockets`)

Individual law firm websites are deliberately not read: the wires already carry every firm's
announcements, and a firm's own site is not a public distribution channel.

A source's `fetch` lists what is currently published (cheap); `read` opens one item in detail
(one extra request). Both raise on failure: a source that cannot be reached is reported as an
error and retried next run, never recorded as "nothing new".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import quote_plus, urljoin

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from . import http
from .models import Item


class SourceError(Exception):
    """The source answered, but not with anything we can read."""


def _clean(s: str) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", s).strip()


def _iso(s: str) -> str:
    """Best-effort ISO timestamp from a wire's date string ('Oct 05, 2026, 03:34 ET')."""
    try:
        return dateparser.parse(re.sub(r"\b[A-Z]{2,4}$", "", s.strip()), fuzzy=True, ignoretz=True).isoformat(timespec="minutes")
    except (ValueError, OverflowError):
        return ""


def _within(iso: str, days: int) -> bool:
    if not iso:
        return True
    return iso[:10] >= (date.today() - timedelta(days=days)).isoformat()


def _read_html(selectors: list[str]) -> Callable[[Item], None]:
    def read(it: Item) -> None:
        soup = BeautifulSoup(http.get(it.link).text, "html.parser")
        node = next((n for n in (soup.select_one(s) for s in selectors) if n), None) or soup.body
        body = re.sub(r"\s+", " ", node.get_text(" ")).strip() if node else ""
        if len(body) <= 200:
            raise SourceError(f"no article body at {it.link}")
        it.text = it.title + ". " + body[:20000]
        it.body_fetched = True
    return read


# ---------------------------------------------------------------------------
# Global newswires
# ---------------------------------------------------------------------------

PRN_SEARCH = "https://www.prnewswire.com/search/news/"


def fetch_prnewswire(days: int) -> list[Item]:
    """PR Newswire's own search page, read for the phrase "securities class action"."""
    items: list[Item] = []
    for page in range(1, 6):
        r = http.get(PRN_SEARCH, params={"keyword": "securities class action", "page": page, "pagesize": 100}, timeout=45)
        cards = [c for c in BeautifulSoup(r.text, "html.parser").select("div.card") if c.select_one("a.news-release")]
        if not cards and page == 1:
            raise SourceError("PR Newswire search page returned no release cards (layout changed?)")
        fresh = 0
        for c in cards:
            a, small = c.select_one("a.news-release"), c.select_one("small")
            published = _iso(small.get_text(" ", strip=True)) if small else ""
            if not _within(published, days):
                continue
            fresh += 1
            title = _clean(a.get_text(" "))
            snippet = _clean(c.select_one("p").get_text(" ")) if c.select_one("p") else ""
            items.append(Item(title=title, link=urljoin(PRN_SEARCH, a.get("href", "")), source="prnewswire",
                              publisher="PR Newswire", published=published, text=f"{title}. {snippet}"))
        if fresh < len(cards) or len(cards) < 100:
            break
        http.pause()
    return items


GNW_TAG = "https://www.globenewswire.com/search/tag/class%20action"


def fetch_globenewswire(days: int) -> list[Item]:
    """GlobeNewswire's own "class action" tag page."""
    items: list[Item] = []
    for page in range(1, 5):
        r = http.get(GNW_TAG, params={"pageSize": 50, "page": page}, timeout=45)
        rows = [li for li in BeautifulSoup(r.text, "html.parser").select("li.row") if li.select_one(".mainLink a")]
        if not rows and page == 1:
            raise SourceError("GlobeNewswire tag page returned no releases (layout changed?)")
        fresh = 0
        for li in rows:
            a = li.select_one(".mainLink a")
            when = li.select_one(".date-source span")
            published = _iso(when.get_text(" ", strip=True)) if when else ""
            if not _within(published, days):
                continue
            fresh += 1
            title = _clean(a.get_text(" "))
            org = li.select_one("a.sourceLink")
            snippet = _clean(li.select_one(".newsTxt").get_text(" ")) if li.select_one(".newsTxt") else ""
            items.append(Item(title=title, link=urljoin(GNW_TAG, a.get("href", "")), source="globenewswire",
                              publisher="GlobeNewswire", published=published, text=f"{title}. {snippet}",
                              fields={"firm": _clean(org.get_text(" "))} if org else {}))
        if fresh < len(rows) or len(rows) < 50:
            break
        http.pause()
    return items


BW_QUERIES = [
    'site:businesswire.com "class action" "lead plaintiff"',
    'site:businesswire.com "securities" "class action" deadline',
]


def fetch_businesswire(days: int) -> list[Item]:
    """Business Wire. Its own index refuses automated readers (HTTP 403), so it is reached through
    the Google News index. Headlines only: the article pages refuse automated readers too."""
    items: list[Item] = []
    for q in BW_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(f"{q} when:{days}d")
               + "&hl=en-US&gl=US&ceid=US:en")
        feed = feedparser.parse(http.get(url).content)
        if feed.bozo and not feed.entries:
            raise SourceError(f"Google News index unreadable: {feed.bozo_exception}")
        for e in feed.entries:
            title = _clean(e.get("title", ""))
            publisher = (e.get("source") or {}).get("title", "")
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(" - " + publisher)]
            items.append(Item(title=title, link=e.get("link", ""), source="businesswire",
                              publisher="Business Wire", published=_iso(e.get("published", "")), text=title))
        http.pause()
    return items


@dataclass
class Source:
    name: str
    label: str
    kind: str                                   # wire
    default: bool
    fetch: Callable[[int], list[Item]]
    read: Callable[[Item], None] | None         # None = headline only
    note: str = ""


SOURCES: dict[str, Source] = {s.name: s for s in [
    Source("prnewswire", "PR Newswire", "wire", True, fetch_prnewswire,
           _read_html(["section.release-body", "article"]),
           'Search page, phrase "securities class action". Highest volume, noisiest.'),
    Source("businesswire", "Business Wire", "wire", True, fetch_businesswire, None,
           "Read through the Google News index; headlines only (site refuses automated readers)."),
    Source("globenewswire", "GlobeNewswire", "wire", True, fetch_globenewswire,
           _read_html(["#main-body-container", ".main-body-container", "article"]),
           '"Class action" tag page. Occasionally slow to respond.'),
]}


# ---------------------------------------------------------------------------
# The federal court record (CourtListener, Free Law Project)
# ---------------------------------------------------------------------------

CL_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
CL_ANON_PAUSE = 12.5   # anonymous reads are limited to about five requests a minute

RE_ENTITY = re.compile(
    r"\b(Inc|Corp|Corporation|Ltd|Limited|plc|Holdings?|Co|Company|LLC|L\.?P|N\.?V|S\.?A|AG|Group|Trust|Bank|"
    r"Technologies|Therapeutics|Pharmaceuticals|Biosciences|Energy|Partners|Capital|Systems|Networks|Labs?)\b\.?", re.I)


def classify_docket(case_name: str, cause: str = "") -> str:
    """A filing code is not a classification: code 850 carries class actions, derivative suits,
    regulatory enforcement and individual investor suits together. This reads the caption only,
    so anything it cannot rule out stays a *candidate* until the complaint is read."""
    n = case_name.lower()
    if re.search(r"securities (and|&) exchange commission|\bsec\b v\.|commodity futures|united states v\.", n):
        return "regulatory_enforcement"
    if "derivative" in n or "on behalf of" in n:
        return "derivative"
    if n.startswith("in re") and "securities litigation" in n:
        return "securities_class_action"
    return "securities_candidate"


def docket_defendant(case_name: str) -> str:
    """The company on the right of the 'v.', when it looks like a company rather than a person."""
    m = re.match(r"in re:?\s+(.+?)\s+(?:securities|stockholder|shareholder|derivative)", case_name, re.I)
    if m:
        return m.group(1).strip(" ,")
    right = re.split(r"\s+v\.?\s+", case_name, maxsplit=1)
    if len(right) == 2 and RE_ENTITY.search(right[1]):
        return re.sub(r"\s+et\s+al\.?$", "", right[1], flags=re.I).strip(" ,")
    return ""


def fetch_dockets(since: date, until: date | None = None, nature_of_suit: str = "850",
                  max_pages: int = 10) -> list[dict]:
    """Every docket with this nature-of-suit code filed on or after `since`, newest first."""
    token = os.environ.get("COURTLISTENER_TOKEN")
    headers = {"Authorization": f"Token {token}"} if token else {}
    params = {"type": "r", "nature_of_suit": nature_of_suit, "filed_after": since.isoformat(),
              "order_by": "dateFiled desc"}
    if until:
        params["filed_before"] = until.isoformat()
    url, out, seen = CL_SEARCH, [], set()
    for page in range(max_pages):
        d = http.get(url, params=params if page == 0 else None, headers=headers).json()
        if "results" not in d:
            raise SourceError("CourtListener search returned no 'results'")
        for r in d["results"]:
            if r["docket_id"] in seen:
                continue
            seen.add(r["docket_id"])
            name = r.get("caseName") or r.get("case_name_full") or ""
            out.append({
                "docket_id": r["docket_id"], "case_name": name, "docket_number": r.get("docketNumber") or "",
                "court": r.get("court") or "", "court_id": r.get("court_id") or "",
                "date_filed": r.get("dateFiled") or "", "judge": r.get("assignedTo") or "",
                "nature_of_suit": r.get("suitNature") or "", "cause": r.get("cause") or "",
                "url": urljoin("https://www.courtlistener.com", r.get("docket_absolute_url") or ""),
                "classification": classify_docket(name, r.get("cause") or ""),
                "defendant": docket_defendant(name),
            })
        url = d.get("next")
        if not url:
            break
        http.pause(http.POLITE_DELAY if token else CL_ANON_PAUSE)
    return out

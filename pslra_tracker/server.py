"""MCP server: lets Claude query and refresh the tracker.

Read tools answer from the local SQLite record; only `refresh_cases` touches the network.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .judge import get_judge
from .pipeline import run
from .sources import SOURCES
from .store import Store

DISCLAIMER = ("Deadlines are extracted by regular expression from law firm press releases and may be wrong. "
              "Verify against the published PSLRA notice or the court docket before relying on a date.")
INSTRUCTIONS = (
    "Tracks US securities class actions and their PSLRA lead plaintiff deadlines, built from free sources "
    "(PR Newswire, Business Wire, GlobeNewswire, and federal dockets via CourtListener). Many press releases describe "
    "one lawsuit; this server collapses them into one case each. Call refresh_cases to pull new announcements "
    "(takes a minute or two), then list_cases / get_case to read. " + DISCLAIMER)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build(db: Path | None = None, public_url: str = "") -> MCPServer:
    """`public_url` switches OAuth on: the address Claude will reach the server at."""
    store = Store(db)
    extra = {}
    provider = None
    if public_url:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        from .auth import SCOPE, Provider
        provider = Provider(store.db, public_url, os.environ["PSLRA_AUTH_PASSWORD"], os.environ.get("PSLRA_API_TOKEN", ""))
        extra = {"auth_server_provider": provider, "auth": AuthSettings(
            issuer_url=public_url, resource_server_url=public_url.rstrip("/") + "/mcp",
            validate_token_resource=False,   # this server issues its own tokens; none exist for another resource
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                  default_scopes=[SCOPE]))}
    mcp = MCPServer("pslra-tracker", instructions=INSTRUCTIONS, version=__version__, **extra)
    if provider:
        mcp.custom_route("/login", methods=["GET", "POST"])(provider.login)

    def _status(c, today: date) -> str:
        if not c.deadline:
            return "no_deadline_found"
        return "open" if c.days_left(today) >= 0 else "expired"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True, openWorldHint=True))
    async def refresh_cases(sources: list[str] | None = None, days: int = 14, max_fetch: int = 60,
                            include_courts: bool = True) -> dict:
        """Run one pass: read the newswires, set aside what is not a filed case,
        extract ticker / class period / deadline, and merge each announcement into its lawsuit.

        sources: any of prnewswire, businesswire, globenewswire (default: all three). days: look-back window. max_fetch: cap on article bodies read per source this run;
        anything over the cap is deferred to the next run, not dropped. include_courts: also sweep
        CourtListener for securities dockets (nature of suit 850) and link them to cases.
        A source that cannot be reached is reported under its name with an error and retried next time.
        """
        try:
            summary = await anyio.to_thread.run_sync(
                lambda: run(store, sources=sources, days=days, max_fetch=max_fetch, courts=include_courts))
        except (ValueError, RuntimeError) as e:   # unknown source name, or a refresh already running
            raise ToolError(str(e)) from e
        summary.pop("stats", None)
        if summary["deferred"]:
            summary["next_step"] = (f"{summary['deferred']} announcements were over this run's reading cap and are "
                                    "not yet recorded. Call refresh_cases again to continue.")
        return summary

    @mcp.tool(annotations=READ)
    def list_cases(status: Literal["open", "expired", "no_deadline_found", "all"] = "open",
                   due_within_days: int | None = None, ticker: str | None = None, query: str | None = None,
                   first_seen_since: str | None = None, limit: int = 50) -> dict:
        """List tracked securities class actions, soonest lead plaintiff deadline first.

        status: open (deadline today or later), expired, no_deadline_found, or all.
        due_within_days: only cases whose deadline falls within this many days.
        ticker: exact stock ticker. query: substring of the company name.
        first_seen_since: ISO date; only cases first seen on or after it (what is new).
        """
        today = date.today()
        out = []
        for c in store.cases():
            st = _status(c, today)
            if status != "all" and st != status:
                continue
            if due_within_days is not None and not (c.deadline and 0 <= c.days_left(today) <= due_within_days):
                continue
            if ticker and c.ticker != ticker.upper().strip():
                continue
            if query and query.lower() not in c.company.lower():
                continue
            if first_seen_since and c.first_seen[:10] < first_seen_since:
                continue
            out.append({**c.to_dict(today), "status": st})
        out.sort(key=lambda c: (c["lead_plaintiff_deadline"] is None, c["lead_plaintiff_deadline"] or ""))
        return {"as_of": today.isoformat(), "total": len(out), "cases": out[:limit], "note": DISCLAIMER}

    @mcp.tool(annotations=READ)
    def get_case(case_id: int | None = None, ticker: str | None = None) -> dict:
        """Full detail for one case by id, or every case for a ticker: the deadline and how many
        announcements voted for each date, the class period, every firm that announced it, the linked
        court docket when one was found, and every source announcement with how it was matched."""
        today = date.today()
        if case_id is not None:
            found = [c for c in [store.get_case(case_id)] if c]
        elif ticker:
            found = [c for c in store.cases() if c.ticker == ticker.upper().strip()]
        else:
            raise ToolError("give case_id or ticker")
        out = []
        for c in found:
            d = {**c.to_dict(today), "status": _status(c, today)}
            d["source_announcements"] = [
                {"title": a["title"], "url": a["url"], "source": a["source"], "published": a["published"],
                 "kind": a["kind"], "matched_by": a["reason"], "match_confidence": a["match_confidence"]}
                for a in store.announcements_for(c.id)]
            if not c.deadline and c.docket.get("date_filed"):
                est = date.fromisoformat(c.docket["date_filed"]) + timedelta(days=60)
                d["estimated_deadline"] = {
                    "date": est.isoformat(), "estimated": True,
                    "why": "60 days from the docket filing date. The statutory window runs from publication "
                           "of the notice, not from filing, so the true deadline is usually later."}
            out.append(d)
        return {"as_of": today.isoformat(), "cases": out, "note": DISCLAIMER}

    @mcp.tool(annotations=READ)
    def search_announcements(query: str = "", kind: Literal["", "filing", "reminder", "investigation",
                                                             "settlement", "other"] = "",
                             source: str = "", limit: int = 30) -> dict:
        """Search every announcement the tracker has read, including those set aside, with the decision
        made about each (why it was set aside, or which case it joined and by which rule).
        kind: filing, reminder, investigation, settlement or other. source: e.g. prnewswire."""
        rows = store.search_announcements(query, kind, source, min(limit, 200))
        return {"total": len(rows), "announcements": rows}

    @mcp.tool(annotations=READ)
    def list_court_dockets(filed_within_days: int = 30, unmatched_only: bool = False,
                           classification: Literal["", "securities_candidate", "securities_class_action",
                                                   "derivative", "regulatory_enforcement", "not_class_action"] = "",
                           limit: int = 50) -> dict:
        """Federal dockets filed under nature-of-suit code 850 (Securities/Commodities), from CourtListener.
        These appear the day a case is filed, typically weeks before any press release.

        A filing code is not a classification: code 850 also carries derivative suits, SEC enforcement
        and individual investor suits. `classification` is read from the caption and cover sheet only
        (`classified_by` says whether a keyword rule or the Jev model decided), so `securities_candidate`
        means "not ruled out", and even `securities_class_action` is unconfirmed until the complaint is read.
        unmatched_only: dockets not yet linked to a tracked case (possible cases no firm has announced)."""
        since = (date.today() - timedelta(days=filed_within_days)).isoformat()
        rows = store.dockets(since, unmatched_only, classification, min(limit, 500))
        return {"total": len(rows), "dockets": rows,
                "note": "Caption-only classification; read the complaint before treating a candidate as a class action."}

    @mcp.tool(annotations=READ)
    def source_status() -> dict:
        """Whether the Jev classifier is active, and every source with whether it is switched on, when it last succeeded, its last error, and the
        newest announcement reached, plus record counts. A source whose newest announcement has not
        moved for days while others are current has quietly stalled."""
        marks = store.watermarks()
        srcs = [{"name": s.name, "label": s.label, "kind": s.kind, "on_by_default": s.default, "note": s.note,
                 **{k: marks.get(s.name, {}).get(k) for k in
                    ("last_ok", "last_error", "newest_published", "last_listed", "last_new")}}
                for s in SOURCES.values()]
        cl = marks.get("courtlistener", {})
        srcs.append({"name": "courtlistener", "label": "CourtListener (federal dockets)", "kind": "court",
                     "on_by_default": True, "note": "Nature of suit 850, searched by filing date.",
                     **{k: cl.get(k) for k in ("last_ok", "last_error", "newest_published", "last_listed", "last_new")}})
        return {"database": str(store.path), "classifier": "jev" if get_judge() else "regex only",
                "counts": store.counts(), "sources": srcs}

    return mcp


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(db: Path | None = None, http: bool = False, host: str = "127.0.0.1", port: int = 8765,
          public_url: str = "") -> None:
    if not http:
        build(db).run(transport="stdio")
        return
    public_url = public_url or os.environ.get("PSLRA_PUBLIC_URL", "")
    if public_url and not os.environ.get("PSLRA_AUTH_PASSWORD"):
        raise SystemExit("--public-url turns sign-in on, which needs a password: set PSLRA_AUTH_PASSWORD.")
    if not public_url and not _is_loopback(host):
        raise SystemExit(f"Refusing to serve on {host} without sign-in. Pass --public-url (and set "
                         "PSLRA_AUTH_PASSWORD), or bind to 127.0.0.1.")
    kwargs = {}
    if public_url:
        from urllib.parse import urlsplit

        from mcp.server.transport_security import TransportSecuritySettings
        u = urlsplit(public_url)
        kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[u.netloc, f"127.0.0.1:{port}", f"localhost:{port}"],
            allowed_origins=[f"{u.scheme}://{u.netloc}", "https://claude.ai", "https://claude.com"])
    build(db, public_url).run(transport="streamable-http", host=host, port=port, **kwargs)

"""The journey of one announcement.

  1. Collect            read the newswires
  2. Skip seen          an address already recorded costs nothing further
  3. Keep real cases    investigations, settlements and unrelated news are set aside (and recorded)
  4. Read the detail    company, ticker, class period, deadline
  5. Match or create    two firms announcing one lawsuit become one case, not two

Reminders are kept: a deadline reminder describes a filed case and carries its deadline, so it is
merged into that case rather than thrown away.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import http
from .extract import RELEVANT, Extracted, extract
from .judge import Judge, get_judge
from .models import Item
from .sources import SOURCES, fetch_dockets
from .store import Store

JEV_TRIAGE = 0.80    # confidence needed to set an announcement aside on its headline alone
JEV_CLASSIFY = 0.60  # confidence needed to take Jev's reading of a release over the regex rule
JEV_DOCKET = 0.70    # confidence needed to label a docket from its caption
JEV_WORKERS = 16     # concurrent Jev requests; far below the service limit, and the wires are never hit in parallel
HELD = "held: headline only, no deadline; waiting for its case"
THRESHOLD = 0.85   # nothing below this is merged; in doubt a separate case is opened

RE_INVESTIGATION = re.compile(r"investigat", re.I)
RE_SETTLEMENT = re.compile(r"\bsettle(?:ment|s|d)?\b", re.I)
RE_CASEWORDS = re.compile(r"class\s+action|lead[\s\-]+plaintiff|deadline|lawsuit|securities\s+fraud|\bsued?\b|\bfile[sd]\b", re.I)
RE_TOPIC = re.compile(r"class\s+action|lead[\s\-]+plaintiff|deadline|lawsuit|securities|investor|shareholder|stockholder|fraud|PSLRA", re.I)
RE_CASE_TITLE = re.compile(r"class\s+action|securities\s+(?:fraud\s+)?(?:lawsuit|litigation)|lead[\s\-]+plaintiff", re.I)
RE_INVESTOR = re.compile(r"securit|investor|shareholder|stockholder", re.I)
RE_FILING_TITLE = re.compile(r"\bfile[sd]\b|class\s+action\s+(?:lawsuit\s+)?(?:has\s+been\s+)?filed|announces\s+(?:a\s+)?(?:securities\s+)?class\s+action", re.I)
COMPANY_NOISE = re.compile(
    r"\b(the|inc|incorporated|corp|corporation|ltd|limited|plc|holdings?|co|company|llc|lp|nv|sa|ag|group|"
    r"class\s+[a-c]|common\s+stock|ordinary\s+shares|adrs?|american\s+depositary\s+shares)\b", re.I)


def normalise_company(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and "))
    return re.sub(r"\s+", " ", COMPANY_NOISE.sub(" ", n)).strip()


def triage_title(it: Item, wire: bool) -> tuple[str, str] | None:
    """Step 3, first half: what can be set aside from the headline alone, before paying to read it."""
    if it.kind == "investigation":
        return "investigation", "source states no lawsuit has been filed"
    if it.kind or not wire:
        return None
    if RE_SETTLEMENT.search(it.title):
        return "settlement", "headline announces a settlement"
    if RE_INVESTIGATION.search(it.title) and not RE_CASEWORDS.search(it.title):
        return "investigation", "headline announces an investigation, not a filed case"
    if not RE_TOPIC.search(it.title):
        return "other", "headline is not about a securities case"
    return None


def prejudge(judge: Judge | None, items: list[Item]) -> None:
    """Ask Jev about many announcements at once and keep each answer on its item. The calls are
    independent, so they run concurrently; a failed call leaves `judged` empty and the regex rule decides."""
    todo = [it for it in items if not it.kind]
    if not judge or not todo:
        return
    with ThreadPoolExecutor(JEV_WORKERS) as pool:
        for it, got in zip(todo, pool.map(lambda it: judge.announcement(it.title, it.text), todo)):
            it.judged = got or ()


def triage(it: Item, wire: bool, judged: bool) -> tuple[str, str] | None:
    """Headline triage. With Jev, the model's reading of the headline (see `prejudge`) replaces the keyword
    rules whenever it is confident; otherwise the keyword rules decide."""
    by_rule = triage_title(it, wire)
    if not (judged and wire) or it.kind or not it.judged:
        return by_rule
    kind, conf = it.judged
    if kind in ("filing", "reminder"):
        return None                       # worth reading, even where a keyword rule would have dropped it
    if conf >= JEV_TRIAGE:
        return kind, f"jev: {kind} ({conf:.2f}) from the headline"
    return by_rule


def judged_classify(it: Item, ex: dict, judged: bool) -> tuple[str, str]:
    """Step 3, second half, with Jev when available (answers already gathered by `prejudge`).
    The regex verdict is kept alongside when they differ."""
    rule_kind, rule_reason = classify(it, ex)
    got = it.judged
    if not judged or it.kind or not got or got[1] < JEV_CLASSIFY:
        return rule_kind, rule_reason
    kind, conf = got
    note = f"jev: {kind} ({conf:.2f})"
    if kind != rule_kind:
        note += f"; regex said {rule_kind}"
    return kind, note


def facts(it: Item) -> dict:
    """Step 4: regex extraction, with any field a structured source already separated taking precedence."""
    ex: Extracted = extract(it)
    d = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(ex).items()}
    f = it.fields
    for k in ("ticker", "exchange", "deadline", "class_start", "class_end", "date_filed"):
        if f.get(k):
            d[k] = f[k]
    for k in ("company", "firm"):
        if f.get(k) and (not d.get(k) or it.kind):
            d[k] = f[k]
    if not normalise_company(d.get("company", "")):
        d["company"] = ""          # 'Company")', 'Holdings, Inc.': not a name
    floor = d.get("class_end") or ""
    try:   # a release cannot announce a deadline that passed weeks before it was published
        floor = max(floor, (date.fromisoformat(it.published[:10]) - timedelta(days=14)).isoformat())
    except ValueError:
        pass
    if d.get("deadline") and d["deadline"] <= floor:
        d["deadline"] = ""         # a "deadline" inside the class period, or long before the release, is a misread
    return {k: v for k, v in d.items() if v}


def classify(it: Item, ex: dict) -> tuple[str, str]:
    """Step 3, second half: is this announcement about a filed securities class action?"""
    if it.kind == "filing":
        return "filing", "source lists it as a filed case with a lead plaintiff deadline"
    if RE_INVESTIGATION.search(it.title) and not ex.get("deadline"):
        return "investigation", "investigation with no lead plaintiff deadline"
    if it.body_fetched:
        if not RELEVANT.search(it.text):
            return "other", "no lead plaintiff / PSLRA language in the release"
    elif not (RE_CASE_TITLE.search(it.title) or (RE_INVESTOR.search(it.title) and (ex.get("ticker") or ex.get("company")))):
        return "other", "headline does not describe a securities class action"
    return ("filing", "") if RE_FILING_TITLE.search(it.title) else ("reminder", "")


def fingerprint(ex: dict) -> str:
    who = ex.get("ticker") or normalise_company(ex.get("company", ""))
    raw = "|".join([who, ex.get("class_start", ""), ex.get("class_end", ""), ex.get("deadline", "")])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _score(ex: dict, row, today: date) -> tuple[float, str]:
    dl, cdl = ex.get("deadline"), row["deadline"]
    gap = abs((date.fromisoformat(dl) - date.fromisoformat(cdl)).days) if dl and cdl else None
    if ex.get("ticker") and ex["ticker"] == row["ticker"]:
        if gap == 0:
            return 0.98, "ticker + deadline"
        if ex.get("class_start") and ex.get("class_end") and (ex["class_start"], ex["class_end"]) == (row["class_start"], row["class_end"]):
            return 0.96, "ticker + class period"
        if gap is not None and gap <= 10:
            return 0.92, "ticker + deadline within 10 days"
        if ex.get("class_end") and ex["class_end"] == row["class_end"]:
            return 0.90, "ticker + class period end"
        if gap is None:
            stale = cdl and date.fromisoformat(cdl) < today - timedelta(days=30)
            differs = ex.get("class_end") and row["class_end"] and ex["class_end"] != row["class_end"]
            if not stale and not differs:
                return 0.86, "ticker, one side has no deadline yet"
        return 0.60, "same ticker, different deadline"
    if gap == 0:
        return 0.88, "company name + deadline"
    return 0.75, "company name alone"


def match(store: Store, ex: dict, today: date) -> tuple[int | None, float, str]:
    """Step 5. Six comparisons, strongest first. Returns (case id or None, confidence, rule)."""
    scored = sorted(((*_score(ex, r, today), r["id"]) for r in
                     store.candidates(ex.get("ticker", ""), normalise_company(ex.get("company", "")))), reverse=True)
    if not scored or scored[0][0] < THRESHOLD:
        return None, scored[0][0] if scored else 0.0, scored[0][1] if scored else "no existing case"
    best = scored[0]
    if best[0] == 0.86 and len([s for s in scored if s[0] == 0.86]) > 1:
        return None, -1.0, "ambiguous: several open cases for this ticker and no deadline to tell them apart"
    return best[2], best[0], best[1]


def place(store: Store, it: Item, ex: dict, kind: str, today: date, note: str = "") -> str:
    """Attach an announcement to its case, creating the case when it is new. Returns the outcome."""
    if not ex.get("ticker") and not normalise_company(ex.get("company", "")):
        store.add_announcement(it, kind, "refused: names no company and no ticker, so it could never be matched", ex)
        return "refused"
    case_id, conf, rule = match(store, ex, today)
    if case_id is None and conf == -1.0:
        store.add_announcement(it, kind, rule, ex)
        return "refused"
    outcome = "merged"
    if case_id is None and not (ex.get("deadline") or ex.get("class_end") or it.body_fetched):
        # a bare headline may join a case but never open one; it is retried every run until its case exists
        store.add_announcement(it, kind, HELD, ex)
        return "held"
    if case_id is None:
        case_id, outcome = store.create_case(fingerprint(ex), ex.get("ticker", "")), "created"
        rule = f"new case (best existing match: {rule}, {conf:.2f})" if conf else "new case"
        conf = 1.0
    if note:
        rule = f"{rule} | {note}"
    store.add_announcement(it, kind, rule, ex, case_id, conf)
    store.recompute_case(case_id, normalise_company)
    return outcome


@contextmanager
def run_lock(store: Store):
    """Only one pass works at a time, so two runs cannot each open a case for the same lawsuit."""
    if str(store.path) == ":memory:":
        yield
        return
    try:
        import fcntl
    except ImportError:  # Windows: no advisory lock available
        yield
        return
    with open(Path(str(store.path) + ".lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another refresh is already running") from None
        yield


def link_dockets(store: Store) -> int:
    """Attach court dockets to cases: same company, and filed within the window a deadline implies.

    The lead plaintiff deadline falls 60 days after the published notice, which itself must follow
    the first complaint within 20 days, so the first docket sits at most ~100 days before it.
    """
    linked = 0
    floor = (date.today() - timedelta(days=200)).isoformat()
    short = lambda n: re.sub(r"^\d:", "", (n or "").lower())  # noqa: E731  "2:26-cv-09999" == "26-cv-09999"
    quoted = {short(json.loads(r["extracted"]).get("docket_number")): r["case_id"] for r in store.db.execute(
        "SELECT case_id, extracted FROM announcements WHERE case_id IS NOT NULL AND extracted LIKE '%docket_number%'")}
    cases = store.db.execute("SELECT id, company_norm, deadline, docket_id FROM cases WHERE company_norm<>''").fetchall()
    # oldest first: the first-filed complaint is the one whose notice set the deadline
    for d in sorted(store.dockets(since=floor, unmatched_only=True, limit=5000), key=lambda d: d["date_filed"]):
        name = normalise_company(d["defendant"])
        if short(d["docket_number"]) in quoted and (not name or name == store.db.execute(
                "SELECT company_norm FROM cases WHERE id=?", (quoted[short(d["docket_number"])],)).fetchone()[0]):
            store.link_docket(d["docket_id"], quoted[short(d["docket_number"])])   # docket number: 0.99
            linked += 1
            continue
        if not name or d["classification"] in ("regulatory_enforcement", "derivative", "not_class_action"):
            continue
        for c in cases:
            if c["company_norm"] != name or not c["deadline"]:
                continue
            lead = (date.fromisoformat(c["deadline"]) - date.fromisoformat(d["date_filed"])).days
            if 0 <= lead <= 100:   # company name + date window: 0.90, above the merge threshold
                store.link_docket(d["docket_id"], c["id"])
                linked += 1
                break
    return linked


def sweep_courts(store: Store, days: int, log: list[str], judge: Judge | None = None) -> dict:
    since = date.today() - timedelta(days=days)
    try:
        dockets = fetch_dockets(since)
    except Exception as e:  # noqa: BLE001
        store.mark("courtlistener", ok=False, error=f"{type(e).__name__}: {e}")
        log.append(f"courtlistener: ERROR {e} (nothing recorded; retried next run)")
        return {"error": str(e)}
    fresh = [d for d in dockets if not store.has_docket(d["docket_id"])]
    if judge and fresh:
        with ThreadPoolExecutor(JEV_WORKERS) as pool:
            for d, got in zip(fresh, pool.map(judge.docket, fresh)):
                if got and got[1] >= JEV_DOCKET:
                    d["classification"], d["classified_by"] = got[0], f"jev ({got[1]:.2f})"
    new = sum(store.add_docket(d) for d in dockets)
    store.mark("courtlistener", ok=True, listed=len(dockets), new=new,
               newest=max((d["date_filed"] for d in dockets), default=""))
    log.append(f"courtlistener: {len(dockets)} dockets filed since {since}, {new} new")
    return {"listed": len(dockets), "new": new}


def retry_held(store: Store, today: date) -> int:
    """Headlines that arrived before their case did: try each again now that more cases exist."""
    joined = 0
    for r in store.db.execute("SELECT url, extracted FROM announcements WHERE case_id IS NULL AND reason=?", (HELD,)).fetchall():
        case_id, conf, rule = match(store, json.loads(r["extracted"]), today)
        if case_id is not None:
            store.db.execute("UPDATE announcements SET case_id=?, match_confidence=?, reason=? WHERE url=?",
                             (case_id, conf, rule, r["url"]))
            store.recompute_case(case_id, normalise_company)
            joined += 1
    return joined


def run(store: Store, sources: list[str] | None = None, days: int = 14, max_fetch: int = 60,
        courts: bool = True, items: list[Item] | None = None, judge: Judge | None | bool = True) -> dict:
    """One pass. `items` replaces the live sources (offline test mode). `judge=True` uses Jev when it is
    configured; `False`/`None` forces regex only."""
    if judge is True:
        judge = get_judge() if items is None else None
    judge = judge or None
    today = date.today()
    log: list[str] = [f"Run date {today}, window {days} days"]
    names = sources or [n for n, s in SOURCES.items() if s.default]
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown}; choose from {sorted(SOURCES)}")
    summary = {"sources": {}, "listed": 0, "already_seen": 0, "new": 0, "set_aside": {}, "deferred": 0,
               "cases_created": 0, "merged_into_existing": 0, "held": 0, "refused": 0}
    pending: list[Item] = []
    read_total = 0

    with run_lock(store):
        batches = [("offline", items)] if items is not None else [(n, None) for n in names]
        for name, given in batches:
            src = SOURCES.get(name)
            try:
                listed = given if given is not None else src.fetch(days)
            except Exception as e:  # noqa: BLE001
                store.mark(name, ok=False, error=f"{type(e).__name__}: {e}")
                summary["sources"][name] = {"error": f"{type(e).__name__}: {e}"}
                log.append(f"{name}: ERROR {e} (nothing recorded; retried next run)")
                continue
            uniq = list({(it.link or it.title): it for it in listed}.values())
            new = [it for it in uniq if not store.seen(it.link or it.title)]
            stat = {"listed": len(listed), "already_seen": len(uniq) - len(new), "new": len(new), "deferred": 0}
            fetched = 0   # the cap is per source, so one busy wire cannot starve the others
            wire = bool(src and src.kind == "wire")
            if wire:
                prejudge(judge, new)          # headlines, all at once
            for it in new:
                it.link = it.link or it.title
                aside = triage(it, wire, bool(judge))
                if aside:
                    store.add_announcement(it, *aside)
                    summary["set_aside"][aside[0]] = summary["set_aside"].get(aside[0], 0) + 1
                    continue
                if src and src.read and not it.body_fetched:
                    if fetched >= max_fetch:
                        stat["deferred"] += 1   # not recorded, so the next run picks it up
                        continue
                    try:
                        src.read(it)
                        fetched += 1
                        read_total += 1
                        http.pause()
                    except Exception as e:  # noqa: BLE001
                        stat["deferred"] += 1
                        log.append(f"{name}: could not read {it.link[:90]}: {e}")
                        continue
                pending.append(it)
            store.mark(name, ok=True, listed=len(listed), new=len(new),
                       newest=max((it.published for it in listed if it.published), default=""))
            summary["sources"][name] = stat
            for k in ("listed", "already_seen", "new", "deferred"):
                summary[k] += stat[k]
            log.append(f"{name}: {stat}")

        prejudge(judge, [it for it in pending if it.body_fetched])   # full releases, all at once
        pairs = [(it, facts(it)) for it in pending]
        relevant = []
        # announcements that state a deadline go first, so headline-only ones find a case to join
        for it, ex in sorted(pairs, key=lambda p: (not p[1].get("deadline"), not p[0].body_fetched)):
            kind, reason = judged_classify(it, ex, bool(judge))
            if kind not in ("filing", "reminder"):
                store.add_announcement(it, kind, reason, ex)
                summary["set_aside"][kind] = summary["set_aside"].get(kind, 0) + 1
                continue
            relevant.append((it, ex))
            outcome = place(store, it, ex, kind, today, reason)
            key = {"created": "cases_created", "merged": "merged_into_existing"}.get(outcome, outcome)
            summary[key] += 1
        summary["held_joined_a_case"] = retry_held(store, today)

        if courts and items is None:
            summary["courtlistener"] = sweep_courts(store, days, log, judge)
        summary["dockets_linked"] = link_dockets(store)
        store.commit()

    body = [ex for it, ex in relevant if it.body_fetched]
    head = [ex for it, ex in relevant if not it.body_fetched]
    n_cases = store.counts()["cases"]
    summary["classifier"] = {"engine": "jev", **judge.usage()} if judge else {"engine": "regex"}
    summary["article_bodies_read"] = read_total
    summary["total_cases"] = n_cases
    summary["log"] = log
    summary["stats"] = {   # the viability figures the HTML report shows
        "days": days, "raw": summary["listed"], "unique": summary["new"], "relevant": len(relevant),
        "with_ticker": sum(1 for _, x in relevant if x.get("ticker")),
        "with_deadline": sum(1 for _, x in relevant if x.get("deadline")),
        "with_period": sum(1 for _, x in relevant if x.get("class_start")),
        "body": len(body), "body_deadline": sum(1 for x in body if x.get("deadline")),
        "title_only": len(head), "title_deadline": sum(1 for x in head if x.get("deadline")),
        "dup_ratio": f"{(summary['cases_created'] + summary['merged_into_existing']) / summary['cases_created']:.1f}"
                     if summary["cases_created"] else "–",
    }
    return summary

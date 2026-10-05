"""The record kept behind the tracker: every announcement ever read and every decision made about it.

SQLite, standard library only. Nothing is ever deleted.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Case

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    url TEXT PRIMARY KEY,          -- the unique key "skip what we have already read" checks
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    publisher TEXT,
    published TEXT,
    text TEXT,
    body_fetched INTEGER DEFAULT 0,
    kind TEXT NOT NULL,            -- filing | reminder | investigation | settlement | other
    reason TEXT,                   -- why it was set aside, or how it was matched
    extracted TEXT,                -- JSON of the extracted fields
    case_id INTEGER REFERENCES cases(id),
    match_confidence REAL,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS announcements_case ON announcements(case_id);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    case_key TEXT UNIQUE NOT NULL, -- fingerprint: ticker + class period + deadline
    ticker TEXT, exchange TEXT, company TEXT, company_norm TEXT,
    deadline TEXT, deadline_votes TEXT DEFAULT '{}',
    class_start TEXT, class_end TEXT, period_conflict INTEGER DEFAULT 0,
    firms TEXT DEFAULT '[]', releases INTEGER DEFAULT 0,
    docket_id INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_ticker ON cases(ticker);
CREATE TABLE IF NOT EXISTS dockets (
    docket_id INTEGER PRIMARY KEY,
    case_name TEXT, docket_number TEXT, court TEXT, court_id TEXT, date_filed TEXT, judge TEXT,
    nature_of_suit TEXT, cause TEXT, url TEXT, classification TEXT, defendant TEXT,
    case_id INTEGER REFERENCES cases(id), first_seen TEXT NOT NULL, classified_by TEXT
);
CREATE TABLE IF NOT EXISTS watermarks (
    source TEXT PRIMARY KEY,
    newest_published TEXT, last_run TEXT, last_ok TEXT, last_error TEXT,
    last_listed INTEGER DEFAULT 0, last_new INTEGER DEFAULT 0
);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("PSLRA_DB", Path.home() / ".pslra-tracker" / "tracker.db"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: Path | str | None = None):
        path = default_db_path() if path is None else path
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "classified_by" not in [r[1] for r in self.db.execute("PRAGMA table_info(dockets)")]:
            self.db.execute("ALTER TABLE dockets ADD COLUMN classified_by TEXT")   # databases from 0.2.0

    # -- announcements ------------------------------------------------------

    def seen(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM announcements WHERE url=?", (url,)).fetchone() is not None

    def add_announcement(self, it, kind: str, reason: str, extracted: dict | None = None,
                         case_id: int | None = None, confidence: float | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO announcements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it.link, it.title, it.source, it.publisher, it.published, it.text, int(it.body_fetched), kind,
             reason, json.dumps(extracted or {}), case_id, confidence, now()))

    def announcements_for(self, case_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM announcements WHERE case_id=? ORDER BY COALESCE(NULLIF(published,''), first_seen)",
            (case_id,)).fetchall()

    def search_announcements(self, query: str = "", kind: str = "", source: str = "", limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM announcements WHERE 1=1", []
        if query:
            sql += " AND (title LIKE ? OR text LIKE ?)"
            args += [f"%{query}%"] * 2
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY COALESCE(NULLIF(published,''), first_seen) DESC LIMIT ?"
        return [{"title": r["title"], "url": r["url"], "source": r["source"], "published": r["published"],
                 "kind": r["kind"], "decision": r["reason"], "case_id": r["case_id"],
                 "match_confidence": r["match_confidence"], "extracted": json.loads(r["extracted"] or "{}")}
                for r in self.db.execute(sql, args + [limit])]

    # -- cases --------------------------------------------------------------

    def candidates(self, ticker: str, company_norm: str) -> list[sqlite3.Row]:
        if ticker:
            return self.db.execute("SELECT * FROM cases WHERE ticker=?", (ticker,)).fetchall()
        return self.db.execute("SELECT * FROM cases WHERE company_norm=? AND company_norm<>''", (company_norm,)).fetchall()

    def create_case(self, case_key: str, ticker: str) -> int:
        row = self.db.execute("SELECT id FROM cases WHERE case_key=?", (case_key,)).fetchone()
        if row:  # the fingerprint is checked again here, independently of the matcher
            return row["id"]
        t = now()
        return self.db.execute("INSERT INTO cases (case_key, ticker, first_seen, last_seen) VALUES (?,?,?,?)",
                               (case_key, ticker, t, t)).lastrowid

    def recompute_case(self, case_id: int, normalise) -> None:
        """Rebuild a case's facts from every announcement attached to it: majority vote per field."""
        votes, names, starts, ends, exch, firms = Counter(), Counter(), Counter(), Counter(), Counter(), set()
        rows = self.announcements_for(case_id)
        for r in rows:
            ex = json.loads(r["extracted"] or "{}")
            for counter, k in ((votes, "deadline"), (names, "company"), (starts, "class_start"),
                               (ends, "class_end"), (exch, "exchange")):
                if ex.get(k):
                    counter[ex[k]] += 1
            if ex.get("firm"):
                firms.add(ex["firm"])
        top = lambda c: c.most_common(1)[0][0] if c else None  # noqa: E731
        company = top(names) or ""
        self.db.execute(
            "UPDATE cases SET exchange=?, company=?, company_norm=?, deadline=?, deadline_votes=?, class_start=?,"
            " class_end=?, period_conflict=?, firms=?, releases=?, last_seen=? WHERE id=?",
            (top(exch) or "", company, normalise(company), top(votes), json.dumps(dict(votes)), top(starts),
             top(ends), int(len(starts) > 1), json.dumps(sorted(firms)), len(rows), now(), case_id))

    def _case(self, r: sqlite3.Row, with_sources: bool = True) -> Case:
        c = Case(key=r["case_key"], ticker=r["ticker"] or "", id=r["id"], exchange=r["exchange"] or "",
                 company=r["company"] or "", deadline=_d(r["deadline"]),
                 deadline_votes=json.loads(r["deadline_votes"] or "{}"), class_start=_d(r["class_start"]),
                 class_end=_d(r["class_end"]), firms=set(json.loads(r["firms"] or "[]")),
                 releases=r["releases"], first_seen=r["first_seen"], last_seen=r["last_seen"],
                 period_conflict=bool(r["period_conflict"]))
        if r["docket_id"]:
            d = self.db.execute("SELECT * FROM dockets WHERE docket_id=?", (r["docket_id"],)).fetchone()
            if d:
                c.docket = {k: d[k] for k in ("docket_number", "court", "judge", "date_filed", "nature_of_suit",
                                              "case_name", "url", "classification")}
        if with_sources:
            c.sources = [{"title": a["title"], "link": a["url"], "publisher": a["publisher"]}
                         for a in self.announcements_for(c.id)]
        return c

    def cases(self) -> list[Case]:
        return [self._case(r) for r in self.db.execute("SELECT * FROM cases WHERE releases>0")]

    def get_case(self, case_id: int) -> Case | None:
        r = self.db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return self._case(r) if r else None

    # -- dockets ------------------------------------------------------------

    def has_docket(self, docket_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM dockets WHERE docket_id=?", (docket_id,)).fetchone() is not None

    def add_docket(self, d: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO dockets VALUES (:docket_id,:case_name,:docket_number,:court,:court_id,:date_filed,"
            ":judge,:nature_of_suit,:cause,:url,:classification,:defendant,NULL,:first_seen,:classified_by)",
            {"classified_by": "caption rule", **d, "first_seen": now()})
        return cur.rowcount == 1

    def link_docket(self, docket_id: int, case_id: int) -> None:
        self.db.execute("UPDATE dockets SET case_id=? WHERE docket_id=?", (case_id, docket_id))
        # a merge may only add: the case keeps a docket it already has
        self.db.execute("UPDATE cases SET docket_id=? WHERE id=? AND docket_id IS NULL", (docket_id, case_id))

    def dockets(self, since: str = "", unmatched_only: bool = False, classification: str = "",
                limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM dockets WHERE date_filed>=?", [since]
        if unmatched_only:
            sql += " AND case_id IS NULL"
        if classification:
            sql += " AND classification=?"
            args.append(classification)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY date_filed DESC LIMIT ?", args + [limit])]

    # -- watermarks ---------------------------------------------------------

    def mark(self, source: str, *, ok: bool, error: str = "", newest: str = "", listed: int = 0, new: int = 0) -> None:
        t = now()
        self.db.execute("INSERT OR IGNORE INTO watermarks (source) VALUES (?)", (source,))
        if ok:
            self.db.execute(
                "UPDATE watermarks SET last_run=?, last_ok=?, last_error=NULL, last_listed=?, last_new=?,"
                " newest_published=MAX(COALESCE(newest_published,''), ?) WHERE source=?",
                (t, t, listed, new, newest, source))
        else:  # the watermark does not move: "we could not tell" is not "there was nothing"
            self.db.execute("UPDATE watermarks SET last_run=?, last_error=? WHERE source=?", (t, error, source))

    def watermarks(self) -> dict[str, dict]:
        return {r["source"]: dict(r) for r in self.db.execute("SELECT * FROM watermarks")}

    def counts(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "announcements": one("SELECT COUNT(*) FROM announcements"),
            "by_kind": dict(self.db.execute("SELECT kind, COUNT(*) FROM announcements GROUP BY kind").fetchall()),
            "cases": one("SELECT COUNT(*) FROM cases WHERE releases>0"),
            "dockets": one("SELECT COUNT(*) FROM dockets"),
            "dockets_matched": one("SELECT COUNT(*) FROM dockets WHERE case_id IS NOT NULL"),
        }

    def commit(self) -> None:
        self.db.commit()

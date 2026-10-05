"""Command line.

  pslra-tracker run                       # one pass over the live sources, then write the report
  pslra-tracker run --days 30 --sources prnewswire globenewswire
  pslra-tracker run --from-json tests/data/real_items.json --benchmark tests/data/bench.txt
  pslra-tracker cases --due-within 14     # print open deadlines
  pslra-tracker sources                   # source catalogue and watermarks
  pslra-tracker serve                     # MCP server over stdio (for Claude Desktop / Claude Code)
  pslra-tracker serve --http --port 8765  # MCP server over streamable HTTP (for a remote connector)
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from .models import Item
from .pipeline import run
from .report import write_csv, write_html
from .sources import SOURCES
from .store import Store


def cmd_run(args) -> None:
    offline = args.from_json is not None
    store = Store(":memory:" if offline and not args.db else args.db)
    items = None
    if offline:
        items = [Item(**{k: v for k, v in d.items() if k in Item.__dataclass_fields__})
                 for d in json.loads(args.from_json.read_text())]
    summary = run(store, sources=args.sources, days=args.days, max_fetch=args.max_fetch,
                  courts=not args.no_courts, items=items, judge=not args.no_jev)
    today = date.today()
    cases = sorted(store.cases(), key=lambda c: (c.deadline is None, c.deadline or date.max))

    benchmark = None
    if args.benchmark:
        benchmark = {ln.strip().upper() for ln in args.benchmark.read_text().splitlines() if ln.strip()}
    args.out.mkdir(parents=True, exist_ok=True)
    write_csv(cases, today, args.out / "cases.csv")
    (args.out / "cases.json").write_text(json.dumps([c.to_dict(today) for c in cases], indent=2))
    write_html(cases, summary["stats"], summary["log"], benchmark, today, args.out / "report.html")

    open_n = sum(1 for c in cases if c.deadline and c.days_left(today) >= 0)
    print(f"Listed {summary['listed']} | new {summary['new']} | set aside {summary['set_aside']} | "
          f"deferred {summary['deferred']}")
    print(f"Cases created {summary['cases_created']} | merged {summary["merged_into_existing"]} | held {summary["held"]} | "
          f"total cases {len(cases)} | open deadlines {open_n}")
    print(f"Classifier: {summary['classifier']}")
    for name, s in summary["sources"].items():
        if "error" in s:
            print(f"  ! {name}: {s['error']}")
    if benchmark is not None:
        found = benchmark & {c.ticker for c in cases}
        print(f"Benchmark recall: {len(found)}/{len(benchmark)}; missing: {sorted(benchmark - found) or 'none'}")
    print(f"Report: {args.out / 'report.html'}")


def cmd_cases(args) -> None:
    today = date.today()
    cases = [c for c in Store(args.db).cases() if c.deadline and 0 <= c.days_left(today) <= args.due_within]
    for c in sorted(cases, key=lambda c: c.deadline):
        flag = " (sources disagree)" if c.conflict else ""
        print(f"{c.deadline}  {c.days_left(today):>3}d  {c.ticker:<6} {c.company}{flag}")
    print(f"{len(cases)} open deadline(s) within {args.due_within} days")


def cmd_sources(args) -> None:
    marks = Store(args.db).watermarks()
    for s in SOURCES.values():
        m = marks.get(s.name, {})
        state = "on " if s.default else "off"
        print(f"[{state}] {s.name:<15} {s.kind:<5} last ok: {m.get('last_ok') or 'never':<26} "
              f"newest: {m.get('newest_published') or '-'}")
        if m.get("last_error"):
            print(f"      last error: {m['last_error']}")
    m = marks.get("courtlistener", {})
    print(f"[on ] {'courtlistener':<15} court last ok: {m.get('last_ok') or 'never':<26} newest: {m.get('newest_published') or '-'}")


def cmd_serve(args) -> None:
    from .server import serve
    serve(db=args.db, http=args.http, host=args.host, port=args.port)


def main() -> None:
    ap = argparse.ArgumentParser(prog="pslra-tracker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="SQLite file (default $PSLRA_DB or ~/.pslra-tracker/tracker.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="one pass over the sources")
    r.add_argument("--days", type=int, default=14, help="look-back window (default 14)")
    r.add_argument("--max-fetch", type=int, default=60, help="max article bodies to read per source per run (default 60)")
    r.add_argument("--sources", nargs="+", choices=sorted(SOURCES), help="default: every source switched on")
    r.add_argument("--no-jev", action="store_true", help="regex only, even when TYPESAFE_API_KEY is set")
    r.add_argument("--no-courts", action="store_true", help="skip the CourtListener docket sweep")
    r.add_argument("--benchmark", type=Path, help="text file of tickers (one per line) to measure recall")
    r.add_argument("--from-json", type=Path, help="offline mode: JSON list of items {title, text, link, source}")
    r.add_argument("--out", type=Path, default=Path("output"), help="report folder (default ./output)")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("cases", help="print open lead plaintiff deadlines")
    c.add_argument("--due-within", type=int, default=60)
    c.set_defaults(fn=cmd_cases)

    sub.add_parser("sources", help="source catalogue and watermarks").set_defaults(fn=cmd_sources)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

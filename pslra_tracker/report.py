"""HTML and CSV reports."""

from __future__ import annotations

import csv
import html
import json
from datetime import date, datetime
from pathlib import Path

from .models import Case


def write_csv(cases: list[Case], today: date, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "exchange", "company", "deadline", "days_left", "class_start", "class_end",
                    "releases", "firms", "deadline_conflict", "first_seen", "top_source"])
        for c in cases:
            w.writerow([c.ticker, c.exchange, c.company, c.deadline or "", c.days_left(today) or "",
                        c.class_start or "", c.class_end or "", c.releases, "; ".join(sorted(c.firms)),
                        "yes" if c.conflict else "", c.first_seen,
                        c.sources[0]["link"] if c.sources else ""])


def pct(a: int, b: int) -> str:
    return f"{(100 * a / b):.0f}%" if b else "–"


def write_html(cases, stats, log, benchmark, today, path: Path) -> None:
    e = html.escape
    open_cases = sorted([c for c in cases if c.deadline and c.days_left(today) >= 0],
                        key=lambda c: c.deadline)
    no_deadline = [c for c in cases if not c.deadline]
    expired = [c for c in cases if c.deadline and c.days_left(today) < 0]

    def row(c: Case) -> str:
        dl = c.days_left(today)
        badge = ("urgent" if dl is not None and dl <= 7 else "")
        conflict = (f'<span class="warn" title="{e(json.dumps(c.deadline_votes))}">conflict</span>'
                    if c.conflict else "")
        period = (f"{c.class_start:%b %d, %Y} – {c.class_end:%b %d, %Y}"
                  if c.class_start and c.class_end else '<span class="muted">not found</span>')
        if c.period_conflict:
            period += '<br><span class="warn">periods differ across releases</span>'
        links = " ".join(f'<a href="{e(s["link"])}" target="_blank" rel="noopener">{i + 1}</a>'
                         for i, s in enumerate(c.sources[:5]))
        return (f"<tr><td><b>{e(c.ticker)}</b><br><span class='muted'>{e(c.exchange)}</span></td>"
                f"<td>{e(c.company) or '<span class=muted>—</span>'}</td>"
                f"<td>{c.deadline:%b %d, %Y}<br>{conflict}</td>" if c.deadline else
                f"<tr><td><b>{e(c.ticker)}</b></td><td>{e(c.company)}</td><td>—</td>") + (
                f"<td class='{badge}'>{'' if dl is None else dl}</td><td>{period}</td>"
                f"<td>{c.releases}</td><td>{e(', '.join(sorted(c.firms))) or '—'}</td><td>{links}</td></tr>")

    head = ("<tr><th>Ticker</th><th>Company</th><th>Deadline</th><th>Days left</th>"
            "<th>Class period</th><th>Releases</th><th>Firms seen</th><th>Sources</th></tr>")

    bench_html = ""
    if benchmark is not None:
        found = sorted(benchmark & {c.ticker for c in cases})
        missing = sorted(benchmark - {c.ticker for c in cases})
        bench_html = (f"<div class='card'><h2>Benchmark recall</h2><p class='big'>{pct(len(found), len(benchmark))}</p>"
                      f"<p>Caught {len(found)} of {len(benchmark)} benchmark tickers.</p>"
                      f"<p class='muted'>Missing: {e(', '.join(missing)) or 'none'}</p></div>")

    s = stats
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PSLRA Tracker – Viability Report {today}</title>
<style>
:root{{--bg:#fafaf8;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e1dc;--card:#fff;--accent:#1f4e79;--warn:#b54708;--urgent:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#ecebe6;--muted:#9a9993;--line:#2e2d2a;--card:#1e1e1c;--accent:#7fb2e5;--warn:#f79009;--urgent:#f97066}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px;max-width:1200px;margin:auto}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 8px}}
.muted{{color:var(--muted)}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:20px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.big{{font-size:28px;font-weight:700;margin:0;color:var(--accent)}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card);margin-bottom:24px}}
table{{border-collapse:collapse;width:100%;min-width:900px}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
.warn{{color:var(--warn);font-size:12px;font-weight:600}} .urgent{{color:var(--urgent);font-weight:700}}
a{{color:var(--accent)}} details{{margin:16px 0}} pre{{white-space:pre-wrap;font-size:12px}}
</style></head><body>
<h1>PSLRA Lead Plaintiff Deadline Tracker — viability report</h1>
<p class="muted">Generated {datetime.now():%Y-%m-%d %H:%M} · window: last {s['days']} days · free sources only · regex extraction (no paid AI)</p>
<div class="grid">
<div class="card"><h2>Relevant releases</h2><p class="big">{s['relevant']}</p><p class="muted">of {s['raw']} fetched items ({s['unique']} unique links)</p></div>
<div class="card"><h2>Distinct cases</h2><p class="big">{len(cases)}</p><p class="muted">{s['dup_ratio']} releases per case on average</p></div>
<div class="card"><h2>Open deadlines</h2><p class="big">{len(open_cases)}</p><p class="muted">{len(expired)} expired · {len(no_deadline)} with no deadline found</p></div>
<div class="card"><h2>Ticker extracted</h2><p class="big">{pct(s['with_ticker'], s['relevant'])}</p><p class="muted">of relevant releases</p></div>
<div class="card"><h2>Deadline extracted</h2><p class="big">{pct(s['with_deadline'], s['relevant'])}</p><p class="muted">title-only items: {pct(s['title_deadline'], s['title_only'])}, full body: {pct(s['body_deadline'], s['body'])}</p></div>
<div class="card"><h2>Class period extracted</h2><p class="big">{pct(s['with_period'], s['relevant'])}</p><p class="muted">mostly needs the full body</p></div>
<div class="card"><h2>Deadline conflicts</h2><p class="big">{sum(c.conflict for c in cases)}</p><p class="muted">cases where sources disagree</p></div>
{bench_html}
</div>
<h2>Open cases ({len(open_cases)})</h2>
<div class="wrap"><table>{head}{''.join(row(c) for c in open_cases)}</table></div>
<details><summary>Cases with no deadline found ({len(no_deadline)}) — often "investigation" releases</summary>
<div class="wrap"><table>{head}{''.join(row(c) for c in no_deadline)}</table></div></details>
<details><summary>Expired ({len(expired)})</summary>
<div class="wrap"><table>{head}{''.join(row(c) for c in expired)}</table></div></details>
<details><summary>Run log</summary><pre>{e(chr(10).join(log))}</pre></details>
</body></html>"""
    path.write_text(doc, encoding="utf-8")

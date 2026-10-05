# PSLRA Case Tracker

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![MCP](https://img.shields.io/badge/MCP-server-purple.svg)

An open-source tracker for newly filed US securities class actions and their PSLRA lead plaintiff deadlines, with an **MCP server** so Claude can query it.

One securities class action is announced separately by eight to fifteen plaintiffs' firms. This tool reads those announcements from free public sources, recognises when many of them describe one lawsuit, and keeps **one case per lawsuit** with its ticker, class period, deadline, every firm that announced it, and the court docket when one can be found.

Runs with no API keys: extraction and classification are plain regular expressions. Optionally, set a TypeSafe key and the classification decisions are made by the Jev model instead (see [Optional: Jev](#optional-jev-for-classification)).

> **Not legal advice.** Deadlines are read by regex from law firm press releases and can be wrong. Verify against the published notice or the court docket before relying on a date.

## Quick start

```bash
uv sync                      # or: pip install -e .
uv run pslra-tracker run     # one pass over the live sources (first run takes a few minutes)
uv run pslra-tracker cases --due-within 14
```

`run` also writes `output/report.html`, `cases.csv` and `cases.json`. The record itself lives in SQLite at `~/.pslra-tracker/tracker.db` (override with `--db` or `PSLRA_DB`).

## Connect it to Claude

**Claude Code** (this repo ships a `.mcp.json`, or add it anywhere):

```bash
claude mcp add pslra-tracker -- uv run --directory /path/to/pslra-tracker pslra-tracker serve
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "pslra-tracker": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pslra-tracker", "pslra-tracker", "serve"]
    }
  }
}
```

**Remote connector (claude.ai and Claude Desktop "custom connector")**: see [Remote connector with sign-in](#remote-connector-with-sign-in).

Then ask things like *"refresh the tracker, then which lead plaintiff deadlines fall in the next two weeks?"* or *"which securities dockets were filed this week that no firm has announced yet?"*

### Tools

| Tool | What it does |
|---|---|
| `refresh_cases` | One pass over the sources. The only tool that touches the network. |
| `list_cases` | Cases by status, deadline window, ticker, company, or first-seen date. |
| `get_case` | One case in full: deadline votes, class period, firms, docket, every source announcement and how it was matched. |
| `search_announcements` | Every announcement ever read, including those set aside, with the decision made about each. |
| `list_court_dockets` | Federal securities dockets from CourtListener, matched or not yet matched to a case. |
| `source_status` | Each source's last success, last error and newest item reached, plus record counts. |

## Remote connector with sign-in

Custom connectors are reached from Anthropic's servers, not from your computer, so the tracker needs a public **https** address and a login.

```bash
export PSLRA_AUTH_PASSWORD='a long passphrase'
pslra-tracker serve --http --port 8765 --public-url https://tracker.example.com
```

`--public-url` is the address Claude will use. It switches on OAuth 2.1: Claude registers itself, opens a sign-in page in your browser, and you approve by typing the password. In Claude, add a custom connector with the URL `https://tracker.example.com/mcp`.

- Put TLS in front (a reverse proxy, or a tunnel such as `cloudflared tunnel --url http://127.0.0.1:8765` for testing; pass the tunnel's https address as `--public-url`).
- Access tokens last an hour and refresh automatically for 30 days. Only token hashes are stored, in the tracker's SQLite file.
- `PSLRA_API_TOKEN` optionally sets a static bearer token for scripts (`Authorization: Bearer ...`).
- Without `--public-url` the server only binds to localhost and has no login. It refuses to bind to any other address without sign-in.
- One password, one operator. There are no per-user accounts; anyone with the password can read and refresh the tracker.

## Optional: Jev for classification

Regex is good at copying a date or a ticker out of a release. It is poor at deciding what a release *is*. With a [TypeSafe](https://typesafe.ai) key, two judgments go to Jev, a small model that returns a typed answer with a confidence rather than text:

```bash
uv sync --extra jev          # or: pip install -e ".[jev]"
export TYPESAFE_API_KEY=...
```

| Judgment | Without a key | With Jev |
|---|---|---|
| Filing, reminder, investigation, settlement or noise? | Keyword rules on the headline and body | One Choice per announcement; taken over the keyword rule at confidence ≥ 0.60 (≥ 0.80 to discard on the headline alone) |
| What kind of case is this docket? | Caption keywords: SEC and derivative ruled out | One Choice on caption and cause of action; taken at ≥ 0.70, so short-swing 16(b) suits and the like are also ruled out |

Jev never extracts values. Dates, tickers and class periods still come from regex. Below the confidence floor, or on any API error, the keyword rule decides, so behaviour degrades rather than breaks. When Jev and the rule disagree, both verdicts are stored (`search_announcements` shows `jev: investigation (0.95); regex said reminder`).

Cost at the time of writing is $0.042 per million input tokens; a first run over a week of wires is a few cents. `--no-jev` or `PSLRA_JEV=0` forces regex only.

## Sources

All checked live on 5 October 2026.

| Source | Kind | Default | How it is read |
|---|---|---|---|
| PR Newswire | wire | on | Its own search page, phrase "securities class action". Highest volume, noisiest. |
| Business Wire | wire | on | Its site refuses automated readers (HTTP 403), so it is reached through the Google News index. **Headlines only.** |
| GlobeNewswire | wire | on | Its "class action" tag page. |
| CourtListener | court | on | Free Law Project's archive of federal dockets, searched by filing date for nature-of-suit code 850. Works without a key; anonymous access is rate-limited, so the sweep pauses between pages. A free `COURTLISTENER_TOKEN` removes the pause. |

Only public distribution channels are read: the newswires every firm publishes through, and the court record. Individual law firm websites are deliberately left out.

Pick sources per run with `--sources prnewswire globenewswire`, skip the docket sweep with `--no-courts`.

## How it works

1. **Collect.** Read each source's current listing.
2. **Skip what is already read.** A web address already in the record costs nothing further.
3. **Keep real cases.** Investigations, settlements and unrelated news are set aside, from the headline alone where possible. They are still recorded, with the reason.
4. **Read the detail.** Ticker, exchange, company, deadline, class period, firm, and the docket number when the release quotes one.
5. **Match or create.** Compare against existing cases, strongest rule first. Nothing below 0.85 is merged.

| Rule | Confidence |
|---|---|
| Ticker + same deadline | 0.98 |
| Ticker + same class period | 0.96 |
| Ticker + deadline within 10 days (recorded as a conflict) | 0.92 |
| Ticker + same class period end | 0.90 |
| Company name + same deadline | 0.88 |
| Ticker, where one side has no deadline yet | 0.86 |
| Company name alone | 0.75, never merged |

A case's facts are a majority vote over its announcements, so one firm's typo does not move the deadline, and disagreement is reported (`deadline_conflict`, `deadline_votes`, `class_period_conflict`).

### Safeguards

- **Silence is not an answer.** A source that cannot be reached is recorded as an error and retried next run. Its watermark does not move.
- **Nothing is lost.** Announcements over a run's reading cap (`--max-fetch`, per source) are deferred, not dropped. Bare headlines with no deadline never open a case; they are held and retried each run until their case exists.
- **In doubt, do not merge.** A weak resemblance opens a separate case. An announcement naming no company and no ticker is refused.
- **Impossible dates are dropped.** A "deadline" inside the class period, or weeks before the release, is a misread.
- **One run at a time.** A file lock stops two passes colliding.
- **Every decision is recorded.** `search_announcements` shows what happened to each announcement and why.

### Court dockets

A lawsuit appears on the docket the day it is filed, typically weeks before any press release. Two caveats are built in:

- **A filing code is not a classification.** Code 850 also carries derivative suits, SEC enforcement and individual investor suits. Dockets are labelled from the caption and cover sheet only: `regulatory_enforcement`, `derivative` and (with Jev) `not_class_action` are ruled out, and everything else stays a `securities_candidate`, meaning *not ruled out*, not *confirmed*. `classified_by` records whether a keyword rule or Jev decided. Nothing reads the complaint.
- **A docket does not state the deadline.** The 60-day window runs from publication of the notice, not from filing. Where `get_case` offers a date computed from a docket it is marked `estimated`.

Dockets are linked to a case by the docket number a release quotes, or by company name plus a filing date within 100 days before the deadline. Unlinked candidates stay visible through `list_court_dockets`.

## Configuration

| Variable | Purpose |
|---|---|
| `PSLRA_DB` | SQLite file. Default `~/.pslra-tracker/tracker.db`. |
| `PSLRA_USER_AGENT` | How the tracker identifies itself to sources. |
| `COURTLISTENER_TOKEN` | Optional. Lifts CourtListener's anonymous rate limit. |
| `TYPESAFE_API_KEY` | Optional. Turns on Jev classification. |
| `PSLRA_AUTH_PASSWORD` | Required with `--public-url`. The sign-in password for the remote connector. |
| `PSLRA_PUBLIC_URL` | Same as `--public-url`. |
| `PSLRA_API_TOKEN` | Optional static bearer token for scripts, HTTP mode only. |
| `PSLRA_JEV=0` | Keeps the key but forces regex only. |

## Project layout

```
pslra_tracker/
  sources.py    newswire readers and the CourtListener docket search
  extract.py    regex extraction: ticker, company, deadline, class period, firm, docket number
  judge.py      optional Jev judgments, with regex fallback
  pipeline.py   collect, skip seen, set aside, extract, match or create
  store.py      SQLite record of announcements, cases, dockets and watermarks
  server.py     MCP server
  auth.py       OAuth sign-in for the HTTP transport
  cli.py        command line
  report.py     HTML and CSV report
tests/          unit tests and 19 real releases used as a benchmark
```

## Tests

```bash
uv run --extra dev pytest
uv run pslra-tracker run --from-json tests/data/real_items.json --benchmark tests/data/bench.txt
```

The suite runs offline with no keys (Jev is replaced by a stub). `tests/data/real_items.json` holds 19 real releases from September 2026; `bench.txt` lists the 16 true tickers. Current result: 16/16 cases, no false cases, the Pentair class-period discrepancy flagged.

## Known limits

- Extraction is regex with or without Jev. Company names are heuristic and sometimes truncated.
- Business Wire items are headlines only, so they rarely carry a class period.
- Press releases trail the filing; the docket sweep narrows that gap but its candidates need a human or a complaint-reading step before they count as cases.
- Securities cases only. Other practice areas would need their own nature-of-suit codes and classifiers.
- Sources change their pages. A layout change surfaces as a source error in `source_status`, not as an empty result.
- Be polite: the tracker identifies itself (`PSLRA_USER_AGENT`) and pauses between requests. Keep it that way.

## Licence

MIT

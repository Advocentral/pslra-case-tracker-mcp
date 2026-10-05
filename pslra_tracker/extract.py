"""Regex extraction of ticker, company, deadline, class period and firm from a press release.

No paid AI: every value is pulled out with plain regular expressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from dateutil import parser as dateparser

from .models import Item

# ---------------------------------------------------------------------------
# Extraction (regex only, no paid AI)
# ---------------------------------------------------------------------------

MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
         r"Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sept?\.?|Oct\.?|Nov\.?|Dec\.?)")
DATE = rf"{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
WEEKDAY = r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"

RE_TICKER = re.compile(
    r"\(\s*(NASDAQ(?:GS|GM|CM)?|NYSE(?:\s+American|\s+Arca|\s+MKT)?|AMEX|OTC(?:QX|QB|MKTS)?|Cboe|TSX|NASD)"
    r"\s*[:\-]\s*([A-Z][A-Z0-9.\-]{0,6})\b", re.I)
RE_DEADLINE = [
    # "CLASS PERIOD: July 30, 2025 to February 24, 2026 DEADLINE: November 3, 2026" (label is upper case)
    re.compile(rf"\bDEADLINE:\s*{WEEKDAY}({DATE})"),
    # "have until tomorrow, Monday, October 5, 2026 to seek appointment as lead plaintiff"
    re.compile(rf"\b(?:have|has)\s+until\s+(?:\w+,\s+)?{WEEKDAY}({DATE})", re.I),
    # "move the Court no later than November 2, 2026" — used by almost every firm
    re.compile(rf"(?:move|petition|apply|file)[^.]{{0,40}}?no\s+later\s+than\s+{WEEKDAY}({DATE})", re.I),
    # "the important November 2, 2026 lead plaintiff deadline"
    re.compile(rf"({DATE}),?\s+(?:lead[\s\-]+plaintiff\s+)?deadline(?!\s*:)", re.I),
    # "Lead Plaintiff Deadline: Oct. 5, 2026" / "Lead Plaintiff Deadline is October 20, 2026"
    re.compile(rf"lead[\s\-]+plaintiff\s+deadline(?:\s*(?:is|of|:|–|—|-|set\s+for|on))*\s*{WEEKDAY}({DATE})", re.I),
    # "last day to move for lead plaintiff is October 2" / "applications must be submitted by October 5"
    re.compile(rf"lead[\s\-]+plaintiff[^.]{{0,120}}?(?:no\s+later\s+than|on\s+or\s+before|before|by|is|deadline(?:\s+is|\s+of)?)"
               rf"\s*[:\-]?\s*{WEEKDAY}({DATE})", re.I),
    re.compile(rf"(?:no\s+later\s+than|on\s+or\s+before)\s+{WEEKDAY}({DATE})", re.I),
]
RE_CLASS_PERIOD = [
    re.compile(rf"(?:between|from)\s+({DATE})\s*,?\s*(?:and|through|to|–|—|-)\s*({DATE})", re.I),
    re.compile(rf"class\s+period[^.]{{0,40}}?({DATE})\s*(?:and|through|to|–|—|-)\s*({DATE})", re.I),
]
RE_DOCKET = re.compile(r"\bNo\.\s*((?:\d:)?\d{2}-cv-\d{3,6})", re.I)
TICKER_STOP = {"LEAD", "LOST", "MONEY", "ALL", "AND", "THE", "LLP", "PC", "INC", "LLC", "NYSE", "USA", "US", "CEO", "IPO", "SEC", "FINAL", "ROSEN", "PSLRA"}
RE_TICKER_TITLE = [
    RE_TICKER,                                                       # (NASDAQ: ABC)
    re.compile(r"\(\s*\$?([A-Z]{1,5})\s*\)"),                          # (ABC)
    re.compile(r"^\s*([A-Z]{1,5})\s+(?:FINAL\s+|IMPORTANT\s+)?(?:DEADLINE|INVESTOR|INVESTORS|ALERT|NOTICE|REMINDER|LAWSUIT|"
               r"SHAREHOLDER|SHAREHOLDERS|STOCKHOLDERS|CLASS\s+ACTION|Shareholder|Stockholders|Investor|Investors)\b"),  # ABC DEADLINE: / ABC Shareholder Alert
    re.compile(r"(?:\b(?:Urges|Reminds|Encourages|Notifies|Informs)|[–—\-])\s+([A-Z]{2,5})\s+(?:Stockholders|Shareholders|Investors)\b"),  # Urges ABC Stockholders / – ABC Investors
    re.compile(r"[-–—]\s*([A-Z]{1,5})\s*$"),                            # ... – ABC
]
RE_COMPANY_TITLE = re.compile(
    r"(?:Encourages|Urges|Reminds|Against|Notifies|in|of)\s+(?:Shareholders\s+of\s+|Investors\s+in\s+)?(?:the\s+)?"
    r"([A-Z][\w.&,'’\- ]{1,70}?(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|plc|Limited|Holdings|Co\.|Company|N\.V\.|S\.A\.|AG|LLC|Group)"
    r"(?:,?\s+(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|plc|Limited|Company|Co\.))?)"
    r"(?=[\s,(]|$)")
RE_COMPANY_LEAD = re.compile(
    r"\b(?:Against|Investing\s+in|Invested\s+in|Investors\s+(?:in|of)|Shareholders\s+of|Stockholders\s+of|Behalf\s+of|"
    r"Encourages|Urges|Reminds|Notifies|Informs|Into|Lead)\s+(?:the\s+)?")
RELEVANT = re.compile(r"lead[\s\-]+plaintiff|PSLRA|private\s+securities\s+litigation\s+reform", re.I)

KNOWN_FIRMS = [
    "Rosen", "Pomerantz", "Levi & Korsinsky", "Robbins Geller", "Glancy Prongay", "Bragar Eagel",
    "Faruqi", "Kessler Topaz", "Bernstein Liebhard", "Bronstein, Gewirtz", "Gainey McKenna",
    "Kirby McInerney", "Schall", "Portnoy", "Howard G. Smith", "Frank R. Cruz", "Holzer",
    "Johnson Fistel", "Hagens Berman", "Bleichmar Fonti", "Block & Leviton", "Kahn Swick",
    "Wolf Haldenstein", "Labaton", "Scott+Scott", "Berger Montague", "DJS Law", "Gross Law",
    "Pawar", "Brodsky & Smith", "ClaimsFiler", "Bottini", "Lieff Cabraser", "Saxena White",
    "Grabar", "Thornton", "Edelson", "Halper Sadeh", "Robbins LLP", "Bernstein Litowitz",
    "Kaplan Fox", "SueWallSt", "SBS Law", "Lynch Carpenter", "Abraham, Fruchter",
]
FIRM_CANON = {f.lower(): f for f in KNOWN_FIRMS}
RE_FIRM = re.compile("|".join(re.escape(f) for f in KNOWN_FIRMS), re.I)

NAME_STOP = {
    "encourages", "reminds", "announces", "announcement", "investors", "investor", "shareholders",
    "shareholder", "stockholders", "alert", "deadline", "lawsuit", "against", "files", "filed", "of",
    "in", "with", "losses", "loss", "class", "action", "securities", "fraud", "notifies", "urges",
    "informs", "investigation", "investigates", "on", "behalf", "purchasers", "lead", "plaintiff",
    "reminder", "important", "notice", "law", "firm", "llp", "p.c.", "announced", "who", "lost",
    "to", "for", "and", "the", "by", "sues", "sued", "litigation", "attention",
}


def parse_date(s: str) -> date | None:
    try:
        return dateparser.parse(s.replace(".", "").replace("Sept ", "Sep "), fuzzy=True).date()
    except (ValueError, OverflowError):
        return None


def extract_company(text: str, start: int) -> str:
    before = re.sub(r"(?:\s*\([^()]*\))+\s*$", "", text[max(0, start - 160):start])
    tokens = before.split()
    name: list[str] = []
    for tok in reversed(tokens):
        bare = tok.strip("“”\"'(),:;").lower()
        if not bare:
            continue
        if bare in NAME_STOP or not (tok[0].isupper() or tok[0].isdigit() or tok in {"&", "de", "of"}):
            break
        name.insert(0, tok)
        if len(name) >= 7:
            break
    return " ".join(name).strip(" ,;:–-“”\"'")


@dataclass
class Extracted:
    ticker: str = ""
    exchange: str = ""
    company: str = ""
    deadline: date | None = None
    class_start: date | None = None
    class_end: date | None = None
    firm: str = ""
    docket_number: str = ""


def extract(it: Item) -> Extracted:
    t = it.text
    ex = Extracted()
    # Ticker: title first (body pages often list other cases in "Recent News" sidebars).
    weak_title = False
    for i, rx in enumerate(RE_TICKER_TITLE):
        mt = rx.search(it.title)
        if mt:
            tk = mt.group(2 if i == 0 else 1).upper().rstrip(".-")
            if tk not in TICKER_STOP:
                ex.ticker = tk
                weak_title = i > 0
                if i == 0:
                    ex.exchange = mt.group(1).upper()
                break
    # Body: take exchange (and ticker if the title had none) from the first match only.
    m = RE_TICKER.search(t)
    listed = {x.group(2).upper().rstrip(".-") for x in RE_TICKER.finditer(t)}
    # "AEVEX INVESTOR ALERT ... AEVEX Corp. (NYSE: AVEX)": the headline word is the company name, not the
    # ticker. Only then does the body win; otherwise the title does, because bodies list other cases too.
    if (weak_title and listed and ex.ticker not in listed and re.search(
            rf"\b{re.escape(ex.ticker)}[\s,]+(?:Corp|Inc|Ltd|plc|Limited|Holdings|Group|Company|Co)\b", it.title, re.I)):
        ex.ticker = ""
    if m:
        body_tk = m.group(2).upper().rstrip(".-")
        if not ex.ticker:
            ex.ticker, ex.exchange = body_tk, m.group(1).upper()
        elif body_tk == ex.ticker and not ex.exchange:
            ex.exchange = m.group(1).upper()
    mc = RE_COMPANY_TITLE.search(it.title)
    if mc:
        # "Frank R. Cruz Files Securities Fraud Lawsuit Against Alphabet Inc." -> "Alphabet Inc."
        ex.company = RE_COMPANY_LEAD.split(mc.group(1))[-1].strip(" ,")
    elif m and m.group(2).upper().rstrip(".-") == ex.ticker:
        ex.company = extract_company(t, m.start())
    for rx in RE_DEADLINE:
        dm = rx.search(t)
        if dm:
            ex.deadline = parse_date(dm.group(1))
            if ex.deadline:
                break
    for rx in RE_CLASS_PERIOD:
        cm = rx.search(t)
        if cm:
            a, b = parse_date(cm.group(1)), parse_date(cm.group(2))
            if a and b and a < b:
                ex.class_start, ex.class_end = a, b
                break
    dk = RE_DOCKET.search(t)
    if dk:
        ex.docket_number = dk.group(1).lower()
    fm = RE_FIRM.search(it.title) or RE_FIRM.search(t[:600])
    if fm:
        ex.firm = FIRM_CANON.get(fm.group(0).lower(), fm.group(0))
    return ex

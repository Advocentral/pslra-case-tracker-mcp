from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Item:
    title: str
    link: str
    source: str            # which source found it
    publisher: str = ""
    published: str = ""    # ISO date/time when the source states one
    text: str = ""         # body text when fetched, else title + snippet
    body_fetched: bool = False
    kind: str = ""         # set by structured sources that state filed / investigation outright
    fields: dict = field(default_factory=dict)  # values a structured source already separated
    judged: tuple = ()     # (kind, confidence) from Jev on the headline, reused for headline-only items


@dataclass
class Case:
    key: str
    ticker: str
    id: int = 0
    exchange: str = ""
    company: str = ""
    deadline: date | None = None
    deadline_votes: dict = field(default_factory=dict)
    class_start: date | None = None
    class_end: date | None = None
    firms: set = field(default_factory=set)
    sources: list = field(default_factory=list)
    releases: int = 0
    first_seen: str = ""
    last_seen: str = ""
    period_conflict: bool = False
    docket: dict = field(default_factory=dict)

    @property
    def conflict(self) -> bool:
        return len(self.deadline_votes) > 1

    def days_left(self, today: date) -> int | None:
        return (self.deadline - today).days if self.deadline else None

    def to_dict(self, today: date) -> dict:
        return {
            "id": self.id, "case_key": self.key, "ticker": self.ticker, "exchange": self.exchange,
            "company": self.company,
            "lead_plaintiff_deadline": self.deadline.isoformat() if self.deadline else None,
            "days_left": self.days_left(today),
            "deadline_conflict": self.conflict, "deadline_votes": self.deadline_votes,
            "class_period_start": self.class_start.isoformat() if self.class_start else None,
            "class_period_end": self.class_end.isoformat() if self.class_end else None,
            "class_period_conflict": self.period_conflict,
            "firms": sorted(self.firms), "announcements": self.releases,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "docket": self.docket or None,
        }

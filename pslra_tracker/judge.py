"""Optional semantic judgments from Jev, TypeSafe's System One model.

Regular expressions are good at copying dates and tickers out of a release; they are poor at deciding
what a release *is*. When `TYPESAFE_API_KEY` is set and `typesafe-sdk` is installed
(`pip install pslra-tracker[jev]`), those decisions are asked of Jev instead:

  * what kind of announcement this is   (filing / reminder / investigation / settlement / other)
  * what kind of case a docket caption describes
  * which of the dates in a release is the lead plaintiff deadline (used to audit the regex)

Every answer comes with a confidence. Below the floor, or on any API error, the caller falls back to
the regex rule, so the tracker behaves the same with or without a key, only less precisely.
Set `PSLRA_JEV=0` to switch it off while leaving the key in place.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

ANNOUNCEMENT_OPTIONS = {
    "securities_class_action_filed": (
        "Announces or reports that a securities class action lawsuit HAS BEEN FILED against a public company on "
        "behalf of its investors, typically by the firm that filed it or the first notice of the case."),
    "lead_plaintiff_deadline_reminder": (
        "Reminds investors of an already-filed securities class action and the deadline to ask the court to be "
        "appointed lead plaintiff. Includes 'deadline alert', 'investors have until', 'contact the firm before'."),
    "investigation_only": (
        "A law firm says it is investigating or has launched an inquiry into possible claims, a merger's fairness "
        "or fiduciary breaches. No class action is said to be filed and no lead plaintiff deadline is given."),
    "settlement_notice": "Announces a proposed or approved settlement, a settlement hearing, or a claims deadline.",
    "not_a_securities_case": (
        "Anything else: consumer, data breach, antitrust or employment class actions, law firm news, awards, "
        "company news, or a retraction."),
}
KIND = {"securities_class_action_filed": "filing", "lead_plaintiff_deadline_reminder": "reminder",
        "investigation_only": "investigation", "settlement_notice": "settlement", "not_a_securities_case": "other"}

DOCKET_OPTIONS = {
    "securities_class_action": (
        "Investors suing a public company and/or its officers for securities fraud (Exchange Act 10(b), "
        "Securities Act 11/12) as a putative class action. Caption is usually an individual or fund v. the company."),
    "derivative": (
        "A shareholder suing officers or directors on behalf of the company itself. Caption says 'derivatively' or "
        "'on behalf of', or names the company as nominal defendant."),
    "regulatory_enforcement": "The SEC, CFTC, United States or a state regulator is the plaintiff.",
    "short_swing_or_individual": (
        "Not a class action: a Section 16(b) short-swing profit suit, an individual investor or customer against a "
        "broker or adviser, an arbitration matter, or a private commercial dispute."),
}
DOCKET_CLASS = {"securities_class_action": "securities_class_action", "derivative": "derivative",
                "regulatory_enforcement": "regulatory_enforcement", "short_swing_or_individual": "not_class_action"}


class Judge:
    def __init__(self, client):
        self.client = client
        self.calls = self.errors = self.input_tokens = 0

    def _choice(self, state: dict, instructions: str, criteria: dict) -> tuple[str, float] | None:
        try:
            from typesafe_sdk import Choice
            self.calls += 1
            r = self.client.system_one(state, {"q": Choice(instructions=instructions, criteria=criteria)})
            self.input_tokens += r.usage.input_tokens
            a = r.choices["q"]
            return a.choice, float(a.confidence)
        except Exception as e:  # noqa: BLE001  any failure means "fall back to the regex rule"
            self.errors += 1
            log.warning("Jev call failed: %s: %s", type(e).__name__, e)
            return None

    def announcement(self, title: str, text: str) -> tuple[str, float] | None:
        """(kind, confidence) for a press release, from its headline and as much body as is available."""
        got = self._choice(
            {"headline": title, "text": text[:6000]},
            "What kind of announcement is this press release? Judge from `headline` and `text`.",
            ANNOUNCEMENT_OPTIONS)
        return (KIND[got[0]], got[1]) if got else None

    def docket(self, d: dict) -> tuple[str, float] | None:
        """(classification, confidence) for a federal docket, from its caption and cover-sheet fields."""
        got = self._choice(
            {"case_name": d.get("case_name"), "cause_of_action": d.get("cause"), "court": d.get("court"),
             "nature_of_suit": d.get("nature_of_suit")},
            "What kind of case does this federal court docket describe? Only the caption and cover-sheet fields "
            "are available.", DOCKET_OPTIONS)
        return (DOCKET_CLASS[got[0]], got[1]) if got else None

    def deadline(self, title: str, text: str, candidates: list[str]) -> tuple[str, float] | None:
        """Which candidate date (ISO) is the lead plaintiff deadline. Code finds the dates; Jev only selects."""
        options = {c: None for c in candidates}
        options["none"] = "None of these dates is the deadline to move for lead plaintiff."
        return self._choice(
            {"headline": title, "text": text[:8000]},
            "Which date is the deadline for investors to ask the court to be appointed lead plaintiff in this "
            "securities class action? Not the class period start or end, the release date, or any other date.",
            options)

    def usage(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "input_tokens": self.input_tokens}


def get_judge() -> Judge | None:
    """A Judge when Jev is configured, else None (regex only)."""
    if os.environ.get("PSLRA_JEV", "1") == "0" or not os.environ.get("TYPESAFE_API_KEY"):
        return None
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError:
        log.warning("TYPESAFE_API_KEY is set but typesafe-sdk is not installed; using regex only")
        return None
    return Judge(TypeSafeClient(timeout=30))

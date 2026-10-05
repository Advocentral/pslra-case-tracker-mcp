import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from pslra_tracker.extract import extract
from pslra_tracker.models import Item
from pslra_tracker.pipeline import HELD, facts, link_dockets, normalise_company, run, triage_title
from pslra_tracker.sources import classify_docket, docket_defendant
from pslra_tracker.store import Store

DATA = Path(__file__).parent / "data"
SOON = (date.today() + timedelta(days=30))
SOON_TXT = f"{SOON:%B} {SOON.day}, {SOON.year}"


def load_items():
    return [Item(**{k: v for k, v in d.items() if k in Item.__dataclass_fields__})
            for d in json.loads((DATA / "real_items.json").read_text())]


def release(title, body="", link=None, **kw):
    return Item(title=title, link=link or f"https://example.test/{abs(hash(title))}", source="test",
                text=f"{title}. {body}", body_fetched=bool(body), **kw)


@pytest.fixture
def store():
    return Store(":memory:")


def test_real_releases_become_one_case_per_lawsuit(store):
    summary = run(store, items=load_items())
    bench = {t for t in (DATA / "bench.txt").read_text().split() if t}
    cases = store.cases()
    assert {c.ticker for c in cases} == bench          # 16/16, and no false cases
    assert len(cases) == 16
    assert all(c.deadline for c in cases)
    assert summary["cases_created"] == 16 and summary["merged_into_existing"] == 2
    pnr = next(c for c in cases if c.ticker == "PNR")
    assert pnr.releases == 2 and pnr.period_conflict    # two firms, one case, class periods disagree


def test_second_pass_skips_everything_already_read(store):
    run(store, items=load_items())
    again = run(store, items=load_items())
    assert again["new"] == 0 and again["already_seen"] == 19 and again["cases_created"] == 0


def test_deadline_label_is_not_confused_with_class_period_end():
    it = release("BBNX Investor Alert: Firm Files Class Action Lawsuit Against Beta Bionics, Inc.",
                 "on behalf of purchasers of Beta Bionics, Inc. (\"Beta Bionics\" or \"the Company\") (NASDAQ: BBNX) "
                 "common stock. Investors have until November 3, 2026 to seek appointment as lead plaintiff. "
                 "CLASS PERIOD: July 30, 2025 to February 24, 2026 DEADLINE: November 3, 2026 If you are")
    ex = extract(it)
    assert ex.deadline == date(2026, 11, 3)
    assert (ex.class_start, ex.class_end) == (date(2025, 7, 30), date(2026, 2, 24))
    assert ex.company == "Beta Bionics, Inc."


def test_deadline_inside_class_period_is_dropped():
    it = release("XYZ Investors: lead plaintiff deadline", "(NYSE: XYZ) securities between January 5, 2026 and "
                 "June 30, 2026. The lead plaintiff deadline is March 1, 2026.")
    assert "deadline" not in facts(it)


def test_headline_word_that_is_the_company_name_is_not_the_ticker():
    it = release("AEVEX INVESTOR ALERT: Class Action Filed Against AEVEX Corp.",
                 "AEVEX Corp. (NYSE: AVEX) lead plaintiff deadline")
    assert extract(it).ticker == "AVEX"


def test_title_ticker_wins_over_other_cases_listed_in_the_body():
    it = release("ROSEN Encourages Ardelyx, Inc. Investors to Secure Counsel – ARDX",
                 "lead plaintiff. Recent news: DICK'S Sporting Goods (NYSE: DKS)")
    assert extract(it).ticker == "ARDX"


def test_investigations_and_noise_are_set_aside_and_recorded(store):
    assert triage_title(release("Firm Announces Investigation of Acme Corp. on Behalf of Investors"), wire=True)[0] == "investigation"
    assert triage_title(release("Acme Corp. Announces Settlement of Class Action"), wire=True)[0] == "settlement"
    assert triage_title(release("Acme Declares Quarterly Dividend"), wire=True)[0] == "other"
    assert triage_title(release("ACME DEADLINE: Firm Reminds Investors of Class Action"), wire=True) is None
    run(store, items=[release("Firm Investigates Acme Corp. (NYSE: ACME)", "We are investigating possible claims.")])
    assert store.cases() == []
    assert store.search_announcements(kind="investigation")[0]["decision"]   # the reason is kept


def test_bare_headline_never_opens_a_case_but_joins_one_later(store):
    head = release("ACME Investors: Firm Reminds Acme Corp. Shareholders of Securities Class Action")
    assert run(store, items=[head])["held"] == 1
    assert store.cases() == []
    full = release("Firm Files Class Action Against Acme Corp.",
                   f"Acme Corp. (NYSE: ACME) securities. Move the Court no later than {SOON_TXT} to be lead plaintiff.")
    out = run(store, items=[full])
    assert out["cases_created"] == 1 and out["held_joined_a_case"] == 1
    assert store.cases()[0].releases == 2
    assert not store.search_announcements(query="Reminds")[0]["decision"].startswith("held")


def test_same_ticker_far_apart_deadlines_are_two_lawsuits(store):
    later = SOON + timedelta(days=45)
    a = release("Firm A Files Class Action Against Acme Corp.", f"(NYSE: ACME) lead plaintiff deadline is {SOON_TXT}.")
    b = release("Firm B Files Class Action Against Acme Corp.",
                f"(NYSE: ACME) lead plaintiff deadline is {later:%B} {later.day}, {later.year}.")
    run(store, items=[a, b])
    assert len(store.cases()) == 2


def test_announcement_naming_no_company_and_no_ticker_is_refused(store):
    out = run(store, items=[release("Investor Alert", f"lead plaintiff deadline is {SOON_TXT}.")])
    assert out["refused"] == 1 and store.cases() == []


def test_docket_caption_is_not_trusted_as_a_class_action():
    assert classify_docket("Securities and Exchange Commission v. Reichman") == "regulatory_enforcement"
    assert classify_docket("ES Trust, Derivatively on Behalf of ADMA Biologics, Inc. v. Grossman") == "derivative"
    assert classify_docket("Stewart v. Alphabet Inc.") == "securities_candidate"   # not ruled out, not confirmed
    assert docket_defendant("Novak v. Endava plc") == "Endava plc"
    assert docket_defendant("Subramanian v. Nadella") == ""


def test_docket_links_to_case_by_company_and_filing_window(store):
    run(store, items=[release("Firm Files Class Action Against Acme Corp.",
                              f"Acme Corp. (NYSE: ACME) lead plaintiff deadline is {SOON_TXT}.")])
    base = {"docket_number": "1:26-cv-01234", "court": "S.D.N.Y.", "court_id": "nysd", "judge": "", "cause": "",
            "nature_of_suit": "850 Securities/Commodities", "url": "https://www.courtlistener.com/docket/1/",
            "classification": "securities_candidate"}
    store.add_docket({**base, "docket_id": 1, "case_name": "Doe v. Acme Corp.", "defendant": "Acme Corp.",
                      "date_filed": (SOON - timedelta(days=50)).isoformat()})
    store.add_docket({**base, "docket_id": 2, "case_name": "Roe v. Acme Corp.", "defendant": "Acme Corp.",
                      "date_filed": (SOON - timedelta(days=300)).isoformat()})   # too old to be this lawsuit
    assert link_dockets(store) == 1
    assert store.cases()[0].docket["docket_number"] == "1:26-cv-01234"
    assert [d["docket_id"] for d in store.dockets(unmatched_only=True)] == [2]


def test_unreachable_source_is_an_error_not_an_empty_answer(store, monkeypatch):
    from pslra_tracker import pipeline, sources

    def boom(days):
        raise ConnectionError("timed out")
    monkeypatch.setattr(sources.SOURCES["prnewswire"], "fetch", boom)
    out = pipeline.run(store, sources=["prnewswire"], courts=False)
    assert "error" in out["sources"]["prnewswire"]
    mark = store.watermarks()["prnewswire"]
    assert mark["last_error"] and mark["last_ok"] is None


def test_normalise_company():
    assert normalise_company("Alphabet Inc.") == normalise_company("ALPHABET, INC") == "alphabet"
    assert normalise_company('Company")') == ""
    assert HELD.startswith("held")


class FakeJudge:
    """Stands in for Jev: answers from a table keyed by a word in the headline."""
    def __init__(self, answers, docket_answer=None):
        self.answers, self.docket_answer, self.calls = answers, docket_answer, 0

    def announcement(self, title, text):
        self.calls += 1
        return next((v for k, v in self.answers.items() if k in title), None)

    def docket(self, d):
        return self.docket_answer

    def usage(self):
        return {"calls": self.calls, "errors": 0, "input_tokens": 0}


def test_jev_overrides_the_regex_verdict_when_confident(store):
    # the keyword rule reads "lead plaintiff" in the body and calls this a case; Jev says it is an investigation
    it = release("Firm Urges Acme Corp. Investors to Contact the Firm",
                 f"Acme Corp. (NYSE: ACME). We are investigating. Lead plaintiff deadline is {SOON_TXT}.")
    out = run(store, items=[it], judge=FakeJudge({"Acme": ("investigation", 0.95)}))
    assert out["classifier"]["engine"] == "jev" and store.cases() == []
    decision = store.search_announcements(kind="investigation")[0]["decision"]
    assert decision.startswith("jev: investigation (0.95)") and "regex said" in decision


def test_low_confidence_or_failed_jev_falls_back_to_regex(store):
    body = f"Acme Corp. (NYSE: ACME) lead plaintiff deadline is {SOON_TXT}."
    unsure = release("Firm A Files Class Action Against Acme Corp.", body)
    failed = release("Firm B Reminds Acme Corp. Investors of Class Action Deadline", body)
    out = run(store, items=[unsure, failed], judge=FakeJudge({"Firm A": ("other", 0.40)}))   # Firm B: no answer
    assert out["cases_created"] == 1 and out["merged_into_existing"] == 1


def test_jev_labels_dockets_only_when_confident(store, monkeypatch):
    from pslra_tracker import pipeline
    d = {"docket_id": 9, "case_name": "Donoghue v. Acme Corp.", "docket_number": "1:26-cv-1", "court": "S.D.N.Y.",
         "court_id": "nysd", "date_filed": date.today().isoformat(), "judge": "", "nature_of_suit": "850",
         "cause": "15:78p(b)", "url": "", "classification": "securities_candidate", "defendant": "Acme Corp."}
    monkeypatch.setattr(pipeline, "fetch_dockets", lambda since: [dict(d), {**d, "docket_id": 10}])
    pipeline.sweep_courts(store, 7, [], FakeJudge({}, ("not_class_action", 0.99)))
    assert {x["classification"] for x in store.dockets()} == {"not_class_action"}
    assert store.dockets()[0]["classified_by"] == "jev (0.99)"
    monkeypatch.setattr(pipeline, "fetch_dockets", lambda since: [{**d, "docket_id": 11}])
    pipeline.sweep_courts(store, 7, [], FakeJudge({}, ("securities_class_action", 0.50)))
    assert [x for x in store.dockets() if x["docket_id"] == 11][0]["classification"] == "securities_candidate"

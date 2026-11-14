"""Evaluation harness: math checks, report graders, judge, runner (fake client), feedback."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import biocycle.llm as llm
from biocycle import feedback
from biocycle.data import load_outlets, load_sources
from biocycle.report import CLOSING_LINE, build_payload
from biocycle.valuation import Lot, evaluate
from evals import math_checks
from evals.feedback_summary import summarize, to_markdown
from evals.judge import CRITERIA, JUDGE_SCHEMA, judge_report
from evals.report_graders import flesch_kincaid_grade, grade_report, sections
from evals.run import main as run_main
from evals.scenarios import SCENARIOS

GOOD = """## Recommendation
Clean the hulls and recover the kernels, then sell the cleaned hulls to the local dairy. \
It nets about $98 per ton, or $13 per ton more than selling locally.

## How the options compare
| Option | Expected $/t |
|---|---|
| Clean and recover | $98 |
| Local dairy | $85 |

## What could change the answer
A distant dairy would need to pay $160 per ton to match selling locally.

## Quality and risk checks
Test for aflatoxin before you sell. Crude fiber must stay below 15% [wcngg2022].

## Assumptions and sources
The cleaning cost and kernel price are placeholder values, not quotes.

""" + CLOSING_LINE


@pytest.fixture(autouse=True)
def usage_log(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "LLM_USAGE_LOG", tmp_path / "usage.jsonl")


@pytest.fixture(scope="module")
def payload():
    sources = load_sources()
    lot = Lot(tons=1000)
    return build_payload(lot, evaluate(lot, load_outlets(sources)), sources)


# ---------- math ----------


def test_all_math_checks_pass():
    failed = [(r.name, r.detail) for r in math_checks.run_all() if not r.passed]
    assert failed == []


def test_golden_case_catches_a_wrong_expectation(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("- id: wrong\n  description: x\n  lot: {tons: 1000}\n  expect:\n"
                   "    outlets: {local_dairy: {net_expected: 84}}\n")
    [r] = math_checks.golden_cases(bad)
    assert not r.passed and "local_dairy.net_expected 85.00 != 84" in r.detail


def test_greedy_matches_hand_example():
    # 150 t at 90, 400 t at 86.5, 600 t at 85.5 into 1,000 t -> 13,500 + 34,600 + 38,475
    assert math_checks.greedy_allocation([85.5, 86.5, 90, 55], [600, 400, 150, 800], 1000) \
        == pytest.approx(86575)


def test_backtest_reports_every_history_point():
    [r] = math_checks.backtest()
    assert len(r.data["rows"]) == len(math_checks.load_price_history())
    assert all(row["source_id"] for row in r.data["rows"])


# ---------- code graders ----------


def by_name(grades):
    return {g.name: g for g in grades}


def test_good_report_passes_every_code_grader(payload):
    failed = {g.name: g.detail for g in grade_report(GOOD, payload) if not g.passed}
    assert failed == {}


def test_graders_catch_common_failures(payload):
    bad = (GOOD.replace("## What could change the answer", "## What might change")
           .replace(CLOSING_LINE, "Good luck!")
           .replace("Clean the hulls and recover the kernels, then sell the cleaned hulls to "
                    "the local dairy.", "Ship it all to Texas.")
           .replace("placeholder values", "estimates")
           .replace("Test for aflatoxin before you sell. ", "")
           .replace("[wcngg2022]", "[madeup2030]")
           + "\nYou could earn $41,250 more.")
    g = by_name(grade_report(bad, payload))
    for name in ("numbers_traced", "sections_in_order", "closing_line", "recommends_top_option",
                 "placeholder_disclosed", "quality_flags_surfaced", "citations_valid"):
        assert not g[name].passed, name
    assert "$41,250" in g["numbers_traced"].detail
    assert "madeup2030" in g["citations_valid"].detail


def test_sections_and_reading_level():
    assert set(sections(GOOD)) == {"Recommendation", "How the options compare",
                                   "What could change the answer", "Quality and risk checks",
                                   "Assumptions and sources"}
    easy = "Sell the hulls. The price is good. Test them first."
    hard = ("Notwithstanding considerable macroeconomic uncertainty, the comparative "
            "profitability characteristics of alternative valorization pathways necessitate "
            "comprehensive quantitative evaluation.")
    assert flesch_kincaid_grade(easy) < 5 < 10 < flesch_kincaid_grade(hard)


# ---------- judge + runner (scripted fake client, no network) ----------


class ScriptedClient:
    """Returns the report for writer calls and a JSON verdict for judge calls."""

    def __init__(self, report: str = GOOD, scores: dict[str, int] | None = None):
        self.calls: list[dict] = []
        scores = scores or {k: 5 for k in CRITERIA}
        verdict = json.dumps({"scores": scores, "issues": ["none"], "pass": True})

        def create(**kwargs):
            self.calls.append(kwargs)
            is_judge = "format" in kwargs["output_config"]
            usage = SimpleNamespace(input_tokens=2000, output_tokens=800, iterations=None,
                                    cache_creation_input_tokens=0, cache_read_input_tokens=0)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=verdict if is_judge else report)],
                model=kwargs["model"], stop_reason="end_turn", usage=usage,
                _request_id="req_test")

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_judge_uses_structured_output_and_stronger_model(payload):
    client = ScriptedClient(scores={**{k: 5 for k in CRITERIA}, "plain_language": 3})
    v = judge_report(GOOD, payload, client=client)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": JUDGE_SCHEMA}
    assert v.scores["plain_language"] == 3
    assert v.passed is False and v.model_pass is True  # our threshold overrides the model's


def test_runner_writes_results_and_respects_spend_cap(tmp_path, monkeypatch):
    import evals.run as run

    client = ScriptedClient()
    monkeypatch.setattr(llm, "make_client", lambda: client)
    # Report: 2,000 in + 800 out on Sonnet 5.5 = $0.012; judge on Opus 5.5 = $0.024.
    ok = run.run_report(list(SCENARIOS), judge=True, max_spend=0.10, out_dir=tmp_path)
    data = json.loads(next(tmp_path.glob("*-report.json")).read_text())
    statuses = [s["status"] for s in data["scenarios"]]
    assert statuses.count("ok") == 2  # 0.10 first-call estimate leaves room for two pairs
    assert "skipped" in statuses and not ok
    assert data["spent_usd"] == pytest.approx(0.072)
    assert next(tmp_path.glob("*-report.md")).read_text().startswith("# AI report evaluation")


def test_cli_math_exit_code(tmp_path):
    assert run_main(["--out", str(tmp_path), "math"]) == 0
    assert list(tmp_path.glob("*-math.md"))


# ---------- feedback ----------


@pytest.fixture
def results():
    return evaluate(Lot(tons=1000), load_outlets(load_sources()))


def test_feedback_entry_snapshot_and_validation(results):
    e = feedback.build_entry({"fb_useful": "yes", "fb_outcome_outlet": "local_dairy",
                              "fb_outcome_price": "$95", "fb_outcome_tons": "800"},
                             Lot(tons=1000), results, "sid-1", "abc")
    assert e["outcome"] == {"outlet_id": "local_dairy", "price_per_ton": 95.0, "tons": 800.0,
                            "miles": None}
    assert e["snapshot"]["top_outlet"] == "clean_and_recover"
    assert e["snapshot"]["predictions"]["food_ingredient"]["net_expected"] is None
    assert e["session"] != "sid-1" and len(e["session"]) == 12
    for bad, msg in [({}, "at least one"), ({"fb_useful": "maybe"}, "yes, somewhat"),
                     ({"fb_outcome_outlet": "local_dairy"}, "price you received"),
                     ({"fb_outcome_outlet": "nope", "fb_outcome_price": "1"}, "from the list")]:
        with pytest.raises(feedback.FeedbackError, match=msg):
            feedback.build_entry(bad, Lot(tons=1000), results, "s", "h")


def test_feedback_summary_compares_outcomes(results):
    lot = Lot(tons=1000)
    def entry(form, sid, mode):
        return feedback.build_entry(form, lot, results, sid, "h", data_mode=mode)

    entries = [
        entry({"fb_useful": "yes", "fb_intent": "follow",
               "fb_outcome_outlet": "local_dairy", "fb_outcome_price": "95"}, "a", "market"),
        entry({"fb_useful": "no", "fb_comment": "Price too high",
               "fb_outcome_outlet": "local_dairy", "fb_outcome_price": "70"}, "b", "market"),
        entry({"fb_outcome_outlet": "local_dairy", "fb_outcome_price": "200"}, "c", "demo"),
    ]
    s = summarize(entries)
    assert s["useful_rate"] == 0.5
    # Headline uses market-mode outcomes only: 95 inside 76-106, 70 below.
    assert s["price_within_predicted_range"] == 0.5
    assert s["price_error_vs_mid"]["mean"] == pytest.approx(-8.5)  # (+4 - 21) / 2
    assert s["by_mode"]["demo"]["within_range"] == 0.0  # the demo sale is scored separately
    assert s["followed_top_rate"] == 0.0
    md = to_markdown(s)
    assert "Price too high" in md
    assert "| local_dairy | USDA market data | $70 | $76–106 | no |" in md
    assert "Predicted with demo values: 1 sale(s)" in md


def test_feedback_summary_without_market_outcomes(results):
    e = feedback.build_entry({"fb_outcome_outlet": "local_dairy", "fb_outcome_price": "95"},
                             Lot(tons=1000), results, "a", "h")  # mode not recorded
    s = summarize([e])
    assert s["price_within_predicted_range"] is None and "unknown" in s["by_mode"]
    assert "no accuracy figure for the market numbers" in to_markdown(s)


def test_feedback_route_saves_and_validates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.test_web import default_form
    from web.app import app

    path = tmp_path / "feedback.jsonl"
    monkeypatch.setattr(feedback, "FEEDBACK_LOG", path)
    client = TestClient(app)
    headers = {"content-type": "application/x-www-form-urlencoded"}

    r = client.post("/feedback", content=urlencode(default_form()), headers=headers)
    assert r.status_code == 422 and "at least one question" in r.text
    form = default_form() + [("fb_useful", "somewhat"), ("fb_comment", "<b>hi</b>")]
    r = client.post("/feedback", content=urlencode(form), headers=headers)
    assert r.status_code == 200 and "saved" in r.text
    [entry] = feedback.load(path)
    assert entry["useful"] == "somewhat" and entry["comment"] == "<b>hi</b>"
    assert re.fullmatch(r"[0-9a-f]{12}", entry["session"])

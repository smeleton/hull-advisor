"""Report payload, number guardrail, and the LLM wrapper, with a fake client (no network)."""

import json
from types import SimpleNamespace

import pytest

import biocycle.llm as llm
from biocycle.config import PLACEHOLDER_BANNER
from biocycle.data import load_outlets, load_sources
from biocycle.llm import LLMError, estimate_cost
from biocycle.report import (
    CLOSING_LINE,
    SECTIONS,
    SYSTEM_PROMPT,
    build_payload,
    extract_numbers,
    find_unmatched,
    generate_report,
    report_download,
)
from biocycle.valuation import Lot, evaluate

GOOD_REPORT = """## Recommendation
Clean the hulls and recover kernels: about $98 per ton expected, +$13/ton or +$13,000 for \
the 1,000-ton lot versus selling locally at $85/ton.

## How the options compare
| Option | Expected $/t |
|---|---|
| Clean and recover | $98 |
| Local dairy | $85 |
| Distant dairy | $56 |

## What could change the answer
A distant dairy would need to pay $160/ton. A food-ingredient buyer would need to quote \
above $175/ton.

## Quality and risk checks
1. Test for aflatoxin.
2. Crude fiber must stay below 15% [wcngg2022].

## Assumptions and sources
Kernel recovery is 1.0–1.3% of hull weight [abc_food2025]; hauling at $0.30 per ton-mile.
- [giannini2026] https://s.giannini.ucop.edu/uploads/pub/2026/06/26/v29n5_3_mg7Nqvg.pdf
"""


@pytest.fixture(autouse=True)
def usage_log(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(llm, "LLM_USAGE_LOG", path)
    return path


@pytest.fixture(scope="module")
def setup():
    sources = load_sources()
    outlets = load_outlets(sources)
    lot = Lot(tons=1000)
    return lot, evaluate(lot, outlets), sources


@pytest.fixture
def payload(setup):
    return build_payload(*setup)


class FakeClient:
    """Mimics client.beta.messages.create and records the request."""

    def __init__(self, text: str, stop_reason: str = "end_turn", iterations=None):
        self.kwargs: dict = {}
        usage = SimpleNamespace(input_tokens=1000, output_tokens=500, iterations=iterations,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        response = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=text)],
            model="claude-sonnet-5-5", stop_reason=stop_reason, usage=usage,
            _request_id="req_test",
        )

        def create(**kwargs):
            self.kwargs = kwargs
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


# ---------- payload ----------


def test_payload_is_json_safe_and_complete(payload):
    text = json.dumps(payload)  # no NaN, no numpy types
    assert "NaN" not in text
    assert payload["outlet_count"] == 6
    assert payload["placeholder_affects_results"] is True
    food = next(o for o in payload["outlets"] if o["outlet_id"] == "food_ingredient")
    assert food["needs_quote"] is True and food["net_expected_per_ton"] is None
    assert food["break_even_price_per_ton"] == 175.0
    ids = {s["id"] for s in payload["sources"]}
    assert {"giannini2026", "abc_food2025", "gras2026"} <= ids
    assert all(s["url"].startswith("https://") for s in payload["sources"])


def test_system_prompt_rules():
    assert "ONLY numbers" in SYSTEM_PROMPT
    assert CLOSING_LINE in SYSTEM_PROMPT
    for section in SECTIONS:
        assert f"## {section}" in SYSTEM_PROMPT


# ---------- guardrail ----------


def test_extract_numbers_formats():
    nums = extract_numbers("Earns $1,234.50 or 12.5% more; about $98k. See https://x.org/2026/1")
    assert [n.value for n in nums] == [1234.5, 12.5, 98000]
    assert [n.text for n in nums] == ["$1,234.50", "12.5%", "$98k"]


def test_extract_ignores_list_markers():
    assert [n.value for n in extract_numbers("1. Test it\n2) Again 15%")] == [15]


def test_guardrail_accepts_payload_numbers(payload):
    assert find_unmatched(GOOD_REPORT, payload) == []


def test_guardrail_catches_injected_fake_number(payload):
    bad = GOOD_REPORT.replace("+$13,000", "+$13,000 (or $41,250 with a bonus)")
    unmatched = find_unmatched(bad, payload)
    assert [n.text for n in unmatched] == ["$41,250"]
    assert unmatched[0].start == bad.index("$41,250")


def test_guardrail_rounding_tolerance(payload):
    # 97.5 rounded to "$98" and a fraction shown as a percent are fine; 99 is not.
    p = {"a": 97.5, "b": 0.013, "c": 85, "d": 97_850, "e": 41_000}
    assert find_unmatched("$98 and 1.3%", p) == []
    assert find_unmatched("about $98,000 or $98k", p) == []  # 97,850 rounded to thousands
    for fake in ("$99", "$86.5", "$41,250", "$45,000"):
        assert [n.text for n in find_unmatched(fake, p)] == [fake]


def test_generate_report_with_mock_llm(setup, usage_log):
    client = FakeClient(GOOD_REPORT)
    report = generate_report(*setup, client=client)
    assert report.unmatched == []
    assert report.markdown.endswith(CLOSING_LINE)  # appended when the model omits it
    assert report.llm.cost_usd == pytest.approx(0.007)  # 1000 x $2/M + 500 x $10/M
    assert report.llm.request_id == "req_test"
    kw = client.kwargs
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["fallbacks"] == "default" and "temperature" not in kw
    assert '"outlet_id": "clean_and_recover"' in kw["messages"][0]["content"]
    entry = json.loads(usage_log.read_text().strip())
    assert entry["purpose"] == "report" and entry["output_tokens"] == 500
    assert "text" not in entry


def test_report_download_carries_warnings(setup):
    report = generate_report(*setup, client=FakeClient(GOOD_REPORT + "\nAlso $41,250."))
    md = report_download(report)
    assert PLACEHOLDER_BANNER in md
    assert "$41,250" in md.split("\n\n")[0]  # called out in the header note


def test_refusal_raises(setup):
    with pytest.raises(LLMError, match="declined"):
        generate_report(*setup, client=FakeClient("", stop_reason="refusal"))


def test_truncated_report_raises(setup):
    with pytest.raises(LLMError, match="cut off"):
        generate_report(*setup, client=FakeClient("## Recommendation", stop_reason="max_tokens"))


def test_cost_sums_fallback_iterations_at_each_models_rate():
    usage = SimpleNamespace(iterations=[
        SimpleNamespace(type="message", input_tokens=100, output_tokens=0),
        SimpleNamespace(type="fallback_message", model="claude-opus-5-5",
                        input_tokens=200, output_tokens=300),
    ])
    # 100 x $2/M + (200 x $4/M + 300 x $20/M)
    assert estimate_cost(usage, "claude-sonnet-5-5") == pytest.approx(0.0002 + 0.0008 + 0.006)


def test_missing_credentials_become_llm_error(setup):
    class NoCreds:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(
            TypeError("Could not resolve authentication method. Expected one of api_key"))))

    with pytest.raises(LLMError, match="No Claude credentials"):
        generate_report(*setup, client=NoCreds())


def test_other_type_errors_are_not_masked(setup):
    class Buggy:
        beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(
            TypeError("unexpected keyword argument 'foo'"))))

    with pytest.raises(TypeError, match="foo"):
        generate_report(*setup, client=Buggy())

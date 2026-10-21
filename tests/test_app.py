"""Smoke tests: the Streamlit page renders without errors and shows the key pieces."""

import pytest
from streamlit.testing.v1 import AppTest

from app import outlet_editor_frame, overrides_from_rows
from biocycle.config import PLACEHOLDER_BANNER
from biocycle.data import load_outlets, load_sources
from biocycle.valuation import Lot, evaluate


def run_app() -> AppTest:
    return AppTest.from_file("../app.py", default_timeout=30).run()


def all_text(at: AppTest) -> str:
    parts = [e.value for e in (*at.markdown, *at.warning, *at.info, *at.error, *at.caption)]
    return "\n".join(str(p) for p in parts)


def test_app_renders_with_placeholder_banner():
    at = run_app()
    assert not at.exception
    assert any(PLACEHOLDER_BANNER in w.value for w in at.warning)
    assert [t.label for t in at.tabs] == ["Your lot", "Compare options", "Report", "Buyers",
                                          "Sources"]
    assert at.metric[0].value == "Clean, recover kernels, then sell to local dairy"


def test_high_crude_fiber_flags_feed_outlets():
    at = run_app()
    at.number_input(key="crude_fiber").set_value(16.0).run()
    assert not at.exception
    assert "Ineligible as feed" in all_text(at)
    # Feed outlets drop below the priced non-feed outlets.
    assert at.metric[0].value == "Bioenergy feedstock"


def test_kernel_price_edit_changes_result():
    at = run_app()
    at.number_input(key="coproduct_Recovered kernel").set_value(0.0).run()
    assert not at.exception
    # With no kernel value, cleaning costs $10/t and selling locally wins.
    assert at.metric[0].value == "Sell to local dairy"


def test_sources_tab_lists_all_sources():
    text = all_text(run_app())
    for sid in ("giannini2026", "wcngg2022", "farmprogress_low", "abc_food2025", "gras2026"):
        assert sid in text


# ---------- editor rows -> overrides (AppTest cannot drive st.data_editor) ----------


@pytest.fixture
def real_outlets():
    return load_outlets(load_sources())


def edited_rows(outlets, outlet_id, **changes):
    rows = outlet_editor_frame(outlets).to_dict("records")
    for r in rows:
        if r["outlet_id"] == outlet_id:
            r.update(changes)
    return rows


def test_unedited_table_gives_no_overrides(real_outlets):
    assert overrides_from_rows(edited_rows(real_outlets, "local_dairy"), real_outlets, {}) == {}


def test_buyer_quote_makes_food_ingredient_rankable(real_outlets):
    rows = edited_rows(real_outlets, "food_ingredient", **{"Your price $/t": 200.0})
    overrides = overrides_from_rows(rows, real_outlets, {})
    assert set(overrides) == {"food_ingredient"}
    df = evaluate(Lot(tons=1000), real_outlets, overrides).set_index("outlet_id")
    food = df.loc["food_ingredient"]
    assert not food.needs_quote
    assert food.input_tiers["price"] == "user"
    assert food.net_expected == pytest.approx(110)  # 200 - 60 - 0.30 x 100
    assert food["rank"] < df.loc["distant_dairy", "rank"]


def test_edited_miles_become_user_override(real_outlets):
    rows = edited_rows(real_outlets, "local_dairy", **{"Miles (one-way)": 35.0})
    overrides = overrides_from_rows(rows, real_outlets, {})
    assert overrides["local_dairy"].miles == 35.0
    df = evaluate(Lot(tons=1000), real_outlets, overrides).set_index("outlet_id")
    assert df.loc["local_dairy"].input_tiers["miles"] == "user"


# ---------- report tab (fake LLM client, no network) ----------


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    import biocycle.llm as llm
    from tests.test_report import GOOD_REPORT, FakeClient

    monkeypatch.setattr(llm, "LLM_USAGE_LOG", tmp_path / "usage.jsonl")

    def use(text=GOOD_REPORT, stop_reason="end_turn"):
        monkeypatch.setattr(llm, "make_client", lambda: FakeClient(text, stop_reason))

    return use


def test_report_tab_generates_and_checks_numbers(fake_llm):
    fake_llm()
    at = run_app()
    assert "none yet" in at.sidebar.markdown[-1].value
    at.button(key="generate_report").click().run()
    assert not at.exception
    assert any("Number check passed" in s.value for s in at.success)
    assert any("## Recommendation" in m.value for m in at.markdown)
    assert "\\$0.0070" in at.sidebar.markdown[-1].value


def test_report_tab_highlights_fake_number(fake_llm):
    from tests.test_report import GOOD_REPORT

    fake_llm(GOOD_REPORT + "\nYou could also earn $41,250.")
    at = run_app()
    at.button(key="generate_report").click().run()
    assert any("Number check failed" in w.value for w in at.warning)
    assert any(":red-background[\\$41,250]" in m.value for m in at.markdown)


def test_report_tab_shows_llm_error(fake_llm):
    fake_llm("", stop_reason="refusal")
    at = run_app()
    at.button(key="generate_report").click().run()
    assert not at.exception
    assert any("declined" in e.value for e in at.error)

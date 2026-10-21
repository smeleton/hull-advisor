"""Smoke tests: the Streamlit page renders without errors and shows the key pieces."""

from streamlit.testing.v1 import AppTest

from biocycle.config import PLACEHOLDER_BANNER


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

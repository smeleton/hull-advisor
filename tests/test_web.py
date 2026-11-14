"""Web UI tests: FastAPI routes render, recalculate from form data, and run the report flow.

These replace the old Streamlit AppTest smoke tests with the same scenarios.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from biocycle.config import PLACEHOLDER_BANNER
from biocycle.data import load_buyers, load_outlets, load_sources
from biocycle.present import BUYER_FIELDS
from web.app import SESSIONS, app


@pytest.fixture
def client() -> TestClient:
    SESSIONS.clear()
    return TestClient(app)


def default_form() -> list[tuple[str, str]]:
    """The form exactly as the page first renders it (repeated buyer fields included)."""
    outlets = load_outlets(load_sources())
    form = [("data_mode", "demo"), ("haul_mode", "flat"),
            ("tons", "1000"), ("haul_rate", "0.30"), ("moisture", ""), ("crude_fiber", ""),
            ("aflatoxin", "unknown"), ("coproduct__recovered_kernel", "1.00"),
            ("corn_price", ""), ("sbm_price", "")]
    for o in outlets.values():
        form += [(f"price__{o.id}", ""), (f"miles__{o.id}", f"{o.default_miles:g}"),
                 (f"processing__{o.id}", f"{o.processing_cost_per_ton:g}")]
    for b in load_buyers(outlets):
        form += [(f"b_{c}", str(getattr(b, c))) for c in BUYER_FIELDS]
    return form


def with_changes(form: list[tuple[str, str]], **changes: str) -> list[tuple[str, str]]:
    return [(k, changes.get(k, v)) for k, v in form]


def answer(html: str) -> str:
    m = re.search(r'<h2 class="answer">(.*?)</h2>', html, re.S)
    assert m, "no answer heading"
    return m.group(1).strip()


def figure(html: str, label: str) -> str:
    m = re.search(rf"<dt>{re.escape(label)}</dt>\s*<dd><span class=\"fig\">(.*?)</span>", html)
    assert m, f"no figure {label!r}"
    return m.group(1)


def post(client: TestClient, path: str, form: list[tuple[str, str]]):
    return client.post(path, content=urlencode(form),
                       headers={"content-type": "application/x-www-form-urlencoded"})


# ---------- page ----------


def test_index_renders_with_banner_and_tabs(client):
    r = client.get("/?data=demo")
    assert r.status_code == 200
    html = r.text
    assert PLACEHOLDER_BANNER in html
    for tab in ("Compare options", "Split across buyers", "Written report", "Material and sources"):
        assert f">{tab}</button>" in html
    assert answer(html) == "Clean, recover kernels, then sell to local dairy"
    assert "<svg class=\"chart\"" in html
    assert SESSIONS, "a session is created on first visit"


def test_sources_tab_lists_all_sources(client):
    html = client.get("/?data=demo").text
    for sid in ("giannini2026", "wcngg2022", "farmprogress_low", "abc_food2025", "gras2026"):
        assert f"<code>{sid}</code>" in html


def test_static_assets_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# ---------- recalculation ----------


def test_default_form_matches_initial_page(client):
    first = client.get("/?data=demo").text
    swapped = post(client, "/calculate", default_form()).text
    assert answer(first) == answer(swapped)
    hash_page = re.search(r'data-current-hash="(\w+)"', first).group(1)
    hash_calc = re.search(r'data-current-hash="(\w+)"', swapped).group(1)
    assert hash_page == hash_calc


def test_high_crude_fiber_flags_feed_outlets(client):
    html = post(client, "/calculate", with_changes(default_form(), crude_fiber="16")).text
    assert "Ineligible as feed" in html
    assert answer(html) == "Bioenergy feedstock (biomass power plant)"


def test_kernel_price_edit_changes_result(client):
    form = with_changes(default_form(), coproduct__recovered_kernel="0")
    # With no kernel value, cleaning costs $10/t and selling locally wins.
    assert answer(post(client, "/calculate", form).text) == "Sell to local dairy"


def test_buyer_quote_makes_food_ingredient_rankable(client):
    form = with_changes(default_form(), price__food_ingredient="200")
    html = post(client, "/calculate", form).text
    assert answer(html) == "Hull powder food ingredient"  # 200 - 60 - 30 = 110/t
    assert figure(html, "Expected net") == "$110"
    assert '<span class="tier tier-user"' in html


def test_bad_numbers_fall_back_to_defaults(client):
    form = with_changes(default_form(), tons="abc", haul_rate="-1", crude_fiber="999")
    r = post(client, "/calculate", form)
    assert r.status_code == 200
    assert "BEST PRICED OPTION FOR 1,000 TONS" in r.text.upper()


def test_banner_is_in_swap_fragment(client):
    """The swap fragment re-renders the banner so it tracks placeholder use."""
    html = post(client, "/calculate", default_form()).text
    assert PLACEHOLDER_BANNER in html


# ---------- buyers ----------


def test_buyers_allocation_matches_known_optimum(client):
    html = post(client, "/calculate", default_form()).text
    assert figure(html, "Best total value") == "$86,575"  # 150x90 + 400x86.5 + 450x85.5
    assert figure(html, "Tons allocated") == "1,000"
    assert figure(html, "Value of one more ton of hulls") == "$85.50"


def test_buyer_rows_validation_and_removal(client):
    form = [(k, v) for k, v in default_form() if not k.startswith("b_")]
    form += [("b_buyer", "Only Buyer"), ("b_outlet_id", "local_dairy"),
             ("b_price_per_ton", "100"), ("b_max_tons", "200"), ("b_miles", "10"),
             ("b_notes", "")]
    form += [("b_buyer", "Broken"), ("b_outlet_id", ""), ("b_price_per_ton", "1"),
             ("b_max_tons", "1"), ("b_miles", "1"), ("b_notes", "")]
    html = post(client, "/calculate", form).text
    assert "Row 2: needs a buyer name, an outlet" in html
    assert figure(html, "Best total value") == "$19,400"  # 200 x (100 - 3)
    assert '<span class="tier tier-user"' in html


def test_no_buyers_shows_prompt(client):
    form = [(k, v) for k, v in default_form() if not k.startswith("b_")]
    assert "Add at least one buyer" in post(client, "/calculate", form).text


# ---------- report (fake LLM client, no network) ----------


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    import biocycle.llm as llm
    from tests.test_report import GOOD_REPORT, FakeClient

    monkeypatch.setattr(llm, "LLM_USAGE_LOG", tmp_path / "usage.jsonl")

    def use(text=GOOD_REPORT, stop_reason="end_turn"):
        monkeypatch.setattr(llm, "make_client", lambda: FakeClient(text, stop_reason))

    return use


def test_report_generates_checks_numbers_and_downloads(client, fake_llm):
    fake_llm()
    assert "none yet" in client.get("/?data=demo").text
    html = post(client, "/report", default_form()).text
    assert "Number check passed" in html
    assert "<h2>Recommendation</h2>" in html
    assert "$0.0070" in html  # spend fragment
    rid = re.search(r'href="/report/([\w-]+)\.md"', html).group(1)
    md = client.get(f"/report/{rid}.md")
    assert md.status_code == 200
    assert "## Recommendation" in md.text and PLACEHOLDER_BANNER in md.text
    report_hash = re.search(r'data-report-hash="(\w+)"', html).group(1)
    page_hash = re.search(r'data-current-hash="(\w+)"', client.get("/?data=demo").text).group(1)
    assert report_hash == page_hash  # unchanged inputs -> no "inputs changed" notice


def test_report_highlights_fake_number(client, fake_llm):
    from tests.test_report import GOOD_REPORT

    fake_llm(GOOD_REPORT + "\nYou could also earn $41,250.")
    html = post(client, "/report", default_form()).text
    assert "Number check failed" in html
    assert '<mark class="unmatched" title="Not in the computed results">$41,250</mark>' in html


def test_report_shows_llm_error(client, fake_llm):
    fake_llm("", stop_reason="refusal")
    html = post(client, "/report", default_form()).text
    assert "declined" in html
    assert "none yet" in html


def test_report_html_escapes_model_markup():
    from web.app import report_html

    out = str(report_html("## Hi\n\n<script>alert(1)</script> & more", []))
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_unknown_report_download_is_404(client):
    client.get("/?data=demo")
    assert client.get("/report/nope.md").status_code == 404


def test_literature_sections_render(client):
    page = client.get("/?data=demo").text
    assert "Feed value check" in page and "Research evidence" in page
    assert "Where the default price comes from" in page
    assert "would have to sell for" in page  # hydrochar material model


def test_feed_price_entry_changes_feed_value(client):
    html = post(client, "/calculate", with_changes(default_form(), corn_price="300")).text
    assert "$300/ton corn" in html

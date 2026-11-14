"""Sustainability summary: factors, per-option and per-buyer footprints, Markdown/PDF, route."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from biocycle import sustainability as s
from biocycle.allocation import allocate
from biocycle.config import PLACEHOLDER_BANNER
from biocycle.data import load_buyers, load_outlets, load_sources
from biocycle.valuation import Lot, Override, evaluate


@pytest.fixture(scope="module")
def setup():
    sources = load_sources()
    outlets = load_outlets(sources)
    lot = Lot(tons=1000)
    return lot, evaluate(lot, outlets), outlets, sources


def test_truck_factor_matches_epa_table_8():
    # 0.186 kg CO2 + 0.0016 g CH4 x 28 + 0.0054 g N2O x 265 per short ton-mile
    assert s.truck_kg_co2e_per_ton_mile() == pytest.approx(0.186 + 0.0000448 + 0.001431)


def test_footprints_by_option(setup):
    lot, df, outlets, _ = setup
    fp = s.assess(lot, df, outlets)
    ef = s.truck_kg_co2e_per_ton_mile()
    local = fp["local_dairy"]
    assert local.ton_miles == 20_000 and local.transport_kg == pytest.approx(20_000 * ef)
    assert local.treatment_kg is None  # no EPA factor for feed: not estimated, not zero-claimed
    compost = fp["compost_soil"]
    assert compost.treatment_kg == pytest.approx(1000 * 0.14 * 1000)
    assert compost.treatment_tier == "placeholder" and compost.tier == "placeholder"
    assert fp["bioenergy"].treatment_kg == pytest.approx(1000 * 0.05 * 1000)
    assert fp["distant_dairy"].kg_per_ton == pytest.approx(250 * ef)


def test_user_miles_make_transport_tier_user(setup):
    lot, _, outlets, _ = setup
    df = evaluate(lot, outlets, {"local_dairy": Override(miles=12)})
    fp = s.assess(lot, df, outlets)["local_dairy"]
    assert fp.miles == 12 and fp.transport_tier == "user"


def test_allocation_footprint_uses_allocated_tons_and_buyer_miles(setup):
    lot, df, outlets, _ = setup
    alloc = allocate(load_buyers(outlets), lot, df)
    rows = {r.key: r for r in s.assess_allocation(alloc.table, outlets)}
    assert set(rows) == {"Example Food Processor D", "Example Dairy B", "Example Dairy A"}
    assert rows["Example Dairy A"].ton_miles == 450 * 15
    assert sum(r.tons for r in rows.values()) == pytest.approx(1000)


def test_markdown_and_pdf_carry_banner_and_exclusions(setup):
    lot, df, outlets, sources = setup
    d = s.report_data(lot, df, outlets, "compost_soil", sources)
    md = s.to_markdown(d)
    assert PLACEHOLDER_BANNER in md
    assert "stand-in" in md and "Avoided emissions" in md
    assert "epa_ghg_hub2025" in md
    assert "more." in md  # compost emits more than the local dairy sale
    pdf = s.to_pdf(d)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000


def test_baseline_summary_has_no_comparison_line(setup):
    lot, df, outlets, sources = setup
    md = s.to_markdown(s.report_data(lot, df, outlets, "local_dairy", sources))
    assert "Compared with selling to the local dairy" not in md


def test_download_routes():
    from fastapi.testclient import TestClient

    from tests.test_web import default_form
    from web.app import app

    client = TestClient(app)
    headers = {"content-type": "application/x-www-form-urlencoded"}
    form = default_form() + [("sus_outlet", "bioenergy")]
    pdf = client.post("/sustainability.pdf", content=urlencode(form), headers=headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert "almond_hulls_sustainability_bioenergy.pdf" in pdf.headers["content-disposition"]
    md = client.post("/sustainability.md", content=urlencode(form), headers=headers)
    assert md.status_code == 200 and "Bioenergy (combustion)" in md.text
    assert client.post("/sustainability.exe", content=urlencode(form),
                       headers=headers).status_code == 404
    page = client.get("/?data=demo").text
    assert ">Sustainability</button>" in page and "Kept in productive use" in page

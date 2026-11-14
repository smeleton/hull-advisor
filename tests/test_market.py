"""Market data: parsers on recorded fixtures (no network), snapshot, and using it in the app."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest

from biocycle.data import load_outlets, load_sources
from biocycle.market import (
    TRUCK_PAYLOAD_SHORT_TONS,
    apply_snapshot,
    build_snapshot,
    esmis_last_page,
    latest_quarter,
    parse_eia_weekly,
    parse_esmis_listing,
    parse_hull_quotes,
    parse_nass_almond_price,
    parse_truck_rates,
    report_date,
    snapshot_from_json,
    snapshot_to_json,
)
from biocycle.valuation import Lot, evaluate

FIX = Path(__file__).parent / "fixtures" / "market"


def read(name: str) -> str:
    return (FIX / name).read_text()


# ---------- parsers ----------


def test_parse_current_report():
    text = read("ams3512_2026-11-06.txt")
    assert report_date(text) == date(2026, 11, 6)
    quotes = parse_hull_quotes(text, url="u")
    assert [(q.region, q.price_low, q.price_high, q.average, q.year_ago, q.basis)
            for q in quotes] == [
        ("CA-San Joaquin Valley", 100, 145, 130, 90, "DLVD-T"),
        ("CA-South", 85, 160, 138.75, 108.75, "DLVD-T"),
    ]  # and the Rice Bran section after the page footer is not read as a hull quote
    assert all(q.delivered and q.source_url == "u" for q in quotes)


def test_parse_older_report_with_fob_rows_and_missing_year_ago():
    quotes = parse_hull_quotes(read("ams3512_2023-12-01.txt"), when=date(2023, 12, 1))
    assert len(quotes) == 4
    fob = quotes[0]
    assert (fob.sale_type, fob.price_low, fob.price_high, fob.average, fob.year_ago,
            fob.basis, fob.delivered) == ("Trade", 110, 115, 112.5, None, "FOB-T/R", False)
    assert quotes[1].year_ago == 284.17 and quotes[1].delivered
    assert quotes[2].price_low == quotes[2].price_high == 119


def test_no_hull_section_returns_empty():
    assert parse_hull_quotes("November 6, 2026\nWheat Middlings\nRegion\n") == []


def test_esmis_listing_ignores_featured_link():
    html = read("esmis_listing.html")
    rows = parse_esmis_listing(html)
    assert len(rows) == 2 and all(h.upper().endswith(".PDF") for _, h in rows)
    assert date(2025, 9, 26) not in [d for d, _ in rows]  # the featured link is not a row
    assert esmis_last_page(html) == 18


def test_eia_weekly():
    rows = parse_eia_weekly(read("eia_ca_diesel.html"))
    assert rows[-1] == (date(2026, 11, 2), 8.076)
    assert rows == sorted(rows) and len(rows) == 5


def test_truck_rates_skip_na_and_convert_to_ton_miles():
    rates = parse_truck_rates(json.loads(read("gtor.json")))
    assert {r.quarter for r in rates} == {"2025Q4", "2026Q1"}
    latest = latest_quarter(rates)
    assert [(r.distance_mi, r.rate_per_loaded_mile) for r in latest] == [
        (25, 6.83), (100, 4.26), (200, 2.94)]
    assert latest[0].per_ton_mile == pytest.approx(6.83 / TRUCK_PAYLOAD_SHORT_TONS)
    assert parse_truck_rates(json.loads(read("gtor.json")), region="West") == []  # all NA


def test_nass_almond_price():
    assert parse_nass_almond_price(read("nass_ncit_excerpt.txt")) == {
        2023: 1.72, 2024: 2.34, 2025: 2.60}


# ---------- snapshot ----------


@pytest.fixture
def snap():
    quotes = (parse_hull_quotes(read("ams3512_2026-11-06.txt"))
              + parse_hull_quotes(read("ams3512_2023-12-01.txt"), when=date(2023, 12, 1)))
    return build_snapshot(quotes, parse_truck_rates(json.loads(read("gtor.json"))),
                          parse_eia_weekly(read("eia_ca_diesel.html")),
                          parse_nass_almond_price(read("nass_ncit_excerpt.txt")),
                          retrieved_at="2026-11-07T00:00:00+00:00")


def test_snapshot_uses_latest_report_and_round_trips(snap):
    assert snap.hull_report_date == date(2026, 11, 6)
    assert (snap.local.region, snap.local.basis) == ("CA-San Joaquin Valley", "DLVD-T")
    assert snap.distant.region == "CA-South"
    assert snap.truck_quarter == "2026Q1" and len(snap.truck_rates) == 3
    assert snap.almond_price == (2025, 2.6)
    assert snapshot_from_json(json.loads(json.dumps(snapshot_to_json(snap)))) == snap


def test_apply_snapshot_replaces_demo_prices_with_sourced(snap):
    sources = load_sources()
    outlets = apply_snapshot(load_outlets(sources), snap)
    local, distant = outlets["local_dairy"], outlets["distant_dairy"]
    assert (local.price_low, local.price_high, local.price_tier) == (100, 145, "sourced")
    assert (distant.price_low, distant.price_high, distant.price_from) == (85, 160, None)
    assert "ams_3512" in local.source_ids and set(local.source_ids) <= set(sources)
    assert any("$2.60/lb in 2025" in r for r in outlets["clean_and_recover"].risks)

    lot = Lot(tons=1000, haul_schedule=snap.truck_rates)
    df = evaluate(lot, outlets).set_index("outlet_id")
    r25 = snap.truck_rates[0][1]  # 6.83 / 27.5, rounded to 4 places in the snapshot
    assert df.loc["local_dairy", "haul_cost"] == pytest.approx(r25 * 20)  # clamps below 25 mi
    assert df.loc["local_dairy", "input_tiers"]["price"] == "sourced"
    # Long hauls get the cheaper 200-mile rate: 250 mi x (2.94 / 27.5)
    assert df.loc["distant_dairy", "haul_cost"] == pytest.approx(250 * snap.truck_rates[-1][1])
    assert df.loc["distant_dairy", "input_tiers"]["price"] == "sourced"


def test_haul_schedule_interpolates():
    lot = Lot(tons=1, haul_schedule=((25, 0.30), (100, 0.20), (200, 0.10)))
    assert lot.haul_rate_at(10) == 0.30 and lot.haul_rate_at(400) == 0.10
    assert lot.haul_rate_at(62.5) == pytest.approx(0.25)
    assert lot.haul_cost(150) == pytest.approx(150 * 0.15)
    assert Lot(tons=1, haul_rate=0.4).haul_cost(10) == pytest.approx(4)


# ---------- web app in market mode ----------


@pytest.fixture
def market_client(snap, monkeypatch):
    from fastapi.testclient import TestClient

    import web.app as webapp

    monkeypatch.setattr(webapp, "market_snapshot", lambda: snap)
    webapp.SESSIONS.clear()
    return TestClient(webapp.app)


def test_market_mode_is_default_when_data_exists(market_client):
    html = market_client.get("/").text
    assert 'name="data_mode" value="market"' in html
    assert "USDA market report of Nov 6, 2026" in html
    assert "Ship to southern California dairies" in html
    assert "Market data" in html and "CA-San Joaquin Valley" in html
    assert 'value="usda" checked' in html


def test_market_mode_recalculates_with_usda_hauling(market_client):
    from tests.test_web import default_form

    form = [(k, v) for k, v in default_form() if k not in ("data_mode", "haul_mode")]
    form += [("data_mode", "market"), ("haul_mode", "usda")]
    html = market_client.post("/calculate", content=urlencode(form), headers={
        "content-type": "application/x-www-form-urlencoded"}).text
    # Local dairy: midpoint of $100-145 minus 20 mi x $0.2484 = $117.53
    assert "$118" in html


def test_without_snapshot_app_falls_back_to_demo(monkeypatch):
    from fastapi.testclient import TestClient

    import web.app as webapp

    monkeypatch.setattr(webapp, "market_snapshot", lambda: None)
    html = TestClient(webapp.app).get("/?data=market").text
    assert 'name="data_mode" value="demo"' in html
    assert "python -m biocycle.refresh" in html

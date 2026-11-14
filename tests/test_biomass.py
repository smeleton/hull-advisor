"""Multi-biomass: profiles, mass yield, disposal fees, must-place allocation, nutrient value,
per-biomass quality rules, and every material's page. Expected numbers are hand-computed."""

from __future__ import annotations

from urllib.parse import urlencode

import numpy as np
import pytest

from biocycle.allocation import solve_lp
from biocycle.data import NutrientContent, QualityRules, load_biomasses, load_sources
from biocycle.directory import load_directory
from biocycle.nutrients import load_recovery, route_value
from biocycle.report import Material, build_payload
from biocycle.valuation import Lot, evaluate, evaluate_biomass, quality_flags
from tests.conftest import make_outlet

LOT = Lot(tons=100, haul_rate=0.5)


@pytest.fixture(scope="module")
def sources():
    return load_sources()


@pytest.fixture(scope="module")
def bms(sources):
    return load_biomasses(sources)


def row(df, oid):
    return df.set_index("outlet_id").loc[oid]


# ---------- data ----------

def test_every_biomass_loads_with_a_priced_baseline(bms):
    assert set(bms) == {"almond_hulls", "almond_shells", "pistachio_hulls", "grape_pomace",
                        "tomato_pomace", "orange_peels", "mango_peels", "kiwi_peels"}
    assert next(iter(bms)) == "almond_hulls"  # the default comes first
    for bm in bms.values():
        df = evaluate_biomass(Lot(tons=bm.default_tons), bm)
        assert not row(df, bm.baseline).needs_quote
        assert len(bm.outlets) >= 5


def test_outlet_ids_are_unique_across_biomasses(bms):
    ids = [oid for bm in bms.values() for oid in bm.outlets]
    assert len(ids) == len(set(ids))


def test_sourced_numbers_cite_sources(bms, sources):
    for bm in bms.values():
        for o in bm.outlets.values():
            if o.price_tier == "sourced" and o.price_low is not None:
                assert o.source_ids, o.id
            for c in o.coproducts:
                if c.yield_tier == "user":  # the team's yields, links pending
                    assert "team_yields_2026" in c.source_ids, o.id
        for n in bm.nutrients.values():
            assert n.tier != "sourced" or n.source_id in sources


def test_directory_lists_no_producers_and_only_known_outlets(bms):
    every = {oid: o for bm in bms.values() for oid, o in bm.outlets.items()}
    companies = load_directory(every)
    ids = {c.id for c in companies}
    assert not ids & {"wonderful_king_plant", "ventura_coastal_tipton", "morning_star_company"}
    assert any(set(c.outlets) & set(bms["grape_pomace"].outlets) for c in companies)


# ---------- valuation ----------

def test_mass_yield_scales_price_and_hauls_only_product():
    dried = make_outlet("dried", price_low=200, price_high=200, mass_yield=0.25,
                        haul_product=True, processing_cost_per_ton=30, default_miles=40)
    base = make_outlet("base")
    df = evaluate(LOT, {"base": base, "dried": dried}, baseline_id="base")
    r = row(df, "dried")
    # 200 x 0.25 - 30 - 0.5 x 40 x 0.25 = 50 - 30 - 5 = 15
    assert r.net_expected == pytest.approx(15)
    assert r.haul_cost == pytest.approx(5)
    # baseline net 100 - 5 = 95; break-even per ton of product = (95 + 30 + 5) / 0.25 = 520
    assert r.break_even_price == pytest.approx(520)


def test_negative_price_is_a_fee():
    fee = make_outlet("fee", price_low=-40, price_high=-20, default_miles=10)
    df = evaluate(LOT, {"fee": fee}, baseline_id="fee")
    assert row(df, "fee").net_expected == pytest.approx(-35)  # -30 - 5


def test_must_place_sends_the_whole_lot_even_at_a_loss():
    margins = np.array([-5.0, -20.0])
    caps = np.array([60.0, 100.0])
    free = solve_lp(margins, caps, 100)
    assert free.tons.sum() == pytest.approx(0)  # nothing pays, so nothing goes
    placed = solve_lp(margins, caps, 100, must_place=True)
    assert placed.tons == pytest.approx([60, 40])
    assert placed.value == pytest.approx(-300 - 800)
    short = solve_lp(margins, np.array([30.0, 20.0]), 100, must_place=True)
    assert short.tons.sum() == pytest.approx(50)  # capacity runs out: fill every buyer


def test_nutrient_value_arithmetic():
    content = {"N": NutrientContent(2.0, "sourced"), "K": NutrientContent(1.0, "sourced")}
    doc = load_recovery()
    v = route_value("compost_retained", content, dry_fraction=0.4, doc=doc)
    eff, price = doc["routes"]["compost_retained"]["efficiency"], doc["prices_per_lb"]
    # lb N per ton = 2000 x 0.4 x 2% = 16 lb before recovery; K: 8 lb
    assert v.lb_low["N"] == pytest.approx(16 * eff["N"]["low"])
    assert v.lb_high["K"] == pytest.approx(8 * eff["K"]["high"])
    assert v.value_low == pytest.approx(16 * eff["N"]["low"] * price["N"]["low"]
                                        + 8 * eff["K"]["low"] * price["K"]["low"])
    assert "P" not in v.lb_low  # no content given, no value


def test_nutrients_count_only_when_the_producer_keeps_them():
    content = {"K": NutrientContent(2.0, "sourced")}
    kept = make_outlet("kept", nutrient_routes=["land_applied"], nutrient_credit=True)
    shown = make_outlet("shown", nutrient_routes=["compost_retained"])
    df = evaluate(Lot(tons=10, haul_rate=0.5, moisture_pct=50), {"kept": kept, "shown": shown},
                  baseline_id="shown", nutrients=content)
    assert row(df, "shown").net_expected == pytest.approx(95)  # 100 - 5, nutrients not counted
    k = row(df, "kept")
    assert k.nutrient_credit and k.nutrient_value > 0
    assert k.net_expected == pytest.approx(95 + k.nutrient_value)
    assert k.input_tiers["nutrients"] == "placeholder"  # demo efficiencies outrank the entered moisture


def test_quality_rules_are_per_biomass():
    feed = make_outlet("feed")
    wet = QualityRules(moisture_typical=(55, 60), moisture_note="", crude_fiber_max=None,
                       mycotoxin=None)
    flags, ok = quality_flags(feed, Lot(tons=1, moisture_pct=58, crude_fiber_pct=30), wet)
    assert ok and flags == []  # no fiber standard, typical moisture, no mycotoxin rule
    flags, _ = quality_flags(feed, Lot(tons=1, moisture_pct=58))  # hull rules by default
    assert any("outside the typical 9–12%" in f for f in flags)


def test_grape_report_payload_names_its_baseline(bms, sources):
    bm = bms["grape_pomace"]
    df = evaluate_biomass(Lot(tons=bm.default_tons), bm)
    payload = build_payload(Lot(tons=300), df, sources, Material.of(bm))
    assert payload["material"]["id"] == "grape_pomace"
    assert payload["baseline_outlet_id"] == "grape_feed_wet"
    assert "feed_standard_crude_fiber_max_pct" not in payload
    assert "crude_fiber_pct" not in payload["lot"]


# ---------- web ----------

def test_every_material_page_renders(bms):
    from fastapi.testclient import TestClient

    from web.app import app

    client = TestClient(app)
    for bid, bm in bms.items():
        html = client.get(f"/?biomass={bid}").text
        assert f'<option value="{bid}" selected>' in html
        assert f"Tons of {bm.noun}" in html
        assert ('name="crude_fiber"' in html) == (bm.rules.crude_fiber_max is not None)
        assert ("USDA market" in html) == bm.market_data
        form = urlencode([("biomass", bid), ("tons", "100")])
        r = client.post("/calculate", content=form,
                        headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200 and 'data-swap="compare-results"' in r.text


def test_unknown_material_falls_back_to_almond_hulls():
    from fastapi.testclient import TestClient

    from web.app import app

    html = TestClient(app).get("/?biomass=nope").text
    assert '<option value="almond_hulls" selected>' in html


def test_sources_cited_inside_risk_text_reach_the_payload(bms, sources):
    bm = bms["orange_peels"]
    df = evaluate_biomass(Lot(tons=1000), bm)
    payload = build_payload(Lot(tons=1000), df, sources, Material.of(bm))
    ids = {s["id"] for s in payload["sources"]}
    assert "calrecycle_stanislaus_fees_2026" in ids  # cited only in the compost outlet's risks
    assert payload["lot"]["measurements_tier"] == "user"

import math

import pytest

from biocycle.data import load_buyers, load_outlets, load_sources
from biocycle.valuation import Lot, Override, any_placeholder, evaluate
from tests.conftest import make_outlet

LOT = Lot(tons=100, haul_rate=0.5, aflatoxin_tested="yes")


def row(df, outlet_id):
    return df.set_index("outlet_id").loc[outlet_id]


def test_net_value_arithmetic(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    better = row(df, "better")
    assert better.haul_cost == pytest.approx(10.0)  # 0.5 $/ton-mile x 20 mi
    assert better.net_expected == pytest.approx(105.0)  # 120 - 5 - 10
    assert better.total_expected == pytest.approx(10_500.0)
    worse = row(df, "worse")
    assert (worse.net_low, worse.net_expected, worse.net_high) == pytest.approx((70, 80, 90))
    assert (worse.total_low, worse.total_high) == pytest.approx((7_000, 9_000))


def test_clean_and_recover_adds_kernel_on_top_of_dairy_price(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    rec = row(df, "recover")
    # Kernel: 2000 lb x 1-2% x $1/lb = $20-40 per ton, on top of the inherited $100 hull price.
    assert (rec.coproduct_low, rec.coproduct_high) == pytest.approx((20, 40))
    assert (rec.price_low, rec.price_high) == pytest.approx((100, 100))
    assert (rec.net_low, rec.net_expected, rec.net_high) == pytest.approx((111, 121, 131))


def test_ranking_order(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    assert list(df.outlet_id) == ["recover", "better", "base", "worse", "quote"]
    assert list(df["rank"]) == [1, 2, 3, 4, 5]


def test_vs_baseline_sign(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    assert row(df, "better").vs_baseline_per_ton == pytest.approx(10)
    assert row(df, "better").vs_baseline_total == pytest.approx(1_000)
    assert row(df, "worse").vs_baseline_per_ton == pytest.approx(-15)
    assert row(df, "base").vs_baseline_per_ton == 0
    assert row(df, "base").is_baseline


@pytest.mark.parametrize("outlet_id", ["better", "worse", "recover", "quote"])
def test_break_even_makes_net_equal_baseline(outlets, outlet_id):
    df = evaluate(LOT, outlets, baseline_id="base")
    break_even = row(df, outlet_id).break_even_price
    df2 = evaluate(LOT, outlets, {outlet_id: Override(price=break_even)}, baseline_id="base")
    assert row(df2, outlet_id).net_expected == pytest.approx(row(df2, "base").net_expected)


def test_break_even_values(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    assert row(df, "worse").break_even_price == pytest.approx(115)  # 95 + 0 + 20
    assert row(df, "recover").break_even_price == pytest.approx(74)  # 95 + 4 + 5 - 30
    assert row(df, "quote").break_even_price == pytest.approx(110)  # 95 + 10 + 5


def test_placeholder_tier_propagates(outlets):
    df = evaluate(LOT, outlets, baseline_id="base")
    assert row(df, "worse").input_tiers["price"] == "placeholder"
    assert row(df, "worse").result_tier == "placeholder"
    assert row(df, "base").result_tier == "sourced"
    assert any_placeholder(df)
    sourced_only = {k: outlets[k] for k in ("base", "better")}
    assert not any_placeholder(evaluate(LOT, sourced_only, baseline_id="base"))


def test_placeholder_coproduct_and_premium_propagate():
    base = make_outlet("base")
    prem = make_outlet("prem", price_low=None, price_high=None, price_from="base",
                       price_premium=20, premium_tier="placeholder")
    cp = make_outlet("cp", coproducts=[{
        "name": "K", "yield_low": 0.01, "yield_high": 0.01, "yield_tier": "sourced",
        "unit_price_per_lb": 1.0, "unit_price_tier": "placeholder",
    }])
    df = evaluate(LOT, {"base": base, "prem": prem, "cp": cp}, baseline_id="base")
    assert row(df, "prem").result_tier == "placeholder"
    assert row(df, "prem").net_expected == pytest.approx(115)  # 100 + 20 - 5
    assert row(df, "cp").result_tier == "placeholder"


def test_user_override_sets_user_tier(outlets):
    df = evaluate(LOT, outlets, {"worse": Override(price=104, miles=8)}, baseline_id="base")
    worse = row(df, "worse")
    assert worse.input_tiers["price"] == "user"
    assert worse.input_tiers["miles"] == "user"
    assert worse.result_tier == "user"
    assert (worse.net_low, worse.net_high) == pytest.approx((100, 100))  # 104 - 0 - 4


def test_missing_price_needs_quote_never_best(outlets):
    # Make every priced outlet deeply negative; the needs-quote outlet must still rank last.
    far = {k: Override(miles=1_000) for k in ("base", "better", "worse", "recover")}
    df = evaluate(LOT, outlets, far, baseline_id="base")
    last = df.iloc[-1]
    assert last.outlet_id == "quote"
    assert last.needs_quote
    assert "needs quote" in last["flags"]
    assert math.isnan(last.net_expected) and math.isnan(last.vs_baseline_per_ton)
    assert df.iloc[0].outlet_id != "quote"


def test_price_from_follows_baseline_override(outlets):
    df = evaluate(LOT, outlets, {"base": Override(price=80)}, baseline_id="base")
    rec = row(df, "recover")
    assert (rec.price_low, rec.price_high) == pytest.approx((80, 80))
    assert rec.input_tiers["price"] == "user"


def test_quality_flags(outlets):
    lot = Lot(tons=100, haul_rate=0.5, crude_fiber_pct=15.0, aflatoxin_tested="unknown",
              moisture_pct=14.0)
    df = evaluate(lot, outlets, baseline_id="base")
    for feed_id in ("base", "better", "worse", "recover"):
        flags = row(df, feed_id)["flags"]
        assert not row(df, feed_id).eligible
        assert any("crude fiber" in f for f in flags)
        assert any("aflatoxin" in f for f in flags)
        assert any("moisture" in f for f in flags)
    quote = row(df, "quote")  # food market: aflatoxin warning only
    assert quote.eligible
    assert any("aflatoxin" in f for f in quote["flags"])
    assert not any("crude fiber" in f for f in quote["flags"])

    clean = evaluate(Lot(tons=100, crude_fiber_pct=12.0, aflatoxin_tested="yes",
                         moisture_pct=10.0), outlets, baseline_id="base")
    assert all(f == ["needs quote"] or f == [] for f in clean["flags"])


def test_ineligible_ranked_after_eligible():
    feed = make_outlet("feed", price_low=200, price_high=200)
    soil = make_outlet("soil", market="soil", price_low=50, price_high=50)
    df = evaluate(Lot(tons=1, crude_fiber_pct=16), {"feed": feed, "soil": soil},
                  baseline_id="feed")
    assert list(df.outlet_id) == ["soil", "feed"]


def test_real_data_loads_and_evaluates():
    sources = load_sources()
    outlets = load_outlets(sources)
    assert len(sources) == 5 and len(outlets) == 6
    assert len(load_buyers(outlets)) == 6
    df = evaluate(Lot(tons=1_000), outlets)
    base = row(df, "local_dairy")
    assert base.net_expected == pytest.approx(85)  # mid 91 - 0.30 x 20 mi
    assert row(df, "clean_and_recover").vs_baseline_per_ton == pytest.approx(13)
    assert row(df, "distant_dairy").break_even_price == pytest.approx(160)
    assert row(df, "food_ingredient").needs_quote
    assert row(df, "food_ingredient").break_even_price == pytest.approx(175)
    assert df.iloc[-1].outlet_id == "food_ingredient"
    assert any_placeholder(df)

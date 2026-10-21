import numpy as np
import pytest

from biocycle.allocation import allocate, solve_lp
from biocycle.data import Buyer, load_buyers, load_outlets, load_sources
from biocycle.valuation import Lot, Override, evaluate


def test_lp_two_buyer_known_optimum():
    # A nets $10/t (cap 60), B nets $5/t (cap 100), lot of 100 t:
    # fill A first, rest to B -> 60 x 10 + 40 x 5 = 800.
    sol = solve_lp(np.array([10.0, 5.0]), np.array([60.0, 100.0]), 100)
    assert sol.tons == pytest.approx([60, 40])
    assert sol.value == pytest.approx(800)
    # One more ton of A's capacity displaces a B ton: +10 - 5 = +5. B has slack: 0.
    assert sol.capacity_marginals == pytest.approx([5, 0])
    # One more ton of hulls goes to B: +5.
    assert sol.hull_marginal == pytest.approx(5)


def test_lp_capacity_binding_when_lot_is_large():
    sol = solve_lp(np.array([10.0, 5.0]), np.array([60.0, 100.0]), 200)
    assert sol.tons == pytest.approx([60, 100])
    assert sol.value == pytest.approx(1_100)
    assert sol.capacity_marginals == pytest.approx([10, 5])
    assert sol.hull_marginal == pytest.approx(0)


def test_lp_skips_negative_margin_buyers():
    sol = solve_lp(np.array([-3.0, 4.0]), np.array([100.0, 30.0]), 100)
    assert sol.tons == pytest.approx([0, 30])
    assert sol.value == pytest.approx(120)


def test_lp_no_buyers():
    sol = solve_lp(np.array([]), np.array([]), 100)
    assert sol.value == 0 and len(sol.tons) == 0


@pytest.fixture(scope="module")
def outlets():
    return load_outlets(load_sources())


def test_allocate_sample_buyers(outlets):
    lot = Lot(tons=1_000)
    alloc = allocate(load_buyers(outlets), lot, evaluate(lot, outlets))
    t = alloc.table.set_index("buyer")
    # Net $/t: Food Processor D 180-60-30 = 90; Dairy B 97-0-10.5 = 86.5;
    # Dairy A 90-0-4.5 = 85.5; Feedlot C 130-75 = 55; compost/biomass far lower.
    assert t["margin_per_ton"].to_dict() == pytest.approx({
        "Example Food Processor D": 90, "Example Dairy B": 86.5, "Example Dairy A": 85.5,
        "Example Feedlot C": 55, "Example Compost Co. E": 17.5, "Example Biomass Plant F": 17,
    })
    assert list(alloc.table["buyer"][:3]) == [
        "Example Food Processor D", "Example Dairy B", "Example Dairy A"]
    assert t["tons"].to_dict() == pytest.approx({
        "Example Food Processor D": 150, "Example Dairy B": 400, "Example Dairy A": 450,
        "Example Feedlot C": 0, "Example Compost Co. E": 0, "Example Biomass Plant F": 0,
    })
    assert alloc.total_value == pytest.approx(150 * 90 + 400 * 86.5 + 450 * 85.5)
    assert alloc.tons_unallocated == pytest.approx(0)
    # Dairy A has spare capacity, so an extra ton of hulls (or of D/B capacity) displaces A.
    assert alloc.hull_marginal == pytest.approx(85.5)
    assert t.loc["Example Food Processor D", "capacity_marginal"] == pytest.approx(4.5)
    assert t.loc["Example Dairy B", "capacity_marginal"] == pytest.approx(1.0)
    assert t.loc["Example Dairy A", "capacity_marginal"] == pytest.approx(0)
    assert alloc.result_tier == "placeholder"


def test_allocate_uses_outlet_overrides(outlets):
    lot = Lot(tons=100)
    results = evaluate(lot, outlets, {"food_ingredient": Override(processing_cost=20)})
    buyers = [Buyer("Example Food Processor D", "food_ingredient", 180, 150, 100)]
    alloc = allocate(buyers, lot, results, tiers=["user"])
    row = alloc.table.iloc[0]
    assert row["margin_per_ton"] == pytest.approx(130)  # 180 - 20 - 0.30 x 100
    assert alloc.result_tier == "user"


def test_allocate_adds_coproduct_value(outlets):
    lot = Lot(tons=100)
    buyers = [Buyer("Example Cleaner G", "clean_and_recover", 90, 100, 20)]
    alloc = allocate(buyers, lot, evaluate(lot, outlets), tiers=["user"])
    # 90 - 10 cleaning - 6 haul + 23 kernel (midpoint of $20-26)
    assert alloc.table.iloc[0]["margin_per_ton"] == pytest.approx(97)


def test_placeholder_coproduct_keeps_allocation_placeholder(outlets):
    # User buyer and user cleaning cost, but the kernel $/lb is still a placeholder.
    lot = Lot(tons=100)
    results = evaluate(lot, outlets, {"clean_and_recover": Override(processing_cost=8)})
    buyers = [Buyer("Example Cleaner G", "clean_and_recover", 90, 100, 20)]
    alloc = allocate(buyers, lot, results, tiers=["user"])
    assert alloc.result_tier == "placeholder"
    priced = evaluate(lot, outlets, {"clean_and_recover": Override(
        processing_cost=8, coproduct_prices={"Recovered kernel": 0.8})})
    assert allocate(buyers, lot, priced, tiers=["user"]).result_tier == "user"


def test_ineligible_outlet_gets_no_tons(outlets):
    lot = Lot(tons=500, crude_fiber_pct=16)
    alloc = allocate(load_buyers(outlets), lot, evaluate(lot, outlets))
    t = alloc.table.set_index("buyer")
    for dairy in ("Example Dairy A", "Example Dairy B", "Example Feedlot C"):
        assert t.loc[dairy, "tons"] == pytest.approx(0)
        assert not t.loc[dairy, "eligible"]
    assert t.loc["Example Food Processor D", "tons"] == pytest.approx(150)


def test_unallocated_when_capacity_short(outlets):
    lot = Lot(tons=1_000)
    buyers = [Buyer("Example Dairy A", "local_dairy", 90, 300, 15)]
    alloc = allocate(buyers, lot, evaluate(lot, outlets))
    assert alloc.tons_unallocated == pytest.approx(700)
    assert alloc.table.iloc[0]["capacity_marginal"] == pytest.approx(85.5)
    assert alloc.hull_marginal == pytest.approx(0)

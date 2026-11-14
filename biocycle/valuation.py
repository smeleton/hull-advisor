"""Deterministic net-value-per-ton math for each outlet, compared to the biomass's baseline
(today's usual route, e.g. selling almond hulls to the local dairy).

Formula per outlet (all $/ton of input material, r = haul rate $/ton-mile, y = mass yield,
tons of product sold per ton of input; h = tons hauled per ton of input, y or 1):
    net = price * y - processing_cost - r * miles * h + sum(coproduct value per ton)
A negative price is a fee the producer pays (disposal, tipping). Low/high come from the price
and coproduct ranges; expected is the midpoint. Haul rate, miles, processing cost and mass
yield are point values.

Recovered-kernel mass (1-1.3% of the pile) is NOT subtracted from the hull tons sold;
the effect is negligible for a demo estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from biocycle.config import BASELINE_ID, DEFAULT_HAUL_RATE, LB_PER_TON
from biocycle.data import HULL_RULES, NutrientContent, Outlet, QualityRules, worst_tier

NAN = float("nan")


@dataclass(frozen=True)
class Lot:
    tons: float
    haul_rate: float = DEFAULT_HAUL_RATE
    haul_rate_tier: str = "sourced"  # set to "user" when entered in the UI
    moisture_pct: float | None = None
    crude_fiber_pct: float | None = None
    aflatoxin_tested: str = "unknown"  # yes | no | unknown
    # Optional distance-based rates ((miles, $/ton-mile), ...), e.g. USDA truck rates. When
    # set, they replace the flat haul_rate: longer hauls cost less per mile.
    haul_schedule: tuple[tuple[float, float], ...] = ()

    def haul_rate_at(self, miles: float) -> float:
        """$/ton-mile for a haul of this length: linear between schedule points, flat beyond."""
        pts = sorted(self.haul_schedule)
        if not pts:
            return self.haul_rate
        if miles <= pts[0][0]:
            return pts[0][1]
        for (m0, r0), (m1, r1) in zip(pts, pts[1:]):
            if miles <= m1:
                return r0 + (r1 - r0) * (miles - m0) / (m1 - m0)
        return pts[-1][1]

    def haul_cost(self, miles: float) -> float:
        """$ per ton hauled `miles` one way."""
        return self.haul_rate_at(miles) * miles


@dataclass(frozen=True)
class Override:
    """User-entered values for one outlet. A price override collapses low/high to one value."""

    price: float | None = None
    miles: float | None = None
    processing_cost: float | None = None
    coproduct_prices: dict[str, float] = field(default_factory=dict)  # name -> $/lb


@dataclass(frozen=True)
class PriceRange:
    low: float | None
    high: float | None
    tier: str
    source_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def missing(self) -> bool:
        return self.low is None


def resolve_price(
    outlet: Outlet, outlets: dict[str, Outlet], overrides: dict[str, Override]
) -> PriceRange:
    """Price range for an outlet after overrides and price_from inheritance."""
    own = overrides.get(outlet.id, Override())
    if own.price is not None:
        return PriceRange(own.price, own.price, "user")
    if outlet.price_from:
        base = resolve_price(outlets[outlet.price_from], outlets, overrides)
        if base.missing:
            return PriceRange(None, None, base.tier)
        prem = outlet.price_premium
        tier = worst_tier(base.tier, outlet.premium_tier) if prem else base.tier
        return PriceRange(base.low + prem, base.high + prem, tier, base.source_ids)
    if outlet.price_low is None:
        return PriceRange(None, None, outlet.price_tier)
    high = outlet.price_high if outlet.price_high is not None else outlet.price_low
    return PriceRange(outlet.price_low, high, outlet.price_tier, outlet.source_ids)


def coproduct_range(outlet: Outlet, ov: Override = Override()) -> tuple[float, float]:
    """($/ton low, $/ton high) of all coproducts recovered from one ton of hulls."""
    low = high = 0.0
    for c in outlet.coproducts:
        price = ov.coproduct_prices.get(c.name, c.unit_price_per_lb)
        low += LB_PER_TON * c.yield_low * price
        high += LB_PER_TON * c.yield_high * price
    return low, high


def quality_flags(outlet: Outlet, lot: Lot,
                  rules: QualityRules = HULL_RULES) -> tuple[list[str], bool]:
    """(flags, eligible). Only a failed feed standard makes an outlet ineligible."""
    flags: list[str] = []
    eligible = True
    fiber_max = rules.crude_fiber_max
    if (outlet.market == "feed" and fiber_max is not None and lot.crude_fiber_pct is not None
            and lot.crude_fiber_pct >= fiber_max):
        eligible = False
        src = f" [{rules.crude_fiber_source}]" if rules.crude_fiber_source else ""
        flags.append(
            f"Ineligible as feed: crude fiber {lot.crude_fiber_pct:g}% is at or above "
            f"the {fiber_max:g}% standard{src}."
        )
    if (rules.moisture_typical and outlet.market in rules.moisture_markets
            and lot.moisture_pct is not None):
        lo, hi = rules.moisture_typical
        if not lo <= lot.moisture_pct <= hi:
            note = f" {rules.moisture_note}" if rules.moisture_note else ""
            flags.append(
                f"Info: moisture {lot.moisture_pct:g}% is outside the typical {lo:g}–{hi:g}%"
                f"{note}."
            )
    if (rules.mycotoxin and outlet.market in rules.mycotoxin_markets
            and lot.aflatoxin_tested != "yes"):
        uses = " or ".join(rules.mycotoxin_markets)
        flags.append(f"Warning: {rules.mycotoxin} not tested. Test before selling for {uses}.")
    return flags, eligible


def nutrient_values(outlet: Outlet, lot: Lot, nutrients: dict[str, NutrientContent] | None,
                    moisture_default: float | None) -> list[Any]:
    """Fertilizer value of the lot's nutrients for each of the outlet's recovery routes."""
    if not outlet.nutrient_routes or not nutrients:
        return []
    from biocycle.nutrients import route_value

    moisture = lot.moisture_pct if lot.moisture_pct is not None else moisture_default
    if moisture is None:
        return []
    tier = "user" if lot.moisture_pct is not None else "sourced"
    dry = max(0.0, 1 - moisture / 100)
    return [route_value(r, nutrients, dry, tier) for r in outlet.nutrient_routes]


def _value_outlet(
    outlet: Outlet, lot: Lot, outlets: dict[str, Outlet], overrides: dict[str, Override],
    rules: QualityRules = HULL_RULES, nutrients: dict[str, NutrientContent] | None = None,
    moisture_default: float | None = None,
) -> dict[str, Any]:
    ov = overrides.get(outlet.id, Override())
    price = resolve_price(outlet, outlets, overrides)
    miles = ov.miles if ov.miles is not None else outlet.default_miles
    processing = (
        ov.processing_cost if ov.processing_cost is not None else outlet.processing_cost_per_ton
    )
    y, h = outlet.mass_yield, outlet.haul_tons_per_ton
    haul_cost = lot.haul_cost(miles) * h  # $ per ton of input
    cp_low, cp_high = coproduct_range(outlet, ov)
    nvals = nutrient_values(outlet, lot, nutrients, moisture_default)
    credited = nvals[0] if outlet.nutrient_credit and nvals else None
    if credited is not None:  # the producer keeps the fertilizer value: count it like a coproduct
        cp_low += credited.value_low
        cp_high += credited.value_high

    input_tiers = {
        "price": price.tier,
        "processing_cost": "user" if ov.processing_cost is not None else outlet.processing_tier,
        "miles": "user" if ov.miles is not None else outlet.miles_tier,
        "haul_rate": lot.haul_rate_tier,
    }
    for c in outlet.coproducts:
        price_tier = "user" if c.name in ov.coproduct_prices else c.unit_price_tier
        input_tiers[f"coproduct: {c.name}"] = worst_tier(c.yield_tier, price_tier)
    if credited is not None:
        input_tiers["nutrients"] = credited.tier

    flags, eligible = quality_flags(outlet, lot, rules)
    if price.missing:
        flags.insert(0, "needs quote")
        net_low = net_high = NAN
    else:
        net_low = price.low * y - processing - haul_cost + cp_low
        net_high = price.high * y - processing - haul_cost + cp_high

    source_ids = sorted(
        set(outlet.source_ids)
        | set(price.source_ids)
        | {s for c in outlet.coproducts for s in c.source_ids}
        | (set(credited.source_ids) if credited is not None else set())
    )
    return {
        "outlet_id": outlet.id,
        "name": outlet.name,
        "status": outlet.status,
        "market": outlet.market,
        "end_use": outlet.end_use,
        "needs_quote": price.missing,
        "eligible": eligible,
        "price_low": NAN if price.missing else price.low,
        "price_high": NAN if price.missing else price.high,
        "processing_cost": processing,
        "miles": miles,
        "mass_yield": y,
        "haul_tons_per_ton": h,
        "haul_cost": haul_cost,
        "coproduct_low": cp_low,
        "coproduct_high": cp_high,
        "coproduct_expected": (cp_low + cp_high) / 2,
        "net_low": net_low,
        "net_expected": (net_low + net_high) / 2,
        "net_high": net_high,
        "flags": flags,
        "nutrients": [vars(v) | {"value_expected": v.value_expected} for v in nvals],
        "nutrient_credit": credited is not None,
        "nutrient_value": credited.value_expected if credited is not None else 0.0,
        "requirements": list(outlet.requirements),
        "risks": list(outlet.risks),
        "input_tiers": input_tiers,
        "result_tier": worst_tier(*input_tiers.values()),
        "source_ids": source_ids,
        # Which outlet's price this one follows (None once the user enters its own price).
        "price_from": None if ov.price is not None else outlet.price_from,
        "price_premium": 0.0 if ov.price is not None else outlet.price_premium,
    }


def evaluate(
    lot: Lot,
    outlets: dict[str, Outlet],
    overrides: dict[str, Override] | None = None,
    baseline_id: str = BASELINE_ID,
    rules: QualityRules = HULL_RULES,
    nutrients: dict[str, NutrientContent] | None = None,
    moisture_default: float | None = None,
) -> pd.DataFrame:
    """One row per outlet, ranked: eligible by expected net, then ineligible, then needs-quote."""
    overrides = overrides or {}
    if baseline_id not in outlets:
        raise ValueError(f"baseline outlet {baseline_id!r} not found")
    rows = [_value_outlet(o, lot, outlets, overrides, rules, nutrients, moisture_default)
            for o in outlets.values()]
    df = pd.DataFrame(rows)

    base = df.loc[df.outlet_id == baseline_id].iloc[0]
    if base.needs_quote:
        raise ValueError(f"baseline outlet {baseline_id!r} has no price")
    base_net = base.net_expected

    df["is_baseline"] = df.outlet_id == baseline_id
    # Sold at exactly the baseline's price (e.g. cleaned hulls to the same dairy): a change in
    # that price moves both equally, so a break-even *price* says nothing useful for them.
    df["price_follows_baseline"] = (df.price_from == baseline_id) & (df.price_premium == 0)
    df["total_low"] = df.net_low * lot.tons
    df["total_expected"] = df.net_expected * lot.tons
    df["total_high"] = df.net_high * lot.tons
    df["vs_baseline_per_ton"] = df.net_expected - base_net
    df["vs_baseline_total"] = df.vs_baseline_per_ton * lot.tons
    # Price per ton of product at which expected net equals the baseline; defined even when a
    # quote is missing.
    df["break_even_price"] = (base_net + df.processing_cost + df.haul_cost
                              - df.coproduct_expected) / df.mass_yield

    df["_sort_net"] = df.net_expected.fillna(-math.inf)
    df = df.sort_values(
        ["needs_quote", "eligible", "_sort_net"], ascending=[True, False, False], kind="stable"
    ).drop(columns="_sort_net")
    df.insert(0, "rank", range(1, len(df) + 1))
    return df.reset_index(drop=True)


def any_placeholder(results: pd.DataFrame) -> bool:
    """True if any placeholder value affects a shown result (drives the demo-data banner)."""
    return bool((results.result_tier == "placeholder").any())


def evaluate_biomass(lot: Lot, bm: Any, overrides: dict[str, Override] | None = None,
                     outlets: dict[str, Outlet] | None = None) -> pd.DataFrame:
    """evaluate() with a biomass profile's outlets, baseline, quality rules and nutrients."""
    return evaluate(lot, outlets or bm.outlets, overrides, baseline_id=bm.baseline, rules=bm.rules,
                    nutrients=bm.nutrients, moisture_default=bm.moisture_default)

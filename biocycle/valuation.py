"""Deterministic net-value-per-ton math for each outlet, compared to the local dairy baseline.

Formula per outlet (all $/ton of hulls, r = haul rate $/ton-mile):
    net = price - processing_cost - r * miles + sum(coproduct value per ton)
Low/high come from the price and coproduct ranges; expected is the midpoint.
Haul rate, miles, and processing cost are point values.

Recovered-kernel mass (1-1.3% of the pile) is NOT subtracted from the hull tons sold;
the effect is negligible for a demo estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from biocycle.config import (
    BASELINE_ID,
    CRUDE_FIBER_MAX,
    DEFAULT_HAUL_RATE,
    LB_PER_TON,
    MOISTURE_TYPICAL_RANGE,
)
from biocycle.data import Outlet, worst_tier

NAN = float("nan")


@dataclass(frozen=True)
class Lot:
    tons: float
    haul_rate: float = DEFAULT_HAUL_RATE
    haul_rate_tier: str = "sourced"  # set to "user" when entered in the UI
    moisture_pct: float | None = None
    crude_fiber_pct: float | None = None
    aflatoxin_tested: str = "unknown"  # yes | no | unknown


@dataclass(frozen=True)
class Override:
    """User-entered values for one outlet. A price override collapses low/high to one value."""

    price: float | None = None
    miles: float | None = None
    processing_cost: float | None = None


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


def coproduct_range(outlet: Outlet) -> tuple[float, float]:
    """($/ton low, $/ton high) of all coproducts recovered from one ton of hulls."""
    low = sum(LB_PER_TON * c.yield_low * c.unit_price_per_lb for c in outlet.coproducts)
    high = sum(LB_PER_TON * c.yield_high * c.unit_price_per_lb for c in outlet.coproducts)
    return low, high


def quality_flags(outlet: Outlet, lot: Lot) -> tuple[list[str], bool]:
    """(flags, eligible). Only a failed feed standard makes an outlet ineligible."""
    flags: list[str] = []
    eligible = True
    if outlet.market == "feed":
        if lot.crude_fiber_pct is not None and lot.crude_fiber_pct >= CRUDE_FIBER_MAX:
            eligible = False
            flags.append(
                f"Ineligible as feed: crude fiber {lot.crude_fiber_pct:g}% is at or above "
                f"the {CRUDE_FIBER_MAX:g}% standard [wcngg2022]."
            )
        lo, hi = MOISTURE_TYPICAL_RANGE
        if lot.moisture_pct is not None and not lo <= lot.moisture_pct <= hi:
            flags.append(
                f"Info: moisture {lot.moisture_pct:g}% is outside the typical {lo:g}–{hi:g}% "
                f"(88–91% dry matter) [wcngg2022]."
            )
    if outlet.market in ("feed", "food") and lot.aflatoxin_tested != "yes":
        flags.append("Warning: aflatoxin not tested. Test before selling for feed or food.")
    return flags, eligible


def _value_outlet(
    outlet: Outlet, lot: Lot, outlets: dict[str, Outlet], overrides: dict[str, Override]
) -> dict[str, Any]:
    ov = overrides.get(outlet.id, Override())
    price = resolve_price(outlet, outlets, overrides)
    miles = ov.miles if ov.miles is not None else outlet.default_miles
    processing = (
        ov.processing_cost if ov.processing_cost is not None else outlet.processing_cost_per_ton
    )
    haul_cost = lot.haul_rate * miles
    cp_low, cp_high = coproduct_range(outlet)

    input_tiers = {
        "price": price.tier,
        "processing_cost": "user" if ov.processing_cost is not None else outlet.processing_tier,
        "miles": "user" if ov.miles is not None else outlet.miles_tier,
        "haul_rate": lot.haul_rate_tier,
    }
    for c in outlet.coproducts:
        input_tiers[f"coproduct: {c.name}"] = c.tier

    flags, eligible = quality_flags(outlet, lot)
    if price.missing:
        flags.insert(0, "needs quote")
        net_low = net_high = NAN
    else:
        net_low = price.low - processing - haul_cost + cp_low
        net_high = price.high - processing - haul_cost + cp_high

    source_ids = sorted(
        set(outlet.source_ids)
        | set(price.source_ids)
        | {s for c in outlet.coproducts for s in c.source_ids}
    )
    return {
        "outlet_id": outlet.id,
        "name": outlet.name,
        "status": outlet.status,
        "market": outlet.market,
        "needs_quote": price.missing,
        "eligible": eligible,
        "price_low": NAN if price.missing else price.low,
        "price_high": NAN if price.missing else price.high,
        "processing_cost": processing,
        "miles": miles,
        "haul_cost": haul_cost,
        "coproduct_low": cp_low,
        "coproduct_high": cp_high,
        "coproduct_expected": (cp_low + cp_high) / 2,
        "net_low": net_low,
        "net_expected": (net_low + net_high) / 2,
        "net_high": net_high,
        "flags": flags,
        "requirements": list(outlet.requirements),
        "risks": list(outlet.risks),
        "input_tiers": input_tiers,
        "result_tier": worst_tier(*input_tiers.values()),
        "source_ids": source_ids,
    }


def evaluate(
    lot: Lot,
    outlets: dict[str, Outlet],
    overrides: dict[str, Override] | None = None,
    baseline_id: str = BASELINE_ID,
) -> pd.DataFrame:
    """One row per outlet, ranked: eligible by expected net, then ineligible, then needs-quote."""
    overrides = overrides or {}
    if baseline_id not in outlets:
        raise ValueError(f"baseline outlet {baseline_id!r} not found")
    rows = [_value_outlet(o, lot, outlets, overrides) for o in outlets.values()]
    df = pd.DataFrame(rows)

    base = df.loc[df.outlet_id == baseline_id].iloc[0]
    if base.needs_quote:
        raise ValueError(f"baseline outlet {baseline_id!r} has no price")
    base_net = base.net_expected

    df["is_baseline"] = df.outlet_id == baseline_id
    df["total_low"] = df.net_low * lot.tons
    df["total_expected"] = df.net_expected * lot.tons
    df["total_high"] = df.net_high * lot.tons
    df["vs_baseline_per_ton"] = df.net_expected - base_net
    df["vs_baseline_total"] = df.vs_baseline_per_ton * lot.tons
    # Price at which expected net equals the baseline; defined even when a quote is missing.
    df["break_even_price"] = base_net + df.processing_cost + df.haul_cost - df.coproduct_expected

    df["_sort_net"] = df.net_expected.fillna(-math.inf)
    df = df.sort_values(
        ["needs_quote", "eligible", "_sort_net"], ascending=[True, False, False], kind="stable"
    ).drop(columns="_sort_net")
    df.insert(0, "rank", range(1, len(df) + 1))
    return df.reset_index(drop=True)


def any_placeholder(results: pd.DataFrame) -> bool:
    """True if any placeholder value affects a shown result (drives the demo-data banner)."""
    return bool((results.result_tier == "placeholder").any())

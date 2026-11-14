"""Split a lot across buyers with a linear program (scipy linprog / HiGHS).

maximize   sum_b margin_b * y_b
subject to sum_b y_b <= T (or == T when the lot must be placed),  0 <= y_b <= max_tons_b
where margin_b = price_b * mass_yield - processing_b - r * miles_b * haul_tons + coproduct_b
($/ton of input; y_b and max_tons_b are tons of input material).

A perishable residue whose options all cost money (disposal fees) would otherwise be "best"
left unplaced, so `must_place` requires the whole lot to go out when buyers can take it.

Processing and coproduct values come from the buyer's outlet in the valuation results, so
UI overrides carry through. Buyers whose outlet fails a quality requirement get no tons.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from biocycle.data import Buyer, worst_tier
from biocycle.valuation import Lot


@dataclass(frozen=True)
class LPSolution:
    tons: np.ndarray
    value: float
    capacity_marginals: np.ndarray  # $ gained per extra ton of each buyer's capacity
    lot_marginal: float  # $ gained per extra ton of material in the lot


def solve_lp(margins: np.ndarray, capacities: np.ndarray, total_tons: float,
             must_place: bool = False) -> LPSolution:
    """Maximize margins @ y with sum(y) <= total_tons and 0 <= y <= capacities.

    must_place: sum(y) == total_tons when the capacities allow it; otherwise every buyer is
    filled to capacity (the rest stays unplaced).
    """
    if len(margins) == 0:
        return LPSolution(np.zeros(0), 0.0, np.zeros(0), 0.0)
    caps = np.asarray(capacities, dtype=float)
    ones = np.ones((1, len(margins)))
    equality = must_place and caps.sum() >= total_tons - 1e-9
    if must_place and not equality:
        total_tons = float(caps.sum())
        equality = True
    res = linprog(
        c=-np.asarray(margins, dtype=float),  # linprog minimizes
        A_ub=None if equality else ones, b_ub=None if equality else [total_tons],
        A_eq=ones if equality else None, b_eq=[total_tons] if equality else None,
        bounds=list(zip(np.zeros(len(margins)), caps)),
        method="highs",
    )
    if res.status != 0:
        raise ValueError(f"allocation LP failed: {res.message}")
    # HiGHS duals are for the minimization; negate to get value per extra unit.
    lot_dual = res.eqlin.marginals[0] if equality else res.ineqlin.marginals[0]
    return LPSolution(
        tons=res.x,
        value=-res.fun,
        capacity_marginals=-res.upper.marginals,
        lot_marginal=-lot_dual,
    )


@dataclass(frozen=True)
class Allocation:
    table: pd.DataFrame  # one row per buyer
    total_value: float
    tons_allocated: float
    tons_unallocated: float
    lot_marginal: float
    result_tier: str
    must_place: bool = False


def buyer_margins(buyers: list[Buyer], lot: Lot, results: pd.DataFrame,
                  tiers: list[str]) -> pd.DataFrame:
    by_outlet = results.set_index("outlet_id")
    rows = []
    for b, tier in zip(buyers, tiers):
        o = by_outlet.loc[b.outlet_id]
        y, h = float(o["mass_yield"]), float(o["haul_tons_per_ton"])
        haul = lot.haul_cost(b.miles) * h
        coproduct = float(o["coproduct_expected"])
        processing = float(o["processing_cost"])
        rows.append({
            "buyer": b.buyer,
            "outlet_id": b.outlet_id,
            "outlet": o["name"],
            "price_per_ton": b.price_per_ton,
            "processing_cost": processing,
            "haul_cost": haul,
            "miles": b.miles,
            "coproduct_value": coproduct,
            "margin_per_ton": b.price_per_ton * y - processing - haul + coproduct,
            "max_tons": b.max_tons,
            "eligible": bool(o["eligible"]),
            "buyer_tier": tier,
            # The buyer's quote and miles replace the outlet's; every other input still counts.
            "outlet_tier": worst_tier(
                *(t for k, t in o["input_tiers"].items() if k not in ("price", "miles"))),
        })
    return pd.DataFrame(rows)


def allocate(buyers: list[Buyer], lot: Lot, results: pd.DataFrame,
             tiers: list[str] | None = None, must_place: bool = False) -> Allocation:
    """Best split of lot.tons across buyers. `tiers` gives each buyer row's data tier."""
    tiers = tiers or ["placeholder"] * len(buyers)
    df = buyer_margins(buyers, lot, results, tiers)
    if df.empty:
        return Allocation(df, 0.0, 0.0, lot.tons, 0.0, "sourced", must_place)

    capacities = np.where(df["eligible"], df["max_tons"], 0.0)
    sol = solve_lp(df["margin_per_ton"].to_numpy(), capacities, lot.tons, must_place)
    df["tons"] = sol.tons
    df["value"] = df["tons"] * df["margin_per_ton"]
    df["capacity_marginal"] = np.where(df["eligible"], sol.capacity_marginals, 0.0)
    df = df.sort_values("margin_per_ton", ascending=False, kind="stable").reset_index(drop=True)

    used = df[df["tons"] > 1e-9]
    tier = worst_tier(lot.haul_rate_tier, *used["buyer_tier"], *used["outlet_tier"])
    allocated = float(df["tons"].sum())
    return Allocation(df, sol.value, allocated, lot.tons - allocated, sol.lot_marginal, tier,
                      must_place)

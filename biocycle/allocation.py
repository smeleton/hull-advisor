"""Split a lot across buyers with a linear program (scipy linprog / HiGHS).

maximize   sum_b margin_b * y_b
subject to sum_b y_b <= T,  0 <= y_b <= max_tons_b
where margin_b = price_b - processing_b - r * miles_b + coproduct_b ($/ton).

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
    hull_marginal: float  # $ gained per extra ton of hulls in the lot


def solve_lp(margins: np.ndarray, capacities: np.ndarray, total_tons: float) -> LPSolution:
    """Maximize margins @ y with sum(y) <= total_tons and 0 <= y <= capacities."""
    if len(margins) == 0:
        return LPSolution(np.zeros(0), 0.0, np.zeros(0), 0.0)
    res = linprog(
        c=-np.asarray(margins, dtype=float),  # linprog minimizes
        A_ub=np.ones((1, len(margins))),
        b_ub=[total_tons],
        bounds=list(zip(np.zeros(len(margins)), capacities)),
        method="highs",
    )
    if res.status != 0:
        raise ValueError(f"allocation LP failed: {res.message}")
    # HiGHS duals are for the minimization; negate to get value per extra unit.
    return LPSolution(
        tons=res.x,
        value=-res.fun,
        capacity_marginals=-res.upper.marginals,
        hull_marginal=-res.ineqlin.marginals[0],
    )


@dataclass(frozen=True)
class Allocation:
    table: pd.DataFrame  # one row per buyer
    total_value: float
    tons_allocated: float
    tons_unallocated: float
    hull_marginal: float
    result_tier: str


def buyer_margins(buyers: list[Buyer], lot: Lot, results: pd.DataFrame,
                  tiers: list[str]) -> pd.DataFrame:
    by_outlet = results.set_index("outlet_id")
    rows = []
    for b, tier in zip(buyers, tiers):
        o = by_outlet.loc[b.outlet_id]
        haul = lot.haul_rate * b.miles
        coproduct = float(o["coproduct_expected"])
        processing = float(o["processing_cost"])
        rows.append({
            "buyer": b.buyer,
            "outlet_id": b.outlet_id,
            "outlet": o["name"],
            "price_per_ton": b.price_per_ton,
            "processing_cost": processing,
            "haul_cost": haul,
            "coproduct_value": coproduct,
            "margin_per_ton": b.price_per_ton - processing - haul + coproduct,
            "max_tons": b.max_tons,
            "eligible": bool(o["eligible"]),
            "buyer_tier": tier,
            # The buyer's quote and miles replace the outlet's; every other input still counts.
            "outlet_tier": worst_tier(
                *(t for k, t in o["input_tiers"].items() if k not in ("price", "miles"))),
        })
    return pd.DataFrame(rows)


def allocate(buyers: list[Buyer], lot: Lot, results: pd.DataFrame,
             tiers: list[str] | None = None) -> Allocation:
    """Best split of lot.tons across buyers. `tiers` gives each buyer row's data tier."""
    tiers = tiers or ["placeholder"] * len(buyers)
    df = buyer_margins(buyers, lot, results, tiers)
    if df.empty:
        return Allocation(df, 0.0, 0.0, lot.tons, 0.0, "sourced")

    capacities = np.where(df["eligible"], df["max_tons"], 0.0)
    sol = solve_lp(df["margin_per_ton"].to_numpy(), capacities, lot.tons)
    df["tons"] = sol.tons
    df["value"] = df["tons"] * df["margin_per_ton"]
    df["capacity_marginal"] = np.where(df["eligible"], sol.capacity_marginals, 0.0)
    df = df.sort_values("margin_per_ton", ascending=False, kind="stable").reset_index(drop=True)

    used = df[df["tons"] > 1e-9]
    tier = worst_tier(lot.haul_rate_tier, *used["buyer_tier"], *used["outlet_tier"])
    allocated = float(df["tons"].sum())
    return Allocation(df, sol.value, allocated, lot.tons - allocated, sol.hull_marginal, tier)

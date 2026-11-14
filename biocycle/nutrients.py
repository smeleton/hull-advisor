"""Fertilizer value of the N, P, K and Mg in a lot, by recovery route (data/nutrients.yaml).

    lb of nutrient per ton = 2000 x dry fraction x (% of dry matter / 100) x recovery efficiency
    $ per ton              = sum over nutrients of lb x $ per lb

Low/high pair low efficiency with low price and high with high. The tier is the worst of the
nutrient contents, efficiencies and prices used (and the moisture, when it is a default).
Whether the value counts in an outlet's net is the outlet's call (Outlet.nutrient_credit).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from biocycle.config import DATA_DIR, LB_PER_TON
from biocycle.data import NUTRIENTS, NutrientContent, worst_tier

NUTRIENTS_PATH = DATA_DIR / "nutrients.yaml"


@dataclass(frozen=True)
class NutrientValue:
    route: str
    name: str
    status: str
    lb_low: dict[str, float]
    lb_high: dict[str, float]
    value_low: float  # $ per ton of material
    value_high: float
    tier: str
    source_ids: tuple[str, ...]
    note: str = ""

    @property
    def value_expected(self) -> float:
        return (self.value_low + self.value_high) / 2


@lru_cache
def load_recovery(path: Path = NUTRIENTS_PATH) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    for rid, r in doc["routes"].items():
        if r["n_price"] not in doc["prices_per_lb"]:
            raise ValueError(f"nutrient route {rid!r}: unknown n_price {r['n_price']!r}")
        for n in r["efficiency"]:
            if n not in NUTRIENTS:
                raise ValueError(f"nutrient route {rid!r}: unknown nutrient {n!r}")
    return doc


def route_value(route_id: str, content: dict[str, NutrientContent], dry_fraction: float,
                moisture_tier: str = "sourced", doc: dict[str, Any] | None = None
                ) -> NutrientValue:
    doc = doc or load_recovery()
    r = doc["routes"][route_id]
    prices = doc["prices_per_lb"]
    lb_lo: dict[str, float] = {}
    lb_hi: dict[str, float] = {}
    lo = hi = 0.0
    tiers = [moisture_tier]
    sources = set(r.get("source_ids") or ())
    for n, eff in r["efficiency"].items():
        c = content.get(n)
        if c is None:
            continue
        price = prices[r["n_price"] if n == "N" else n]
        base = LB_PER_TON * dry_fraction * c.pct_dry / 100
        lb_lo[n], lb_hi[n] = base * eff["low"], base * eff["high"]
        lo += lb_lo[n] * price["low"]
        hi += lb_hi[n] * price["high"]
        tiers += [c.tier, eff.get("tier", "sourced"), price.get("tier", "sourced")]
        sources |= {s for s in (c.source_id, eff.get("source_id"), price.get("source_id")) if s}
    return NutrientValue(route_id, r["name"], r.get("status", "emerging"), lb_lo, lb_hi, lo, hi,
                         worst_tier(*tiers), tuple(sorted(sources)), r.get("note", ""))

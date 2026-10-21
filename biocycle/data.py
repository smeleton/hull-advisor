"""Load and validate sources, outlets, and sample buyers into dataclasses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml

from biocycle.config import DATA_DIR

Tier = Literal["sourced", "user", "placeholder"]
TIERS: tuple[str, ...] = ("sourced", "user", "placeholder")  # least to most severe
STATUSES = ("established", "emerging", "experimental")
MARKETS = ("feed", "food", "soil", "energy")


def worst_tier(*tiers: str) -> str:
    """Most severe tier among inputs: placeholder > user > sourced."""
    if not tiers:
        return "sourced"
    return max(tiers, key=TIERS.index)


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    url: str
    what_it_supports: str


@dataclass(frozen=True)
class Coproduct:
    name: str
    yield_low: float  # fraction of hull weight
    yield_high: float
    yield_tier: str
    unit_price_per_lb: float
    unit_price_tier: str
    source_ids: tuple[str, ...] = ()

    @property
    def tier(self) -> str:
        return worst_tier(self.yield_tier, self.unit_price_tier)


@dataclass(frozen=True)
class Outlet:
    id: str
    name: str
    description: str
    status: str
    market: str
    price_low: float | None
    price_high: float | None
    price_tier: str
    processing_cost_per_ton: float
    processing_tier: str
    default_miles: float
    miles_tier: str
    price_from: str | None = None
    price_premium: float = 0.0
    premium_tier: str = "sourced"
    coproducts: tuple[Coproduct, ...] = ()
    requirements: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Buyer:
    buyer: str
    outlet_id: str
    price_per_ton: float
    max_tons: float
    miles: float
    notes: str = ""


# ---------- validation helpers ----------


def _check_choice(value: str, allowed: tuple[str, ...], what: str, owner: str) -> None:
    if value not in allowed:
        raise ValueError(f"{owner}: invalid {what} {value!r}; expected one of {allowed}")


def _check_sources(ids: tuple[str, ...], known: dict[str, Source], owner: str) -> None:
    unknown = [s for s in ids if s not in known]
    if unknown:
        raise ValueError(f"{owner}: unknown source_ids {unknown}")


def _check_non_negative(value: float | None, what: str, owner: str) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{owner}: {what} must be >= 0, got {value}")


def _read_yaml_list(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        items = yaml.safe_load(f) or []
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected a list of entries")
    return items


# ---------- loaders ----------


def load_sources(path: Path = DATA_DIR / "sources.yaml") -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for raw in _read_yaml_list(path):
        src = Source(**raw)
        if src.id in sources:
            raise ValueError(f"duplicate source id {src.id!r}")
        sources[src.id] = src
    return sources


def _parse_coproduct(raw: dict[str, Any], sources: dict[str, Source], owner: str) -> Coproduct:
    cp = Coproduct(
        name=raw["name"],
        yield_low=float(raw["yield_low"]),
        yield_high=float(raw["yield_high"]),
        yield_tier=raw["yield_tier"],
        unit_price_per_lb=float(raw["unit_price_per_lb"]),
        unit_price_tier=raw["unit_price_tier"],
        source_ids=tuple(raw.get("source_ids") or ()),
    )
    where = f"{owner} coproduct {cp.name!r}"
    _check_choice(cp.yield_tier, TIERS, "yield_tier", where)
    _check_choice(cp.unit_price_tier, TIERS, "unit_price_tier", where)
    _check_sources(cp.source_ids, sources, where)
    if not 0 <= cp.yield_low <= cp.yield_high <= 1:
        raise ValueError(f"{where}: need 0 <= yield_low <= yield_high <= 1")
    _check_non_negative(cp.unit_price_per_lb, "unit_price_per_lb", where)
    return cp


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_outlet(raw: dict[str, Any], sources: dict[str, Source]) -> Outlet:
    owner = f"outlet {raw.get('id')!r}"
    outlet = Outlet(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        status=raw["status"],
        market=raw["market"],
        price_low=_opt_float(raw.get("price_low")),
        price_high=_opt_float(raw.get("price_high")),
        price_tier=raw.get("price_tier", "sourced"),
        processing_cost_per_ton=float(raw["processing_cost_per_ton"]),
        processing_tier=raw["processing_tier"],
        default_miles=float(raw["default_miles"]),
        miles_tier=raw["miles_tier"],
        price_from=raw.get("price_from"),
        price_premium=float(raw.get("price_premium") or 0.0),
        premium_tier=raw.get("premium_tier", "sourced"),
        coproducts=tuple(_parse_coproduct(c, sources, owner) for c in raw.get("coproducts") or ()),
        requirements=tuple(raw.get("requirements") or ()),
        risks=tuple(raw.get("risks") or ()),
        source_ids=tuple(raw.get("source_ids") or ()),
    )
    _check_choice(outlet.status, STATUSES, "status", owner)
    _check_choice(outlet.market, MARKETS, "market", owner)
    for name in ("price_tier", "processing_tier", "miles_tier", "premium_tier"):
        _check_choice(getattr(outlet, name), TIERS, name, owner)
    _check_sources(outlet.source_ids, sources, owner)
    _check_non_negative(outlet.price_low, "price_low", owner)
    _check_non_negative(outlet.processing_cost_per_ton, "processing_cost_per_ton", owner)
    _check_non_negative(outlet.default_miles, "default_miles", owner)
    if outlet.price_high is not None and outlet.price_low is None:
        raise ValueError(f"{owner}: price_high given without price_low")
    if outlet.price_low is not None and outlet.price_high is not None:
        if outlet.price_low > outlet.price_high:
            raise ValueError(f"{owner}: price_low > price_high")
    if outlet.price_from and outlet.price_low is not None:
        raise ValueError(f"{owner}: use either price_from or price_low/high, not both")
    return outlet


def load_outlets(
    sources: dict[str, Source], path: Path = DATA_DIR / "outlets.yaml"
) -> dict[str, Outlet]:
    outlets: dict[str, Outlet] = {}
    for raw in _read_yaml_list(path):
        outlet = parse_outlet(raw, sources)
        if outlet.id in outlets:
            raise ValueError(f"duplicate outlet id {outlet.id!r}")
        outlets[outlet.id] = outlet
    validate_outlet_refs(outlets)
    return outlets


def validate_outlet_refs(outlets: dict[str, Outlet]) -> None:
    """price_from must point at an existing outlet that has its own price (no chains)."""
    for o in outlets.values():
        if o.price_from is None:
            continue
        ref = outlets.get(o.price_from)
        if ref is None:
            raise ValueError(f"outlet {o.id!r}: price_from {o.price_from!r} does not exist")
        if ref.price_from is not None:
            raise ValueError(f"outlet {o.id!r}: price_from {ref.id!r} must have its own price")


def load_buyers(
    outlets: dict[str, Outlet], path: Path = DATA_DIR / "buyers_sample.csv"
) -> list[Buyer]:
    df = pd.read_csv(path).fillna({"notes": ""})
    buyers = [
        Buyer(
            buyer=str(r.buyer),
            outlet_id=str(r.outlet_id),
            price_per_ton=float(r.price_per_ton),
            max_tons=float(r.max_tons),
            miles=float(r.miles),
            notes=str(r.notes),
        )
        for r in df.itertuples(index=False)
    ]
    for b in buyers:
        owner = f"buyer {b.buyer!r}"
        if b.outlet_id not in outlets:
            raise ValueError(f"{owner}: unknown outlet_id {b.outlet_id!r}")
        for what in ("price_per_ton", "max_tons", "miles"):
            _check_non_negative(getattr(b, what), what, owner)
    return buyers

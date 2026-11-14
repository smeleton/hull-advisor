"""Load and validate the literature evidence (data/evidence.yaml) and the math models built on it
(data/models.yaml), including a small unit check so a model can't silently multiply apples by
oranges.

Units are strings like "GJ/tonne_biooil" or "t_protein/t_dry": tokens joined by "*", one "/".
`unit_product` multiplies units by adding token exponents, so
    t_biooil/t_dry × GJ/tonne_biooil × tonne_biooil/t_biooil  ->  GJ/t_dry.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from biocycle.config import DATA_DIR, LB_PER_TON
from biocycle.data import TIERS, Source, _check_choice, _read_yaml_list

EVIDENCE_PATH = DATA_DIR / "evidence.yaml"
MODELS_PATH = DATA_DIR / "models.yaml"

EVIDENCE_LEVELS = ("database", "review", "animal_trial", "in_vitro", "lab", "thesis",
                   "extension", "market_report")
# Short, plain labels for the UI, strongest first in the sense of "closest to a real farm sale".
LEVEL_LABEL = {
    "market_report": "Market report", "animal_trial": "Animal trial", "database": "Feed table",
    "review": "Review", "extension": "Extension", "in_vitro": "In vitro", "lab": "Lab study",
    "thesis": "Thesis",
}

TONNE_PER_SHORT_TON = 0.90718474
LB_PER_BU_CORN = 56.0  # standard bushel weight for shelled corn


# ---------- units ----------


def parse_unit(unit: str) -> Counter:
    """'a*b/c' -> Counter({'a': 1, 'b': 1, 'c': -1}). '1' or '' is dimensionless."""
    if unit.count("/") > 1:
        raise ValueError(f"unit {unit!r}: use at most one '/'")
    num, _, den = unit.partition("/")
    exps: Counter = Counter()
    for tok in filter(None, num.split("*")):
        if tok != "1":
            exps[tok] += 1
    for tok in filter(None, den.split("*")):
        exps[tok] -= 1
    return exps


def format_unit(exps: Counter) -> str:
    num = sorted(t for t, e in exps.items() for _ in range(max(e, 0)))
    den = sorted(t for t, e in exps.items() for _ in range(max(-e, 0)))
    n = "*".join(num) or "1"
    return f"{n}/{'*'.join(den)}" if den else n


def unit_product(units: list[str], divide: list[str] = ()) -> str:
    total: Counter = Counter()
    for u in units:
        total.update(parse_unit(u))
    for u in divide:
        total.subtract(parse_unit(u))
    return format_unit(total)


def same_unit(a: str, b: str) -> bool:
    return {k: v for k, v in parse_unit(a).items() if v} == {
        k: v for k, v in parse_unit(b).items() if v}


def conversion_factor(unit: str) -> float:
    """Value of a pure unit-conversion factor such as 'tonne_dry/t_dry' (metric per short ton)."""
    if re.fullmatch(r"tonne_(\w+)/t_\1", unit):
        return TONNE_PER_SHORT_TON
    if unit == "bu_corn/t_corn":
        return LB_PER_TON / LB_PER_BU_CORN
    raise ValueError(f"unknown conversion {unit!r}")


# ---------- records ----------


@dataclass(frozen=True)
class Evidence:
    id: str
    source_id: str
    pathways: tuple[str, ...]
    parameter: str
    value_low: float
    value_high: float
    unit: str
    reported: str
    locator: str
    evidence_level: str
    feedstock: str
    conditions: str = ""
    notes: str = ""

    @property
    def single(self) -> bool:
        return self.value_low == self.value_high


@dataclass(frozen=True)
class Product:
    name: str
    amount: tuple[str, ...]          # evidence ids, multiplied
    convert: tuple[str, ...]         # conversion-factor units
    price_unit: str
    price_low: float | None = None
    price_high: float | None = None
    price_tier: str = "placeholder"
    price_multiply: tuple[str, ...] = ()  # evidence-derived price: product of these ...
    price_divide: tuple[str, ...] = ()    # ... divided by these
    price_note: str = ""

    @property
    def price_from_evidence(self) -> bool:
        return bool(self.price_multiply)

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return (*self.amount, *self.price_multiply, *self.price_divide)


@dataclass(frozen=True)
class ConversionModel:
    id: str
    name: str
    summary: str
    products: tuple[Product, ...]
    conversion_cost_per_dry_ton: float
    conversion_cost_tier: str
    conversion_cost_note: str = ""


@dataclass(frozen=True)
class MaterialModel:
    """A product with no market price: the model gives the price it would need instead."""
    id: str
    name: str
    summary: str
    product: str
    amount: tuple[str, ...]
    product_unit: str
    conversion_cost_per_dry_ton: float
    conversion_cost_tier: str
    conversion_cost_note: str = ""


@dataclass(frozen=True)
class FeedRef:
    name: str
    nel: str
    cp: str
    dm: str
    price: str
    price_convert: tuple[str, ...] = ()


@dataclass(frozen=True)
class HullScenario:
    name: str
    nel: str
    cp: str
    dm: str


@dataclass(frozen=True)
class FeedValueSpec:
    method_source: str
    corn: FeedRef
    sbm: FeedRef
    scenarios: tuple[HullScenario, ...]


@dataclass(frozen=True)
class Models:
    dry_matter: str
    conversion: dict[str, ConversionModel]
    feed_value: FeedValueSpec
    evidence: dict[str, Evidence] = field(repr=False, default_factory=dict)
    material: dict[str, MaterialModel] = field(default_factory=dict)


# ---------- loaders ----------


def load_evidence(sources: dict[str, Source], path: Path = EVIDENCE_PATH) -> dict[str, Evidence]:
    out: dict[str, Evidence] = {}
    for raw in _read_yaml_list(path):
        ev = Evidence(
            id=raw["id"], source_id=raw["source_id"], pathways=tuple(raw.get("pathways") or ()),
            parameter=raw["parameter"], value_low=float(raw["value_low"]),
            value_high=float(raw["value_high"]), unit=str(raw["unit"]),
            reported=str(raw["reported"]), locator=str(raw["locator"]),
            evidence_level=raw["evidence_level"], feedstock=str(raw["feedstock"]),
            conditions=str(raw.get("conditions") or ""), notes=str(raw.get("notes") or ""),
        )
        owner = f"evidence {ev.id!r}"
        if ev.id in out:
            raise ValueError(f"duplicate evidence id {ev.id!r}")
        if ev.source_id not in sources:
            raise ValueError(f"{owner}: unknown source_id {ev.source_id!r}")
        _check_choice(ev.evidence_level, EVIDENCE_LEVELS, "evidence_level", owner)
        if ev.value_low > ev.value_high:
            raise ValueError(f"{owner}: value_low > value_high")
        if not ev.reported.strip() or not ev.locator.strip():
            raise ValueError(f"{owner}: needs 'reported' and 'locator' so it can be checked")
        parse_unit(ev.unit)
        out[ev.id] = ev
    return out


def _need(ev: dict[str, Evidence], eid: str, owner: str) -> Evidence:
    if eid not in ev:
        raise ValueError(f"{owner}: unknown evidence id {eid!r}")
    return ev[eid]


def _parse_product(raw: dict[str, Any], ev: dict[str, Evidence], owner: str) -> Product:
    pf = raw.get("price_from") or {}
    p = Product(
        name=raw["name"], amount=tuple(raw["amount"]), convert=tuple(raw.get("convert") or ()),
        price_unit=raw["price_unit"],
        price_low=None if raw.get("price_low") is None else float(raw["price_low"]),
        price_high=None if raw.get("price_high") is None else float(raw["price_high"]),
        price_tier=raw.get("price_tier", "sourced" if pf else "placeholder"),
        price_multiply=tuple(pf.get("multiply") or ()), price_divide=tuple(pf.get("divide") or ()),
        price_note=str(raw.get("price_note") or ""),
    )
    where = f"{owner} product {p.name!r}"
    _check_choice(p.price_tier, TIERS, "price_tier", where)
    for eid in p.evidence_ids:
        _need(ev, eid, where)
    for c in p.convert:
        conversion_factor(c)
    money, _, per = p.price_unit.partition("/")
    if money != "usd" or not per:
        raise ValueError(f"{where}: price_unit must look like 'usd/<thing>'")
    got = unit_product([ev[e].unit for e in p.amount] + list(p.convert))
    want = f"{per}/t_dry"
    if not same_unit(got, want):
        raise ValueError(f"{where}: amount units multiply to {got!r}, expected {want!r}")
    if p.price_from_evidence:
        if p.price_low is not None:
            raise ValueError(f"{where}: give price_low/high or price_from, not both")
        got = unit_product([ev[e].unit for e in p.price_multiply],
                           [ev[e].unit for e in p.price_divide])
        if not same_unit(got, p.price_unit):
            raise ValueError(f"{where}: price_from units give {got!r}, expected {p.price_unit!r}")
    else:
        if p.price_low is None:
            raise ValueError(f"{where}: needs price_low/high or price_from")
        high = p.price_high if p.price_high is not None else p.price_low
        if p.price_low > high:
            raise ValueError(f"{where}: price_low > price_high")
    return p


def _check_unit(ev: Evidence, want: str, where: str) -> None:
    if not same_unit(ev.unit, want):
        raise ValueError(f"{where}: evidence {ev.id!r} has unit {ev.unit!r}, expected {want!r}")


def _check_unit_like(ev: Evidence, pattern: str, where: str) -> None:
    if not re.fullmatch(pattern, ev.unit):
        raise ValueError(f"{where}: evidence {ev.id!r} unit {ev.unit!r} doesn't match {pattern}")


def _feed_ref(raw: dict[str, Any], ev: dict[str, Evidence], sources: dict[str, Source],
              owner: str) -> FeedRef:
    ref = FeedRef(name=raw["name"], nel=raw["nel"], cp=raw["cp"], dm=raw["dm"],
                  price=raw["price"], price_convert=tuple(raw.get("price_convert") or ()))
    where = f"{owner} {ref.name!r}"
    _check_unit(_need(ev, ref.nel, where), "Mcal/kg_dm", where)
    _check_unit_like(_need(ev, ref.cp, where), r"t_protein/t_\w+dm", where)
    dm = _need(ev, ref.dm, where)
    _check_unit_like(dm, r"t_(\w+)dm/t_\1", where)
    material = dm.unit.split("/")[1]  # e.g. t_corn
    price = _need(ev, ref.price, where)
    for c in ref.price_convert:
        conversion_factor(c)
    got = unit_product([price.unit, *ref.price_convert])
    if not same_unit(got, f"usd/{material}"):
        raise ValueError(f"{where}: price units give {got!r}, expected 'usd/{material}'")
    return ref


def load_models(sources: dict[str, Source], path: Path = MODELS_PATH,
                evidence_path: Path = EVIDENCE_PATH) -> Models:
    ev = load_evidence(sources, evidence_path)
    raw = yaml.safe_load(Path(path).read_text()) or {}
    dm = _need(ev, raw["dry_matter"], "models dry_matter")
    _check_unit(dm, "t_dry/t_hull", "models dry_matter")

    conversion: dict[str, ConversionModel] = {}
    for m in raw.get("conversion_models") or []:
        owner = f"model {m.get('id')!r}"
        cm = ConversionModel(
            id=m["id"], name=m["name"], summary=str(m.get("summary") or "").strip(),
            products=tuple(_parse_product(p, ev, owner) for p in m["products"]),
            conversion_cost_per_dry_ton=float(m["conversion_cost_per_dry_ton"]),
            conversion_cost_tier=m["conversion_cost_tier"],
            conversion_cost_note=str(m.get("conversion_cost_note") or ""),
        )
        _check_choice(cm.conversion_cost_tier, TIERS, "conversion_cost_tier", owner)
        if cm.id in conversion:
            raise ValueError(f"duplicate model id {cm.id!r}")
        if not cm.products:
            raise ValueError(f"{owner}: needs at least one product")
        conversion[cm.id] = cm

    fv = raw["feed_value"]
    if fv["method_source"] not in sources:
        raise ValueError(f"feed_value: unknown method_source {fv['method_source']!r}")
    refs = fv["reference_feeds"]
    scenarios = []
    for s in fv["hull_scenarios"]:
        hs = HullScenario(name=s["name"], nel=s["nel"], cp=s["cp"], dm=s["dm"])
        where = f"feed_value scenario {hs.name!r}"
        _check_unit(_need(ev, hs.nel, where), "Mcal/kg_dm", where)
        _check_unit(_need(ev, hs.cp, where), "t_protein/t_dry", where)
        _check_unit(_need(ev, hs.dm, where), "t_dry/t_hull", where)
        scenarios.append(hs)
    if not scenarios:
        raise ValueError("feed_value: needs at least one hull scenario")
    spec = FeedValueSpec(
        method_source=fv["method_source"],
        corn=_feed_ref(refs["corn"], ev, sources, "feed_value reference"),
        sbm=_feed_ref(refs["sbm"], ev, sources, "feed_value reference"),
        scenarios=tuple(scenarios),
    )
    material: dict[str, MaterialModel] = {}
    for m in raw.get("material_models") or []:
        owner = f"material model {m.get('id')!r}"
        mm = MaterialModel(
            id=m["id"], name=m["name"], summary=str(m.get("summary") or "").strip(),
            product=m["product"], amount=tuple(m["amount"]), product_unit=m["product_unit"],
            conversion_cost_per_dry_ton=float(m["conversion_cost_per_dry_ton"]),
            conversion_cost_tier=m["conversion_cost_tier"],
            conversion_cost_note=str(m.get("conversion_cost_note") or ""),
        )
        _check_choice(mm.conversion_cost_tier, TIERS, "conversion_cost_tier", owner)
        for eid in mm.amount:
            _need(ev, eid, owner)
        got = unit_product([ev[e].unit for e in mm.amount])
        if not same_unit(got, f"{mm.product_unit}/t_dry"):
            raise ValueError(f"{owner}: amount units give {got!r}, expected "
                             f"'{mm.product_unit}/t_dry'")
        if mm.id in material or mm.id in conversion:
            raise ValueError(f"duplicate model id {mm.id!r}")
        material[mm.id] = mm
    return Models(dry_matter=raw["dry_matter"], conversion=conversion, feed_value=spec,
                  evidence=ev, material=material)

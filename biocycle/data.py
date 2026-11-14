"""Load and validate sources, biomass profiles, outlets, and sample buyers into dataclasses.

Each biomass (almond hulls, grape pomace, ...) lives in data/biomass/<id>.yaml: a profile
(names, moisture, composition, quality rules, which outlet is today's usual route) plus its
outlets. Outlets of one biomass only reference each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml

from biocycle.config import (
    BASELINE_ID,
    CRUDE_FIBER_MAX,
    DATA_DIR,
    DEFAULT_BIOMASS,
    MOISTURE_TYPICAL_RANGE,
)

BIOMASS_DIR = DATA_DIR / "biomass"

Tier = Literal["sourced", "user", "placeholder"]
TIERS: tuple[str, ...] = ("sourced", "user", "placeholder")  # least to most severe
STATUSES = ("established", "emerging", "experimental")
MARKETS = ("feed", "food", "soil", "energy", "chemicals", "materials", "disposal")
MARKET_LABEL = {
    "feed": "Animal feed", "food": "Food ingredient", "soil": "Compost and soil",
    "energy": "Energy and biochar", "chemicals": "Extracts and chemicals",
    "materials": "Bio-based materials", "disposal": "Disposal",
}
# How the material ends up being used; drives the sustainability summary (EPA factor and
# Wasted Food Scale position). Defaults from the market when an outlet doesn't say.
PATHWAYS = ("feed", "food", "extraction", "materials", "compost", "land_application",
            "digestion", "combustion", "biochar", "landfill")
DEFAULT_PATHWAY = {
    "feed": "feed", "food": "food", "soil": "compost", "energy": "combustion",
    "chemicals": "extraction", "materials": "materials", "disposal": "landfill",
}
NUTRIENTS = ("N", "P", "K", "Mg")


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
    # Tons of product sold per ton of input (e.g. 0.25 when 1 wet ton dries to 0.25 t of
    # pellets). The price is per ton of product; every other $/ton is per ton of input.
    mass_yield: float = 1.0
    # True when the product is made on site and the product is hauled; False when the buyer
    # takes the material as is (the default), so the full input weight is hauled.
    haul_product: bool = False
    pathway: str = ""  # see PATHWAYS; "" = default for the market
    # Nutrient recovery routes (ids in data/nutrients.yaml) whose products add value here.
    nutrient_routes: tuple[str, ...] = ()
    # True when the producer keeps the nutrients' fertilizer value (e.g. spreading on their own
    # land), so it counts in the net. Otherwise recoverable nutrients are shown, not counted,
    # because a composter's or digester's gate fee already reflects them.
    nutrient_credit: bool = False

    @property
    def end_use(self) -> str:
        return self.pathway or DEFAULT_PATHWAY[self.market]

    @property
    def haul_tons_per_ton(self) -> float:
        """Tons hauled per ton of input."""
        return self.mass_yield if self.haul_product else 1.0


@dataclass(frozen=True)
class QualityRules:
    """Per-biomass quality checks. Defaults reproduce the almond hull rules."""

    moisture_typical: tuple[float, float] | None = MOISTURE_TYPICAL_RANGE
    moisture_note: str = "(88–91% dry matter) [wcngg2022]"
    moisture_markets: tuple[str, ...] = ("feed",)
    crude_fiber_max: float | None = CRUDE_FIBER_MAX
    crude_fiber_source: str = "wcngg2022"
    mycotoxin: str | None = "aflatoxin"
    mycotoxin_markets: tuple[str, ...] = ("feed", "food")


HULL_RULES = QualityRules()


@dataclass(frozen=True)
class CompositionItem:
    component: str
    value: str
    basis: str  # dry | wet | as-is
    source_id: str | None
    tier: str = "sourced"


@dataclass(frozen=True)
class NutrientContent:
    pct_dry: float
    tier: str
    source_id: str | None = None


@dataclass(frozen=True)
class DirectoryGroup:
    key: str
    title: str
    outlet: str  # the outlet a quote request is for
    outlets: tuple[str, ...]  # outlets that point at this group (to pin it first)
    blurb: str
    empty: str


@dataclass(frozen=True)
class Biomass:
    id: str
    name: str  # "Almond hulls"
    noun: str  # "hulls" in "tons of hulls"
    producers: str  # who has this material, e.g. "Almond hullers and shellers"
    baseline: str  # outlet id of today's usual route
    outlets: dict[str, Outlet]
    rules: QualityRules = HULL_RULES
    unit_label: str = ""
    season: str = ""
    ca_volume: str = ""
    summary: str = ""
    market_data: bool = False  # USDA market data applies (almond hulls only)
    must_place: bool = False  # perishable: the whole lot has to go somewhere
    default_tons: float = 1000.0
    moisture_default: float | None = None  # % as delivered; used for nutrient value
    ef_material: str = "yard_trimmings"  # EPA end-of-use proxy row (emission_factors.yaml)
    composition: tuple[CompositionItem, ...] = ()
    nutrients: dict[str, NutrientContent] = field(default_factory=dict)
    quality_notes: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    directory_groups: tuple[DirectoryGroup, ...] = ()
    source_ids: tuple[str, ...] = ()

    @property
    def baseline_outlet(self) -> Outlet:
        return self.outlets[self.baseline]


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
        mass_yield=float(raw.get("mass_yield", 1.0)),
        haul_product=bool(raw.get("haul_product", False)),
        pathway=raw.get("pathway") or "",
        nutrient_routes=tuple(raw.get("nutrient_routes") or ()),
        nutrient_credit=bool(raw.get("nutrient_credit", False)),
    )
    _check_choice(outlet.status, STATUSES, "status", owner)
    _check_choice(outlet.market, MARKETS, "market", owner)
    if outlet.pathway:
        _check_choice(outlet.pathway, PATHWAYS, "pathway", owner)
    for name in ("price_tier", "processing_tier", "miles_tier", "premium_tier"):
        _check_choice(getattr(outlet, name), TIERS, name, owner)
    _check_sources(outlet.source_ids, sources, owner)
    # Prices may be negative: a disposal or tipping fee the producer pays.
    if not 0 < outlet.mass_yield <= 1:
        raise ValueError(f"{owner}: mass_yield must be in (0, 1], got {outlet.mass_yield}")
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


def _parse_outlet_list(items: list[dict[str, Any]], sources: dict[str, Source]
                       ) -> dict[str, Outlet]:
    outlets: dict[str, Outlet] = {}
    for raw in items:
        outlet = parse_outlet(raw, sources)
        if outlet.id in outlets:
            raise ValueError(f"duplicate outlet id {outlet.id!r}")
        outlets[outlet.id] = outlet
    validate_outlet_refs(outlets)
    return outlets


def load_outlets(
    sources: dict[str, Source], path: Path = BIOMASS_DIR / f"{DEFAULT_BIOMASS}.yaml"
) -> dict[str, Outlet]:
    """Outlets from a biomass file (its `outlets` list) or a plain list of outlets."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or []
    items = doc.get("outlets") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected a list of outlets")
    return _parse_outlet_list(items, sources)


def _parse_rules(raw: dict[str, Any] | None) -> QualityRules:
    if not raw:
        return QualityRules(moisture_typical=None, crude_fiber_max=None, mycotoxin=None)
    mt = raw.get("moisture_typical")
    return QualityRules(
        moisture_typical=(float(mt[0]), float(mt[1])) if mt else None,
        moisture_note=raw.get("moisture_note", ""),
        moisture_markets=tuple(raw.get("moisture_markets") or ("feed",)),
        crude_fiber_max=_opt_float(raw.get("crude_fiber_max")),
        crude_fiber_source=raw.get("crude_fiber_source", ""),
        mycotoxin=raw.get("mycotoxin"),
        mycotoxin_markets=tuple(raw.get("mycotoxin_markets") or ("feed", "food")),
    )


def parse_biomass(doc: dict[str, Any], sources: dict[str, Source]) -> Biomass:
    owner = f"biomass {doc.get('id')!r}"
    outlets = _parse_outlet_list(doc.get("outlets") or [], sources)
    nutrients = {}
    for k, v in (doc.get("nutrients_pct_dry") or {}).items():
        _check_choice(k, NUTRIENTS, "nutrient", owner)
        nc = NutrientContent(float(v["value"]), v.get("tier", "sourced"), v.get("source_id"))
        _check_choice(nc.tier, TIERS, "nutrient tier", owner)
        _check_sources(tuple(s for s in [nc.source_id] if s), sources, owner)
        nutrients[k] = nc
    composition = []
    for c in doc.get("composition") or []:
        item = CompositionItem(str(c["component"]), str(c["value"]), c.get("basis", "dry"),
                               c.get("source_id"), c.get("tier", "sourced"))
        _check_choice(item.tier, TIERS, "composition tier", owner)
        _check_sources(tuple(s for s in [item.source_id] if s), sources, owner)
        composition.append(item)
    groups = tuple(
        DirectoryGroup(g["key"], g["title"], g["outlet"], tuple(g.get("outlets") or [g["outlet"]]),
                       g.get("blurb", ""), g.get("empty", "No verified companies listed yet."))
        for g in doc.get("directory_groups") or [])
    bm = Biomass(
        id=doc["id"], name=doc["name"], noun=doc["noun"], producers=doc.get("producers", ""),
        baseline=doc["baseline"], outlets=outlets, rules=_parse_rules(doc.get("rules")),
        unit_label=doc.get("unit_label", ""), season=doc.get("season", ""),
        ca_volume=doc.get("ca_volume", ""), summary=doc.get("summary", ""),
        market_data=bool(doc.get("market_data", False)),
        must_place=bool(doc.get("must_place", False)),
        default_tons=float(doc.get("default_tons", 1000)),
        moisture_default=_opt_float(doc.get("moisture_default")),
        ef_material=doc.get("ef_material", "yard_trimmings"),
        composition=tuple(composition), nutrients=nutrients,
        quality_notes=tuple(doc.get("quality_notes") or ()),
        caveats=tuple(doc.get("caveats") or ()),
        directory_groups=groups, source_ids=tuple(doc.get("source_ids") or ()),
    )
    _check_sources(bm.source_ids, sources, owner)
    if bm.baseline not in outlets:
        raise ValueError(f"{owner}: baseline {bm.baseline!r} is not one of its outlets")
    base = outlets[bm.baseline]
    priced = base.price_low is not None or (
        base.price_from and outlets[base.price_from].price_low is not None)
    if not priced:
        raise ValueError(f"{owner}: baseline {bm.baseline!r} needs a price")
    for g in groups:
        for oid in (g.outlet, *g.outlets):
            if oid not in outlets:
                raise ValueError(f"{owner}: directory group {g.key!r} names unknown outlet {oid!r}")
    return bm


def load_biomasses(sources: dict[str, Source], folder: Path = BIOMASS_DIR
                   ) -> dict[str, Biomass]:
    """Every biomass profile, default biomass first. Outlet ids are unique across biomasses."""
    out: dict[str, Biomass] = {}
    seen: dict[str, str] = {}
    paths = sorted(folder.glob("*.yaml"), key=lambda p: (p.stem != DEFAULT_BIOMASS, p.stem))
    for path in paths:
        with open(path, encoding="utf-8") as f:
            bm = parse_biomass(yaml.safe_load(f), sources)
        if bm.id != path.stem:
            raise ValueError(f"{path}: id {bm.id!r} must match the file name")
        for oid in bm.outlets:
            if oid in seen:
                raise ValueError(f"outlet id {oid!r} used by both {seen[oid]} and {bm.id}")
            seen[oid] = bm.id
        out[bm.id] = bm
    if DEFAULT_BIOMASS in out and out[DEFAULT_BIOMASS].baseline != BASELINE_ID:
        raise ValueError(f"{DEFAULT_BIOMASS} baseline must stay {BASELINE_ID!r}")
    return out


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
    outlets: dict[str, Outlet], path: Path = DATA_DIR / "buyers_sample.csv",
    biomass: str = DEFAULT_BIOMASS,
) -> list[Buyer]:
    """Fictional sample buyers for one biomass (rows without a biomass column are hulls)."""
    df = pd.read_csv(path).fillna({"notes": ""})
    if "biomass" in df.columns:
        df = df[df["biomass"].fillna(DEFAULT_BIOMASS) == biomass].drop(columns="biomass")
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
        for what in ("max_tons", "miles"):  # a negative price is a gate fee
            _check_non_negative(getattr(b, what), what, owner)
    return buyers


def all_outlets(sources: dict[str, Source] | None = None) -> dict[str, Outlet]:
    """Outlets of every biomass, keyed by id (ids are unique across biomasses)."""
    bms = load_biomasses(sources or load_sources())
    return {oid: o for bm in bms.values() for oid, o in bm.outlets.items()}

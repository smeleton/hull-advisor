"""Literature-to-dollars math for the conversion pathways and the feed-value check.

Conversion models (data/models.yaml), all per US short ton:
    amount_i  = Π evidence values × unit conversions          (e.g. GJ of bio-oil per dry ton)
    gross     = Σ amount_i × price_i                           ($ per dry ton)
    per_dry   = gross − conversion_cost                        (processor break-even, $/dry ton)
    price     = per_dry × dry matter                           ($ per ton of hulls as delivered)
Low/high take every combination of low/high inputs and keep the extremes, so a negative value
(a processor that would need a gate fee) still gets a correct range.

Feed value (Petersen method, [stpierre2004]): find a kg of corn DM and b kg of soybean-meal DM that
supply the same NEL and crude protein as 1 kg of hull DM,
    a·NEL_corn + b·NEL_sbm = NEL_hull
    a·CP_corn  + b·CP_sbm  = CP_hull
then value_per_ton_DM = a·P_corn/DM_corn + b·P_sbm/DM_sbm and value_as_fed = value_per_ton_DM ×
DM_hull. b is negative when hulls carry less protein per unit energy than corn: the hulls are
worth corn's energy minus the soybean meal you'd have to add back.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

from biocycle.data import worst_tier
from biocycle.evidence import (ConversionModel, Evidence, FeedRef, MaterialModel, Models,
                               conversion_factor, unit_product)


def _prod(xs) -> float:
    return math.prod(xs) if xs else 1.0


def _span(values) -> tuple[float, float]:
    vs = list(values)
    return min(vs), max(vs)


# ---------- conversion models ----------


@dataclass(frozen=True)
class ProductValue:
    name: str
    amount_low: float
    amount_high: float
    amount_unit: str           # e.g. GJ/t_dry
    price_low: float
    price_high: float
    price_unit: str            # e.g. usd/GJ
    price_tier: str
    value_low: float           # $ per dry ton
    value_high: float
    evidence_ids: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class ModelValue:
    model_id: str
    name: str
    summary: str
    products: tuple[ProductValue, ...]
    gross_low: float           # $ per dry ton, before conversion cost
    gross_high: float
    conversion_cost: float     # $ per dry ton
    conversion_cost_tier: str
    conversion_cost_note: str
    per_dry_low: float         # processor break-even, $ per dry ton
    per_dry_high: float
    dm_low: float
    dm_high: float
    price_low: float           # $ per ton as delivered
    price_high: float
    tier: str
    evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...]

    @property
    def gross_mid(self) -> float:
        return (self.gross_low + self.gross_high) / 2

    @property
    def dm_mid(self) -> float:
        return (self.dm_low + self.dm_high) / 2

    def multiple_needed(self, target_price: float) -> float | None:
        """How many times today's product value a processor would need to pay `target_price`
        per delivered ton at break-even (1.0 = already there). None if there's no product value."""
        if self.gross_mid <= 0:
            return None
        return (target_price / self.dm_mid + self.conversion_cost) / self.gross_mid


def _price_range(p, ev: dict[str, Evidence]) -> tuple[float, float]:
    if not p.price_from_evidence:
        high = p.price_high if p.price_high is not None else p.price_low
        return p.price_low, high
    num = [(ev[e].value_low, ev[e].value_high) for e in p.price_multiply]
    den = [(ev[e].value_low, ev[e].value_high) for e in p.price_divide]
    combos = [_prod(n) / _prod(d)
              for n in itertools.product(*num)
              for d in itertools.product(*den)]
    return _span(combos)


def value_model(model: ConversionModel, models: Models) -> ModelValue:
    ev = models.evidence
    dm = ev[models.dry_matter]
    products, tiers, evidence_ids = [], [model.conversion_cost_tier], [models.dry_matter]
    for p in model.products:
        factor = _prod(conversion_factor(c) for c in p.convert)
        lows = [ev[e].value_low for e in p.amount]
        highs = [ev[e].value_high for e in p.amount]
        a_low, a_high = _prod(lows) * factor, _prod(highs) * factor
        pr_low, pr_high = _price_range(p, ev)
        v_low, v_high = _span(a * pr for a in (a_low, a_high) for pr in (pr_low, pr_high))
        tier = p.price_tier
        tiers.append(tier)
        evidence_ids.extend(p.evidence_ids)
        products.append(ProductValue(
            name=p.name, amount_low=a_low, amount_high=a_high,
            amount_unit=unit_product([ev[e].unit for e in p.amount] + list(p.convert)),
            price_low=pr_low, price_high=pr_high, price_unit=p.price_unit, price_tier=tier,
            value_low=v_low, value_high=v_high, evidence_ids=p.evidence_ids, note=p.price_note))
    gross_low = sum(p.value_low for p in products)
    gross_high = sum(p.value_high for p in products)
    cost = model.conversion_cost_per_dry_ton
    per_low, per_high = gross_low - cost, gross_high - cost
    price_low, price_high = _span(v * d for v in (per_low, per_high)
                                  for d in (dm.value_low, dm.value_high))
    ids = tuple(dict.fromkeys(evidence_ids))
    return ModelValue(
        model_id=model.id, name=model.name, summary=model.summary, products=tuple(products),
        gross_low=gross_low, gross_high=gross_high, conversion_cost=cost,
        conversion_cost_tier=model.conversion_cost_tier,
        conversion_cost_note=model.conversion_cost_note,
        per_dry_low=per_low, per_dry_high=per_high, dm_low=dm.value_low, dm_high=dm.value_high,
        price_low=price_low, price_high=price_high, tier=worst_tier(*tiers),
        evidence_ids=ids, source_ids=tuple(sorted({ev[e].source_id for e in ids})),
    )


def value_all(models: Models) -> dict[str, ModelValue]:
    return {mid: value_model(m, models) for mid, m in models.conversion.items()}


# ---------- material models: the product price a pathway would need ----------


@dataclass(frozen=True)
class MaterialValue:
    model_id: str
    name: str
    summary: str
    product: str
    product_unit: str          # e.g. t_char
    yield_low: float           # product per dry ton of hulls
    yield_high: float
    conversion_cost: float     # $ per dry ton
    conversion_cost_tier: str
    conversion_cost_note: str
    dm_low: float
    dm_high: float
    evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...]

    def price_needed(self, target_price: float) -> tuple[float, float]:
        """($ low, $ high) per product unit so a break-even processor can pay `target_price` per
        ton of hulls as delivered:  (target ÷ dry matter + conversion cost) ÷ yield."""
        return _span((target_price / dm + self.conversion_cost) / y
                     for dm in (self.dm_low, self.dm_high)
                     for y in (self.yield_low, self.yield_high))


def value_material(model: MaterialModel, models: Models) -> MaterialValue:
    ev = models.evidence
    dm = ev[models.dry_matter]
    ids = tuple(dict.fromkeys((models.dry_matter, *model.amount)))
    return MaterialValue(
        model_id=model.id, name=model.name, summary=model.summary, product=model.product,
        product_unit=model.product_unit,
        yield_low=_prod(ev[e].value_low for e in model.amount),
        yield_high=_prod(ev[e].value_high for e in model.amount),
        conversion_cost=model.conversion_cost_per_dry_ton,
        conversion_cost_tier=model.conversion_cost_tier,
        conversion_cost_note=model.conversion_cost_note,
        dm_low=dm.value_low, dm_high=dm.value_high, evidence_ids=ids,
        source_ids=tuple(sorted({ev[e].source_id for e in ids})),
    )


# ---------- feed value (Petersen) ----------


def petersen(nel_h: float, cp_h: float, nel_c: float, cp_c: float,
             nel_s: float, cp_s: float) -> tuple[float, float]:
    """(a, b): kg corn DM and kg soybean-meal DM matching 1 kg hull DM in NEL and CP."""
    det = nel_c * cp_s - nel_s * cp_c
    if abs(det) < 1e-12:
        raise ValueError("reference feeds have proportional energy and protein; can't solve")
    a = (nel_h * cp_s - nel_s * cp_h) / det
    b = (nel_c * cp_h - nel_h * cp_c) / det
    return a, b


@dataclass(frozen=True)
class FeedScenarioValue:
    name: str
    nel: float                 # Mcal/kg DM
    cp: float                  # fraction of DM
    dm: float                  # fraction
    corn_kg: float             # a
    sbm_kg: float              # b
    value_low: float           # $ per ton of hulls as fed
    value_high: float
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class FeedValue:
    scenarios: tuple[FeedScenarioValue, ...]
    low: float                 # $ per ton as fed, across scenarios and prices
    high: float
    corn_price_low: float      # $ per ton as fed
    corn_price_high: float
    corn_price_tier: str
    sbm_price_low: float
    sbm_price_high: float
    sbm_price_tier: str
    tier: str
    evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...]


def _ref_price(ref: FeedRef, ev: dict[str, Evidence], override: float | None
               ) -> tuple[float, float, str]:
    if override is not None:
        return override, override, "user"
    f = _prod(conversion_factor(c) for c in ref.price_convert)
    e = ev[ref.price]
    return e.value_low * f, e.value_high * f, "sourced"


def feed_value(models: Models, corn_price: float | None = None,
               sbm_price: float | None = None) -> FeedValue:
    """Nutrient-replacement value of hulls, $/ton as fed. Prices are $/ton as fed; None = sourced."""
    spec, ev = models.feed_value, models.evidence
    c, s = spec.corn, spec.sbm
    c_lo, c_hi, c_tier = _ref_price(c, ev, corn_price)
    s_lo, s_hi, s_tier = _ref_price(s, ev, sbm_price)
    nel_c, cp_c, dm_c = (ev[c.nel].value_low, ev[c.cp].value_low, ev[c.dm].value_low)
    nel_s, cp_s, dm_s = (ev[s.nel].value_low, ev[s.cp].value_low, ev[s.dm].value_low)
    out = []
    for sc in spec.scenarios:
        nel_h, cp_h, dm_h = ev[sc.nel].value_low, ev[sc.cp].value_low, ev[sc.dm].value_low
        a, b = petersen(nel_h, cp_h, nel_c, cp_c, nel_s, cp_s)
        values = [(a * pc / dm_c + b * ps / dm_s) * dm_h
                  for pc in (c_lo, c_hi) for ps in (s_lo, s_hi)]
        lo, hi = _span(values)
        out.append(FeedScenarioValue(sc.name, nel_h, cp_h, dm_h, a, b, lo, hi,
                                     (sc.nel, sc.cp, sc.dm)))
    ids = [e for sc in out for e in sc.evidence_ids]
    for ref in (c, s):
        ids += [ref.nel, ref.cp, ref.dm] + ([] if (ref is c and corn_price is not None) or
                                           (ref is s and sbm_price is not None) else [ref.price])
    ids = tuple(dict.fromkeys(ids))
    sources = {ev[e].source_id for e in ids} | {spec.method_source}
    return FeedValue(
        scenarios=tuple(out), low=min(x.value_low for x in out),
        high=max(x.value_high for x in out),
        corn_price_low=c_lo, corn_price_high=c_hi, corn_price_tier=c_tier,
        sbm_price_low=s_lo, sbm_price_high=s_hi, sbm_price_tier=s_tier,
        tier=worst_tier(c_tier, s_tier), evidence_ids=ids, source_ids=tuple(sorted(sources)),
    )

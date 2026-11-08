"""UI-free presentation helpers: formatting, form parsing, table shaping, and the SVG chart.

The web layer (web/app.py) only wires these to HTTP. Nothing here does valuation math; it
formats numbers that biocycle.valuation and biocycle.allocation already computed.
"""

from __future__ import annotations

import math
import re
import textwrap
from dataclasses import dataclass
from html import escape
from typing import Any, Mapping

import pandas as pd

from biocycle.config import DEFAULT_HAUL_RATE
from biocycle.data import Buyer, Outlet
from biocycle.valuation import Lot, Override

TIER_LABEL = {"sourced": "Sourced", "user": "Your entry", "placeholder": "Demo value"}
TIER_HELP = {
    "sourced": "From a cited public source (see Sources).",
    "user": "A number you entered.",
    "placeholder": "Made up for this demo. Replace it with a real quote or measurement.",
}
STATUS_LABEL = {"established": "Established", "emerging": "Emerging",
                "experimental": "Experimental"}
NO_QUOTE_TEXT = "No public market price yet. Enter a buyer quote to evaluate."
AFLATOXIN_CHOICES = ("unknown", "yes", "no")


# ---------- formatting ----------


def is_missing(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def money(x: float | None, signed: bool = False, decimals: int = 0) -> str:
    if is_missing(x):
        return "—"
    q = round(abs(x) + 1e-9, decimals)  # round halves up, not to even
    sign = "" if q == 0 else ("−" if x < 0 else ("+" if signed else ""))
    return f"{sign}${q:,.{decimals}f}"


def tons_fmt(x: float) -> str:
    return f"{x:,.0f}"


def default_price_text(o: Outlet) -> str:
    if o.price_from:
        base = o.price_from.replace("_", " ")
        if o.price_premium:
            return f"{base} price + ${o.price_premium:,.0f}"
        return f"Same as {base} price"
    if o.price_low is None:
        return "No public price"
    high = o.price_high if o.price_high is not None else o.price_low
    return f"${o.price_low:,.0f}" if high == o.price_low else f"${o.price_low:,.0f}–{high:,.0f}"


def default_price_tier(o: Outlet) -> str:
    return o.premium_tier if o.price_from and o.price_premium else o.price_tier


# ---------- form parsing ----------


def parse_number(raw: Any) -> float | None:
    """Float from a form field; blank or unparseable -> None. Accepts '$1,200'."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def lot_from_form(form: Mapping[str, Any]) -> Lot:
    tons = parse_number(form.get("tons"))
    haul = parse_number(form.get("haul_rate"))
    tons = 1000.0 if tons is None or tons < 1 else tons
    haul = DEFAULT_HAUL_RATE if haul is None or haul < 0 else haul
    afla = str(form.get("aflatoxin") or "unknown").lower()
    return Lot(
        tons=tons,
        haul_rate=haul,
        haul_rate_tier="sourced" if math.isclose(haul, DEFAULT_HAUL_RATE) else "user",
        moisture_pct=_pct(form.get("moisture")),
        crude_fiber_pct=_pct(form.get("crude_fiber")),
        aflatoxin_tested=afla if afla in AFLATOXIN_CHOICES else "unknown",
    )


def _pct(raw: Any) -> float | None:
    v = parse_number(raw)
    return None if v is None or not 0 <= v <= 100 else v


def edited_value(value: Any, default: float) -> float | None:
    """The user's value if it is a valid number different from the default, else None."""
    v = parse_number(value)
    if v is None or v < 0 or math.isclose(v, default):
        return None
    return v


def coproduct_names(outlets: dict[str, Outlet]) -> dict[str, Any]:
    """Unique coproducts by name, first occurrence wins."""
    seen: dict[str, Any] = {}
    for o in outlets.values():
        for c in o.coproducts:
            seen.setdefault(c.name, c)
    return seen


def overrides_from_form(form: Mapping[str, Any], outlets: dict[str, Outlet]) -> dict[str, Override]:
    """Overrides from the outlet assumption fields; values equal to the defaults are ignored.

    Fields: price__<id> (blank = use default), miles__<id>, processing__<id>,
    coproduct__<slug(name)> ($/lb).
    """
    cp_prices: dict[str, float] = {}
    for name, c in coproduct_names(outlets).items():
        v = edited_value(form.get(f"coproduct__{slug(name)}"), c.unit_price_per_lb)
        if v is not None:
            cp_prices[name] = v
    overrides: dict[str, Override] = {}
    for o in outlets.values():
        price = parse_number(form.get(f"price__{o.id}"))
        price = None if price is None or price < 0 else price
        miles = edited_value(form.get(f"miles__{o.id}"), o.default_miles)
        processing = edited_value(form.get(f"processing__{o.id}"), o.processing_cost_per_ton)
        cps = {n: p for n, p in cp_prices.items() if any(c.name == n for c in o.coproducts)}
        if price is not None or miles is not None or processing is not None or cps:
            overrides[o.id] = Override(price, miles, processing, cps)
    return overrides


BUYER_FIELDS = ("buyer", "outlet_id", "price_per_ton", "max_tons", "miles", "notes")


def buyer_key(b: Buyer) -> tuple:
    return (b.buyer, b.outlet_id, b.price_per_ton, b.max_tons, b.miles)


def buyers_from_rows(
    rows: list[dict[str, Any]], outlets: dict[str, Outlet], sample: list[Buyer]
) -> tuple[list[Buyer], list[str], list[str]]:
    """(buyers, tiers, errors) from edited table rows. Unchanged sample rows stay placeholder."""
    sample_keys = {buyer_key(b) for b in sample}
    buyers, tiers, errors = [], [], []
    for i, r in enumerate(rows, start=1):
        values = [r.get(c) for c in BUYER_FIELDS[:5]]
        if all(is_missing(v) or str(v).strip() == "" for v in values):
            continue  # blank row
        name = str(r.get("buyer") or "").strip()
        nums = [parse_number(r.get(c)) for c in ("price_per_ton", "max_tons", "miles")]
        if not name or r.get("outlet_id") not in outlets or any(v is None for v in nums):
            errors.append(f"Row {i}: needs a buyer name, an outlet, a price, max tons and miles.")
            continue
        price, max_tons, miles = nums
        if min(price, max_tons, miles) < 0:
            errors.append(f"Row {i} ({name}): price, max tons and miles can't be negative.")
            continue
        b = Buyer(name, str(r["outlet_id"]), price, max_tons, miles, str(r.get("notes") or ""))
        buyers.append(b)
        tiers.append("placeholder" if buyer_key(b) in sample_keys else "user")
    return buyers, tiers, errors


def buyer_rows_from_form(form: Any) -> list[dict[str, Any]]:
    """Rows from repeated buyer fields (b_buyer, b_outlet_id, ...), aligned by position."""
    cols = {c: form.getlist(f"b_{c}") for c in BUYER_FIELDS}
    n = max((len(v) for v in cols.values()), default=0)
    return [{c: (cols[c][i] if i < len(cols[c]) else None) for c in BUYER_FIELDS}
            for i in range(n)]


# ---------- results shaping ----------


@dataclass(frozen=True)
class Check:
    kind: str  # ok | warn | ineligible | quote
    label: str


def check_for(row: Mapping[str, Any]) -> Check:
    if row["needs_quote"]:
        return Check("quote", "Needs a quote")
    if not row["eligible"]:
        return Check("ineligible", "Fails quality")
    n = len(row["flags"])
    return Check("warn", f"{n} to check") if n else Check("ok", "OK")


def result_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Display-ready rows for the ranked results table."""
    rows = []
    for r in df.to_dict("records"):
        priced = not r["needs_quote"]
        same = priced and math.isclose(r["net_low"], r["net_high"])
        rows.append({
            **r,
            "expected_text": money(r["net_expected"]) if priced else "Needs quote",
            "range_text": (f"{money(r['net_low'])} to {money(r['net_high'])}"
                           if priced and not same else ""),
            "total_text": money(r["total_expected"]),
            "vs_ton_text": "" if r["is_baseline"] else money(r["vs_baseline_per_ton"], True),
            "vs_lot_text": "" if r["is_baseline"] else money(r["vs_baseline_total"], True),
            "vs_sign": (0 if r["is_baseline"] or is_missing(r["vs_baseline_per_ton"])
                        else (1 if r["vs_baseline_per_ton"] > 0.005 else
                              (-1 if r["vs_baseline_per_ton"] < -0.005 else 0))),
            "break_even_text": money(r["break_even_price"]),
            "check": check_for(r),
            "detail_flags": [f for f in r["flags"] if f != "needs quote"],
        })
    return rows


def quality_flags(df: pd.DataFrame) -> list[str]:
    return sorted({f for fs in df["flags"] for f in fs if f != "needs quote"})


# ---------- chart (server-rendered SVG) ----------

CHART_W = 940
LABEL_W = 260
ROW_H = 44
TOP = 34
BOTTOM = 40
RIGHT = 24


def _nice_step(span: float) -> float:
    raw = span / 5
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def chart_svg(df: pd.DataFrame) -> str:
    """Horizontal bars of expected net $/ton with low–high whiskers and a local-dairy line.

    One series, so no legend; the ranked table is the table view. Bars for outlets that fail
    a quality check are drawn in the muted color and labelled; outlets needing a quote get a
    text note instead of a bar.
    """
    priced = df[~df["needs_quote"]]
    lows = list(priced["net_low"]) + [0.0]
    highs = list(priced["net_high"]) + [0.0]
    lo, hi = min(lows), max(highs)
    step = _nice_step(hi - lo or 1)
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step
    plot_w = CHART_W - LABEL_W - RIGHT
    height = TOP + ROW_H * len(df) + BOTTOM

    def x(v: float) -> float:
        return LABEL_W + (v - lo) / (hi - lo) * plot_w

    parts = [
        f'<svg class="chart" viewBox="0 0 {CHART_W} {height}" role="img" '
        f'aria-labelledby="chart-title chart-desc" preserveAspectRatio="xMinYMin meet">',
        '<title id="chart-title">Expected net value per ton, by option</title>',
        '<desc id="chart-desc">Bars show expected net dollars per ton after processing and '
        'hauling; whiskers show the low-to-high range; the dashed line is selling to the local '
        'dairy. The same numbers are in the table above.</desc>',
    ]
    # grid + ticks
    t = lo
    while t <= hi + 1e-9:
        gx = x(t)
        cls = "axis-zero" if abs(t) < 1e-9 else "grid"
        parts.append(f'<line class="{cls}" x1="{gx:.1f}" x2="{gx:.1f}" y1="{TOP - 6}" '
                     f'y2="{height - BOTTOM}"/>')
        parts.append(f'<text class="tick" x="{gx:.1f}" y="{height - BOTTOM + 18}" '
                     f'text-anchor="middle">{escape(money(t))}</text>')
        t += step
    parts.append(f'<text class="axis-title" x="{LABEL_W + plot_w / 2:.1f}" y="{height - 6}" '
                 'text-anchor="middle">Expected net $ per ton of hulls</text>')

    bar_h = 18
    for i, r in enumerate(df.to_dict("records")):
        cy = TOP + ROW_H * i + ROW_H / 2
        name_lines = textwrap.wrap(r["name"], 34)[:2]
        label_y = cy - (len(name_lines) - 1) * 7
        label = "".join(
            f'<tspan x="{LABEL_W - 12}" dy="{0 if j == 0 else 14}">{escape(s)}</tspan>'
            for j, s in enumerate(name_lines))
        parts.append(f'<text class="row-label" x="{LABEL_W - 12}" y="{label_y + 4:.1f}" '
                     f'text-anchor="end">{label}</text>')
        if r["needs_quote"]:
            parts.append(f'<text class="note" x="{x(0) + 8:.1f}" y="{cy + 4:.1f}">Needs a quote '
                         f'· beats local dairy above {escape(money(r["break_even_price"]))}/t'
                         '</text>')
            continue
        v = r["net_expected"]
        x0, x1 = sorted((x(0), x(v)))
        cls = "bar" if r["eligible"] else "bar bar-muted"
        tip = (f'{r["name"]}: expected {money(v)}/ton, range {money(r["net_low"])} to '
               f'{money(r["net_high"])}' + ("" if r["eligible"] else " (fails quality check)"))
        parts.append(f'<g class="mark" tabindex="0"><title>{escape(tip)}</title>')
        parts.append(f'<rect class="hit" x="{LABEL_W}" y="{cy - ROW_H / 2:.1f}" '
                     f'width="{plot_w}" height="{ROW_H}"/>')
        parts.append(f'<rect class="{cls}" x="{x0:.1f}" y="{cy - bar_h / 2:.1f}" '
                     f'width="{max(x1 - x0, 1):.1f}" height="{bar_h}" rx="3"/>')
        if not math.isclose(r["net_low"], r["net_high"]):
            wl, wh = x(r["net_low"]), x(r["net_high"])
            parts.append(f'<line class="whisker" x1="{wl:.1f}" x2="{wh:.1f}" y1="{cy:.1f}" '
                         f'y2="{cy:.1f}"/>')
            for wx in (wl, wh):
                parts.append(f'<line class="whisker" x1="{wx:.1f}" x2="{wx:.1f}" '
                             f'y1="{cy - 5:.1f}" y2="{cy + 5:.1f}"/>')
        value_x = max(x1, x(r["net_high"])) + 6
        suffix = "" if r["eligible"] else " · fails quality"
        parts.append(f'<text class="value" x="{value_x:.1f}" y="{cy + 4:.1f}">'
                     f'{escape(money(v))}{suffix}</text></g>')

    base = df.loc[df["is_baseline"], "net_expected"].iloc[0]
    bx = x(base)
    parts.append(f'<line class="baseline" x1="{bx:.1f}" x2="{bx:.1f}" y1="{TOP - 10}" '
                 f'y2="{height - BOTTOM}"/>')
    parts.append(f'<text class="baseline-label" x="{bx:.1f}" y="{TOP - 16}" '
                 f'text-anchor="middle">Local dairy {escape(money(base))}/t</text>')
    parts.append("</svg>")
    return "".join(parts)

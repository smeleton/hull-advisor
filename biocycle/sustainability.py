"""Sustainability summary for a lot of hulls: deterministic, cited, and honest about gaps.

What it measures (per option, or per buyer in an allocation):
- tons kept in productive use (every outlet in the app is a use, not disposal);
- hauling emissions = tons x one-way miles x EPA truck factor (kg CO2e per short ton-mile);
- end-of-use process emissions, only where EPA has a factor: composting and combustion, using
  EPA's "Yard Trimmings" rows as proxies (tagged as demo values, since hulls aren't yard trimmings).
What it does not claim: avoided emissions (displaced feed, fertilizer or fuel), on-farm or
processing energy, or animal emissions. The report says so explicitly.
"""

from __future__ import annotations


from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from biocycle.config import BASELINE_ID, DATA_DIR, PLACEHOLDER_BANNER
from biocycle.data import Outlet, Source, worst_tier
from biocycle.valuation import Lot

FACTORS_PATH = DATA_DIR / "emission_factors.yaml"

PATHWAYS: dict[str, dict[str, str]] = {
    # market -> how the hulls are used, and which EPA treatment proxy applies (if any)
    "feed": {"use": "Animal feed (dairy rations)", "treatment": ""},
    "food": {"use": "Human food ingredient (upcycled)", "treatment": ""},
    "soil": {"use": "Compost or soil amendment", "treatment": "composted"},
    "energy": {"use": "Bioenergy (combustion)", "treatment": "combusted"},
}
NOT_INCLUDED = (
    "Avoided emissions from the feed, fertilizer or fuel the hulls replace (no published "
    "almond-hull factor; not claimed).",
    "Energy used to clean, mill, pelletize or dry the hulls.",
    "Emissions after delivery, such as from the animals that eat the feed.",
    "Orchard and hulling operations before the lot is sold.",
)


@lru_cache
def load_factors(path: Path = FACTORS_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def truck_kg_co2e_per_ton_mile(f: dict[str, Any] | None = None) -> float:
    t = (f or load_factors())["truck_freight"]
    return (t["co2_kg_per_short_ton_mile"]
            + t["ch4_g_per_short_ton_mile"] / 1000 * t["gwp_ch4"]
            + t["n2o_g_per_short_ton_mile"] / 1000 * t["gwp_n2o"])


@dataclass(frozen=True)
class Footprint:
    key: str           # outlet id, or buyer name in an allocation
    name: str
    outlet_id: str
    use: str
    tons: float
    miles: float
    ton_miles: float
    transport_kg: float
    transport_tier: str
    treatment_kg: float | None   # None = no EPA factor applies (feed, food)
    treatment_tier: str | None
    total_kg: float
    kg_per_ton: float
    tier: str


def _footprint(key: str, name: str, outlet: Outlet, tons: float, miles: float,
               miles_tier: str, f: dict[str, Any]) -> Footprint:
    ef = truck_kg_co2e_per_ton_mile(f)
    ton_miles = tons * miles
    transport = ton_miles * ef
    transport_tier = worst_tier(f["truck_freight"]["tier"], miles_tier)
    proxy = PATHWAYS[outlet.market]["treatment"]
    if proxy:
        p = f["treatment_proxies"]
        treatment = tons * p[f"{proxy}_t_co2e_per_short_ton"] * 1000
        treatment_tier = p["tier"]
    else:
        treatment, treatment_tier = None, None
    total = transport + (treatment or 0.0)
    tier = worst_tier(transport_tier, *([treatment_tier] if treatment_tier else []))
    return Footprint(key, name, outlet.id, PATHWAYS[outlet.market]["use"], tons, miles,
                     ton_miles, transport, transport_tier, treatment, treatment_tier, total,
                     total / tons if tons else 0.0, tier)


def assess(lot: Lot, results: pd.DataFrame, outlets: dict[str, Outlet],
           factors: dict[str, Any] | None = None) -> dict[str, Footprint]:
    """Footprint if the whole lot went to each option."""
    f = factors or load_factors()
    out = {}
    for r in results.to_dict("records"):
        o = outlets[r["outlet_id"]]
        out[o.id] = _footprint(o.id, r["name"], o, lot.tons, float(r["miles"]),
                               r["input_tiers"]["miles"], f)
    return out


def assess_allocation(table: pd.DataFrame, outlets: dict[str, Outlet],
                      factors: dict[str, Any] | None = None) -> list[Footprint]:
    """Footprint per buyer for the tons the allocation actually sends them."""
    f = factors or load_factors()
    rows = []
    for r in table.to_dict("records"):
        if r["tons"] <= 1e-9:
            continue
        rows.append(_footprint(r["buyer"], r["buyer"], outlets[r["outlet_id"]], float(r["tons"]),
                               float(r["miles"]), r["buyer_tier"], f))
    return rows


# ---------- report ----------


def report_data(lot: Lot, results: pd.DataFrame, outlets: dict[str, Outlet], outlet_id: str,
                sources: dict[str, Source], today: date | None = None) -> dict[str, Any]:
    """Everything the summary shows, as plain data (rendered to HTML, Markdown and PDF)."""
    f = load_factors()
    fps = assess(lot, results, outlets, f)
    chosen, base = fps[outlet_id], fps[BASELINE_ID]
    row = results.set_index("outlet_id").loc[outlet_id]
    flags = [x for x in row["flags"] if x != "needs quote"]
    tiers = {"hauling distance": row["input_tiers"]["miles"],
             "truck emission factor": f["truck_freight"]["tier"]}
    if chosen.treatment_tier:
        tiers["end-of-use factor (proxy)"] = chosen.treatment_tier
    used_sources = ["epa_ghg_hub2025"] + [s for s in ("epa_wasted_food_scale", "abc_orchard2025")
                                          if s in sources]
    return {
        "date": (today or date.today()).isoformat(),
        "tons": lot.tons,
        "chosen": asdict(chosen),
        "baseline": asdict(base),
        "is_baseline": outlet_id == BASELINE_ID,
        "vs_local_kg": chosen.total_kg - base.total_kg,
        "truck_factor_kg": truck_kg_co2e_per_ton_mile(f),
        "proxy": f["treatment_proxies"],
        "quality": {"aflatoxin_tested": lot.aflatoxin_tested,
                    "crude_fiber_pct": lot.crude_fiber_pct, "moisture_pct": lot.moisture_pct,
                    "flags": flags, "eligible": bool(row["eligible"])},
        "tiers": tiers,
        "demo": chosen.tier == "placeholder" or "placeholder" in tiers.values(),
        "not_included": list(NOT_INCLUDED),
        "sources": [{"id": s, "title": sources[s].title, "url": sources[s].url}
                    for s in used_sources if s in sources],
        "all_options": [asdict(fp) for fp in fps.values()],
    }


def _kg(x: float) -> str:
    return f"{x / 1000:,.1f} t" if abs(x) >= 1000 else f"{x:,.0f} kg"


TIER_WORD = {"sourced": "Sourced", "user": "Your entry", "placeholder": "Demo value"}


def to_markdown(d: dict[str, Any]) -> str:
    c, b = d["chosen"], d["baseline"]
    lines = []
    if d["demo"]:
        lines += [f"> **{PLACEHOLDER_BANNER}** Some inputs below are demo values.", ""]
    lines += [
        "# Almond hull lot: sustainability summary", "",
        f"Prepared {d['date']} with the BioCycle Hull Value Advisor. "
        f"Lot: {d['tons']:,.0f} short tons of almond hulls.", "",
        "## Summary", "",
        f"- **Use:** {c['name']} ({c['use']}).",
        f"- **Kept in productive use:** {c['tons']:,.0f} tons (100% of the lot); none sent to "
        "landfill or open burning.",
        f"- **Estimated emissions:** {_kg(c['total_kg'])} CO2e "
        f"({c['kg_per_ton']:,.1f} kg CO2e per ton of hulls).",
    ]
    if not d["is_baseline"]:
        diff = d["vs_local_kg"]
        if abs(diff) < 0.5:
            lines.append("- **Compared with selling to the local dairy:** about the same.")
        else:
            lines.append(f"- **Compared with selling to the local dairy:** {_kg(abs(diff))} "
                         f"CO2e {'more' if diff > 0 else 'less'}.")
    lines += [
        "", "## Hauling", "",
        f"{c['tons']:,.0f} tons × {c['miles']:,.0f} miles one way = {c['ton_miles']:,.0f} "
        f"ton-miles × {d['truck_factor_kg']:.4f} kg CO2e per ton-mile (EPA, medium- and "
        f"heavy-duty truck) = **{_kg(c['transport_kg'])} CO2e**. "
        f"Data: {TIER_WORD[c['transport_tier']]}.", "",
        "## End use", "",
    ]
    if c["treatment_kg"] is None:
        lines.append(f"{c['use']}: EPA publishes no end-of-use emission factor for this use, so "
                     "none is estimated.")
    else:
        lines.append(f"{c['use']}: {_kg(c['treatment_kg'])} CO2e, using EPA's factor for "
                     f"{d['proxy']['material'].lower()} as a stand-in (hulls are not yard "
                     "trimmings). Data: Demo value.")
    q = d["quality"]
    lines += ["", "## Quality and safety", "",
              f"- Aflatoxin tested: {q['aflatoxin_tested']}.",
              f"- Crude fiber: {'not measured' if q['crude_fiber_pct'] is None else str(q['crude_fiber_pct']) + '%'}"
              f"; moisture: {'not measured' if q['moisture_pct'] is None else str(q['moisture_pct']) + '%'}."]
    lines += [f"- {fl}" for fl in q["flags"]]
    lines += ["", "## Where the numbers come from", ""]
    lines += [f"- {k}: {TIER_WORD[v]}" for k, v in d["tiers"].items()]
    lines += ["", "## Not included", ""] + [f"- {x}" for x in d["not_included"]]
    lines += ["", "## Sources", ""] + [f"- [{s['id']}] {s['title']}: {s['url']}"
                                       for s in d["sources"]]
    return "\n".join(lines) + "\n"


# ---------- PDF ----------

_ASCII = str.maketrans({"—": "-", "–": "-", "−": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
                        "≈": "~", "₂": "2", "…": "...", "≥": ">=", "≤": "<="})


def _latin1(text: str) -> str:
    return text.translate(_ASCII).encode("latin-1", "replace").decode("latin-1")


def to_pdf(d: dict[str, Any]) -> bytes:
    """A one-to-two page PDF of the Markdown summary (core fonts, no system libraries)."""
    from fpdf import FPDF

    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    for line in to_markdown(d).splitlines():
        text = _latin1(line.replace("**", ""))
        if line.startswith("> "):
            pdf.set_fill_color(247, 234, 208)
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(width, 6, text[2:], fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif line.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(width, 8, text[2:], new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("## "):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(width, 7, text[3:], new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("- "):
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(width, 5, "  -  " + text[2:], align="L", new_x="LMARGIN", new_y="NEXT")
        elif line.strip():
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(width, 5, text, align="L", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.ln(2)
    return bytes(pdf.output())

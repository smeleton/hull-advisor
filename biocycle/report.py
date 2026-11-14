"""AI-written report from computed results, plus a guardrail that every number is from the payload."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biocycle.config import (
    LB_PER_TON,
    PLACEHOLDER_BANNER,
    REPORT_EFFORT,
    REPORT_MAX_TOKENS,
)
from biocycle.data import HULL_RULES, QualityRules, Source
from biocycle.llm import LLMResult, complete
from biocycle.valuation import Lot, any_placeholder

CLOSING_LINE = (
    "These are estimates. Confirm prices with buyer quotes and test the material's quality "
    "before deciding."
)
SECTIONS = (
    "Recommendation",
    "How the options compare",
    "What could change the answer",
    "Quality and risk checks",
    "Assumptions and sources",
)

def system_prompt(material: str = "almond hulls",
                  reader: str = "an almond huller manager or farmer", noun: str = "hulls",
                  baseline: str = "selling to the local dairy") -> str:
    return f"""You write a short decision report for {reader} deciding what to do with a lot of \
{material}. Write in plain English for a busy, practical reader. No jargon, no hype.

You receive a JSON payload with the lot, every outlet's computed results (ranked), quality \
flags, data tiers, and the cited sources. All math is already done.

Rules:
- Use ONLY numbers that appear in the JSON. Never calculate, estimate, or introduce a new \
number, including sums, differences, percentages, or counts. Spell out small counts in words \
("three options") instead of digits.
- Money is in US dollars; per-ton values are $ per short ton of {noun} as delivered (the \
input material), except price_low/high_per_ton, which are per ton of the product sold \
(see mass_yield). A negative price or net is money the producer pays, such as a disposal or \
gate fee. You may round to whole dollars and use thousands separators (e.g. $98,000).
- Every number has a data tier: "sourced" (from a cited source), "user" (entered by the user), \
or "placeholder" (made up for this demo). Clearly separate sourced facts from placeholder \
assumptions; say plainly when a result depends on placeholder values.
- The baseline is today's usual route ({baseline}); vs_baseline values compare with it.
- An outlet with needs_quote true has no price, so it is listed last and must not be \
recommended. Say what quote it would need to match the baseline \
(price_to_match_baseline_per_ton).
- An outlet with eligible false fails a quality requirement; do not recommend it. If \
baseline_eligible is false, the baseline is only a reference point: say so, and \
recommend the best eligible priced outlet.
- Read the "definitions" in the payload and use the terms exactly as defined. In particular, \
price_to_match_baseline_per_ton is a comparison with the baseline, never a \
"break-even" or "cover costs" price; when it is null, don't give one.
- Nutrient values (nutrients) are fertilizer value. They count in an outlet's net only when \
nutrient_credit is true; otherwise mention them only as information.
- In "Recommendation", name the top-ranked eligible priced outlet first as the best-priced \
option, even when its edge rests on placeholder values; then say plainly whether that edge \
depends on placeholders and which quote would confirm it before switching. Count options \
only from the payload (outlets with vs_baseline_per_ton above zero beat the baseline).
- Cite sources by their id in square brackets, e.g. [giannini2026], and list their titles \
and URLs in the last section.
- The payload may include "literature": research-based models behind experimental options \
(conversion_models: what a processor could pay at break-even; material_models: what the product \
would have to sell for), a feed_value reference check, and the evidence records used. Describe \
evidence by its evidence_level and feedstock (e.g. "a lab study on almond shells"); never present \
a lab result as proven at farm scale. The feed value is a reference check, not a price offered.

Format: Markdown with exactly these level-2 headings, in order: \
{", ".join(f'"## {s}"' for s in SECTIONS)}. Do not number the headings. Keep the whole \
report under 600 words. A small comparison table is welcome in "How the options compare". \
End the report with this exact sentence on its own line: "{CLOSING_LINE}"
"""


SYSTEM_PROMPT = system_prompt()


# ---------- payload ----------


def _clean(value: Any) -> Any:
    """JSON-safe: NaN -> None, floats rounded (cents, or 4 decimals below 1), numpy to Python."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    if isinstance(value, float):
        if math.isnan(value):
            return None
        # Cents for money; small literature values (yields like 0.072) keep 4 decimals.
        return round(value, 2) if abs(value) >= 1 else round(value, 4)
    return value


OUTLET_FIELDS = {
    "rank": "rank",
    "outlet_id": "outlet_id",
    "name": "name",
    "status": "status",
    "market": "market",
    "end_use": "end_use",
    "is_baseline": "is_baseline",
    "needs_quote": "needs_quote",
    "eligible": "eligible",
    "price_low": "price_low_per_ton",
    "price_high": "price_high_per_ton",
    "processing_cost": "processing_cost_per_ton",
    "miles": "one_way_miles",
    "mass_yield": "mass_yield",
    "haul_cost": "haul_cost_per_ton",
    "coproduct_low": "coproduct_value_low_per_ton",
    "coproduct_high": "coproduct_value_high_per_ton",
    "net_low": "net_low_per_ton",
    "net_expected": "net_expected_per_ton",
    "net_high": "net_high_per_ton",
    "total_low": "total_low",
    "total_expected": "total_expected",
    "total_high": "total_high",
    "vs_baseline_per_ton": "vs_baseline_per_ton",
    "vs_baseline_total": "vs_baseline_total",
    "break_even_price": "price_to_match_baseline_per_ton",
    "price_follows_baseline": "price_follows_baseline",
    "nutrient_credit": "nutrient_credit",
    "nutrient_value": "nutrient_value_counted_per_ton",
    "result_tier": "result_tier",
    "input_tiers": "input_tiers",
    "flags": "flags",
    "requirements": "requirements",
    "risks": "risks",
    "source_ids": "source_ids",
}


DEFINITIONS = {
    "net_expected_per_ton": "Money left per ton of input after the outlet's processing and "
                            "hauling, plus any coproduct value (negative = the producer pays). "
                            "Expected is the midpoint of low and high.",
    "mass_yield": "Tons of product sold per ton of input (below 1 when the material is dried "
                  "first). Prices are per ton of product.",
    "vs_baseline_per_ton": "This outlet's expected net minus the baseline's expected net.",
    "price_to_match_baseline_per_ton": "The price per ton of product this outlet's buyer would "
        "have to pay for its expected net to equal the baseline. It is NOT the price needed "
        "to cover costs. It is null for the baseline itself and for outlets whose price "
        "follows the baseline price (price_follows_baseline true): a baseline price change "
        "moves both equally, so their edge comes only from their own costs and coproducts.",
    "nutrient_value_counted_per_ton": "Fertilizer value of the nutrients the producer keeps, "
        "already included in net (0 when nutrient_credit is false).",
    "rank": "Order in the comparison. Outlets that need a quote are listed last; outlets that "
            "fail a quality check are listed after every eligible priced outlet.",
}


@dataclass(frozen=True)
class Material:
    """What the report is about. Defaults describe almond hulls."""

    id: str = "almond_hulls"
    name: str = "Almond hulls"
    noun: str = "hulls"
    producers: str = "Almond hullers and shellers"
    rules: QualityRules = HULL_RULES

    @classmethod
    def of(cls, bm: Any) -> "Material":
        return cls(bm.id, bm.name, bm.noun, bm.producers, bm.rules)


def build_payload(lot: Lot, results: pd.DataFrame, sources: dict[str, Source],
                  material: Material = Material(),
                  literature: dict[str, Any] | None = None) -> dict[str, Any]:
    outlets = []
    for row in results.to_dict("records"):
        out = {name: row[col] for col, name in OUTLET_FIELDS.items()}
        if row["is_baseline"] or row["price_follows_baseline"]:
            out["price_to_match_baseline_per_ton"] = None
        if row.get("nutrients"):
            out["nutrients"] = [{"route": n["name"], "value_low_per_ton": n["value_low"],
                                 "value_high_per_ton": n["value_high"], "tier": n["tier"]}
                                for n in row["nutrients"]]
        outlets.append(out)
    cited = {sid for row in outlets for sid in row["source_ids"]}
    # Sources named inside requirement and risk text (e.g. "[calrecycle_stanislaus_fees_2026]")
    # must be in the payload too, or the report can cite an id the reader can't look up.
    for row in outlets:
        for text in (*row["requirements"], *row["risks"]):
            for group in re.findall(r"\[([a-z0-9_, ]+)\]", text):
                cited |= {i.strip() for i in group.split(",")}
    if literature:
        cited |= {e["source_id"] for e in literature.get("evidence", [])}
        if "feed_value" in literature:
            cited.add(literature["feed_value"]["source_id"])
    cited = sorted(c for c in cited if c in sources)
    baseline = next(o for o in outlets if o["is_baseline"])
    rules = material.rules
    return _clean({
        "material": {"id": material.id, "name": material.name, "producers": material.producers},
        "lot": {
            "tons": lot.tons,
            "haul_rate_per_ton_mile": None if lot.haul_schedule else lot.haul_rate,
            "haul_rate_by_distance": ([{"miles": m, "per_ton_mile": r}
                                       for m, r in lot.haul_schedule] or None),
            "haul_rate_tier": lot.haul_rate_tier,
            "moisture_pct": lot.moisture_pct,
            # Lot measurements are what the user typed in, never a sourced figure.
            "measurements_tier": "user",
            **({"crude_fiber_pct": lot.crude_fiber_pct}
               if rules.crude_fiber_max is not None else {}),
            **({f"{rules.mycotoxin}_tested": lot.aflatoxin_tested} if rules.mycotoxin else {}),
        },
        "baseline_outlet_id": baseline["outlet_id"],
        "baseline_name": baseline["name"],
        "baseline_eligible": baseline["eligible"],
        "outlet_count": len(outlets),
        **({"feed_standard_crude_fiber_max_pct": rules.crude_fiber_max}
           if rules.crude_fiber_max is not None else {}),
        "lb_per_ton": LB_PER_TON,
        "placeholder_affects_results": any_placeholder(results),
        "definitions": DEFINITIONS,
        "outlets": outlets,
        **({"literature": literature} if literature else {}),
        "sources": [
            {"id": s, "title": sources[s].title, "url": sources[s].url,
             "what_it_supports": sources[s].what_it_supports}
            for s in cited
        ],
    })


# ---------- number guardrail ----------

# A number, optionally with $ / thousands separators / decimals / % / k suffix.
NUMBER_RE = re.compile(
    r"(?<![\w.])\$?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?P<suffix>%|k\b|K\b)?"
)
LIST_MARKER_RE = re.compile(r"^(\s*)\d+[.)](?=\s)", re.MULTILINE)
URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class NumberMatch:
    text: str
    value: float
    start: int
    end: int


def extract_numbers(text: str) -> list[NumberMatch]:
    """Every number in the text, ignoring URLs and list-item markers like '1.'."""
    masked = URL_RE.sub(lambda m: " " * len(m.group()), text)
    masked = LIST_MARKER_RE.sub(lambda m: m.group(1) + " " * (len(m.group()) - len(m.group(1))),
                                masked)
    found = []
    for m in NUMBER_RE.finditer(masked):
        value = float(m.group("num").replace(",", ""))
        if (m.group("suffix") or "").lower() == "k":
            value *= 1000
        found.append(NumberMatch(text[m.start():m.end()], value, m.start(), m.end()))
    return found


def payload_numbers(payload: Any) -> set[float]:
    """Absolute values of every number in the payload, including numbers inside strings."""
    out: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            out.add(abs(float(v)))
        elif isinstance(v, str):
            out.update(abs(n.value) for n in extract_numbers(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(payload)
    return out


def _tolerance(n: NumberMatch) -> tuple[float, float | None]:
    """(absolute, relative) tolerance implied by how the number is written.

    '$13.25' must match to the cent; '$13' to within 0.5. A rounded '$98,000' may stand for
    anything that rounds to it, but never more than 5% away.
    """
    digits = re.sub(r"[^\d.]", "", n.text)
    if "." in digits:
        return 0.5 * 10 ** -len(digits.split(".")[1]), None
    trailing_zeros = len(digits) - len(digits.rstrip("0")) if digits.strip("0") else 0
    if n.text.lower().endswith("k"):
        trailing_zeros += 3
    return 0.5 * 10 ** trailing_zeros, (0.05 if trailing_zeros else None)


def number_matches(n: NumberMatch, allowed: set[float]) -> bool:
    """True if n equals an allowed number once rounded to the precision it is written with.

    Percentages also match fractions in the payload (e.g. '1.3%' matches 0.013).
    """
    tol, rel = _tolerance(n)
    candidates = [(n.value, tol)]
    if n.text.endswith("%"):
        candidates.append((n.value / 100, tol / 100))
    return any(
        abs(x - p) <= t and (rel is None or abs(x - p) <= rel * p)
        for x, t in candidates
        for p in allowed
    )


def find_unmatched(report: str, payload: dict[str, Any]) -> list[NumberMatch]:
    allowed = payload_numbers(payload) | payload_numbers(CLOSING_LINE)
    return [n for n in extract_numbers(report) if not number_matches(n, allowed)]


# ---------- report ----------


@dataclass(frozen=True)
class Report:
    markdown: str
    unmatched: list[NumberMatch]
    llm: LLMResult
    payload: dict[str, Any]


def ensure_closing_line(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith(CLOSING_LINE) else f"{text}\n\n{CLOSING_LINE}"


def report_download(report: Report) -> str:
    """Markdown file for download, carrying the demo-data and number-check warnings."""
    notes = []
    if report.payload.get("placeholder_affects_results"):
        notes.append(f"> **{PLACEHOLDER_BANNER}**")
    if report.unmatched:
        nums = ", ".join(n.text for n in report.unmatched)
        notes.append(f"> **Number check:** these numbers did not match the computed results "
                     f"and should not be relied on: {nums}")
    header = "\n>\n".join(notes)
    return f"{header}\n\n{report.markdown}\n" if header else f"{report.markdown}\n"


def prompt_for(material: Material, baseline_name: str) -> str:
    if material.id == "almond_hulls":
        return SYSTEM_PROMPT
    reader = f"a manager at one of the {material.producers.lower()}"
    return system_prompt(material.name.lower(), reader, material.noun,
                         f'"{baseline_name}"')


def generate_report(lot: Lot, results: pd.DataFrame, sources: dict[str, Source],
                    client: Any = None, material: Material = Material(),
                    literature: dict[str, Any] | None = None) -> Report:
    payload = build_payload(lot, results, sources, material, literature)
    user = ("Write the report for this lot. Payload:\n\n```json\n"
            + json.dumps(payload, indent=2) + "\n```")
    llm = complete(prompt_for(material, payload["baseline_name"]), user, max_tokens=REPORT_MAX_TOKENS, effort=REPORT_EFFORT,
                   purpose="report", client=client)
    markdown = ensure_closing_line(llm.text)
    return Report(markdown, find_unmatched(markdown, payload), llm, payload)

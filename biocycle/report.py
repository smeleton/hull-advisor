"""AI-written report from computed results, plus a guardrail that every number is from the payload."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from biocycle.config import (
    BASELINE_ID,
    CRUDE_FIBER_MAX,
    LB_PER_TON,
    PLACEHOLDER_BANNER,
    REPORT_EFFORT,
    REPORT_MAX_TOKENS,
)
from biocycle.data import Source
from biocycle.llm import LLMResult, complete
from biocycle.valuation import Lot, any_placeholder

CLOSING_LINE = (
    "These are estimates. Confirm prices with buyer quotes and test hull quality before deciding."
)
SECTIONS = (
    "Recommendation",
    "How the options compare",
    "What could change the answer",
    "Quality and risk checks",
    "Assumptions and sources",
)

SYSTEM_PROMPT = f"""You write a short decision report for an almond huller manager or farmer \
deciding what to do with a lot of almond hulls. Write in plain English for a busy, practical \
reader. No jargon, no hype.

You receive a JSON payload with the lot, every outlet's computed results (ranked), quality \
flags, data tiers, and the cited sources. All math is already done.

Rules:
- Use ONLY numbers that appear in the JSON. Never calculate, estimate, or introduce a new \
number, including sums, differences, percentages, or counts. Spell out small counts in words \
("three options") instead of digits.
- Money is in US dollars; per-ton values are $ per short ton of hulls as delivered. You may \
round to whole dollars and use thousands separators (e.g. $98,000).
- Every number has a data tier: "sourced" (from a cited source), "user" (entered by the user), \
or "placeholder" (made up for this demo). Clearly separate sourced facts from placeholder \
assumptions; say plainly when a result depends on placeholder values.
- An outlet with needs_quote true has no price. Do not rank or recommend it; explain the \
break-even price a buyer quote would need to beat.
- An outlet with eligible false fails a quality requirement; do not recommend it.
- Cite sources by their id in square brackets, e.g. [giannini2026], and list their titles \
and URLs in the last section.

Format: Markdown with exactly these level-2 headings, in order: \
{", ".join(f'"## {s}"' for s in SECTIONS)}. Do not number the headings. Keep the whole \
report under 600 words. A small comparison table is welcome in "How the options compare". \
End the report with this exact sentence on its own line: "{CLOSING_LINE}"
"""


# ---------- payload ----------


def _clean(value: Any) -> Any:
    """JSON-safe: NaN -> None, floats rounded to cents, numpy scalars to Python."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 2)
    return value


OUTLET_FIELDS = {
    "rank": "rank",
    "outlet_id": "outlet_id",
    "name": "name",
    "status": "status",
    "market": "market",
    "is_baseline": "is_baseline",
    "needs_quote": "needs_quote",
    "eligible": "eligible",
    "price_low": "price_low_per_ton",
    "price_high": "price_high_per_ton",
    "processing_cost": "processing_cost_per_ton",
    "miles": "one_way_miles",
    "haul_cost": "haul_cost_per_ton",
    "coproduct_low": "coproduct_value_low_per_ton",
    "coproduct_high": "coproduct_value_high_per_ton",
    "net_low": "net_low_per_ton",
    "net_expected": "net_expected_per_ton",
    "net_high": "net_high_per_ton",
    "total_low": "total_low",
    "total_expected": "total_expected",
    "total_high": "total_high",
    "vs_baseline_per_ton": "vs_local_dairy_per_ton",
    "vs_baseline_total": "vs_local_dairy_total",
    "break_even_price": "break_even_price_per_ton",
    "result_tier": "result_tier",
    "input_tiers": "input_tiers",
    "flags": "flags",
    "requirements": "requirements",
    "risks": "risks",
    "source_ids": "source_ids",
}


def build_payload(lot: Lot, results: pd.DataFrame, sources: dict[str, Source]) -> dict[str, Any]:
    outlets = [
        {out: row[col] for col, out in OUTLET_FIELDS.items()}
        for row in results.to_dict("records")
    ]
    cited = sorted({sid for row in outlets for sid in row["source_ids"]})
    return _clean({
        "lot": {
            "tons": lot.tons,
            "haul_rate_per_ton_mile": lot.haul_rate,
            "haul_rate_tier": lot.haul_rate_tier,
            "moisture_pct": lot.moisture_pct,
            "crude_fiber_pct": lot.crude_fiber_pct,
            "aflatoxin_tested": lot.aflatoxin_tested,
        },
        "baseline_outlet_id": BASELINE_ID,
        "outlet_count": len(outlets),
        "feed_standard_crude_fiber_max_pct": CRUDE_FIBER_MAX,
        "lb_per_ton": LB_PER_TON,
        "placeholder_affects_results": any_placeholder(results),
        "outlets": outlets,
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


def to_streamlit_markdown(report: str, unmatched: list[NumberMatch]) -> str:
    """Escape $ (Streamlit renders $...$ as LaTeX) and highlight unmatched numbers in red."""
    def esc(t: str) -> str:
        return t.replace("$", "\\$")

    out, last = [], 0
    for n in sorted(unmatched, key=lambda n: n.start):
        out.append(esc(report[last:n.start]))
        out.append(f":red-background[{esc(n.text)}]")
        last = n.end
    out.append(esc(report[last:]))
    return "".join(out)


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


def generate_report(lot: Lot, results: pd.DataFrame, sources: dict[str, Source],
                    client: Any = None) -> Report:
    payload = build_payload(lot, results, sources)
    user = ("Write the report for this lot. Payload:\n\n```json\n"
            + json.dumps(payload, indent=2) + "\n```")
    llm = complete(SYSTEM_PROMPT, user, max_tokens=REPORT_MAX_TOKENS, effort=REPORT_EFFORT,
                   purpose="report", client=client)
    markdown = ensure_closing_line(llm.text)
    return Report(markdown, find_unmatched(markdown, payload), llm, payload)

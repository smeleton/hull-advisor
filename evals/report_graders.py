"""Code graders for the AI report: deterministic checks that need no second model.

Each grader takes (raw model text, payload) and returns a Grade. The raw text is graded, not the
post-processed report, so these measure whether the *prompt* gets the behavior we want
(e.g. the closing line is graded before ensure_closing_line() patches it in).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from biocycle.report import CLOSING_LINE, SECTIONS, find_unmatched

# Words a reader would use for each outlet. Used to find which option a section names.
OUTLET_ALIASES: dict[str, tuple[str, ...]] = {
    "local_dairy": ("local dairy", "sell locally", "selling locally", "sell to the local"),
    "distant_dairy": ("distant dairy", "distant market", "texas", "long-haul"),
    "clean_and_recover": ("clean", "recover"),
    "food_ingredient": ("food ingredient", "hull powder", "food buyer", "food-ingredient"),
    "compost_soil": ("compost", "soil amendment"),
    "bioenergy": ("bioenergy", "biomass"),
}
MAX_WORDS = 600
MAX_GRADE_LEVEL = 10.0  # Flesch-Kincaid; a busy manager should not need more than 10th grade


@dataclass(frozen=True)
class Grade:
    name: str
    passed: bool
    detail: str = ""
    score: float | None = None


def sections(text: str) -> dict[str, str]:
    """Body text under each level-2 heading, keyed by heading."""
    parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def prose(text: str) -> str:
    """Report text without tables, URLs, citations, headings or list markers."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("|")]
    t = "\n".join(lines)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\[[a-z0-9_]+\]", " ", t)
    t = re.sub(r"^#+.*$", " ", t, flags=re.M)
    t = re.sub(r"^\s*([-*]|\d+[.)])\s+", "", t, flags=re.M)
    return t


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def flesch_kincaid_grade(text: str) -> float:
    t = prose(text)
    sentences = [s for s in re.split(r"[.!?:;]+(?:\s|$)|\n{2,}", t) if re.search(r"[A-Za-z]", s)]
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", t)
    if not sentences or not words:
        return 0.0
    syl = sum(_syllables(w) for w in words)
    return 0.39 * len(words) / len(sentences) + 11.8 * syl / len(words) - 15.59


# ---------- graders ----------


def numbers_traced(text: str, payload: dict[str, Any]) -> Grade:
    bad = find_unmatched(text, payload)
    return Grade("numbers_traced", not bad,
                 "every number is in the payload" if not bad
                 else "untraceable: " + ", ".join(n.text for n in bad[:8]))


def sections_in_order(text: str, payload: dict[str, Any]) -> Grade:
    found = [h for h in re.findall(r"^##\s+(.+?)\s*$", text, flags=re.M)]
    ok = found == list(SECTIONS)
    return Grade("sections_in_order", ok, "" if ok else f"headings were {found}")


def closing_line(text: str, payload: dict[str, Any]) -> Grade:
    last = text.strip().splitlines()[-1].strip() if text.strip() else ""
    ok = last == CLOSING_LINE
    return Grade("closing_line", ok, "" if ok else f"last line was {last[:80]!r}")


def _top_choice(payload: dict[str, Any]) -> dict[str, Any] | None:
    return next((o for o in payload["outlets"] if o["eligible"] and not o["needs_quote"]), None)


def _mentions(text: str, outlet_id: str) -> bool:
    low = text.lower()
    return any(a in low for a in OUTLET_ALIASES.get(outlet_id, ()))


def _first_index(text: str, outlet_id: str) -> int | None:
    low = text.lower()
    hits = [low.find(a) for a in OUTLET_ALIASES.get(outlet_id, ()) if a in low]
    return min(hits) if hits else None


def recommends_top_option(text: str, payload: dict[str, Any]) -> Grade:
    """Of the eligible, priced options, the Recommendation names the top-ranked one first.

    Ineligible and needs-quote options may be mentioned earlier (e.g. "the feed options are
    ruled out"), so only eligible, priced options compete for "named first".
    """
    top = _top_choice(payload)
    rec = sections(text).get("Recommendation", "")
    if top is None:
        ok = "no" in rec.lower() or "none" in rec.lower()
        return Grade("recommends_top_option", ok, "no eligible priced option exists")
    candidates = [o["outlet_id"] for o in payload["outlets"]
                  if o["eligible"] and not o["needs_quote"]]
    positions = {oid: i for oid in candidates if (i := _first_index(rec, oid)) is not None}
    first = min(positions, key=positions.get) if positions else None
    ok = first == top["outlet_id"]
    return Grade("recommends_top_option", ok, f"top option is {top['outlet_id']}"
                 + ("" if ok else f"; first eligible option named was {first}"))


def placeholder_disclosed(text: str, payload: dict[str, Any]) -> Grade:
    if not payload.get("placeholder_affects_results"):
        return Grade("placeholder_disclosed", True, "no placeholders in play")
    ok = bool(re.search(r"placeholder|demo (value|data|number)|made up", text, re.I))
    return Grade("placeholder_disclosed", ok, "" if ok else "placeholders used but not disclosed")


def quality_flags_surfaced(text: str, payload: dict[str, Any]) -> Grade:
    flags = " ".join(f for o in payload["outlets"] for f in o["flags"]).lower()
    missing = []
    if "aflatoxin" in flags and "aflatoxin" not in text.lower():
        missing.append("aflatoxin")
    if "ineligible as feed" in flags and "fiber" not in text.lower():
        missing.append("crude fiber")
    return Grade("quality_flags_surfaced", not missing,
                 "" if not missing else "not mentioned: " + ", ".join(missing))


def citations_valid(text: str, payload: dict[str, Any]) -> Grade:
    cited = set(re.findall(r"\[([a-z0-9_]+)\]", text))
    known = {s["id"] for s in payload["sources"]}
    unknown = sorted(cited - known)
    return Grade("citations_valid", not unknown,
                 "" if not unknown else f"unknown source ids: {unknown}")


def within_length(text: str, payload: dict[str, Any]) -> Grade:
    n = len(re.findall(r"\b\w+\b", prose(text)))
    return Grade("within_length", n <= MAX_WORDS, f"{n} words (limit {MAX_WORDS})", float(n))


def plain_reading_level(text: str, payload: dict[str, Any]) -> Grade:
    g = flesch_kincaid_grade(text)
    return Grade("plain_reading_level", g <= MAX_GRADE_LEVEL,
                 f"grade level {g:.1f} (limit {MAX_GRADE_LEVEL:g})", round(g, 1))


GRADERS: tuple[Callable[[str, dict[str, Any]], Grade], ...] = (
    numbers_traced, sections_in_order, closing_line, recommends_top_option,
    placeholder_disclosed, quality_flags_surfaced, citations_valid, within_length,
    plain_reading_level,
)


def grade_report(text: str, payload: dict[str, Any]) -> list[Grade]:
    return [g(text, payload) for g in GRADERS]

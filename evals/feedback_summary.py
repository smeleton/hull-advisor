"""Summarize real-user feedback and compare reported outcomes with what the app predicted."""

from __future__ import annotations

from collections import Counter
from statistics import mean, median
from typing import Any

from biocycle.feedback import INTENT_CHOICES


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    useful = Counter(e["useful"] for e in entries if e.get("useful"))
    intents = Counter(e["intent"] for e in entries if e.get("intent"))
    answered = sum(useful.values())

    outcomes = []
    for e in entries:
        out = e.get("outcome")
        if not out:
            continue
        pred = e["snapshot"]["predictions"].get(out["outlet_id"], {})
        lo, hi = pred.get("price_low"), pred.get("price_high")
        price = out["price_per_ton"]
        row = {
            "outlet_id": out["outlet_id"],
            "followed_top": out["outlet_id"] == e["snapshot"]["top_outlet"],
            "price_received": price,
            "predicted_low": lo, "predicted_high": hi,
            "within_range": None if lo is None else lo <= price <= hi,
            "error_vs_mid": None if lo is None else round(price - (lo + hi) / 2, 2),
            "tier": pred.get("result_tier"),
        }
        outcomes.append(row)
    errors = [o["error_vs_mid"] for o in outcomes if o["error_vs_mid"] is not None]
    in_range = [o["within_range"] for o in outcomes if o["within_range"] is not None]

    return {
        "submissions": len(entries),
        "useful": dict(useful),
        "useful_rate": round(useful.get("yes", 0) / answered, 3) if answered else None,
        "intents": {INTENT_CHOICES.get(k, k): v for k, v in intents.items()},
        "outcomes": outcomes,
        "outcome_count": len(outcomes),
        "followed_top_rate": (round(sum(o["followed_top"] for o in outcomes) / len(outcomes), 3)
                              if outcomes else None),
        "price_within_predicted_range": (round(sum(in_range) / len(in_range), 3)
                                         if in_range else None),
        "price_error_vs_mid": ({"mean": round(mean(errors), 2), "median": round(median(errors), 2),
                                "mean_abs": round(mean(abs(x) for x in errors), 2)}
                               if errors else None),
        "comments": [e["comment"] for e in entries if e.get("comment")][-20:],
    }


def to_markdown(s: dict[str, Any]) -> str:
    lines = ["# User feedback summary", "", f"- Submissions: {s['submissions']}"]
    if s["useful_rate"] is not None:
        lines.append(f"- Found it useful: {s['useful_rate']:.0%} said yes ({s['useful']})")
    if s["intents"]:
        lines.append("- Intended next step: " + ", ".join(f"{k}: {v}" for k, v in
                                                          s["intents"].items()))
    lines += ["", "## Outcomes vs. predictions", ""]
    if not s["outcomes"]:
        lines.append("No outcomes reported yet. Outcomes are the real-world check on the "
                     "price assumptions, so ask pilot users to record them after each sale.")
    else:
        lines.append(f"- Reported sales: {s['outcome_count']}; went with the top option: "
                     f"{s['followed_top_rate']:.0%}")
        if s["price_within_predicted_range"] is not None:
            lines.append(f"- Price received inside the predicted range: "
                         f"{s['price_within_predicted_range']:.0%}")
        if s["price_error_vs_mid"]:
            e = s["price_error_vs_mid"]
            lines.append(f"- Price received minus predicted midpoint: mean ${e['mean']:+,.2f}, "
                         f"median ${e['median']:+,.2f}, mean absolute ${e['mean_abs']:,.2f}/t")
        lines += ["", "| Option | Price received | Predicted range | Within | Data tier |",
                  "|---|---:|---:|---|---|"]
        for o in s["outcomes"]:
            rng = ("—" if o["predicted_low"] is None
                   else f"${o['predicted_low']:,.0f}–{o['predicted_high']:,.0f}")
            within = {True: "yes", False: "no", None: "—"}[o["within_range"]]
            lines.append(f"| {o['outlet_id']} | ${o['price_received']:,.0f} | {rng} | {within} "
                         f"| {o['tier']} |")
    if s["comments"]:
        lines += ["", "## Recent comments", ""] + [f"> {c}" for c in s["comments"]]
    return "\n".join(lines) + "\n"

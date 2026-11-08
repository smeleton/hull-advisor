"""Summarize real-user feedback and compare reported outcomes with what the app predicted.

Outcomes are scored separately by the prices the prediction used (USDA "market" data or
"demo" values): a real sale says something about the market numbers, but only about how far off
the demo numbers were. The headline accuracy figures use market-mode outcomes only.
"""

from __future__ import annotations

from collections import Counter
from statistics import mean, median
from typing import Any

from biocycle.feedback import INTENT_CHOICES

MODE_LABEL = {"market": "USDA market data", "demo": "demo values", "unknown": "mode not recorded"}


def _accuracy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [o["error_vs_mid"] for o in rows if o["error_vs_mid"] is not None]
    in_range = [o["within_range"] for o in rows if o["within_range"] is not None]
    return {
        "count": len(rows),
        "within_range": round(sum(in_range) / len(in_range), 3) if in_range else None,
        "error_vs_mid": ({"mean": round(mean(errors), 2), "median": round(median(errors), 2),
                          "mean_abs": round(mean(abs(x) for x in errors), 2)}
                         if errors else None),
    }


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
        outcomes.append({
            "outlet_id": out["outlet_id"],
            "mode": e["snapshot"].get("data_mode") or "unknown",
            "followed_top": out["outlet_id"] == e["snapshot"]["top_outlet"],
            "price_received": price,
            "predicted_low": lo, "predicted_high": hi,
            "within_range": None if lo is None else lo <= price <= hi,
            "error_vs_mid": None if lo is None else round(price - (lo + hi) / 2, 2),
            "tier": pred.get("result_tier"),
        })
    by_mode = {m: _accuracy([o for o in outcomes if o["mode"] == m])
               for m in sorted({o["mode"] for o in outcomes})}
    market = by_mode.get("market")

    return {
        "submissions": len(entries),
        "useful": dict(useful),
        "useful_rate": round(useful.get("yes", 0) / answered, 3) if answered else None,
        "intents": {INTENT_CHOICES.get(k, k): v for k, v in intents.items()},
        "outcomes": outcomes,
        "outcome_count": len(outcomes),
        "followed_top_rate": (round(sum(o["followed_top"] for o in outcomes) / len(outcomes), 3)
                              if outcomes else None),
        "by_mode": by_mode,
        # Headline: market-mode outcomes only.
        "price_within_predicted_range": market["within_range"] if market else None,
        "price_error_vs_mid": market["error_vs_mid"] if market else None,
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
        if "market" not in s["by_mode"]:
            lines.append("- No sales yet were predicted with USDA market data, so there is no "
                         "accuracy figure for the market numbers.")
        for mode, acc in s["by_mode"].items():
            label = MODE_LABEL.get(mode, mode)
            parts = [f"{acc['count']} sale(s)"]
            if acc["within_range"] is not None:
                parts.append(f"inside the predicted range {acc['within_range']:.0%}")
            if acc["error_vs_mid"]:
                e = acc["error_vs_mid"]
                parts.append(f"price minus predicted midpoint: mean ${e['mean']:+,.2f}, "
                             f"median ${e['median']:+,.2f}, mean absolute ${e['mean_abs']:,.2f}/t")
            lines.append(f"- Predicted with {label}: " + "; ".join(parts))
        lines += ["", "| Option | Prices from | Price received | Predicted range | Within | "
                  "Data tier |", "|---|---|---:|---:|---|---|"]
        for o in s["outcomes"]:
            rng = ("—" if o["predicted_low"] is None
                   else f"${o['predicted_low']:,.0f}–{o['predicted_high']:,.0f}")
            within = {True: "yes", False: "no", None: "—"}[o["within_range"]]
            lines.append(f"| {o['outlet_id']} | {MODE_LABEL.get(o['mode'], o['mode'])} | "
                         f"${o['price_received']:,.0f} | {rng} | {within} | {o['tier']} |")
    if s["comments"]:
        lines += ["", "## Recent comments", ""] + [f"> {c}" for c in s["comments"]]
    return "\n".join(lines) + "\n"

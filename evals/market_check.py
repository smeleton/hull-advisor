"""Test the app on real market data: demo values vs. the USDA snapshot, side by side."""

from __future__ import annotations

from typing import Any

from biocycle.data import load_outlets, load_sources
from biocycle.market import MarketSnapshot, apply_snapshot, load_snapshot
from biocycle.present import TIER_LABEL, money
from biocycle.valuation import Lot, evaluate
from evals.math_checks import market_backtest


def compare(snap: MarketSnapshot, tons: float = 1000) -> dict[str, Any]:
    outlets = load_outlets(load_sources())
    demo = evaluate(Lot(tons=tons), outlets).set_index("outlet_id")
    market = evaluate(Lot(tons=tons, haul_schedule=snap.truck_rates),
                      apply_snapshot(outlets, snap)).set_index("outlet_id")
    rows = []
    for oid in demo.index:
        d, m = demo.loc[oid], market.loc[oid]
        newly_sourced = sorted(k for k, t in m["input_tiers"].items()
                               if t == "sourced" and d["input_tiers"].get(k) != "sourced")
        rows.append({
            "outlet_id": oid, "name": m["name"],
            "demo_rank": int(d["rank"]), "market_rank": int(m["rank"]),
            "demo_net": None if d["needs_quote"] else round(float(d["net_expected"]), 2),
            "market_net": None if m["needs_quote"] else round(float(m["net_expected"]), 2),
            "demo_tier": d["result_tier"], "market_tier": m["result_tier"],
            "newly_sourced_inputs": newly_sourced,
            "still_demo_inputs": sorted(k for k, t in m["input_tiers"].items()
                                        if t == "placeholder"),
        })
    return {"tons": tons, "hull_report_date": snap.hull_report_date.isoformat(),
            "truck_quarter": snap.truck_quarter,
            "demo_top": demo.index[0], "market_top": market.index[0],
            "rows": sorted(rows, key=lambda r: r["market_rank"])}


def to_markdown(c: dict[str, Any], backtest_detail: str) -> str:
    lines = [
        "# Real-data check: demo values vs. USDA market data", "",
        f"Lot: {c['tons']:,.0f} tons. Market data: USDA almond hull report of "
        f"{c['hull_report_date']} [ams_3512], truck rates {c['truck_quarter']} [usda_gtor].", "",
        f"- Top option with demo values: **{c['demo_top']}**; with market data: "
        f"**{c['market_top']}**.", "",
        "| Option | Rank demo → market | Expected net demo → market ($/t) | Data | "
        "Now sourced | Still demo values |",
        "|---|---|---|---|---|---|",
    ]
    for r in c["rows"]:
        net = (f"{money(r['demo_net'])} → {money(r['market_net'])}"
               if r["market_net"] is not None else "needs quote")
        lines.append(
            f"| {r['name']} | {r['demo_rank']} → {r['market_rank']} | {net} | "
            f"{TIER_LABEL[r['demo_tier']]} → {TIER_LABEL[r['market_tier']]} | "
            f"{', '.join(r['newly_sourced_inputs']) or '—'} | "
            f"{', '.join(r['still_demo_inputs']) or '—'} |")
    lines += ["", "## Weekly backtest on real prices", "", backtest_detail, "",
              "## What this means", "",
              "- Dairy prices and hauling are now real and cited. Every option still carries at "
              "least one demo value (distances, cleaning cost, kernel price, and the compost, "
              "bioenergy and food prices), so the demo-data banner stays on.",
              "- The next numbers to replace with real quotes are the ones listed under "
              "*Still demo values* for the top options.", ""]
    return "\n".join(lines)


def run() -> tuple[dict[str, Any], str] | None:
    snap = load_snapshot()
    if snap is None:
        return None
    c = compare(snap)
    bt = market_backtest()[0]
    c["backtest"] = bt.data
    return c, to_markdown(c, bt.detail)

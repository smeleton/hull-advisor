"""Validation of the deterministic math: golden cases, invariants, LP cross-check, backtest.

Each check returns CheckResult rows so the same code drives pytest (tests/test_evals.py) and
the CLI report (python -m evals.run math).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from biocycle.allocation import allocate, solve_lp
from biocycle.config import BASELINE_ID, DATA_DIR
from biocycle.data import Outlet, load_buyers, load_outlets, load_sources
from biocycle.valuation import Lot, Override, evaluate

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "valuation_cases.yaml"
PRICE_HISTORY_PATH = DATA_DIR / "price_history.yaml"
TOL = 0.01  # dollars


@dataclass
class CheckResult:
    group: str
    name: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _outlets() -> dict[str, Outlet]:
    return load_outlets(load_sources())


def _lot(raw: dict[str, Any]) -> Lot:
    return Lot(
        tons=float(raw["tons"]),
        haul_rate=float(raw.get("haul_rate", 0.30)),
        crude_fiber_pct=raw.get("crude_fiber_pct"),
        moisture_pct=raw.get("moisture_pct"),
        aflatoxin_tested=raw.get("aflatoxin_tested", "unknown"),
    )


def _overrides(raw: dict[str, Any] | None) -> dict[str, Override]:
    return {oid: Override(price=v.get("price"), miles=v.get("miles"),
                          processing_cost=v.get("processing_cost"),
                          coproduct_prices=v.get("coproduct_prices") or {})
            for oid, v in (raw or {}).items()}


# ---------- golden cases ----------


def golden_cases(path: Path = GOLDEN_PATH) -> list[CheckResult]:
    outlets = _outlets()
    results: list[CheckResult] = []
    for case in yaml.safe_load(path.read_text()):
        lot = _lot(case["lot"])
        df = evaluate(lot, outlets, _overrides(case.get("overrides")))
        by_id = df.set_index("outlet_id")
        exp = case["expect"]
        problems: list[str] = []

        if "ranking" in exp and list(df["outlet_id"]) != exp["ranking"]:
            problems.append(f"ranking {list(df['outlet_id'])} != {exp['ranking']}")
        for oid, fields in (exp.get("outlets") or {}).items():
            row = by_id.loc[oid]
            for key, want in fields.items():
                got = {"break_even": row["break_even_price"]}.get(key, row.get(key))
                if key == "needs_quote":
                    if bool(got) != want:
                        problems.append(f"{oid}.needs_quote {got} != {want}")
                elif not math.isclose(float(got), float(want), abs_tol=TOL):
                    problems.append(f"{oid}.{key} {got:.2f} != {want}")
        for oid, want in (exp.get("vs_local_total") or {}).items():
            got = by_id.loc[oid, "vs_baseline_total"]
            if not math.isclose(got, want, abs_tol=TOL):
                problems.append(f"{oid}.vs_local_total {got:.2f} != {want}")
        for oid in exp.get("ineligible") or []:
            if by_id.loc[oid, "eligible"]:
                problems.append(f"{oid} should be ineligible")
        if "allocation" in exp:
            alloc = allocate(load_buyers(outlets), lot, df)
            for key, want in exp["allocation"].items():
                got = getattr(alloc, key)
                if not math.isclose(got, want, abs_tol=TOL):
                    problems.append(f"allocation.{key} {got:.2f} != {want}")

        results.append(CheckResult("golden", case["id"], not problems,
                                   "; ".join(problems) or case["description"]))
    return results


# ---------- invariants on randomized lots ----------


def _random_lot(rng: random.Random) -> tuple[Lot, dict[str, Override]]:
    lot = Lot(
        tons=rng.choice([1, 50, 400, 1000, 7500]),
        haul_rate=round(rng.uniform(0.0, 0.6), 2),
        crude_fiber_pct=rng.choice([None, 10.0, 14.9, 15.0, 22.0]),
        aflatoxin_tested=rng.choice(["yes", "no", "unknown"]),
    )
    overrides: dict[str, Override] = {}
    for oid in ("local_dairy", "distant_dairy", "food_ingredient", "compost_soil"):
        if rng.random() < 0.4:
            overrides[oid] = Override(
                price=rng.choice([None, round(rng.uniform(0, 250))]),
                miles=rng.choice([None, round(rng.uniform(0, 400))]),
                processing_cost=rng.choice([None, round(rng.uniform(0, 80))]))
    if rng.random() < 0.3:
        overrides["clean_and_recover"] = Override(
            coproduct_prices={"Recovered kernel": round(rng.uniform(0, 3), 2)})
    return lot, overrides


def invariants(n: int = 300, seed: int = 7) -> list[CheckResult]:
    """Properties that must hold for any input. Seeded, so failures are reproducible."""
    outlets = _outlets()
    rng = random.Random(seed)
    failures: dict[str, list[str]] = {k: [] for k in (
        "low <= expected <= high", "total = net x tons", "vs_local = net - local net",
        "break-even price gives local net", "net falls by haul rate per mile",
        "needs-quote never ranked above a priced option",
        "ineligible never ranked above eligible", "placeholder input -> placeholder result")}

    for i in range(n):
        lot, ov = _random_lot(rng)
        df = evaluate(lot, outlets, ov)
        base = df.loc[df.is_baseline].iloc[0]
        tag = f"case {i} ({lot.tons:g} t, ${lot.haul_rate}/t-mi)"
        priced = df[~df.needs_quote]

        if ((priced.net_low > priced.net_expected + 1e-9)
                | (priced.net_expected > priced.net_high + 1e-9)).any():
            failures["low <= expected <= high"].append(tag)
        if not np.allclose(priced.total_expected, priced.net_expected * lot.tons):
            failures["total = net x tons"].append(tag)
        if not np.allclose(priced.vs_baseline_per_ton, priced.net_expected - base.net_expected):
            failures["vs_local = net - local net"].append(tag)

        flags = list(zip(df.needs_quote, df.eligible))
        first_quote = next((k for k, (q, _) in enumerate(flags) if q), len(flags))
        if any(not q for q, _ in flags[first_quote:]):
            failures["needs-quote never ranked above a priced option"].append(tag)
        priced_flags = [e for q, e in flags if not q]
        if priced_flags != sorted(priced_flags, reverse=True):
            failures["ineligible never ranked above eligible"].append(tag)

        for r in df.itertuples():
            if "placeholder" in r.input_tiers.values() and r.result_tier != "placeholder":
                failures["placeholder input -> placeholder result"].append(f"{tag} {r.outlet_id}")

        # Break-even: pricing an outlet at its break-even makes its net equal the baseline's.
        oid = rng.choice([o for o in outlets if o != BASELINE_ID])
        be = float(df.set_index("outlet_id").loc[oid, "break_even_price"])
        ov2 = dict(ov)
        ov2[oid] = replace(ov.get(oid, Override()), price=be)
        df2 = evaluate(lot, outlets, ov2).set_index("outlet_id")
        if not math.isclose(df2.loc[oid, "net_expected"], df2.loc[BASELINE_ID, "net_expected"],
                            abs_tol=1e-6):
            failures["break-even price gives local net"].append(f"{tag} {oid}")

        # Ten more miles cost exactly 10 x haul rate per ton.
        o = outlets[oid]
        miles = (ov.get(oid) or Override()).miles
        miles = o.default_miles if miles is None else miles
        ov3 = dict(ov2)
        ov3[oid] = replace(ov2[oid], miles=miles + 10)
        df3 = evaluate(lot, outlets, ov3).set_index("outlet_id")
        if not math.isclose(df2.loc[oid, "net_expected"] - df3.loc[oid, "net_expected"],
                            10 * lot.haul_rate, abs_tol=1e-6):
            failures["net falls by haul rate per mile"].append(f"{tag} {oid}")

    return [CheckResult("invariant", name, not bad,
                        f"{n} random lots" if not bad else f"{len(bad)} failures, e.g. {bad[:3]}")
            for name, bad in failures.items()]


# ---------- allocation LP vs an independent greedy solver ----------


def greedy_allocation(margins: list[float], caps: list[float], total: float) -> float:
    """For one shared capacity constraint, filling the best-paying buyers first is optimal."""
    value, left = 0.0, total
    for m, c in sorted(zip(margins, caps), reverse=True):
        if m <= 0 or left <= 0:
            break
        take = min(c, left)
        value += m * take
        left -= take
    return value


def lp_cross_check(n: int = 200, seed: int = 11) -> list[CheckResult]:
    rng = random.Random(seed)
    bad = []
    for i in range(n):
        k = rng.randint(1, 8)
        margins = [round(rng.uniform(-40, 150), 2) for _ in range(k)]
        caps = [round(rng.uniform(0, 1500)) for _ in range(k)]
        total = round(rng.uniform(1, 5000))
        lp = solve_lp(np.array(margins), np.array(caps, dtype=float), total).value
        greedy = greedy_allocation(margins, caps, total)
        if not math.isclose(lp, greedy, rel_tol=1e-7, abs_tol=1e-4):
            bad.append(f"case {i}: LP {lp:.2f} vs greedy {greedy:.2f}")
    return [CheckResult("cross-check", "allocation LP equals greedy optimum", not bad,
                        f"{n} random buyer sets" if not bad else "; ".join(bad[:3]))]


# ---------- backtest over historical hull prices ----------


def load_price_history(path: Path = PRICE_HISTORY_PATH) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text())


def backtest(lot: Lot | None = None, path: Path = PRICE_HISTORY_PATH) -> list[CheckResult]:
    """Replay the recommendation at each historical local hull price.

    This doesn't prove a recommendation was "right" (we don't have realized outcomes yet — the
    in-app outcome form collects those). It shows whether today's recommendation holds across
    the price swings the market has actually had, and by how much.
    """
    outlets = _outlets()
    lot = lot or Lot(tons=1000)
    today = evaluate(lot, outlets).iloc[0]
    rows = []
    for point in load_price_history(path):
        p = float(point["price_per_ton"])
        df = evaluate(lot, outlets, {BASELINE_ID: Override(price=p)})
        top = df.iloc[0]
        rows.append({"period": point["period"], "local_price": p,
                     "source_id": point["source_id"], "best": top["outlet_id"],
                     "best_net": round(float(top["net_expected"]), 2),
                     "vs_local_per_ton": round(float(top["vs_baseline_per_ton"]), 2)})
    same = sum(r["best"] == today["outlet_id"] for r in rows)
    return [CheckResult(
        "backtest", f"recommendation holds at historical prices ({same}/{len(rows)})",
        same == len(rows),
        f"today's pick: {today['outlet_id']}; " + "; ".join(
            f"{r['period']} @ ${r['local_price']:g}: {r['best']} ({r['vs_local_per_ton']:+g}/t)"
            for r in rows),
        {"rows": rows, "today": today["outlet_id"]})]


def run_all() -> list[CheckResult]:
    return golden_cases() + invariants() + lp_cross_check() + backtest()

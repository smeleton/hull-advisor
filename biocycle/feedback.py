"""Real-user feedback: usefulness, intended action, and reported outcomes.

Each submission is one JSON line in FEEDBACK_LOG (gitignored: it is user data). It stores a
snapshot of what the app predicted for that lot, so a later outcome ("I sold to the local dairy
at $92/t") can be compared with the prediction (evals/feedback_summary.py).
No names or contact details are collected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from biocycle.config import FEEDBACK_LOG
from biocycle.valuation import Lot

log = logging.getLogger(__name__)

USEFUL_CHOICES = ("yes", "somewhat", "no")
INTENT_CHOICES = {
    "follow": "Go with the top option",
    "quote": "Get a buyer quote first",
    "stay_local": "Keep today's usual route",
    "unsure": "Not sure yet",
}
MAX_COMMENT = 2000


class FeedbackError(ValueError):
    """The submission is empty or invalid; the message is shown to the user."""


def _num(raw: Any) -> float | None:
    try:
        v = float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def build_entry(form: Mapping[str, Any], lot: Lot, results: pd.DataFrame, session_id: str,
                payload_hash: str, now: datetime | None = None,
                data_mode: str | None = None, haul_mode: str | None = None,
                biomass: str = "almond_hulls") -> dict[str, Any]:
    """Validate the fb_* fields and attach a snapshot of the prediction."""
    useful = str(form.get("fb_useful") or "").strip().lower()
    intent = str(form.get("fb_intent") or "").strip()
    comment = str(form.get("fb_comment") or "").strip()[:MAX_COMMENT]
    outcome_outlet = str(form.get("fb_outcome_outlet") or "").strip()
    outcome_price = _num(form.get("fb_outcome_price"))
    outcome_tons = _num(form.get("fb_outcome_tons"))
    outcome_miles = _num(form.get("fb_outcome_miles"))

    if useful and useful not in USEFUL_CHOICES:
        raise FeedbackError("Pick yes, somewhat or no.")
    if intent and intent not in INTENT_CHOICES:
        raise FeedbackError("Pick one of the listed next steps.")
    known = set(results["outlet_id"])
    if outcome_outlet and outcome_outlet not in known:
        raise FeedbackError("Pick the option you sold to from the list.")
    if outcome_outlet and outcome_price is None:
        raise FeedbackError("Enter the price you received per ton.")
    has_outcome = bool(outcome_outlet)
    if not (useful or intent or comment or has_outcome):
        raise FeedbackError("Answer at least one question before sending.")

    snapshot = {
        # Which prices the prediction used: "market" (USDA) or "demo"; outcomes are scored
        # per mode, so a real sale is never judged against a demo range.
        "data_mode": data_mode,
        "haul_mode": haul_mode,
        "biomass": biomass,
        "lot": {"tons": lot.tons, "haul_rate": lot.haul_rate,
                "moisture_pct": lot.moisture_pct, "crude_fiber_pct": lot.crude_fiber_pct,
                "aflatoxin_tested": lot.aflatoxin_tested},
        "top_outlet": str(results.iloc[0]["outlet_id"]),
        "payload_hash": payload_hash,
        "predictions": {
            r["outlet_id"]: {k: (None if isinstance(r[k], float) and math.isnan(r[k])
                                 else round(float(r[k]), 2))
                             for k in ("price_low", "price_high", "net_low", "net_expected",
                                       "net_high", "haul_cost", "processing_cost")}
            | {"result_tier": r["result_tier"], "rank": int(r["rank"])}
            for r in results.to_dict("records")
        },
    }
    return {
        "ts": (now or datetime.now(timezone.utc)).isoformat(),
        # A one-way hash: lets us group one browser's answers without storing its cookie.
        "session": hashlib.sha256(session_id.encode()).hexdigest()[:12],
        "useful": useful or None,
        "intent": intent or None,
        "comment": comment or None,
        "outcome": ({"outlet_id": outcome_outlet, "price_per_ton": outcome_price,
                     "tons": outcome_tons, "miles": outcome_miles} if has_outcome else None),
        "snapshot": snapshot,
    }


def record(entry: dict[str, Any], path: Path | None = None) -> None:
    path = path or FEEDBACK_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or FEEDBACK_LOG
    if not path.exists():
        return []
    entries = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping unreadable feedback line %d in %s", i, path)
    return entries

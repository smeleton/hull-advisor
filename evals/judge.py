"""LLM judge for the AI report: a rubric graded by a stronger model with structured output.

The judge sees the same JSON payload the report writer saw, plus the report, and scores five
criteria 1-5. It goes through biocycle.llm.complete like every other Claude call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from biocycle.config import JUDGE_EFFORT, JUDGE_MAX_TOKENS, JUDGE_MODEL
from biocycle.llm import LLMResult, complete

CRITERIA: dict[str, str] = {
    "faithfulness": "Every claim and recommendation is supported by the payload. Nothing is "
                    "invented, and nothing contradicts the ranking, flags or tiers. 5 = fully "
                    "faithful; 1 = a material false claim.",
    "decision_clarity": "A busy huller manager finishes the Recommendation knowing what to do "
                        "next and why. 5 = one clear, justified action; 1 = no clear answer.",
    "uncertainty_honesty": "The report separates sourced facts from placeholder (demo) "
                           "assumptions and does not overstate confidence. 5 = clear and "
                           "proportionate; 1 = presents placeholders as fact.",
    "risk_coverage": "Quality flags (crude fiber, aflatoxin, moisture), ineligible or "
                     "needs-quote options, and break-even prices are explained correctly. "
                     "5 = all relevant ones, correctly; 1 = missing or wrong.",
    "plain_language": "Plain English for a farmer or huller manager: short sentences, no "
                      "jargon or hype. 5 = very easy to read; 1 = hard to follow.",
}
PASS_MIN = 4  # every criterion must score at least this for the report to pass

JUDGE_SYSTEM = """You are a strict reviewer of short decision reports written for almond \
huller managers and farmers. You receive the JSON payload of computed results that the \
report writer was given, and the report itself. Judge the report only against the payload: \
the payload is the ground truth, and all math in it is correct.

Score each criterion from 1 to 5 using its description. Be specific in "issues": quote the \
exact phrase from the report and say what is wrong. Return an empty issues list if there are \
none. Set "pass" to true only if every criterion scores 4 or 5.

Criteria:
""" + "\n".join(f"- {k}: {v}" for k, v in CRITERIA.items())

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "object",
            "properties": {k: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for k in CRITERIA},
            "required": list(CRITERIA),
            "additionalProperties": False,
        },
        "issues": {"type": "array", "items": {"type": "string"}},
        "pass": {"type": "boolean"},
    },
    "required": ["scores", "issues", "pass"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Verdict:
    scores: dict[str, int]
    issues: list[str]
    passed: bool  # recomputed from scores; the model's own "pass" is advisory
    model_pass: bool
    llm: LLMResult


def judge_report(report_text: str, payload: dict[str, Any], client: Any = None) -> Verdict:
    user = ("Payload:\n```json\n" + json.dumps(payload, indent=2) + "\n```\n\n"
            "Report:\n<report>\n" + report_text + "\n</report>")
    llm = complete(JUDGE_SYSTEM, user, max_tokens=JUDGE_MAX_TOKENS, effort=JUDGE_EFFORT,
                   purpose="eval_judge", client=client, model=JUDGE_MODEL,
                   json_schema=JUDGE_SCHEMA)
    data = json.loads(llm.text)
    scores = {k: int(data["scores"][k]) for k in CRITERIA}
    return Verdict(scores, list(data["issues"]), all(v >= PASS_MIN for v in scores.values()),
                   bool(data["pass"]), llm)

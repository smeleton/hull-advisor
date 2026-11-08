"""Static configuration: model name, units, and default assumptions."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# LLM. All calls go through biocycle.llm.
MODEL = "claude-sonnet-5-5"
REPORT_EFFORT = "medium"  # low | medium | high | xhigh | max
REPORT_MAX_TOKENS = 16000
LLM_MAX_RETRIES = 3  # SDK retries 408/409/429/5xx and connection errors with backoff
LLM_TIMEOUT_S = 180.0
# Server-side refusal fallback (Claude API only): routes a declined request to another model.
LLM_FALLBACK_BETA = "server-side-fallback-2026-07-01"
LLM_USAGE_LOG = ROOT_DIR / "logs" / "llm_usage.jsonl"

# Evaluation. The judge grades the report model's output, so it is the stronger model.
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_EFFORT = "medium"
JUDGE_MAX_TOKENS = 8000
EVAL_SPEND_CAP_USD = 2.00  # default cap for one live eval run
EVAL_RESULTS_DIR = ROOT_DIR / "evals" / "results"
FEEDBACK_LOG = ROOT_DIR / "logs" / "feedback.jsonl"  # user data: gitignored, never committed

# Anthropic list prices, $ per million tokens (input, output), as of 2026-10.
# Cache writes bill at 1.25x input, cache reads at 0.1x input.
TOKEN_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

# Units: US short tons of hulls as delivered.
LB_PER_TON = 2000

# Hauling rate $/ton-mile. Default sits inside the sourced range (giannini2026).
DEFAULT_HAUL_RATE = 0.30
HAUL_RATE_RANGE = (0.20, 0.40)
HAUL_RATE_SOURCE = "giannini2026"

# CDFA hull feed standard: crude fiber must be below 15% (wcngg2022).
CRUDE_FIBER_MAX = 15.0

# Hulls are typically delivered at 88-91% dry matter, i.e. 9-12% moisture (wcngg2022).
MOISTURE_TYPICAL_RANGE = (9.0, 12.0)

BASELINE_ID = "local_dairy"

PLACEHOLDER_BANNER = "Demo data — verify with real buyer quotes before acting."

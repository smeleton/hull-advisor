"""Static configuration: model name, units, and default assumptions."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# LLM (Phase 3 adds token prices after checking current pricing).
MODEL = "claude-sonnet-5-5"

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

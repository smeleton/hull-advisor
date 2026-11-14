"""Report-eval scenarios: lots that exercise different branches of the report prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

from biocycle.valuation import Lot, Override


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    lot: Lot
    overrides: dict[str, Override] = field(default_factory=dict)


ALL_USER_INPUTS = {
    # Every placeholder replaced with a (hypothetical) entered value, so nothing is "demo".
    "local_dairy": Override(price=92, miles=18),
    "distant_dairy": Override(price=138, miles=240),
    "clean_and_recover": Override(miles=18, processing_cost=12,
                                  coproduct_prices={"Recovered kernel": 0.85}),
    "food_ingredient": Override(price=150, miles=90, processing_cost=70),
    "compost_soil": Override(price=28, miles=25, processing_cost=4),
    "bioenergy": Override(price=32, miles=55, processing_cost=2),
}

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("default", "Default 1,000 t lot, nothing measured.", Lot(tons=1000)),
    Scenario("high_fiber", "Crude fiber 16%: every feed outlet fails the standard.",
             Lot(tons=1000, crude_fiber_pct=16.0)),
    Scenario("food_quote", "A $200/t hull-powder buyer quote.", Lot(tons=1000),
             {"food_ingredient": Override(price=200)}),
    Scenario("worthless_kernels", "Kernel value $0: local dairy should win.", Lot(tons=1000),
             {"clean_and_recover": Override(coproduct_prices={"Recovered kernel": 0.0})}),
    Scenario("tested_wet_lot", "Aflatoxin tested, 14% moisture (outside typical range).",
             Lot(tons=2500, moisture_pct=14.0, crude_fiber_pct=12.0, aflatoxin_tested="yes")),
    Scenario("expensive_haul_small_lot", "40 t at $0.60/ton-mile.",
             Lot(tons=40, haul_rate=0.60, haul_rate_tier="user")),
    Scenario("all_user_inputs", "Every placeholder replaced by an entered value.",
             Lot(tons=1200, aflatoxin_tested="yes"), ALL_USER_INPUTS),
)

"""Small hand-computable fixtures. Haul rate 0.5 $/ton-mile keeps the arithmetic obvious."""

from typing import Any

import pytest

from biocycle.data import Outlet, Source, parse_outlet, validate_outlet_refs

SOURCES = {"src_a": Source("src_a", "Source A", "https://example.org/a", "test fact")}


def make_outlet(id: str, **overrides: Any) -> Outlet:
    raw: dict[str, Any] = {
        "id": id,
        "name": id.replace("_", " ").title(),
        "status": "established",
        "market": "feed",
        "price_low": 100,
        "price_high": 100,
        "price_tier": "sourced",
        "processing_cost_per_ton": 0,
        "processing_tier": "sourced",
        "default_miles": 10,
        "miles_tier": "sourced",
    }
    raw.update(overrides)
    return parse_outlet(raw, SOURCES)


@pytest.fixture
def outlets() -> dict[str, Outlet]:
    """
    At haul rate 0.5:
      base      100 - 0 - 5              = 95   (baseline)
      better    120 - 5 - 10             = 105  (+10)
      worse     90..110 - 0 - 20         = 70..90, expected 80  (-15), placeholder price
      recover   100 - 4 - 5 + 20..40     = 111..131, expected 121 (+26)
      quote     no price, food market    -> needs quote
    """
    items = [
        make_outlet("base", source_ids=["src_a"]),
        make_outlet("better", price_low=120, price_high=120, processing_cost_per_ton=5,
                    default_miles=20),
        make_outlet("worse", price_low=90, price_high=110, price_tier="placeholder",
                    default_miles=40),
        make_outlet(
            "recover",
            price_low=None, price_high=None, price_from="base",
            processing_cost_per_ton=4,
            coproducts=[{
                "name": "Kernel", "yield_low": 0.01, "yield_high": 0.02, "yield_tier": "sourced",
                "unit_price_per_lb": 1.0, "unit_price_tier": "sourced", "source_ids": ["src_a"],
            }],
        ),
        make_outlet("quote", market="food", price_low=None, price_high=None,
                    price_tier="placeholder", processing_cost_per_ton=10),
    ]
    result = {o.id: o for o in items}
    validate_outlet_refs(result)
    return result

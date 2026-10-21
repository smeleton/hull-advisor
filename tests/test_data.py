from pathlib import Path

import pytest

from biocycle.data import load_buyers, load_outlets, load_sources, worst_tier
from tests.conftest import SOURCES, make_outlet


def test_worst_tier():
    assert worst_tier("sourced", "user") == "user"
    assert worst_tier("user", "placeholder", "sourced") == "placeholder"
    assert worst_tier() == "sourced"


def test_loader_rejects_unknown_source_id():
    with pytest.raises(ValueError, match="unknown source_ids"):
        make_outlet("x", source_ids=["nope"])


def test_loader_rejects_bad_tier():
    with pytest.raises(ValueError, match="invalid price_tier"):
        make_outlet("x", price_tier="guess")


@pytest.mark.parametrize(
    "kwargs, msg",
    [
        ({"price_low": 120, "price_high": 100}, "price_low > price_high"),
        ({"price_low": None, "price_high": 100}, "price_high given without price_low"),
        ({"market": "space"}, "invalid market"),
        ({"default_miles": -1}, "default_miles must be >= 0"),
    ],
)
def test_loader_rejects_bad_values(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        make_outlet("x", **kwargs)


def test_point_price_when_high_missing():
    o = make_outlet("x", price_low=90, price_high=None)
    assert o.price_low == 90 and o.price_high is None


def test_bad_price_from_reference(tmp_path: Path):
    path = tmp_path / "outlets.yaml"
    path.write_text(
        "- {id: a, name: A, status: emerging, market: feed, price_from: missing,\n"
        "   processing_cost_per_ton: 0, processing_tier: sourced,\n"
        "   default_miles: 1, miles_tier: sourced}\n"
    )
    with pytest.raises(ValueError, match="does not exist"):
        load_outlets(SOURCES, path)


def test_buyer_with_unknown_outlet(tmp_path: Path):
    outlets = load_outlets(load_sources())
    path = tmp_path / "buyers.csv"
    path.write_text("buyer,outlet_id,price_per_ton,max_tons,miles,notes\nExample X,nope,1,1,1,\n")
    with pytest.raises(ValueError, match="unknown outlet_id"):
        load_buyers(outlets, path)


def test_sample_buyers_are_fictional():
    buyers = load_buyers(load_outlets(load_sources()))
    assert all(b.buyer.startswith("Example ") for b in buyers)

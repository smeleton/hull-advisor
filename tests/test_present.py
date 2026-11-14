"""Pure presentation helpers: form parsing, buyer rows, formatting, chart."""

import pytest

from biocycle.data import load_buyers, load_outlets, load_sources
from biocycle.present import (
    BUYER_FIELDS,
    buyers_from_rows,
    chart_svg,
    lot_from_form,
    money,
    overrides_from_form,
    parse_number,
)
from biocycle.valuation import Lot, evaluate


@pytest.fixture
def real_outlets():
    return load_outlets(load_sources())


def defaults_form(outlets) -> dict[str, str]:
    form = {"coproduct__recovered_kernel": "1.00"}
    for o in outlets.values():
        form |= {f"price__{o.id}": "", f"miles__{o.id}": f"{o.default_miles:g}",
                 f"processing__{o.id}": f"{o.processing_cost_per_ton:g}"}
    return form


def test_unedited_form_gives_no_overrides(real_outlets):
    assert overrides_from_form(defaults_form(real_outlets), real_outlets) == {}


def test_buyer_quote_makes_food_ingredient_rankable(real_outlets):
    form = defaults_form(real_outlets) | {"price__food_ingredient": "200"}
    overrides = overrides_from_form(form, real_outlets)
    assert set(overrides) == {"food_ingredient"}
    df = evaluate(Lot(tons=1000), real_outlets, overrides).set_index("outlet_id")
    food = df.loc["food_ingredient"]
    assert not food.needs_quote
    assert food.input_tiers["price"] == "user"
    assert food.net_expected == pytest.approx(110)  # 200 - 60 - 0.30 x 100
    assert food["rank"] < df.loc["distant_dairy", "rank"]


def test_edited_miles_become_user_override(real_outlets):
    form = defaults_form(real_outlets) | {"miles__local_dairy": "35"}
    overrides = overrides_from_form(form, real_outlets)
    assert overrides["local_dairy"].miles == 35.0
    df = evaluate(Lot(tons=1000), real_outlets, overrides).set_index("outlet_id")
    assert df.loc["local_dairy"].input_tiers["miles"] == "user"


def test_coproduct_price_override_applies_to_its_outlet(real_outlets):
    form = defaults_form(real_outlets) | {"coproduct__recovered_kernel": "0.5"}
    overrides = overrides_from_form(form, real_outlets)
    assert set(overrides) == {"clean_and_recover"}
    assert overrides["clean_and_recover"].coproduct_prices == {"Recovered kernel": 0.5}


def test_invalid_and_negative_edits_are_ignored(real_outlets):
    form = defaults_form(real_outlets) | {"miles__local_dairy": "-5", "price__bioenergy": "x"}
    assert overrides_from_form(form, real_outlets) == {}


def test_lot_from_form_parses_and_tiers():
    lot = lot_from_form({"tons": "1,250", "haul_rate": "0.35", "moisture": "11",
                         "crude_fiber": "", "aflatoxin": "YES"})
    assert lot.tons == 1250 and lot.haul_rate == 0.35 and lot.haul_rate_tier == "user"
    assert lot.moisture_pct == 11 and lot.crude_fiber_pct is None
    assert lot.aflatoxin_tested == "yes"
    default = lot_from_form({})
    assert default.tons == 1000 and default.haul_rate_tier == "sourced"


def test_parse_number():
    assert parse_number("$1,200.5") == 1200.5
    assert parse_number("") is None and parse_number("nan") is None
    assert parse_number(None) is None and parse_number("abc") is None


def test_buyers_from_rows_tiers_and_validation(real_outlets):
    sample = load_buyers(real_outlets)
    rows = [{c: getattr(b, c) for c in BUYER_FIELDS} for b in sample]
    rows[0]["price_per_ton"] = 95.0  # edited -> user
    rows.append({c: "" for c in BUYER_FIELDS})  # blank new row -> skipped
    rows.append({"buyer": "My Buyer", "outlet_id": "nope", "price_per_ton": "1",
                 "max_tons": "1", "miles": "1", "notes": ""})
    rows.append({"buyer": "Fee", "outlet_id": "bioenergy", "price_per_ton": "-1",
                 "max_tons": "1", "miles": "1", "notes": ""})  # a gate fee: allowed
    rows.append({"buyer": "Neg", "outlet_id": "bioenergy", "price_per_ton": "1",
                 "max_tons": "-1", "miles": "1", "notes": ""})
    buyers, tiers, errors = buyers_from_rows(rows, real_outlets, sample)
    assert len(buyers) == 7 and buyers[-1].price_per_ton == -1
    assert tiers == ["user"] + ["placeholder"] * 5 + ["user"]
    assert len(errors) == 2
    assert "Row 8" in errors[0] and "Row 10 (Neg)" in errors[1]


def test_form_strings_for_sample_buyers_stay_placeholder(real_outlets):
    """Form posts send strings; an unchanged sample row must still be recognised."""
    sample = load_buyers(real_outlets)
    rows = [{c: (f"{getattr(b, c):g}" if isinstance(getattr(b, c), float) else getattr(b, c))
             for c in BUYER_FIELDS} for b in sample]
    _, tiers, errors = buyers_from_rows(rows, real_outlets, sample)
    assert errors == [] and tiers == ["placeholder"] * len(sample)


def test_money_rounds_halves_up():
    assert money(86.5) == "$87" and money(85.5) == "$86"
    assert money(-0.4) == "$0" and money(-29.0, signed=True) == "−$29"
    assert money(85.5, decimals=2) == "$85.50"


def test_chart_svg_has_every_option_and_baseline(real_outlets):
    df = evaluate(Lot(tons=1000, crude_fiber_pct=16), real_outlets)
    svg = chart_svg(df)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    for name in df["name"]:
        assert name.split()[0] in svg
    assert "Local dairy $85/t" in svg
    assert "fails quality" in svg  # ineligible feed outlets are labelled, not color-only
    assert "Needs a quote" in svg

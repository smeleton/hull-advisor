"""Literature evidence, unit checks, conversion/material models and the Petersen feed value.

Expected numbers are worked by hand from data/evidence.yaml and data/models.yaml.
"""

from pathlib import Path

import pytest

from biocycle.data import load_outlets, load_sources
from biocycle.evidence import (TONNE_PER_SHORT_TON, conversion_factor, load_evidence, load_models,
                               same_unit, unit_product)
from biocycle.pathways import feed_value, petersen, value_all, value_material

SOURCES = load_sources()
MODELS = load_models(SOURCES)


# ---------- units ----------


def test_unit_algebra_cancels_tokens():
    assert same_unit(unit_product(["t_biooil/t_dry", "GJ/tonne_biooil", "tonne_biooil/t_biooil"]),
                     "GJ/t_dry")
    assert same_unit(unit_product(["usd/t_sbm"], ["t_protein/t_sbmdm", "t_sbmdm/t_sbm"]),
                     "usd/t_protein")
    assert not same_unit("GJ/t_dry", "GJ/tonne_dry")


def test_conversion_factors():
    assert conversion_factor("tonne_dry/t_dry") == pytest.approx(0.90718474)
    assert conversion_factor("bu_corn/t_corn") == pytest.approx(2000 / 56)
    with pytest.raises(ValueError):
        conversion_factor("furlong/fortnight")


# ---------- loading and validation ----------


def test_every_evidence_record_is_checkable():
    for e in MODELS.evidence.values():
        assert e.source_id in SOURCES
        assert e.reported and e.locator and e.feedstock


def test_outlet_evidence_ids_resolve():
    outlets = load_outlets(SOURCES)
    for o in outlets.values():
        assert all(e in MODELS.evidence for e in o.evidence_ids), o.id
    assert outlets["fermentation_yeast_protein"].price_model == "yeast_protein"
    assert outlets["biomaterial_hydrochar"].material_model == "hydrochar_material"


def test_shell_results_never_feed_the_hull_math():
    used = {e for m in MODELS.conversion.values() for p in m.products for e in p.evidence_ids}
    used |= {e for m in MODELS.material.values() for e in m.amount}
    fv = MODELS.feed_value
    used |= {getattr(s, f) for s in fv.scenarios for f in ("nel", "cp", "dm")}
    assert not [e for e in used if "shell" in MODELS.evidence[e].feedstock.lower()]


def _write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text)
    return p


def test_unit_mismatch_is_rejected(tmp_path):
    bad = (Path("data/models.yaml").read_text()
           .replace("convert: [tonne_biooil/t_biooil]", "convert: []"))
    with pytest.raises(ValueError, match="amount units multiply to"):
        load_models(SOURCES, _write(tmp_path, "models.yaml", bad))


def test_evidence_needs_a_locator(tmp_path):
    path = _write(tmp_path, "ev.yaml", (
        "- {id: x, source_id: sitepu2023, parameter: p, value_low: 1, value_high: 1, unit: pct,\n"
        "   reported: '1%', locator: '', evidence_level: lab, feedstock: almond hulls}\n"))
    with pytest.raises(ValueError, match="locator"):
        load_evidence(SOURCES, path)


def test_unknown_evidence_level_is_rejected(tmp_path):
    path = _write(tmp_path, "ev.yaml", (
        "- {id: x, source_id: sitepu2023, parameter: p, value_low: 1, value_high: 1, unit: pct,\n"
        "   reported: '1%', locator: Abstract, evidence_level: rumor, feedstock: almond hulls}\n"))
    with pytest.raises(ValueError, match="evidence_level"):
        load_evidence(SOURCES, path)


# ---------- conversion models ----------


def test_yeast_protein_model_by_hand():
    mv = value_all(MODELS)["yeast_protein"]
    protein_price = 340 / 0.538 / 0.895                      # $706.11 per ton of protein
    assert mv.products[0].price_low == pytest.approx(protein_price)
    assert mv.gross_low == pytest.approx(0.072 * protein_price)          # $50.84 per dry ton
    assert mv.price_low == pytest.approx((0.072 * protein_price - 30) * 0.88)
    assert mv.price_high == pytest.approx((0.072 * protein_price - 30) * 0.91)
    assert mv.tier == "placeholder"                           # conversion cost is a demo value


def test_ethanol_model_by_hand():
    mv = value_all(MODELS)["ethanol_biogas"]
    eth, ch4 = 185 * TONNE_PER_SHORT_TON, 75 * TONNE_PER_SHORT_TON
    assert mv.gross_low == pytest.approx(eth * 0.45 + ch4 * 0.10)
    assert mv.gross_high == pytest.approx(eth * 0.60 + ch4 * 0.25)
    assert mv.price_low == pytest.approx((mv.gross_low - 70) * 0.88)


def test_negative_value_range_is_ordered():
    mv = value_all(MODELS)["htl"]
    oil = 0.11 * 30 * TONNE_PER_SHORT_TON
    char = 0.32 * 29 * TONNE_PER_SHORT_TON
    per_dry_low = oil * 6 + char * 2 - 150
    assert per_dry_low < 0
    # Negative: the low end takes the higher dry matter (0.91), not 0.88.
    assert mv.price_low == pytest.approx(per_dry_low * 0.91)
    assert mv.price_low < mv.price_high < 0


def test_multiple_needed_is_one_at_the_models_own_price():
    mv = value_all(MODELS)["ethanol_biogas"]
    mid_price = (mv.gross_mid - mv.conversion_cost) * mv.dm_mid
    assert mv.multiple_needed(mid_price) == pytest.approx(1.0)


# ---------- material model ----------


def test_hydrochar_price_needed_by_hand():
    mat = value_material(MODELS.material["hydrochar_material"], MODELS)
    lo, hi = mat.price_needed(115)
    assert lo == pytest.approx((115 / 0.91 + 120) / 0.32)
    assert hi == pytest.approx((115 / 0.88 + 120) / 0.32)


# ---------- feed value (Petersen) ----------


def test_petersen_solves_the_two_equations():
    a, b = petersen(1.0, 0.05, 2.0, 0.10, 2.0, 0.50)   # hull = half a unit of corn
    assert (a, b) == (pytest.approx(0.5), pytest.approx(0.0))
    a, b = petersen(1.565, 0.051, 2.01, 0.094, 2.21, 0.538)
    assert a * 2.01 + b * 2.21 == pytest.approx(1.565)
    assert a * 0.094 + b * 0.538 == pytest.approx(0.051)
    assert b < 0                                         # less protein per unit energy than corn


def test_feed_value_with_user_prices_by_hand():
    fv = feed_value(MODELS, corn_price=250, sbm_price=400)
    assert fv.tier == "user" and fv.corn_price_tier == "user"
    sc = fv.scenarios[0]                                 # UC Davis hulls
    expect = (sc.corn_kg * 250 / 0.881 + sc.sbm_kg * 400 / 0.895) * 0.929
    assert sc.value_low == pytest.approx(expect) == sc.value_high
    assert fv.low <= fv.high


def test_feed_value_default_prices_are_sourced():
    fv = feed_value(MODELS)
    assert fv.tier == "sourced"
    assert fv.corn_price_low == pytest.approx(6.245 * 2000 / 56)
    assert fv.sbm_price_low == 340


# ---------- report payload ----------


def test_report_payload_carries_literature_and_its_sources():
    from biocycle.report import build_payload, find_unmatched
    from biocycle.valuation import Lot, evaluate
    from web.app import compute, literature_for_report

    c = compute({"data_mode": "demo", "haul_mode": "flat"})
    lit = literature_for_report(c)
    payload = build_payload(c.lot, c.results, SOURCES, literature=lit)
    assert {m["outlet_id"] for m in payload["literature"]["conversion_models"]} == {
        "bioenergy_htl", "fermentation_yeast_protein", "fermentation_ethanol"}
    assert payload["literature"]["material_models"][0]["outlet_id"] == "biomaterial_hydrochar"
    cited = {s["id"] for s in payload["sources"]}
    assert {"sitepu2023", "holtman2015", "stpierre2004", "nrc2001"} <= cited
    # A literature number in the report text passes the number guardrail.
    assert find_unmatched("Yeast protein is 0.072 ton per dry ton.", payload) == []
    assert Lot and evaluate  # imported for clarity of what the payload is built from

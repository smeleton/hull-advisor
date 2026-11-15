"""Real-company directory: validation (no personal contacts), grouping, quote drafts, tab."""

from __future__ import annotations

from datetime import date
from urllib.parse import unquote

import pytest

from biocycle.data import all_outlets, load_sources
from biocycle.directory import (
    by_outlet,
    load_directory,
    mailto,
    parse_company,
    quote_request,
)
from biocycle.present import buyers_from_rows
from biocycle.valuation import Lot


@pytest.fixture(scope="module")
def outlets():
    return all_outlets(load_sources())


def raw(**changes):
    entry = {"id": "x", "name": "Example Co", "outlets": ["local_dairy"], "what": "Buys hulls.",
             "location": "Fresno, CA", "website": "https://example.com",
             "contact_url": "https://example.com/contact", "status": "active",
             "evidence": [{"url": "https://example.com/hulls", "quote": "We buy almond hulls"}],
             "checked": "2026-11-07", "notes": ""}
    entry.update(changes)
    return entry


def test_real_directory_loads_with_evidence_for_every_entry(outlets):
    companies = load_directory(outlets)
    assert len(companies) >= 5
    for c in companies:
        assert c.evidence and all(e.url.startswith("https://") and e.quote for e in c.evidence)
        assert c.checked <= date.today()
    groups = by_outlet(companies, list(outlets))
    assert groups["local_dairy"] and groups["compost_soil"] == []  # none verified: shown as such


@pytest.mark.parametrize("field,value", [
    ("notes", "Call Jane at jane.doe@example.com"),
    ("what", "Phone (559) 555-0100 for prices"),
    ("contact_url", "mailto:sales@example.com"),
    ("website", "http://example.com"),
])
def test_personal_contacts_and_insecure_links_are_rejected(outlets, field, value):
    with pytest.raises(ValueError):
        parse_company(raw(**{field: value}), outlets)


def test_entries_need_evidence_and_known_outlets(outlets):
    with pytest.raises(ValueError, match="evidence"):
        parse_company(raw(evidence=[]), outlets)
    with pytest.raises(ValueError, match="outlets"):
        parse_company(raw(outlets=["nope"]), outlets)
    with pytest.raises(ValueError, match="status"):
        parse_company(raw(status="maybe"), outlets)


def test_quote_request_uses_lot_details_and_placeholders(outlets):
    c = parse_company(raw(), outlets)
    subject, body = quote_request(Lot(tons=1250, crude_fiber_pct=13.5, aflatoxin_tested="yes"),
                                  c, outlets["local_dairy"])
    assert subject == "Quote request: 1,250 tons of almond hulls"
    assert "Crude fiber: 13.5%" in body and "Aflatoxin: tested" in body
    assert "[your city]" in body and "[Your name]" in body  # user fills these in
    link = mailto(subject, body)
    assert link.startswith("mailto:?subject=")  # no recipient: the user addresses it
    assert unquote(link.split("body=")[1]) == body


def test_energy_request_asks_about_hulls_vs_shells(outlets):
    c = parse_company(raw(outlets=["bioenergy"]), outlets)
    _, body = quote_request(Lot(tons=100), c, outlets["bioenergy"])
    assert "accept almond hulls as well as shells" in body


def test_directory_buyer_row_without_quote_gets_friendly_prompt(outlets):
    rows = [{"buyer": "Seley & Co.", "outlet_id": "local_dairy", "price_per_ton": "",
             "max_tons": "", "miles": "", "notes": ""}]
    buyers, _, errors = buyers_from_rows(rows, outlets, [])
    assert buyers == [] and errors == [
        "Row 1 (Seley & Co.): enter their quote ($/ton), max tons and miles to include them."]


def test_find_buyers_tab_renders():
    from fastapi.testclient import TestClient

    from web.app import app

    html = TestClient(app).get("/?data=demo").text
    assert ">Find buyers</button>" in html
    assert "Penny Newman Grain Co." in html and "Best option for this lot" in html
    assert "couldn&#39;t find a California composter" in html or \
        "couldn't find a California composter" in html
    assert "@" not in html.split('id="panel-directory"')[1].split("<!-- Report -->")[0]


@pytest.mark.parametrize("bid", ["almond_hulls", "tomato_pomace"])
def test_every_option_links_to_its_own_buyer_list(bid):
    import re

    from fastapi.testclient import TestClient

    from web.app import app

    html = TestClient(app).get(f"/?biomass={bid}").text
    links = set(re.findall(r'class="to-buyers" data-outlet="([^"]+)"', html))
    sections = set(re.findall(r'class="dir-focus" data-outlet="([^"]+)"', html))
    assert links and links <= sections
    assert 'class="dir-all"' in html


def test_possible_fit_is_labelled_and_sorted_after_strong(outlets):
    companies = {c.id: c for c in load_directory(outlets)}
    assert companies["carbonzero_eco"].fit == "strong"
    assert companies["anaergia_rialto"].fit == "possible"

"""Directory of real companies that take almond hulls and other residues, and quote-request
drafts.

Entries live in data/buyers_directory.yaml. Each one is public business information backed by
a page we loaded (evidence URL + quote + date). The loader refuses anything that looks like a
personal contact (email address or phone number), so those can't slip in later.

The app never contacts anyone: a quote request is a draft the user copies, or opens in their own
email app, and sends themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from biocycle.config import DATA_DIR
from biocycle.data import Outlet
from biocycle.valuation import Lot

DIRECTORY_PATH = DATA_DIR / "buyers_directory.yaml"
STATUSES = {
    "active": "Active",
    "listed": "Listed by a trade group",
    "research": "Research stage",
    "planned": "Planned, not yet operating",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


@dataclass(frozen=True)
class Evidence:
    url: str
    quote: str


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    outlets: tuple[str, ...]
    what: str
    location: str
    website: str | None
    contact_url: str | None
    status: str
    evidence: tuple[Evidence, ...]
    checked: date
    notes: str = ""

    @property
    def status_label(self) -> str:
        return STATUSES[self.status]


def _check_text(value: str, owner: str) -> None:
    if EMAIL_RE.search(value) or PHONE_RE.search(value):
        raise ValueError(f"{owner}: looks like a personal contact ({value[:40]!r}); "
                         "the directory holds business websites only")


def _check_url(url: str | None, owner: str) -> None:
    if url is not None and not url.startswith("https://"):
        raise ValueError(f"{owner}: URLs must be https, got {url!r}")


def parse_company(raw: dict[str, Any], outlets: dict[str, Outlet]) -> Company:
    owner = f"directory entry {raw.get('id')!r}"
    c = Company(
        id=raw["id"],
        name=raw["name"],
        outlets=tuple(raw["outlets"]),
        what=raw["what"],
        location=raw["location"],
        website=raw.get("website"),
        contact_url=raw.get("contact_url"),
        status=raw["status"],
        evidence=tuple(Evidence(e["url"], e["quote"]) for e in raw["evidence"]),
        checked=raw["checked"] if isinstance(raw["checked"], date)
        else date.fromisoformat(str(raw["checked"])),
        notes=raw.get("notes") or "",
    )
    if c.status not in STATUSES:
        raise ValueError(f"{owner}: unknown status {c.status!r}")
    unknown = [o for o in c.outlets if o not in outlets]
    if unknown or not c.outlets:
        raise ValueError(f"{owner}: unknown or missing outlets {unknown}")
    if not c.evidence:
        raise ValueError(f"{owner}: needs at least one evidence URL")
    for text in (c.name, c.what, c.location, c.notes, *(e.quote for e in c.evidence)):
        _check_text(text, owner)
    for url in (c.website, c.contact_url, *(e.url for e in c.evidence)):
        _check_url(url, owner)
        if url:
            _check_text(url, owner)
    return c


def load_directory(outlets: dict[str, Outlet], path: Path = DIRECTORY_PATH) -> list[Company]:
    raw = yaml.safe_load(path.read_text()) or []
    companies = [parse_company(r, outlets) for r in raw]
    ids = [c.id for c in companies]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate directory ids")
    return companies


def by_outlet(companies: list[Company], outlet_ids: list[str]) -> dict[str, list[Company]]:
    return {oid: [c for c in companies if oid in c.outlets] for oid in outlet_ids}


# ---------- quote request ----------


def _quality_lines(lot: Lot, mycotoxin: str | None = "aflatoxin",
                   crude_fiber: bool = True) -> list[str]:
    lines = []
    if mycotoxin:
        tested = {"yes": "tested (results available on request)", "no": "not tested yet",
                  "unknown": "not tested yet"}[lot.aflatoxin_tested]
        lines.append(f"- {mycotoxin.capitalize()}: {tested}")
    if crude_fiber:
        lines.append(f"- Crude fiber: {lot.crude_fiber_pct:g}%" if lot.crude_fiber_pct is not None
                     else "- Crude fiber: not measured yet")
    lines.append(f"- Moisture: {lot.moisture_pct:g}%" if lot.moisture_pct is not None
                 else "- Moisture: not measured yet")
    return lines


def quote_request(lot: Lot, company: Company, outlet: Outlet,
                  biomass: Any = None) -> tuple[str, str]:
    """(subject, body) for a quote request the user edits and sends themselves."""
    if biomass is None or biomass.id == "almond_hulls":
        product = "almond shells/hulls" if outlet.market == "energy" else "almond hulls"
        noun, mycotoxin, fiber = "hulls", "aflatoxin", True
    else:
        product = biomass.name.lower()
        noun = biomass.noun
        mycotoxin, fiber = biomass.rules.mycotoxin, biomass.rules.crude_fiber_max is not None
    subject = f"Quote request: {lot.tons:,.0f} tons of {product}"
    accept = (f"whether you accept almond hulls as well as shells, and your price per ton "
              "delivered") if product == "almond shells/hulls" else (
        f"whether you accept {product}, and your price per ton delivered")
    ask = {
        "feed": "your price per ton, delivered and FOB our site, and your quality requirements",
        "food": f"whether you are sourcing {noun} for food use, your quality and handling "
                "requirements, and indicative pricing",
        "soil": f"whether you accept {product}, and your price or tipping fee per ton",
        "energy": accept,
        "chemicals": f"whether you are sourcing {product} for extraction, your quality and "
                     "handling requirements, and indicative pricing",
        "materials": f"whether you can use {product} as a raw material, your specifications, "
                     "and indicative pricing",
        "disposal": f"whether you accept {product}, and your gate fee per ton",
    }[outlet.market]
    if product == "almond hulls":  # hull-specific asks for the literature pathways
        ask = {
            "materials": "whether you would take almond hulls as a hydrochar or biochar "
                         "feedstock, your specifications, and indicative pricing",
            "chemicals": "whether you are sourcing almond hulls as a fermentation feedstock, your "
                         "specifications (moisture, sugar content), and indicative pricing",
        }.get(outlet.market, ask)
    body = "\n".join([
        f"Hello {company.name} team,",
        "",
        f"We have a lot of about {lot.tons:,.0f} short tons of {product} available "
        "[month/timing] at [your city], California.",
        "",
        "Lot details:",
        *_quality_lines(lot, mycotoxin, fiber),
        "",
        f"Could you share {ask}? We can arrange hauling or have you pick up.",
        "",
        "Thank you,",
        "[Your name]",
        "[Company] · [Phone]",
    ])
    return subject, body


def mailto(subject: str, body: str) -> str:
    """A mailto link with no recipient: the user's email app opens a draft they address."""
    return f"mailto:?subject={quote(subject)}&body={quote(body)}"

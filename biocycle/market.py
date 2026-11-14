"""Real market data: parsers for public USDA/EIA sources and the snapshot the app reads.

The refresh script (biocycle/refresh.py) fetches raw files, these pure functions parse them,
and the results are saved under data/market/. The app never fetches at request time.

Sources (all keyless; see data/sources.yaml):
- ams_3512   USDA AMS National Mill-Feeds and Miscellaneous Feedstuff Report (weekly PDF):
             almond hull asking prices by California region, FOB or delivered.
- usda_gtor  USDA AMS Grain Truck and Ocean Rate Advisory (agtransport.usda.gov dataset):
             truck rates per loaded mile by distance.
- eia_diesel EIA weekly California No. 2 diesel retail price (context only).
- nass_ncit  USDA NASS Noncitrus Fruits and Nuts Annual Summary: almond price per pound.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from biocycle.config import BASELINE_ID, DATA_DIR
from biocycle.data import Outlet

MARKET_DIR = DATA_DIR / "market"
SNAPSHOT_PATH = MARKET_DIR / "snapshot.json"

# Regions in the AMS report that stand in for our outlets.
LOCAL_REGION = "CA-San Joaquin Valley"   # most hullers are in the SJV
DISTANT_REGION = "CA-South"               # southern California dairies, ~250 mi from the SJV
DELIVERED = "DLVD-T"                      # delivered by truck: the buyer's price at the dairy

# USDA GTOR truck rates are per loaded mile for a 55,000 lb (27.5 short ton) load.
TRUCK_PAYLOAD_SHORT_TONS = 27.5

MONTHS = ("january february march april may june july august september october november "
          "december").split()


# ---------- AMS 3512: almond hulls ----------


@dataclass(frozen=True)
class HullQuote:
    report_date: date
    region: str
    sale_type: str
    price_low: float
    price_high: float
    average: float
    year_ago: float | None
    basis: str  # e.g. DLVD-T (delivered, truck) or FOB-T/R (at the seller, truck or rail)
    source_url: str = ""

    @property
    def delivered(self) -> bool:
        return self.basis.startswith("DLVD")


PRICE_RE = re.compile(r"^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?$")
NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
BASIS_RE = re.compile(r"^(?:FOB|DLVD)-[A-Z/]+$")
DATE_RE = re.compile(r"\b(" + "|".join(m.title() for m in MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})")


def report_date(text: str) -> date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    return date(int(m.group(3)), MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))


def parse_hull_quotes(text: str, when: date | None = None, url: str = "") -> list[HullQuote]:
    """Almond hull rows from the text of an AMS 3512 report (one PDF field per line)."""
    when = when or report_date(text)
    if when is None:
        raise ValueError("no report date found")
    lines = [ln.strip() for ln in text.splitlines()]
    try:
        start = lines.index("Almond Hulls")
    except ValueError:
        return []
    try:
        start = lines.index("Freight", start) + 1  # skip the column headers
    except ValueError:
        return []

    body = [ln for ln in lines[start:] if ln]
    quotes: list[HullQuote] = []
    record: list[str] = []
    for i, ln in enumerate(body):
        # Stop at the page footer, or at the next category/commodity: a line that is followed
        # within two lines by the "Region" column header (e.g. "Hay", "Alfalfa Meal", "Region").
        if ln.startswith("Page ") or ln == "Explanatory Notes:" or (
                not record and "Region" in (ln, *body[i + 1:i + 3])):
            break
        record.append(ln)
        if BASIS_RE.match(ln):
            q = _quote_from_record(record, when, url)
            if q:
                quotes.append(q)
            record = []
    return quotes


def _quote_from_record(rec: list[str], when: date, url: str) -> HullQuote | None:
    """[region, sale type, price, change..., average, (year ago), basis]."""
    if len(rec) < 5:
        return None
    region, sale_type, basis = rec[0], rec[1], rec[-1]
    price_i = next((i for i in range(2, len(rec) - 1) if PRICE_RE.match(rec[i])), None)
    if price_i is None:
        return None
    lo_s, _, hi_s = rec[price_i].partition("-")
    lo, hi = float(lo_s), float(hi_s or lo_s)
    nums = [float(x) for x in rec[price_i + 1:-1] if NUM_RE.match(x)]
    if not nums:
        return None
    average = nums[0]
    year_ago = nums[1] if len(nums) > 1 else None
    return HullQuote(when, region, sale_type, lo, hi, average, year_ago, basis, url)


# ---------- ESMIS archive listing ----------


def parse_esmis_listing(html: str) -> list[tuple[date, str]]:
    """(release date, PDF path) for each row of an ESMIS publication listing page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        t = tr.find("time")
        a = tr.find("a", href=lambda h: bool(h) and h.upper().endswith(".PDF"))
        if t and a and t.get("datetime"):
            out.append((datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).date(),
                        a["href"]))
    return out


def esmis_last_page(html: str) -> int:
    pages = [int(m) for m in re.findall(r'href="\?page=(\d+)"', html)]
    return max(pages, default=0)


# ---------- EIA diesel ----------


def parse_eia_weekly(html: str) -> list[tuple[date, float]]:
    """(week date, $/gal) from EIA's LeafHandler weekly history table."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells or not re.match(r"^\d{4}-[A-Z][a-z]{2}$", cells[0]):
            continue
        year = int(cells[0][:4])
        for md, val in zip(cells[1::2], cells[2::2]):
            if re.match(r"^\d{2}/\d{2}$", md) and NUM_RE.match(val):
                m, d = (int(x) for x in md.split("/"))
                out.append((date(year, m, d), float(val)))
    return sorted(out)


# ---------- USDA GTOR truck rates ----------


@dataclass(frozen=True)
class TruckRate:
    quarter: str      # e.g. 2026Q1
    distance_mi: float
    rate_per_loaded_mile: float  # $ per truckload per loaded mile

    @property
    def per_ton_mile(self) -> float:
        return self.rate_per_loaded_mile / TRUCK_PAYLOAD_SHORT_TONS


def parse_truck_rates(rows: Iterable[dict[str, Any]], region: str = "National") -> list[TruckRate]:
    out = []
    for r in rows:
        if r.get("region") != region:
            continue
        try:
            rate = float(r["rate_mile_trukload"])
            miles = float(str(r["distance"]).split()[0])
        except (KeyError, ValueError, TypeError):
            continue  # NA rows
        if math.isfinite(rate) and rate > 0:
            out.append(TruckRate(r["yearquarter"], miles, rate))
    return sorted(out, key=lambda t: (t.quarter, t.distance_mi))


def latest_quarter(rates: list[TruckRate]) -> list[TruckRate]:
    if not rates:
        return []
    q = max(r.quarter for r in rates)
    return [r for r in rates if r.quarter == q]


# ---------- NASS almond price ----------


def parse_nass_almond_price(text: str) -> dict[int, float]:
    """{year: $/lb} for California shelled almonds from the NASS noncitrus summary text."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("Almond, (shelled) (California)") and "pounds" in ln:
            header = next((h for h in reversed(lines[max(0, i - 12):i])
                           if re.search(r"\b(19|20)\d{2}\b.*\b(19|20)\d{2}\b", h)), None)
            if header is None:
                continue
            years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", header)]
            values = re.findall(r"\d[\d,]*\.\d+|\d[\d,]*", ln.split(":", 1)[1])
            n = len(years) // 2  # header repeats the years for price and for value
            return {y: float(v.replace(",", "")) for y, v in zip(years[:n], values[:n])}
    return {}


# ---------- snapshot ----------


@dataclass(frozen=True)
class MarketSnapshot:
    retrieved_at: str
    hull_report_date: date
    local: HullQuote
    distant: HullQuote | None
    truck_quarter: str | None
    truck_rates: tuple[tuple[float, float], ...]  # (distance mi, $/short ton-mile)
    diesel: tuple[date, float] | None
    almond_price: tuple[int, float] | None  # (year, $/lb shelled, all grades)
    hull_quotes: tuple[HullQuote, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return f"USDA market report of {self.hull_report_date:%b %-d, %Y}"


def _quote_to_json(q: HullQuote) -> dict[str, Any]:
    return {"report_date": q.report_date.isoformat(), "region": q.region,
            "sale_type": q.sale_type, "price_low": q.price_low, "price_high": q.price_high,
            "average": q.average, "year_ago": q.year_ago, "basis": q.basis,
            "source_url": q.source_url}


def _quote_from_json(d: dict[str, Any]) -> HullQuote:
    return HullQuote(date.fromisoformat(d["report_date"]), d["region"], d["sale_type"],
                     float(d["price_low"]), float(d["price_high"]), float(d["average"]),
                     None if d.get("year_ago") is None else float(d["year_ago"]),
                     d["basis"], d.get("source_url", ""))


def pick_quote(quotes: Iterable[HullQuote], region: str) -> HullQuote | None:
    """Delivered quote for a region (what a huller is paid at the dairy), else any quote."""
    qs = [q for q in quotes if q.region == region]
    return next((q for q in qs if q.delivered), qs[0] if qs else None)


def build_snapshot(quotes: list[HullQuote], rates: list[TruckRate],
                   diesel: list[tuple[date, float]], almond: dict[int, float],
                   retrieved_at: str) -> MarketSnapshot:
    latest_date = max(q.report_date for q in quotes)
    latest = [q for q in quotes if q.report_date == latest_date]
    local = pick_quote(latest, LOCAL_REGION)
    if local is None:
        raise ValueError(f"no {LOCAL_REGION} almond hull quote in the latest report")
    tq = latest_quarter(rates)
    return MarketSnapshot(
        retrieved_at=retrieved_at,
        hull_report_date=latest_date,
        local=local,
        distant=pick_quote(latest, DISTANT_REGION),
        truck_quarter=tq[0].quarter if tq else None,
        truck_rates=tuple((r.distance_mi, round(r.per_ton_mile, 4)) for r in tq),
        diesel=diesel[-1] if diesel else None,
        almond_price=max(almond.items()) if almond else None,
        hull_quotes=tuple(latest),
    )


def snapshot_to_json(s: MarketSnapshot) -> dict[str, Any]:
    return {
        "retrieved_at": s.retrieved_at,
        "hull_report_date": s.hull_report_date.isoformat(),
        "local": _quote_to_json(s.local),
        "distant": _quote_to_json(s.distant) if s.distant else None,
        "truck_quarter": s.truck_quarter,
        "truck_payload_short_tons": TRUCK_PAYLOAD_SHORT_TONS,
        "truck_rates_per_ton_mile": [list(x) for x in s.truck_rates],
        "diesel_ca": ({"date": s.diesel[0].isoformat(), "usd_per_gal": s.diesel[1]}
                      if s.diesel else None),
        "almond_price": ({"year": s.almond_price[0], "usd_per_lb": s.almond_price[1]}
                         if s.almond_price else None),
        "hull_quotes": [_quote_to_json(q) for q in s.hull_quotes],
    }


def snapshot_from_json(d: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        retrieved_at=d["retrieved_at"],
        hull_report_date=date.fromisoformat(d["hull_report_date"]),
        local=_quote_from_json(d["local"]),
        distant=_quote_from_json(d["distant"]) if d.get("distant") else None,
        truck_quarter=d.get("truck_quarter"),
        truck_rates=tuple((float(a), float(b)) for a, b in d.get("truck_rates_per_ton_mile", [])),
        diesel=((date.fromisoformat(d["diesel_ca"]["date"]), float(d["diesel_ca"]["usd_per_gal"]))
                if d.get("diesel_ca") else None),
        almond_price=((int(d["almond_price"]["year"]), float(d["almond_price"]["usd_per_lb"]))
                      if d.get("almond_price") else None),
        hull_quotes=tuple(_quote_from_json(q) for q in d.get("hull_quotes", [])),
    )


def load_snapshot(path: Path = SNAPSHOT_PATH) -> MarketSnapshot | None:
    if not path.exists():
        return None
    return snapshot_from_json(json.loads(path.read_text()))


def load_hull_history(path: Path = MARKET_DIR / "hull_prices.csv") -> list[HullQuote]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return [_quote_from_json(r | {"year_ago": r["year_ago"] or None})
                for r in csv.DictReader(f)]


def merge_quotes(old: Iterable[HullQuote], new: Iterable[HullQuote]) -> list[HullQuote]:
    """History plus newly fetched quotes, one per (date, region, sale type, basis); new wins.

    The weekly report is overwritten every week and the public archive has stopped updating, so
    the saved history is the only record of past weeks: a refresh must add to it, never replace it.
    """
    merged = {(q.report_date, q.region, q.sale_type, q.basis): q for q in old}
    merged.update({(q.report_date, q.region, q.sale_type, q.basis): q for q in new})
    return sorted(merged.values(), key=lambda q: (q.report_date, q.region, q.basis, q.sale_type))


def load_truck_rate_history(path: Path = MARKET_DIR / "truck_rates.csv") -> dict[str, tuple]:
    """{quarter: ((miles, $/short ton-mile), ...)} from the saved USDA truck rates."""
    if not path.exists():
        return {}
    out: dict[str, list[tuple[float, float]]] = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["quarter"], []).append(
                (float(r["distance_mi"]), float(r["per_short_ton_mile"])))
    return {q: tuple(sorted(v)) for q, v in out.items()}


def rates_for(when: date, history: dict[str, tuple]) -> tuple | None:
    """Truck rates for the quarter containing `when`, else the nearest earlier quarter."""
    q = f"{when.year}Q{(when.month - 1) // 3 + 1}"
    earlier = [k for k in history if k <= q]
    if earlier:
        return history[max(earlier)]
    return history[min(history)] if history else None


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue())


# ---------- applying the snapshot ----------


def apply_snapshot(outlets: dict[str, Outlet], snap: MarketSnapshot) -> dict[str, Outlet]:
    """Outlets with market prices in place of demo values, each tagged sourced.

    - local dairy: the SJV delivered asking-price range (ams_3512)
    - distant dairy: the southern California delivered range instead of "local + premium"
    - clean and recover: still follows the local price; adds the NASS almond price as context
    """
    out = dict(outlets)
    base = out[BASELINE_ID]
    when = f"{snap.hull_report_date:%b %-d, %Y}"
    out[BASELINE_ID] = replace(
        base, price_low=snap.local.price_low, price_high=snap.local.price_high,
        price_tier="sourced", source_ids=tuple(dict.fromkeys((*base.source_ids, "ams_3512"))),
        description=f"Baseline. Sell the hulls as-is to a nearby dairy. Price: "
                    f"{snap.local.region} {snap.local.basis} asking range, USDA report of {when}.")
    if snap.distant and "distant_dairy" in out:
        d = out["distant_dairy"]
        out["distant_dairy"] = replace(
            d, price_from=None, price_premium=0.0, premium_tier="sourced",
            price_low=snap.distant.price_low, price_high=snap.distant.price_high,
            price_tier="sourced", source_ids=tuple(dict.fromkeys((*d.source_ids, "ams_3512"))),
            name="Ship to southern California dairies",
            description=f"Truck the hulls to dairies in southern California. Price: "
                        f"{snap.distant.region} {snap.distant.basis} asking range, "
                        f"USDA report of {when}.")
    if snap.almond_price and "clean_and_recover" in out:
        c = out["clean_and_recover"]
        year, price = snap.almond_price
        out["clean_and_recover"] = replace(c, risks=(*c.risks, (
            f"California shelled almonds averaged ${price:.2f}/lb in {year} across all grades "
            f"[nass_ncit]; recovered kernels usually sell for less.")),
            source_ids=tuple(dict.fromkeys((*c.source_ids, "nass_ncit"))))
    return out


def haul_schedule(snap: MarketSnapshot | None) -> tuple[tuple[float, float], ...]:
    return snap.truck_rates if snap else ()

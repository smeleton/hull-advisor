"""Refresh real market data from public USDA/EIA sources into data/market/.

    python -m biocycle.refresh                # latest reports + full hull price history
    python -m biocycle.refresh --no-history   # latest reports only (5 requests)

Writes (committed, small): data/market/hull_prices.csv, truck_rates.csv, diesel_ca.csv,
almond_prices.csv and snapshot.json. Downloaded files are cached in data/market/cache/
(gitignored), so re-running only fetches what is new. All sources are keyless and their
robots.txt allows these paths; requests are spaced one second apart.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from biocycle.market import (
    MARKET_DIR,
    SNAPSHOT_PATH,
    HullQuote,
    build_snapshot,
    esmis_last_page,
    load_hull_history,
    merge_quotes,
    parse_eia_weekly,
    parse_esmis_listing,
    parse_hull_quotes,
    parse_nass_almond_price,
    parse_truck_rates,
    snapshot_to_json,
    write_csv,
)

log = logging.getLogger("refresh")

USER_AGENT = "BioCycleHullAdvisor/0.1 (market data refresh)"
AMS_3512_URL = "https://www.ams.usda.gov/mnreports/AMS_3512.pdf"
ESMIS_BASE = "https://esmis.nal.usda.gov"
ESMIS_3512_LISTING = f"{ESMIS_BASE}/publication/national-mill-feeds-and-miscellaneous-feedstuff-report"
GTOR_URL = "https://agtransport.usda.gov/resource/fxkn-2w9c.json?$limit=5000"
EIA_CA_DIESEL_URL = ("https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx"
                     "?n=PET&s=EMD_EPD2D_PTE_SCA_DPG&f=W")
# Annual (May) release; update the file name when the next summary is published.
NASS_NONCITRUS_URL = f"{ESMIS_BASE}/sites/default/release-files/795891/ncit0526.txt"
CACHE_DIR = MARKET_DIR / "cache"
DELAY_S = 1.0


class Fetcher:
    def __init__(self, cache_dir: Path = CACHE_DIR, delay: float = DELAY_S) -> None:
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                                   timeout=60.0)
        self.cache_dir = cache_dir
        self.delay = delay
        self._last = 0.0

    def get(self, url: str, cache_name: str | None = None) -> bytes:
        """GET with a one-second spacing; cache_name makes the response reusable offline."""
        path = self.cache_dir / cache_name if cache_name else None
        if path and path.exists():
            return path.read_bytes()
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        resp = self.client.get(url)
        self._last = time.monotonic()
        resp.raise_for_status()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        return resp.content


def pdf_text(data: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)


def latest_hull_quotes(f: Fetcher) -> list[HullQuote]:
    # The current report is overwritten weekly, so it is never served from cache.
    text = pdf_text(f.get(AMS_3512_URL))
    quotes = parse_hull_quotes(text, url=AMS_3512_URL)
    if quotes:
        f.cache_dir.mkdir(parents=True, exist_ok=True)
        (f.cache_dir / f"ams3512/{quotes[0].report_date}.txt").parent.mkdir(exist_ok=True)
        (f.cache_dir / f"ams3512/{quotes[0].report_date}.txt").write_text(text)
    return quotes


def history_hull_quotes(f: Fetcher, max_pages: int | None = None) -> list[HullQuote]:
    first = f.get(ESMIS_3512_LISTING).decode()
    last = esmis_last_page(first)
    if max_pages is not None:
        last = min(last, max_pages - 1)
    releases = parse_esmis_listing(first)
    for page in range(1, last + 1):
        releases += parse_esmis_listing(f.get(f"{ESMIS_3512_LISTING}?page={page}").decode())
    quotes: list[HullQuote] = []
    for when, href in sorted(set(releases)):
        url = ESMIS_BASE + href
        try:
            text = pdf_text(f.get(url, cache_name=f"esmis3512/{when}.pdf"))
            quotes += parse_hull_quotes(text, when=when, url=url)
        except Exception as e:  # one bad PDF must not stop the history
            log.warning("skipping %s (%s): %s", when, url, e)
    return quotes


def run(history: bool, max_pages: int | None, out_dir: Path = MARKET_DIR) -> dict:
    f = Fetcher()
    quotes = latest_hull_quotes(f)
    if not quotes:
        raise RuntimeError("the current AMS 3512 report has no almond hull rows")
    if history:
        quotes += history_hull_quotes(f, max_pages)
    # Add to the saved history; never replace it (past weekly reports can't be re-fetched).
    quotes = merge_quotes(load_hull_history(out_dir / "hull_prices.csv"), quotes)

    rates = parse_truck_rates(json.loads(f.get(GTOR_URL)))
    diesel = parse_eia_weekly(f.get(EIA_CA_DIESEL_URL).decode("utf-8", "replace"))
    almond = parse_nass_almond_price(f.get(NASS_NONCITRUS_URL).decode("utf-8", "replace"))

    # Build everything before writing anything, so a parse failure leaves old files intact.
    snap = build_snapshot(quotes, rates, diesel, almond,
                          retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_csv(out_dir / "hull_prices.csv",
              [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(q).items()}
               for q in quotes])
    write_csv(out_dir / "truck_rates.csv",
              [asdict(r) | {"per_short_ton_mile": round(r.per_ton_mile, 4)} for r in rates])
    write_csv(out_dir / "diesel_ca.csv",
              [{"week": d.isoformat(), "usd_per_gal": p} for d, p in diesel])
    write_csv(out_dir / "almond_prices.csv",
              [{"year": y, "usd_per_lb_shelled": p} for y, p in sorted(almond.items())])

    (out_dir / SNAPSHOT_PATH.name).write_text(json.dumps(snapshot_to_json(snap), indent=2) + "\n")
    return {
        "hull_quotes": len(quotes),
        "hull_report_dates": len({q.report_date for q in quotes}),
        "latest_hull_report": snap.hull_report_date.isoformat(),
        "local": f"{snap.local.region} {snap.local.basis} "
                 f"${snap.local.price_low:g}-{snap.local.price_high:g}",
        "truck_quarter": snap.truck_quarter,
        "diesel": snap.diesel and f"{snap.diesel[0]} ${snap.diesel[1]}",
        "almond": snap.almond_price,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m biocycle.refresh", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-history", action="store_true", help="skip the ESMIS archive")
    p.add_argument("--max-pages", type=int, default=None,
                   help="limit ESMIS listing pages (about 10 weekly reports each)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        summary = run(not args.no_history, args.max_pages)
    except (httpx.HTTPError, RuntimeError, ValueError) as e:
        print(f"Refresh failed: {e}. The previous snapshot in {MARKET_DIR} is unchanged.",
              file=sys.stderr)
        return 1
    for k, v in summary.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

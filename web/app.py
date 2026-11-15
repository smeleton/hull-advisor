"""BioCycle Byproduct Value Advisor — FastAPI web app (server-rendered HTML, no JS build step).

One material at a time (almond hulls by default; ?biomass=<id> picks another from
data/biomass/).

Every request recomputes from the submitted form: inputs -> biocycle.valuation / allocation ->
templates. The only server-side state is per-browser-session report history and LLM spend,
kept in memory (a local prototype; restarting the server clears it).

Run:  .venv/bin/uvicorn web.app:app --reload --port 8000
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import markdown as md_lib
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from biocycle import present as p
from biocycle.allocation import Allocation, allocate
from biocycle.config import (
    DEFAULT_BIOMASS,
    DEFAULT_HAUL_RATE,
    HAUL_RATE_RANGE,
    HAUL_RATE_SOURCE,
    PLACEHOLDER_BANNER,
)
from biocycle import directory, feedback, nutrients, sustainability
from biocycle.data import (
    MARKET_LABEL,
    Biomass,
    Buyer,
    Outlet,
    Source,
    load_biomasses,
    load_buyers,
    load_sources,
)
from biocycle.evidence import LEVEL_LABEL, Models, load_models
from biocycle.pathways import FeedValue, feed_value, value_all, value_material
from biocycle.llm import LLMError, LLMResult
from biocycle.market import (
    LOCAL_REGION,
    TRUCK_PAYLOAD_SHORT_TONS,
    MarketSnapshot,
    apply_snapshot,
    load_hull_history,
    load_snapshot,
)
from biocycle.report import (
    Material,
    NumberMatch,
    Report,
    build_payload,
    generate_report,
    report_download,
)
from biocycle.valuation import Lot, any_placeholder, evaluate_biomass

WEB_DIR = Path(__file__).resolve().parent
SESSION_COOKIE = "hva_session"

app = FastAPI(title="Byproduct Value Advisor")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(
    money=p.money, tons=p.tons_fmt, TIER_LABEL=p.TIER_LABEL, TIER_HELP=p.TIER_HELP,
    STATUS_LABEL=p.STATUS_LABEL, NO_QUOTE_TEXT=p.NO_QUOTE_TEXT, slug=p.slug,
    PLACEHOLDER_BANNER=PLACEHOLDER_BANNER, default_price_text=p.default_price_text,
    default_price_tier=p.default_price_tier, MARKET_LABEL=MARKET_LABEL,
    pretty_unit=p.pretty_unit, num=p.num,
    range_text=p.range_text, price_num=p.price_num, LEVEL_LABEL=LEVEL_LABEL,
)

# Evidence pathways, in the order the Sources tab lists them.
PATHWAY_LABEL = {
    "animal_feed": "Animal feed",
    "bioenergy": "Bioenergy: biomass power plant",
    "bioenergy_htl": "Bioenergy: hydrothermal bio-oil and hydro-char",
    "fermentation_yeast_protein": "Fermentation: high-protein yeast",
    "fermentation_ethanol": "Fermentation: ethanol and biogas",
    "biomaterial_hydrochar": "Biomaterials: hydrochar",
}


# ---------- data ----------


@lru_cache
def load_biomass_map() -> dict[str, Biomass]:
    return load_biomasses(load_sources())


def get_biomass(biomass_id: str | None) -> Biomass:
    bms = load_biomass_map()
    return bms.get(str(biomass_id or ""), bms[DEFAULT_BIOMASS])


@lru_cache
def load_data(biomass_id: str = DEFAULT_BIOMASS
              ) -> tuple[dict[str, Source], dict[str, Outlet], tuple[Buyer, ...]]:
    """(sources, outlets, fictional sample buyers) for one biomass."""
    bm = get_biomass(biomass_id)
    return load_sources(), bm.outlets, tuple(load_buyers(bm.outlets, biomass=bm.id))


@lru_cache
def load_models_cached() -> Models:
    return load_models(load_data()[0])


def literature(models: Models) -> dict[str, Any]:
    """Model breakdowns for the templates: conversion models and material models by id."""
    return {"conversion": value_all(models),
            "material": {k: value_material(m, models) for k, m in models.material.items()}}


@lru_cache
def load_hull_history_cached() -> tuple:
    return tuple(load_hull_history())


# ---------- per-session state ----------


@dataclass
class Session:
    calls: list[LLMResult] = field(default_factory=list)
    reports: dict[str, Report] = field(default_factory=dict)

    @property
    def spend(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def tokens(self) -> int:
        return sum(c.input_tokens + c.output_tokens for c in self.calls)


SESSIONS: dict[str, Session] = {}


def get_session(request: Request) -> tuple[str, Session]:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid or sid not in SESSIONS:
        sid = secrets.token_urlsafe(16)
        SESSIONS[sid] = Session()
    return sid, SESSIONS[sid]


def with_cookie(resp: Response, sid: str) -> Response:
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return resp


# ---------- computation ----------


@dataclass(frozen=True)
class Computed:
    lot: Lot
    results: pd.DataFrame
    buyer_rows: list[dict[str, Any]]
    alloc: Allocation | None
    buyer_errors: list[str]
    mode: str  # "market" (USDA data) or "demo" (outlets.yaml values)
    haul_mode: str  # "usda" (rates by distance) or "flat"
    outlets: dict[str, Outlet]
    sus_outlet: str  # option shown on the Sustainability tab
    biomass: Biomass
    feed: FeedValue | None = None  # nutrient-replacement value of the hulls (Petersen)

    @property
    def show_banner(self) -> bool:
        return any_placeholder(self.results) or (
            self.alloc is not None and self.alloc.result_tier == "placeholder")


def market_snapshot() -> MarketSnapshot | None:
    """The latest refreshed market data, or None if `python -m biocycle.refresh` never ran."""
    return _cached_snapshot()


@lru_cache
def _cached_snapshot() -> MarketSnapshot | None:
    return load_snapshot()


def resolve_modes(mode: str | None, haul_mode: str | None,
                  market_data: bool = True) -> tuple[str, str]:
    """Price source (USDA market data exists for almond hulls only) and hauling mode."""
    snap = market_snapshot()
    mode = mode if mode in ("market", "demo") else ("market" if snap else "demo")
    if mode == "market" and (snap is None or not market_data):
        mode = "demo"
    can_usda = snap is not None and bool(snap.truck_rates)
    default_haul = "usda" if can_usda and (mode == "market" or not market_data) else "flat"
    haul_mode = haul_mode if haul_mode in ("usda", "flat") else default_haul
    if haul_mode == "usda" and not can_usda:
        haul_mode = "flat"
    return mode, haul_mode


def outlets_for(mode: str, bm: Biomass | None = None) -> dict[str, Outlet]:
    bm = bm or get_biomass(None)
    snap = market_snapshot()
    if mode == "market" and snap and bm.market_data:
        return apply_snapshot(bm.outlets, snap)
    return bm.outlets


def sample_buyer_rows(bm: Biomass | None = None) -> list[dict[str, Any]]:
    bm = bm or get_biomass(None)
    return [{c: getattr(b, c) for c in p.BUYER_FIELDS} for b in load_data(bm.id)[2]]


def compute(form: Any | None, mode: str | None = None, haul_mode: str | None = None,
            biomass: str | None = None) -> Computed:
    form = form or {}
    bm = get_biomass(biomass or form.get("biomass"))
    sample = load_data(bm.id)[2]
    mode, haul_mode = resolve_modes(mode or form.get("data_mode"),
                                    haul_mode or form.get("haul_mode"), bm.market_data)
    outlets = outlets_for(mode, bm)
    lot = p.lot_from_form(form, default_tons=bm.default_tons)
    if haul_mode == "usda":
        lot = replace(lot, haul_schedule=market_snapshot().truck_rates, haul_rate_tier="sourced")
    overrides = p.overrides_from_form(form, outlets)
    results = evaluate_biomass(lot, bm, overrides, outlets)
    rows = p.buyer_rows_from_form(form) if hasattr(form, "getlist") else sample_buyer_rows(bm)
    buyers, tiers, errors = p.buyers_from_rows(rows, outlets, list(sample))
    alloc = allocate(buyers, lot, results, tiers, must_place=bm.must_place) if buyers else None
    sus = str(form.get("sus_outlet") or "")
    sus = sus if sus in outlets else str(results.iloc[0]["outlet_id"])
    # The literature feed-value check describes almond hulls only.
    feed = (feed_value(load_models_cached(), corn_price=_price_field(form, "corn_price"),
                       sbm_price=_price_field(form, "sbm_price"))
            if bm.id == DEFAULT_BIOMASS else None)
    return Computed(lot, results, rows, alloc, errors, mode, haul_mode, outlets, sus, bm, feed)


def _price_field(form: Any, name: str) -> float | None:
    v = p.parse_number(form.get(name))
    return v if v is not None and v > 0 else None


def payload_hash(c: Computed) -> str:
    payload = build_payload(c.lot, c.results, load_sources_cached(), Material.of(c.biomass),
                            literature_for_report(c))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@lru_cache
def load_sources_cached() -> dict[str, Source]:
    return load_sources()


@lru_cache
def load_directory_cached() -> tuple:
    every = {oid: o for bm in load_biomass_map().values() for oid, o in bm.outlets.items()}
    return tuple(directory.load_directory(every))


def directory_groups(c: Computed) -> list[dict[str, Any]]:
    best = str(c.results.iloc[0]["outlet_id"])
    groups = []
    for g in c.biomass.directory_groups:
        key, title, outlet_id, blurb, empty = g.key, g.title, g.outlet, g.blurb, g.empty
        points_here = g.outlets
        outlet = c.outlets[outlet_id]
        items = []
        for co in load_directory_cached():
            if not set(points_here) & set(co.outlets):
                continue
            subject, body = directory.quote_request(c.lot, co, outlet, c.biomass)
            items.append({"company": co, "subject": subject, "body": body,
                          "mailto": directory.mailto(subject, body)})
        groups.append({"key": key, "title": title, "outlet_id": outlet_id, "blurb": blurb,
                       "empty": empty, "companies": items, "is_best": best in points_here})
    groups.sort(key=lambda g: not g["is_best"])
    return groups


def biomass_sources(bm: Biomass, outlets: dict[str, Outlet], sources: dict[str, Source]
                    ) -> dict[str, Source]:
    """Sources this material's page cites: its outlets, profile, nutrients and the shared ones."""
    ids = set(bm.source_ids)
    for o in outlets.values():
        ids |= set(o.source_ids) | {s for cp in o.coproducts for s in cp.source_ids}
    if bm.id == DEFAULT_BIOMASS:  # the literature evidence library and feed-value check
        models = load_models_cached()
        ids |= {e.source_id for e in models.evidence.values()}
        ids |= {models.feed.method_source} if getattr(models, "feed", None) else set()
    ids |= {n.source_id for n in bm.nutrients.values() if n.source_id}
    ids |= {c.source_id for c in bm.composition if c.source_id}
    text = " ".join([bm.summary, bm.ca_volume, *bm.quality_notes, *bm.caveats,
                     *(t for o in outlets.values() for t in (*o.requirements, *o.risks))])
    ids |= {s for s in sources if f"[{s}" in text or f", {s}" in text}
    ids |= {"epa_ghg_hub2025", "epa_wasted_food_scale", "nutr_dtn_retail_2026_11_11"}
    if any(o.nutrient_routes for o in outlets.values()):
        doc = nutrients.load_recovery()
        ids |= {s for r in doc["routes"].values() for s in r.get("source_ids") or ()}
    if bm.market_data:
        ids |= {"ams_3512", "usda_gtor", "eia_ca_diesel", "nass_ncit", HAUL_RATE_SOURCE}
    else:
        ids |= {"usda_gtor", HAUL_RATE_SOURCE}
    return {k: v for k, v in sources.items() if k in ids}


def outlet_buyers(c: Computed) -> list[dict[str, Any]]:
    """Per option: the listed companies that could take the lot for it (Compare -> Find buyers)."""
    out = []
    for oid, outlet in c.outlets.items():
        items = []
        for co in load_directory_cached():
            if oid in co.outlets:
                subject, body = directory.quote_request(c.lot, co, outlet, c.biomass)
                items.append({"company": co, "subject": subject, "body": body,
                              "mailto": directory.mailto(subject, body)})
        items.sort(key=lambda i: i["company"].fit == "possible")
        out.append({"outlet_id": oid, "name": outlet.name, "companies": items})
    return out


def context(c: Computed, session: Session) -> dict[str, Any]:
    all_sources = load_sources_cached()
    outlets = c.outlets
    bm = c.biomass
    sources = biomass_sources(bm, outlets, all_sources)
    snap = market_snapshot()
    history = [q for q in load_hull_history_cached()
               if q.region == LOCAL_REGION and q.delivered] if bm.market_data else []
    top = c.results.iloc[0]
    models = load_models_cached()
    base_row = c.results.loc[c.results["is_baseline"]].iloc[0]
    local_total = c.results.loc[c.results["is_baseline"], "total_expected"].iloc[0]
    base_name = c.results.loc[c.results["is_baseline"], "name"].iloc[0]
    return {
        "biomass": bm,
        "biomasses": list(load_biomass_map().values()),
        "rules": bm.rules,
        "baseline_name": base_name,
        "vs_label": p.vs_label(base_name),
        "lot": c.lot,
        "results": p.result_rows(c.results),
        "top": top,
        "quality_flags": p.quality_flags(c.results),
        "chart": Markup(p.chart_svg(c.results, baseline_label=p.short_label(base_name),
                                    noun=bm.noun)),
        "outlets": outlets,
        "sources": sources,
        "all_sources": all_sources,
        "coproducts": p.coproduct_names(outlets),
        "buyer_rows": c.buyer_rows,
        "buyer_errors": c.buyer_errors,
        "alloc": c.alloc,
        "alloc_rows": c.alloc.table.to_dict("records") if c.alloc else [],
        "local_total": local_total,
        "show_banner": c.show_banner,
        "payload_hash": payload_hash(c),
        "session": session,
        "intent_choices": feedback.INTENT_CHOICES,
        "default_haul": DEFAULT_HAUL_RATE,
        "haul_range": HAUL_RATE_RANGE,
        "haul_source": HAUL_RATE_SOURCE,
        "fiber_max": bm.rules.crude_fiber_max,
        "mode": c.mode,
        "haul_mode": c.haul_mode,
        "snap": snap,
        "truck_payload": TRUCK_PAYLOAD_SHORT_TONS,
        "history_chart": (Markup(p.history_chart_svg(
            [(q.report_date, q.average) for q in history],
            title=f"{LOCAL_REGION} delivered almond hull price, weekly average"))
            if len(history) > 1 else None),
        "history_span": (history[0].report_date, history[-1].report_date) if history else None,
        "history_count": len(history),
        "directory_groups": directory_groups(c),
        "outlet_buyers": outlet_buyers(c),
        "sus": sustainability.report_data(c.lot, c.results, outlets, c.sus_outlet, all_sources,
                                          biomass=bm),
        "sus_rows": list(sustainability.assess(c.lot, c.results, outlets, biomass=bm).values()),
        "sus_alloc": (sustainability.assess_allocation(c.alloc.table, outlets, biomass=bm)
                      if c.alloc is not None else []),
        "sus_outlet": c.sus_outlet,
        "lit": literature(models),
        "evidence": models.evidence,
        "evidence_groups": evidence_groups(models),
        "feed": c.feed,
        "feed_spec": models.feed_value,
        "base_price": (base_row["price_low"], base_row["price_high"]),
        "cited_by": {
            s: [o.name for o in outlets.values()
                if s in o.source_ids or any(s in cp.source_ids for cp in o.coproducts)]
            for s in sources
        },
    }


def evidence_groups(models: Models) -> list[tuple[str, list]]:
    """Evidence records grouped by their first pathway, in PATHWAY_LABEL order."""
    groups: dict[str, list] = {k: [] for k in PATHWAY_LABEL}
    for e in models.evidence.values():
        key = e.pathways[0] if e.pathways else "other"
        groups.setdefault(key, []).append(e)
    return [(PATHWAY_LABEL.get(k, k.replace("_", " ").capitalize()), v)
            for k, v in groups.items() if v]


def literature_for_report(c: Computed) -> dict[str, Any]:
    """Literature numbers the AI report may cite: models behind each outlet, feed value, evidence."""
    models = load_models_cached()
    lit = literature(models)
    rows = c.results.set_index("outlet_id")
    out: dict[str, Any] = {"conversion_models": [], "material_models": [], "evidence": []}
    used: list[str] = []
    for oid, r in rows.iterrows():
        used += list(r["evidence_ids"])
        price_model, material_model = p.opt_str(r["price_model"]), p.opt_str(r["material_model"])
        if price_model:
            mv = lit["conversion"][price_model]
            out["conversion_models"].append({
                "outlet_id": oid, "model": mv.name,
                "products": [{"name": pv.name, "amount_low_per_dry_ton": pv.amount_low,
                              "amount_high_per_dry_ton": pv.amount_high,
                              "amount_unit": pv.amount_unit, "price_low": pv.price_low,
                              "price_high": pv.price_high, "price_unit": pv.price_unit,
                              "price_tier": pv.price_tier} for pv in mv.products],
                "conversion_cost_per_dry_ton": mv.conversion_cost,
                "conversion_cost_tier": mv.conversion_cost_tier,
                "processor_can_pay_low_per_ton": mv.price_low,
                "processor_can_pay_high_per_ton": mv.price_high,
                "product_value_multiple_needed_to_match_local_dairy":
                    mv.multiple_needed(float(r["break_even_price"])),
            })
        if material_model:
            mat = lit["material"][material_model]
            lo, hi = mat.price_needed(float(r["break_even_price"]))
            out["material_models"].append({
                "outlet_id": oid, "model": mat.name, "product": mat.product,
                "yield_low_per_dry_ton": mat.yield_low, "yield_high_per_dry_ton": mat.yield_high,
                "conversion_cost_per_dry_ton": mat.conversion_cost,
                "conversion_cost_tier": mat.conversion_cost_tier,
                "product_price_needed_to_match_local_dairy_low": lo,
                "product_price_needed_to_match_local_dairy_high": hi,
                "product_price_unit": f"usd/{mat.product_unit}",
            })
    if c.feed is not None:
        f = c.feed
        out["feed_value"] = {
            "method": "Petersen (corn + soybean meal replacement)", "source_id": "stpierre2004",
            "value_low_per_ton": f.low, "value_high_per_ton": f.high, "tier": f.tier,
            "corn_price_per_ton": [f.corn_price_low, f.corn_price_high],
            "soybean_meal_price_per_ton": [f.sbm_price_low, f.sbm_price_high],
            "scenarios": [{"name": sc.name, "value_low_per_ton": sc.value_low,
                           "value_high_per_ton": sc.value_high} for sc in f.scenarios],
            "caveat": "A reference check, not a price offered: assumes corn and soybean meal are "
                      "fairly priced and values only energy and protein [stpierre2004].",
        }
        used += list(f.evidence_ids)
    for eid in dict.fromkeys(used):
        e = models.evidence[eid]
        out["evidence"].append({
            "id": e.id, "source_id": e.source_id, "parameter": e.parameter,
            "value_low": e.value_low, "value_high": e.value_high, "unit": e.unit,
            "evidence_level": e.evidence_level, "feedstock": e.feedstock})
    return out


# ---------- report rendering ----------


def report_html(markdown_text: str, unmatched: list[NumberMatch]) -> Markup:
    """Markdown -> HTML with unmatched numbers wrapped in <mark class="unmatched">.

    Model text is HTML-escaped (except '>' so blockquotes still work) before rendering.
    """
    def esc(t: str) -> str:
        return t.replace("&", "&amp;").replace("<", "&lt;")

    out, last = [], 0
    for n in sorted(unmatched, key=lambda n: n.start):
        out.append(esc(markdown_text[last:n.start]))
        out.append(f'<mark class="unmatched" title="Not in the computed results">'
                   f'{esc(n.text)}</mark>')
        last = n.end
    out.append(esc(markdown_text[last:]))
    return Markup(md_lib.markdown("".join(out), extensions=["tables", "sane_lists"]))


# ---------- routes ----------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, data: str | None = None, biomass: str | None = None) -> Response:
    sid, session = get_session(request)
    c = compute(None, mode=data, biomass=biomass)
    resp = templates.TemplateResponse(request, "index.html", context(c, session))
    return with_cookie(resp, sid)


@app.post("/calculate", response_class=HTMLResponse)
async def calculate(request: Request) -> Response:
    sid, session = get_session(request)
    c = compute(await request.form())
    resp = templates.TemplateResponse(request, "partials/swap.html", context(c, session))
    return with_cookie(resp, sid)


@app.post("/report", response_class=HTMLResponse)
async def report(request: Request) -> Response:
    sid, session = get_session(request)
    c = compute(await request.form())
    sources = load_sources_cached()
    ctx: dict[str, Any] = {"error": None, "report": None, "session": session}
    try:
        rep = generate_report(c.lot, c.results, sources, material=Material.of(c.biomass),
                              literature=literature_for_report(c))
    except LLMError as e:
        ctx["error"] = str(e)
    else:
        rid = secrets.token_urlsafe(8)
        session.calls.append(rep.llm)
        session.reports[rid] = rep
        ctx.update(report=rep, rid=rid, html=report_html(rep.markdown, rep.unmatched),
                   payload_hash=payload_hash(c))
    resp = templates.TemplateResponse(request, "partials/report.html", ctx)
    return with_cookie(resp, sid)


@app.post("/feedback", response_class=HTMLResponse)
async def send_feedback(request: Request) -> Response:
    """Store one feedback/outcome submission with a snapshot of the current prediction."""
    sid, _ = get_session(request)
    form = await request.form()
    c = compute(form)
    try:
        entry = feedback.build_entry(form, c.lot, c.results, sid, payload_hash(c),
                                     data_mode=c.mode, haul_mode=c.haul_mode,
                                     biomass=c.biomass.id)
        feedback.record(entry)
    except feedback.FeedbackError as e:
        resp = HTMLResponse(f'<span class="fb-error">{escape(str(e))}</span>', status_code=422)
    except OSError:
        resp = HTMLResponse('<span class="fb-error">Couldn&#39;t save your feedback. '
                            "Please try again.</span>", status_code=500)
    else:
        resp = HTMLResponse('<span class="fb-ok">Thanks — your feedback was saved.</span>')
    return with_cookie(resp, sid)


@app.post("/sustainability.{fmt}")
async def sustainability_download(fmt: str, request: Request) -> Response:
    """The Sustainability tab as a PDF or Markdown file, for the option chosen there."""
    if fmt not in ("pdf", "md"):
        return PlainTextResponse("Unknown format.", status_code=404)
    c = compute(await request.form())
    data = sustainability.report_data(c.lot, c.results, c.outlets, c.sus_outlet,
                                      load_sources_cached(), biomass=c.biomass)
    name = f"{c.biomass.id}_sustainability_{c.sus_outlet}.{fmt}"
    if fmt == "pdf":
        return Response(sustainability.to_pdf(data), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})
    return PlainTextResponse(sustainability.to_markdown(data), media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/report/{rid}.md")
def report_markdown(rid: str, request: Request) -> Response:
    _, session = get_session(request)
    rep = session.reports.get(rid)
    if rep is None:
        return PlainTextResponse("Report not found. Generate it again.", status_code=404)
    return PlainTextResponse(
        report_download(rep), media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="{rep.payload["material"]["id"]}_value_report.md"'})


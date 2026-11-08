"""BioCycle Hull Value Advisor — FastAPI web app (server-rendered HTML, no JS build step).

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
    BASELINE_ID,
    CRUDE_FIBER_MAX,
    DEFAULT_HAUL_RATE,
    HAUL_RATE_RANGE,
    HAUL_RATE_SOURCE,
    PLACEHOLDER_BANNER,
)
from biocycle import feedback, sustainability
from biocycle.data import Buyer, Outlet, Source, load_buyers, load_outlets, load_sources
from biocycle.llm import LLMError, LLMResult
from biocycle.market import (
    LOCAL_REGION,
    TRUCK_PAYLOAD_SHORT_TONS,
    MarketSnapshot,
    apply_snapshot,
    load_hull_history,
    load_snapshot,
)
from biocycle.report import NumberMatch, Report, build_payload, generate_report, report_download
from biocycle.valuation import Lot, any_placeholder, evaluate

WEB_DIR = Path(__file__).resolve().parent
SESSION_COOKIE = "hva_session"

app = FastAPI(title="Hull Value Advisor")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")
templates.env.globals.update(
    money=p.money, tons=p.tons_fmt, TIER_LABEL=p.TIER_LABEL, TIER_HELP=p.TIER_HELP,
    STATUS_LABEL=p.STATUS_LABEL, NO_QUOTE_TEXT=p.NO_QUOTE_TEXT, slug=p.slug,
    PLACEHOLDER_BANNER=PLACEHOLDER_BANNER, default_price_text=p.default_price_text,
    default_price_tier=p.default_price_tier,
)


# ---------- data ----------


@lru_cache
def load_data() -> tuple[dict[str, Source], dict[str, Outlet], tuple[Buyer, ...]]:
    sources = load_sources()
    outlets = load_outlets(sources)
    return sources, outlets, tuple(load_buyers(outlets))


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


def resolve_modes(mode: str | None, haul_mode: str | None) -> tuple[str, str]:
    snap = market_snapshot()
    mode = mode if mode in ("market", "demo") else ("market" if snap else "demo")
    if mode == "market" and snap is None:
        mode = "demo"
    can_usda = snap is not None and bool(snap.truck_rates)
    default_haul = "usda" if mode == "market" and can_usda else "flat"
    haul_mode = haul_mode if haul_mode in ("usda", "flat") else default_haul
    if haul_mode == "usda" and not can_usda:
        haul_mode = "flat"
    return mode, haul_mode


def outlets_for(mode: str) -> dict[str, Outlet]:
    outlets = load_data()[1]
    snap = market_snapshot()
    return apply_snapshot(outlets, snap) if mode == "market" and snap else outlets


def sample_buyer_rows() -> list[dict[str, Any]]:
    return [{c: getattr(b, c) for c in p.BUYER_FIELDS} for b in load_data()[2]]


def compute(form: Any | None, mode: str | None = None, haul_mode: str | None = None) -> Computed:
    _, _, sample = load_data()
    form = form or {}
    mode, haul_mode = resolve_modes(mode or form.get("data_mode"),
                                    haul_mode or form.get("haul_mode"))
    outlets = outlets_for(mode)
    lot = p.lot_from_form(form)
    if haul_mode == "usda":
        lot = replace(lot, haul_schedule=market_snapshot().truck_rates, haul_rate_tier="sourced")
    overrides = p.overrides_from_form(form, outlets)
    results = evaluate(lot, outlets, overrides, baseline_id=BASELINE_ID)
    rows = p.buyer_rows_from_form(form) if hasattr(form, "getlist") else sample_buyer_rows()
    buyers, tiers, errors = p.buyers_from_rows(rows, outlets, list(sample))
    alloc = allocate(buyers, lot, results, tiers) if buyers else None
    sus = str(form.get("sus_outlet") or "")
    sus = sus if sus in outlets else str(results.iloc[0]["outlet_id"])
    return Computed(lot, results, rows, alloc, errors, mode, haul_mode, outlets, sus)


def payload_hash(c: Computed) -> str:
    payload = build_payload(c.lot, c.results, load_data()[0])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def context(c: Computed, session: Session) -> dict[str, Any]:
    sources = load_data()[0]
    outlets = c.outlets
    snap = market_snapshot()
    history = [q for q in load_hull_history_cached()
               if q.region == LOCAL_REGION and q.delivered]
    top = c.results.iloc[0]
    local_total = c.results.loc[c.results["is_baseline"], "total_expected"].iloc[0]
    return {
        "lot": c.lot,
        "results": p.result_rows(c.results),
        "top": top,
        "quality_flags": p.quality_flags(c.results),
        "chart": Markup(p.chart_svg(c.results)),
        "outlets": outlets,
        "sources": sources,
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
        "fiber_max": CRUDE_FIBER_MAX,
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
        "sus": sustainability.report_data(c.lot, c.results, outlets, c.sus_outlet, sources),
        "sus_rows": list(sustainability.assess(c.lot, c.results, outlets).values()),
        "sus_alloc": (sustainability.assess_allocation(c.alloc.table, outlets)
                      if c.alloc is not None else []),
        "sus_outlet": c.sus_outlet,
        "cited_by": {
            s: [o.name for o in outlets.values()
                if s in o.source_ids or any(s in cp.source_ids for cp in o.coproducts)]
            for s in sources
        },
    }


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
def index(request: Request, data: str | None = None) -> Response:
    sid, session = get_session(request)
    c = compute(None, mode=data)
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
    sources = load_data()[0]
    ctx: dict[str, Any] = {"error": None, "report": None, "session": session}
    try:
        rep = generate_report(c.lot, c.results, sources)
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
        entry = feedback.build_entry(form, c.lot, c.results, sid, payload_hash(c))
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
    data = sustainability.report_data(c.lot, c.results, c.outlets, c.sus_outlet, load_data()[0])
    name = f"hull_sustainability_{c.sus_outlet}.{fmt}"
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
        headers={"Content-Disposition": 'attachment; filename="hull_value_report.md"'})


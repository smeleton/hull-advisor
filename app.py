"""BioCycle Hull Value Advisor — single-page Streamlit prototype.

All numbers come from biocycle.valuation; this file only collects inputs and displays results.
"""

from __future__ import annotations

import math
import textwrap
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from biocycle.config import (
    BASELINE_ID,
    CRUDE_FIBER_MAX,
    DEFAULT_HAUL_RATE,
    HAUL_RATE_RANGE,
    HAUL_RATE_SOURCE,
    PLACEHOLDER_BANNER,
)
from biocycle.data import Outlet, Source, load_outlets, load_sources
from biocycle.valuation import Lot, Override, any_placeholder, evaluate

TIER_BADGE = {"sourced": "🟢 sourced", "user": "🔵 user", "placeholder": "🟠 placeholder"}
NO_QUOTE_TEXT = "No public market price yet. Enter a buyer quote to evaluate."
BAR_COLOR = "#2a78d6"
MUTED_COLOR = "#8a8985"
INELIGIBLE_COLOR = "#c8c6bf"

# Editor column names
COL_YOUR_PRICE = "Your price $/t"
COL_MILES = "Miles (one-way)"
COL_PROCESSING = "Processing $/t"

st.set_page_config(page_title="Hull Value Advisor", page_icon="🌰", layout="wide")


@st.cache_data
def load_data() -> tuple[dict[str, Source], dict[str, Outlet]]:
    sources = load_sources()
    return sources, load_outlets(sources)


# ---------- formatting ----------


def is_missing(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def money(x: float | None, signed: bool = False) -> str:
    if is_missing(x):
        return "—"
    r = round(x)
    sign = "−" if r < 0 else ("+" if signed and r > 0 else "")
    return f"{sign}${abs(r):,.0f}"


def default_price_text(o: Outlet, outlets: dict[str, Outlet]) -> str:
    if o.price_from:
        base = o.price_from.replace("_", " ")
        if o.price_premium:
            return f"{base} price + ${o.price_premium:,.0f} ({o.premium_tier} premium)"
        return f"Same as {base} price"
    if o.price_low is None:
        return "No public price — enter a quote"
    high = o.price_high if o.price_high is not None else o.price_low
    rng = f"${o.price_low:,.0f}" if high == o.price_low else f"${o.price_low:,.0f}–{high:,.0f}"
    return f"{rng} ({o.price_tier})"


# ---------- tab 1: lot ----------


def lot_inputs() -> Lot:
    st.markdown(
        "Tell us about your lot of almond hulls. The app compares what each outlet would earn "
        "**per ton, after processing and hauling**, against selling to the local dairy. "
        "All math is deterministic; every number is tagged as 🟢 sourced, 🔵 entered by you, "
        "or 🟠 a demo placeholder."
    )
    st.caption("Units: US short tons (2,000 lb) of hulls as delivered · prices in $/ton · "
               "distances in one-way miles.")
    c1, c2 = st.columns(2)
    tons = c1.number_input("Tons of hulls in this lot", min_value=1.0, value=1000.0,
                           step=50.0, key="tons")
    lo, hi = HAUL_RATE_RANGE
    haul = c2.number_input(
        "Hauling rate ($ per ton-mile)", min_value=0.0, value=DEFAULT_HAUL_RATE, step=0.01,
        format="%.2f", key="haul_rate",
        help=f"Default ${DEFAULT_HAUL_RATE:.2f}; illustrative sourced range "
             f"${lo:.2f}–{hi:.2f} [{HAUL_RATE_SOURCE}].",
    )
    if not lo <= haul <= hi:
        c2.caption(f"Outside the sourced range ${lo:.2f}–{hi:.2f}/ton-mile.")

    st.subheader("Quality (optional)")
    c3, c4, c5 = st.columns(3)
    moisture = c3.number_input("Moisture %", min_value=0.0, max_value=100.0, value=None,
                               step=0.5, placeholder="Not measured", key="moisture")
    fiber = c4.number_input(
        "Crude fiber %", min_value=0.0, max_value=100.0, value=None, step=0.5,
        placeholder="Not measured", key="crude_fiber",
        help=f"CDFA hull feed standard: below {CRUDE_FIBER_MAX:g}% [wcngg2022].",
    )
    afla = c5.radio("Aflatoxin tested?", ["Unknown", "Yes", "No"], horizontal=True,
                    key="aflatoxin")
    return Lot(
        tons=tons,
        haul_rate=haul,
        haul_rate_tier="sourced" if math.isclose(haul, DEFAULT_HAUL_RATE) else "user",
        moisture_pct=moisture,
        crude_fiber_pct=fiber,
        aflatoxin_tested=afla.lower(),
    )


# ---------- tab 2: compare ----------


def outlet_editor_frame(outlets: dict[str, Outlet]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "outlet_id": o.id,
            "Outlet": o.name,
            "Status": o.status,
            "Default price": default_price_text(o, outlets),
            COL_YOUR_PRICE: float("nan"),
            COL_MILES: o.default_miles,
            "Miles tier": TIER_BADGE[o.miles_tier],
            COL_PROCESSING: o.processing_cost_per_ton,
            "Processing tier": TIER_BADGE[o.processing_tier],
        }
        for o in outlets.values()
    ])


def edited_value(value: Any, default: float) -> float | None:
    """Return the user's value if it differs from the default, else None."""
    if is_missing(value) or math.isclose(float(value), default):
        return None
    return float(value)


def coproduct_inputs(outlets: dict[str, Outlet]) -> dict[str, float]:
    """One input per coproduct price; returns only the prices the user changed."""
    changed: dict[str, float] = {}
    seen: set[str] = set()
    for o in outlets.values():
        for c in o.coproducts:
            if c.name in seen:
                continue
            seen.add(c.name)
            value = st.number_input(
                f"{c.name} price ($/lb) — {TIER_BADGE[c.unit_price_tier]} default",
                min_value=0.0, value=c.unit_price_per_lb, step=0.05, format="%.2f",
                key=f"coproduct_{c.name}",
                help="Recovered kernels may grade lower than standard kernels. "
                     "Yield 1.0–1.3% of hull weight is sourced [abc_food2025].",
            )
            if not math.isclose(value, c.unit_price_per_lb):
                changed[c.name] = value
    return changed


def outlet_overrides(outlets: dict[str, Outlet]) -> dict[str, Override]:
    st.markdown("**Outlet assumptions.** Edit *Your price*, miles, or processing cost. "
                "Tier columns describe the default values; anything you change is treated "
                "as 🔵 user data.")
    edited = st.data_editor(
        outlet_editor_frame(outlets),
        key="outlet_editor",
        hide_index=True,
        width="stretch",
        disabled=["Outlet", "Status", "Default price", "Miles tier", "Processing tier"],
        column_config={
            "outlet_id": None,
            COL_YOUR_PRICE: st.column_config.NumberColumn(
                min_value=0.0, format="$%.0f",
                help="A buyer quote. Replaces the default price range with one number."),
            COL_MILES: st.column_config.NumberColumn(min_value=0.0, format="%.0f"),
            COL_PROCESSING: st.column_config.NumberColumn(min_value=0.0, format="$%.0f"),
        },
    )
    return overrides_from_rows(edited.to_dict("records"), outlets, coproduct_inputs(outlets))


def overrides_from_rows(
    rows: list[dict[str, Any]], outlets: dict[str, Outlet], cp_prices: dict[str, float]
) -> dict[str, Override]:
    """Turn edited outlet-table rows into overrides; values equal to the defaults are ignored."""
    overrides: dict[str, Override] = {}
    for r in rows:
        o = outlets[r["outlet_id"]]
        price = None if is_missing(r[COL_YOUR_PRICE]) else float(r[COL_YOUR_PRICE])
        miles = edited_value(r[COL_MILES], o.default_miles)
        processing = edited_value(r[COL_PROCESSING], o.processing_cost_per_ton)
        cps = {name: p for name, p in cp_prices.items() if any(c.name == name for c in o.coproducts)}
        if price is not None or miles is not None or processing is not None or cps:
            overrides[o.id] = Override(price, miles, processing, cps)
    return overrides


def checks_text(row: pd.Series) -> str:
    if row["needs_quote"]:
        return "❓ needs quote"
    if not row["eligible"]:
        return "⛔ ineligible"
    n = len(row["flags"])
    return f"⚠️ {n} check{'s' if n > 1 else ''}" if n else "✓"


def results_table(df: pd.DataFrame) -> pd.DataFrame:
    def vs(row: pd.Series, col: str) -> str:
        return "baseline" if row["is_baseline"] else money(row[col], signed=True)

    def rng(row: pd.Series) -> str:
        if row["needs_quote"] or math.isclose(row["net_low"], row["net_high"]):
            return "—"
        return f"{money(row['net_low'])} to {money(row['net_high'])}"

    return pd.DataFrame({
        "Rank": df["rank"],
        "Option": df["name"],
        "Expected net $/t": [
            "needs quote" if r["needs_quote"] else money(r["net_expected"])
            for _, r in df.iterrows()
        ],
        "Range $/t": [rng(r) for _, r in df.iterrows()],
        "Expected total": df["total_expected"].map(money),
        "vs. selling locally $/t": [vs(r, "vs_baseline_per_ton") for _, r in df.iterrows()],
        "vs. selling locally (lot)": [vs(r, "vs_baseline_total") for _, r in df.iterrows()],
        "Break-even price $/t": df["break_even_price"].map(money),
        "Data": df["result_tier"].map(TIER_BADGE),
        "Checks": [checks_text(r) for _, r in df.iterrows()],
    })


def chart_label(row: pd.Series) -> str:
    tags = [t for t, on in (("baseline", row["is_baseline"]), ("ineligible", not row["eligible"]))
            if on]
    name = "<br>".join(textwrap.wrap(row["name"], 26))
    return f"{name}<br>({', '.join(tags)})" if tags else name


def results_chart(df: pd.DataFrame) -> go.Figure:
    labels = [chart_label(r) for _, r in df.iterrows()]
    priced = ~df["needs_quote"]
    expected = df["net_expected"].where(priced)
    fig = go.Figure(go.Bar(
        x=[None if is_missing(v) else v for v in expected],
        y=labels,
        orientation="h",
        marker=dict(color=[BAR_COLOR if ok else INELIGIBLE_COLOR for ok in df["eligible"]]),
        error_x=dict(
            type="data", symmetric=False,
            array=(df["net_high"] - df["net_expected"]).fillna(0),
            arrayminus=(df["net_expected"] - df["net_low"]).fillna(0),
            color=MUTED_COLOR, thickness=1.5, width=5,
        ),
        customdata=np.stack([df["net_low"], df["net_high"]], axis=-1),
        hovertemplate="<b>%{y}</b><br>Expected: $%{x:,.0f}/ton"
                      "<br>Range: $%{customdata[0]:,.0f} to $%{customdata[1]:,.0f}/ton"
                      "<extra></extra>",
    ))
    base_net = df.loc[df["is_baseline"], "net_expected"].iloc[0]
    fig.add_vline(
        x=base_net, line_dash="dash", line_color=MUTED_COLOR, line_width=1.5,
        annotation_text=f"Local dairy {money(base_net)}/t", annotation_position="top",
    )
    for label, (_, r) in zip(labels, df.iterrows()):
        if r["needs_quote"]:
            fig.add_annotation(
                x=0, y=label, xanchor="left", showarrow=False,
                text=f"  Needs a quote · break-even {money(r['break_even_price'])}/t",
            )
    fig.update_layout(
        height=120 + 64 * len(df),
        margin=dict(l=10, r=20, t=40, b=40),
        barcornerradius=4,
        bargap=0.4,
        showlegend=False,
        xaxis=dict(title="Expected net value ($ per ton of hulls)", tickprefix="$",
                   zeroline=True),
        yaxis=dict(autorange="reversed", automargin=True, ticksuffix="  "),
    )
    return fig


def headline(df: pd.DataFrame, tons: float) -> None:
    top = df.iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("Best priced option", top["name"])
    c2.metric("Expected net", f"{money(top['net_expected'])}/ton",
              help=f"Data tier: {TIER_BADGE[top['result_tier']]}")
    if top["is_baseline"]:
        c3.metric("vs. selling locally", "This is the local sale")
    else:
        c3.metric(f"vs. selling locally ({tons:,.0f} t)", money(top["vs_baseline_total"], True),
                  delta=f"{money(top['vs_baseline_per_ton'], True)}/ton")
    if not top["eligible"]:
        st.error("No eligible priced option: every priced outlet fails a quality requirement.")


def quality_summary(df: pd.DataFrame) -> None:
    flags = sorted({f for fs in df["flags"] for f in fs if f != "needs quote"})
    if flags:
        st.warning("**Quality checks for this lot**\n\n" + "\n".join(f"- {f}" for f in flags))


def outlet_details(df: pd.DataFrame, sources: dict[str, Source]) -> None:
    st.subheader("Details, risks and sources")
    for _, r in df.iterrows():
        with st.expander(f"{r['rank']}. {r['name']} — {TIER_BADGE[r['result_tier']]}"):
            if r["needs_quote"]:
                st.info(f"{NO_QUOTE_TEXT} To beat selling locally, a quote must exceed "
                        f"**{money(r['break_even_price'])}/ton**.")
            else:
                st.markdown(
                    f"Price {money(r['price_low'])}–{money(r['price_high'])} − processing "
                    f"{money(r['processing_cost'])} − hauling {money(r['haul_cost'])} "
                    f"({r['miles']:,.0f} mi) + coproducts {money(r['coproduct_low'])}–"
                    f"{money(r['coproduct_high'])} = **{money(r['net_low'])} to "
                    f"{money(r['net_high'])}/ton**. Break-even price vs. local: "
                    f"{money(r['break_even_price'])}/ton."
                )
            for f in r["flags"]:
                if f != "needs quote":
                    st.markdown(f"- {f}")
            c1, c2 = st.columns(2)
            c1.markdown("**Requirements**\n\n" + "\n".join(f"- {x}" for x in r["requirements"]))
            c2.markdown("**Risks**\n\n" + "\n".join(f"- {x}" for x in r["risks"]))
            st.markdown("**Input data tiers:** " + " · ".join(
                f"{k} {TIER_BADGE[v]}" for k, v in r["input_tiers"].items()))
            if r["source_ids"]:
                st.markdown("**Sources:** " + " · ".join(
                    f"[{sid}]({sources[sid].url})" for sid in r["source_ids"]))


def compare_tab(lot: Lot, outlets: dict[str, Outlet], sources: dict[str, Source]) -> pd.DataFrame:
    overrides = outlet_overrides(outlets)
    df = evaluate(lot, outlets, overrides, baseline_id=BASELINE_ID)
    st.divider()
    headline(df, lot.tons)
    quality_summary(df)
    st.dataframe(results_table(df), hide_index=True, width="stretch")
    st.caption("Break-even price: what this outlet would have to pay per ton for its expected "
               "net to equal selling to the local dairy. Expected = midpoint of the low–high range.")
    st.plotly_chart(results_chart(df), width="stretch")
    outlet_details(df, sources)
    return df


# ---------- tab 5: sources ----------


def sources_tab(sources: dict[str, Source], outlets: dict[str, Outlet]) -> None:
    st.markdown("Every 🟢 sourced number traces to one of these. 🟠 Placeholder numbers are "
                "made up for the demo; 🔵 user numbers are what you entered.")
    for s in sources.values():
        cited_by = [o.name for o in outlets.values()
                    if s.id in o.source_ids or any(s.id in c.source_ids for c in o.coproducts)]
        st.markdown(f"**[{s.title}]({s.url})** `{s.id}`  \n{s.what_it_supports}")
        if cited_by:
            st.caption("Used by: " + ", ".join(cited_by))


# ---------- page ----------


def main() -> None:
    sources, outlets = load_data()
    st.title("BioCycle Hull Value Advisor")
    st.caption("What's the best thing to do with this lot of almond hulls, and how much more "
               "would it earn than selling it to the local dairy?")
    banner = st.empty()
    with st.sidebar:
        st.markdown("**Data tiers**\n\n"
                    "- 🟢 **sourced**: from a cited source\n"
                    "- 🔵 **user**: entered by you\n"
                    "- 🟠 **placeholder**: made up for the demo\n\n"
                    "A result is as weak as its weakest input.")

    tab_lot, tab_compare, tab_report, tab_buyers, tab_sources = st.tabs(
        ["Your lot", "Compare options", "Report", "Buyers", "Sources"])
    with tab_lot:
        lot = lot_inputs()
    with tab_compare:
        results = compare_tab(lot, outlets, sources)
    with tab_report:
        st.info("AI report — coming in Phase 3.")
    with tab_buyers:
        st.info("Buyer table and allocation — coming in Phase 4.")
    with tab_sources:
        sources_tab(sources, outlets)

    if any_placeholder(results):
        banner.warning(PLACEHOLDER_BANNER, icon="⚠️")


if __name__ == "__main__":
    main()

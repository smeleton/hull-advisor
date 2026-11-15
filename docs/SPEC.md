# Original prototype spec (historical)

> Written before the build started; kept for context. The shipped app differs (FastAPI instead of Streamlit, eight materials instead of almond hulls only). See the [README](../README.md).

# BioCycle Hull Value Advisor — Prototype Spec

## Context
BioCycle AI helps whoever controls a lot of almond hulls (a huller/sheller, a grower co-op, or a
farmer who gets their hulls back) answer one question:

> "What is the best thing to do with this lot of hulls, and how much more would it earn than
> selling it to the local dairy?"

This is a DEMO PROTOTYPE to ship tonight (~4–5 hours of build). It runs locally as a Streamlit app.
Ignore scalability, auth, deployment, and databases.

**Design rules (do not violate):**
1. All calculations are deterministic Python. The LLM only writes the explanation, using numbers
   the code already computed.
2. Every number carries a **tier**: `sourced` (from sources.yaml), `user` (entered in the UI), or
   `placeholder` (made up for the demo). If any placeholder affects a result, show a persistent
   banner: "Demo data — verify with real buyer quotes before acting."
3. Never invent company names or contacts. Sample buyers in the allocation demo stay fictional
   ("Example Dairy A"). Real companies appear only in the buyer directory
   (`data/buyers_directory.yaml`), and only with public business information backed by a page
   we loaded (evidence URL, quote, date checked). Never personal names, emails or phone numbers,
   and never guessed URLs. The app never contacts anyone; quote requests are drafts the user
   sends. (Updated for the cofounder review, Nov 2026.) The directory lists only companies that
   take, buy or process someone else's material, never other producers of the same residue, and
   every quote is re-checked against its page before it is added (multi-biomass, Nov 14, 2026).

## Stack
Python 3.11+, Streamlit, pandas, scipy (`linprog` with HiGHS), pyyaml, plotly, anthropic SDK,
python-dotenv, pytest. API key in `.env` as `ANTHROPIC_API_KEY` (ship `.env.example`, gitignore
`.env`). Model name in config: `claude-sonnet-5-5`. All LLM calls go through one wrapper.

## Layout
```
hull-advisor/
  app.py                    # single Streamlit page with tabs
  biocycle/
    config.py               # model name, token prices, defaults
    data.py                 # load + validate YAML/CSV into pydantic or dataclasses
    valuation.py            # net value per ton per outlet, ranges, break-even
    allocation.py           # (Phase 4) split tons across buyers with an LP
    llm.py                  # Claude wrapper: retries, token + cost logging
    report.py               # AI report + number guardrail
  data/
    outlets.yaml
    sources.yaml
    buyers_sample.csv
  tests/
  requirements.txt
  README.md
```

## Units
US short tons (2,000 lb) of hulls as delivered. Prices in $/ton. Distances in one-way miles.
Hauling cost = rate ($/ton-mile) × miles.

## Data files

### sources.yaml (seed exactly these; each: id, title, url, what_it_supports)
- `giannini2026` — UC Giannini Foundation, "Almond Shell Circularity in California's Bioeconomy" (2026),
  https://s.giannini.ucop.edu/uploads/pub/2026/06/26/v29n5_3_mg7Nqvg.pdf —
  recent hull feed price ≈ $76–106/ton; illustrative hauling rates $0.20–0.40 per ton-mile.
- `wcngg2022` — WCNGG, "Almond Hull Prices are a Bright Spot" (2022),
  https://wcngg.com/2022/04/18/almond-hull-prices-are-a-bright-spot-for-the-almond-industry/ —
  price history ($93/ton 2020, $130 2021); CDFA hull feed standard < 15% crude fiber; delivered
  at 88–91% dry matter.
- `farmprogress_low` — Farm Progress, "Almond hulls – Low prices call for expanded markets",
  https://www.farmprogress.com/tree-nuts/almond-hulls-low-prices-call-for-expanded-markets —
  ~$60/ton slump; ~97% of hulls go to dairy rations; rail shipments to Texas.
- `abc_food2025` — Almond Board of California, "Unlocking New Value: Commercializing Almond Hulls
  as Food Ingredients",
  https://www.almonds.org/almond-industry/industry-news/unlocking-new-value-commercializing-almond-hulls-food-ingredients —
  1–1.3% of hull-pile weight is recoverable kernel; hulls traded below production cost.
- `gras2026` — AgInfo, "Unlocking New Uses for Almond Hulls",
  https://www.aginfo.net/report/65481/32/Unlocking-New-Uses-for-Almond-Hulls —
  almond hull powder judged GRAS for human food (emerging market, no public price).

### outlets.yaml
Each outlet: `id, name, description, status (established | emerging | experimental),
price_low, price_high (nullable), price_tier, processing_cost_per_ton, processing_tier,
default_miles, coproducts (list), requirements (list), risks (list), source_ids`.

Seed these:
1. **local_dairy** (baseline, established): price $76–106 [sourced: giannini2026];
   processing $0; default 20 miles. Requirement: meets feed standard (<15% crude fiber);
   risk: price volatility ($60–170+ in recent years) [wcngg2022, farmprogress_low].
2. **distant_dairy** (established): same base price as local_dairy plus a placeholder premium;
   default 250 miles; note the Texas rail precedent [farmprogress_low].
3. **clean_and_recover** (emerging): clean the hulls, recover kernels, then still sell the
   cleaned hulls to the local dairy. Coproduct: recovered kernel at 1.0–1.3% of hull weight
   [sourced: abc_food2025] × recovered-kernel price ($/lb, **placeholder default, editable**;
   note recovered kernels may grade lower). Cleaning cost per ton: placeholder.
4. **food_ingredient** (emerging): hull powder [gras2026]. price = null → UI shows
   "No public market price yet. Enter a buyer quote to evaluate." Processing cost: placeholder.
5. **compost_soil** (experimental): all placeholder values.
6. **bioenergy** (experimental): all placeholder values.

### buyers_sample.csv (fictional)
`buyer, outlet_id, price_per_ton, max_tons, miles, notes` — about 6 rows, e.g. "Example Dairy A".
Editable in the UI.

## Valuation (valuation.py)
Inputs (the "lot"): tons T, haul rate r ($/ton-mile, default $0.30, sourced range 0.20–0.40),
optional quality fields (moisture %, crude fiber %, aflatoxin tested yes/no/unknown), and per-outlet
overrides (price, miles, processing cost).

For each outlet:
- net_per_ton = price − processing_cost − r × miles + Σ coproduct_value_per_ton
- compute **low / expected / high**: low/high from price ranges and coproduct ranges, expected = midpoint
- total = net_per_ton × T
- **vs_baseline** = expected − local_dairy expected (per ton and total)
- **break-even price**: the outlet price at which its expected net equals the baseline
- **eligibility flags**: crude fiber ≥ 15% → flag feed outlets; aflatoxin unknown → warning for
  feed and food outlets; price missing → "needs quote"
- each output carries the tiers of its inputs; if any input is placeholder, the result is
  marked placeholder

Return a tidy DataFrame ranked by expected net per ton (outlets needing a quote listed last).

## UI (app.py, tabs)
1. **Your lot:** tons, haul rate, quality fields. A short explainer of what the app does.
2. **Compare options:**
   - editable outlet table (price, miles, processing cost, with tier badges)
   - ranked results table and a bar chart of expected net $/ton with low–high error bars, with a
     baseline line
   - "vs. selling locally" column; break-even prices; flags and risks
   - placeholder banner when relevant
3. **Report:** "Generate report" button (Phase 3).
4. **Buyers:** fictional buyer table, editable; allocation results (Phase 4).
5. **Sources:** list sources.yaml with links and what each supports.

## AI report (report.py, Phase 3)
- Send Claude a JSON payload: lot inputs, ranked outlet results, flags, tiers, cited source ids
  with titles and URLs.
- System prompt: plain English for a huller manager or farmer. Use ONLY numbers in the JSON.
  Clearly separate sourced facts from placeholder assumptions. End with: "These are estimates.
  Confirm prices with buyer quotes and test hull quality before deciding."
- **Number guardrail:** regex-extract every number from the report; each must match a payload
  number (with rounding tolerance, including formatted forms like $1,234 or 12.5%). Highlight
  unmatched numbers in the UI.
- Sections: Recommendation, How the options compare, What could change the answer (break-evens),
  Quality and risk checks, Assumptions and sources.
- Download as Markdown. llm.py logs tokens and estimated cost; show spend in the sidebar.

## Buyer allocation (allocation.py, Phase 4 — only if time allows)
LP over buyers b with tons y_b:
maximize Σ (price_b − processing_b − r·miles_b) · y_b
subject to Σ y_b ≤ T, 0 ≤ y_b ≤ max_tons_b.
Solve with `scipy.optimize.linprog(method="highs")` (negate the objective). Show tons per buyer,
total value, and the marginal value of one more ton of each buyer's capacity (from the HiGHS duals).

## Tests (pytest; mock the LLM)
- net value arithmetic, including transport and coproducts
- clean_and_recover adds kernel value on top of the cleaned-hull sale
- ranking order; vs_baseline sign; break-even price makes net equal the baseline
- placeholder tier propagates to the result
- missing price → "needs quote", never ranked as best
- report guardrail catches an injected fake number
- (Phase 4) LP on a 2-buyer case returns the known optimum

## Build phases (stop after each so I can run it)
1. (~1.5 h) Data files, loaders, valuation.py, tests.
2. (~1.5 h) Streamlit tabs 1, 2, 5, with banner and chart.
3. (~45 min) llm.py, report.py, guardrail, Report tab.
4. (~45 min, optional) Buyer table and allocation LP.

**If behind schedule, cut Phase 4, then Phase 3.** A working comparison calculator is the must-ship.

## Working agreement
- Start in plan mode: read this spec, propose a plan for Phase 1, wait for my approval.
- Keep code simple; type hints; small functions.
- Never present placeholder values as real. Real companies only per design rule 3.
- Run tests after each phase and tell me the exact run command (`streamlit run app.py`).
- Ask before adding dependencies not listed above. Commit after each working phase.

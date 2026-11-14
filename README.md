# BioCycle Byproduct Value Advisor (demo prototype)

Answers one question for whoever controls a lot of a crop residue:

> What is the best thing to do with this lot, and how does it compare with today's usual
> route (for almond hulls, selling to the local dairy)?

Materials (pick one at the top of "Your lot"; each lives in `data/biomass/<id>.yaml`):

| Material | Who has it | Today's usual route (baseline) |
|---|---|---|
| Almond hulls | Hullers and shellers | Sell to the local dairy (USDA market data) |
| Almond shells | Hullers and shellers | Dairy bedding |
| Pistachio hulls | Pistachio processors | Wet hulls to cattle feed |
| Grape pomace | Wineries | Wet pomace to cattle feed |
| Tomato pomace | Tomato processors | Wet pomace to dairy feed |
| Orange peels | Juice plants and packers | Pressed peel to cattle feeders |
| Mango peels | Fresh-cut processors | Compost / green bin (small California volume) |
| Kiwi peels | Kiwifruit processors | Compost (small California volume) |

Each profile has composition, N-P-K-Mg content, quality rules, 5–7 outlets and verified
companies. Outside almond hulls most prices are **Demo values**: no public market exists, so
enter real quotes. Wet residues are perishable, so the buyer split always places the whole lot
(even where a buyer charges a gate fee, entered as a negative price). Outlets that dry or
extract on site use `mass_yield` (tons of product per ton of input): the price is per ton of
product, and only the product is hauled.

**Nutrient recovery** (`data/nutrients.yaml`): the N, P, K and Mg in a lot are valued at
Nov 2026 DTN fertilizer prices for each recovery route on composting, digestion and land
application outlets (retained in compost, digestate spread on land, struvite, ammonia
stripping, biochar). The value counts in the net only where the producer keeps the nutrients
(spreading on their own land); otherwise it is shown for information, because the composter's
or digester's gate fee already reflects it.

**Adding a material:** copy a file in `data/biomass/`, keep outlet ids unique, give the
baseline a price, cite sources by id from `data/sources.yaml`, and add fictional sample
buyers to `data/buyers_sample.csv` (with the `biomass` column). The loader validates all of it.

All calculations are deterministic Python (`biocycle/valuation.py`). Every number is tagged
**Sourced** (`data/sources.yaml`), **Your entry** (entered in the UI), or **Demo value**
(a placeholder made up for the demo). If any placeholder affects a result, the app shows a banner:
*Demo data — verify with real buyer quotes before acting.* Sample buyers are fictional.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY (needed from Phase 3, the AI report)
```

## Run

```bash
.venv/bin/uvicorn web.app:app --reload --port 8000
# then open http://localhost:8000
```

## Test

```bash
.venv/bin/pytest -q
```

## Real market data

```bash
.venv/bin/python -m biocycle.refresh              # latest USDA/EIA data + hull price history
.venv/bin/python -m biocycle.refresh --no-history # latest reports only
```

The refresh pulls public, keyless sources and writes small committed files to `data/market/`
(the app never fetches at request time; downloads are cached in the gitignored
`data/market/cache/`):

| Data | Source | Used for |
|---|---|---|
| Almond hull asking prices by region, weekly (FOB and delivered) | USDA AMS report 3512 + ESMIS archive (`ams_3512`) | Local dairy price (San Joaquin Valley, delivered); southern California dairy price; price history |
| Truck rates per loaded mile at 25/100/200 mi, quarterly | USDA Grain Truck and Ocean Rate Advisory (`usda_gtor`) | Hauling by distance: rate ÷ 27.5-ton load, interpolated by miles |
| California diesel, weekly | EIA (`eia_ca_diesel`) | Context only |
| Shelled almond price, annual | USDA NASS (`nass_ncit`) | Ceiling for recovered-kernel value (the kernel price stays a demo value) |

In the app, **Prices from: USDA market** (the default when a snapshot exists) uses these numbers,
tagged *Sourced*; **Demo values** uses `data/biomass/almond_hulls.yaml`. (Market data
exists for almond hulls only.) **Hauling: USDA by distance** or a
flat rate. The Sources tab shows the latest quotes and the weekly price history.
`python -m evals.run market` compares demo and market results and replays the recommendation
over every week of real prices.

## Sustainability summary (Sustainability tab)

For dairies, food companies and lenders asking about Scope 3 and circularity. Deterministic,
no LLM (`biocycle/sustainability.py`, factors in `data/emission_factors.yaml`):

- **Kept in productive use:** every option is a use, not disposal.
- **Hauling emissions:** tons × one-way miles × 0.1875 kg CO2e per short ton-mile (EPA GHG
  Emission Factors Hub 2025, Table 8, medium/heavy-duty truck; AR5 GWPs).
- **End-of-use emissions** only where EPA has a factor: composting (0.14) and combustion (0.05)
  t CO2e per short ton, using EPA's yard-trimmings rows as labelled stand-ins (tagged *Demo value*).
- **Position on EPA's Wasted Food Scale**, stated as a comparison (the scale is for wasted food).
- **Not included**, listed in every report: avoided emissions, processing energy, emissions after
  delivery, orchard operations. No avoided-emissions claims are made.
- Per option or per buyer (from the allocation); PDF (fpdf2) and Markdown downloads carry the
  demo-data banner and data tiers.

## Buyer directory (Find buyers tab)

Real California companies that buy, trade or work with almond hulls (`data/buyers_directory.yaml`),
grouped by kind of buyer, with the group matching your best option first. Each entry has only
public business information: what the company does with hulls, location, website and contact
page, plus the evidence page, a short quote and the date checked. The loader rejects anything that
looks like an email address or phone number. Categories we couldn't verify (e.g. composters that
publicly accept hulls) say so instead of guessing.

Each company has a **quote-request draft** built from your lot (tons, aflatoxin, fiber,
moisture) to copy or open in your own email app, and **Add to my buyer list**, which puts the
company in the allocation table so you can enter its quote (counted as *Your entry*). The app
never contacts anyone. Re-check entries before relying on them; companies change.

## Evaluation

See [EVALUATION.md](EVALUATION.md). In short:

```bash
.venv/bin/python -m evals.run math       # golden cases, invariants, LP cross-check, backtest
.venv/bin/python -m evals.run report     # AI report graders + LLM judge (calls Claude, capped)
.venv/bin/python -m evals.run feedback   # in-app usefulness + reported sale outcomes
```

## How the numbers work

Per outlet, in $/ton of hulls (r = haul rate in $/ton-mile, miles one-way):

```
net = price − processing_cost − r × miles + coproduct value per ton
```

- Low/high come from the price and coproduct ranges; expected is the midpoint.
- `vs. selling locally` = expected net − local dairy expected net.
- `Price to match local` = the price an outlet's buyer would have to pay for its expected net to
  equal the local dairy's. It is blank for the local dairy itself and for outlets sold at the
  local dairy's price (a price change moves both equally).
- Outlets with no public price show "needs quote" and are always ranked last; their price to
  match local tells you what quote would beat selling locally.
- If crude fiber is ≥ 15%, feed outlets are flagged ineligible and ranked below eligible ones.

## Literature engine (research papers → numbers → math)

Experimental pathways are backed by the research literature, not just placeholders:

- `data/evidence.yaml`: one record per number taken from a paper (value, unit, the sentence or
  table it came from, conditions, feedstock, evidence level: lab, in vitro, animal trial, feed
  table, market report…). Almond-*shell* results are kept for context but never used in hull math.
- `data/models.yaml`: how the numbers combine. The loader checks units multiply out
  (`t_biooil/t_dry × GJ/tonne_biooil × tonne/t → GJ/t_dry`).
- `biocycle/pathways.py`:
  - **Conversion models** (hydrothermal bio-oil + char, yeast protein, ethanol + biogas): what a
    processor could pay at break-even = Σ product yield × price − processing cost, × dry matter.
    The details show how many times today's product value it would take to match selling locally.
  - **Material model** (hydrochar as a material): no market price, so it answers the reverse
    question: what hydrochar would have to sell for so a processor could pay you the local price.
  - **Feed value check** (Petersen method [stpierre2004]): what corn + soybean meal with the same
    NEL and crude protein would cost (USDA corn and soybean-meal prices, editable).
- Outlets point at these with `price_model`, `material_model` and `evidence_ids`. The Compare tab
  shows each model's breakdown and evidence; the Sources tab lists every record; the AI report
  gets the same numbers (so the number guardrail allows them).

To add a paper: add the source to `sources.yaml`, one record per number to `evidence.yaml`, then
reference the ids from `models.yaml` or an outlet's `evidence_ids`. `pytest` catches unknown ids
and unit mistakes.

## AI report (Report tab)

Claude (`claude-sonnet-5-5`, set in `biocycle/config.py`) writes a plain-English report from a
JSON payload of the computed results. It never does the math.

- **Number guardrail:** every number in the report is extracted and must match a payload number,
  within the precision it is written at (`$98,000` may round 97,850; `$41,250` must be exact to
  ±$5). Unmatched numbers are highlighted in red and listed at the top of the download.
- **Spend:** `biocycle/llm.py` is the single Claude wrapper. It retries 429/5xx/connection errors
  (SDK, 3 retries) and logs tokens and estimated cost to `logs/llm_usage.jsonl`. The page header shows
  this session's spend.
- **Refusal fallback:** requests opt into Anthropic's server-side fallback
  (`fallbacks="default"`), so a safety-classifier decline is retried on another model; the
  cost estimate prices each model's tokens at its own rate.

## Buyer allocation (Buyers tab)

Splits the lot across specific buyers with a linear program
(`scipy.optimize.linprog`, HiGHS):

```
maximize   Σ net_b × tons_b          net_b = quote − processing − haul rate × miles + coproducts
subject to Σ tons_b ≤ lot tons,  0 ≤ tons_b ≤ buyer max tons
```

Processing and coproduct values come from the buyer's outlet (including your edits on the
Compare tab). Buyers whose outlet fails a quality check get no tons. The tab shows tons and value
per buyer, the best total, and from the HiGHS duals: the value of one more ton of hulls and of
one more ton of each buyer's capacity. Sample buyers are fictional; their quotes are placeholders.

## Web app

A FastAPI app with server-rendered HTML (Jinja templates, one small vanilla JS file, no build
step), designed desktop-first for huller and sheller managers:

- **Your lot** (left rail, always visible): tons, haul rate, optional quality fields.
- **Compare options**: the answer first, then a ranked table, a chart of expected net $/ton
  with low–high whiskers and the local-dairy line, editable price/miles/processing per outlet,
  and per-outlet details, risks and sources.
- **Split across buyers**: editable buyer rows (add/remove) and the allocation LP.
- **Written report**: the Claude report with the number guardrail and Markdown download.
- **Sources**: every cited source.

Every edit posts the whole form to `/calculate`; the server recomputes and returns HTML
fragments that `web/static/app.js` swaps in. The only server state is per-browser-session
report history and AI spend, held in memory (restarting the server clears it).

## Layout

```
web/app.py             FastAPI routes: /, /calculate, /report, /report/{id}.md, /feedback,
                       /sustainability.{pdf,md}
web/templates/         page and partial templates
web/static/            style.css, app.js, favicon
biocycle/config.py     model name, units, defaults
biocycle/data.py       YAML/CSV loaders and validation (biomass profiles, outlets)
biocycle/nutrients.py  fertilizer value of N, P, K, Mg by recovery route
biocycle/valuation.py  net value, ranges, break-even, flags, tiers
biocycle/present.py    UI-free helpers: form parsing, formatting, table rows, SVG chart
biocycle/llm.py        Claude wrapper: retries, refusal handling, token + cost logging
biocycle/report.py     report payload, system prompt, number guardrail
biocycle/allocation.py buyer allocation LP and dual values
biocycle/evidence.py   literature evidence + models loader, unit checks
biocycle/pathways.py   conversion/material models and the Petersen feed value
data/                  sources.yaml, biomass/<id>.yaml, nutrients.yaml, evidence.yaml,
                       models.yaml, emission_factors.yaml, buyers_directory.yaml,
                       buyers_sample.csv
tests/                 pytest suite (web routes via FastAPI TestClient; LLM mocked)
```

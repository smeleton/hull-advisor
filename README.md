# BioCycle Hull Value Advisor (demo prototype)

Answers one question for whoever controls a lot of almond hulls:

> What is the best thing to do with this lot of hulls, and how much more would it earn than
> selling it to the local dairy?

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
- `Break-even price` = the price at which an outlet's expected net equals the local dairy's.
- Outlets with no public price show "needs quote" and are always ranked last; their break-even
  price tells you what quote would beat selling locally.
- If crude fiber is ≥ 15%, feed outlets are flagged ineligible and ranked below eligible ones.

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
web/app.py             FastAPI routes: /, /calculate, /report, /report/{id}.md
web/templates/         page and partial templates
web/static/            style.css, app.js, favicon
biocycle/config.py     model name, units, defaults
biocycle/data.py       YAML/CSV loaders and validation
biocycle/valuation.py  net value, ranges, break-even, flags, tiers
biocycle/present.py    UI-free helpers: form parsing, formatting, table rows, SVG chart
biocycle/llm.py        Claude wrapper: retries, refusal handling, token + cost logging
biocycle/report.py     report payload, system prompt, number guardrail
biocycle/allocation.py buyer allocation LP and dual values
data/                  sources.yaml, outlets.yaml, buyers_sample.csv
tests/                 pytest suite (web routes via FastAPI TestClient; LLM mocked)
```

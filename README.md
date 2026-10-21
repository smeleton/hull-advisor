# BioCycle Hull Value Advisor (demo prototype)

Answers one question for whoever controls a lot of almond hulls:

> What is the best thing to do with this lot of hulls, and how much more would it earn than
> selling it to the local dairy?

All calculations are deterministic Python (`biocycle/valuation.py`). Every number is tagged
🟢 **sourced** (`data/sources.yaml`), 🔵 **user** (entered in the UI), or 🟠 **placeholder**
(made up for the demo). If any placeholder affects a result, the app shows a banner:
*Demo data — verify with real buyer quotes before acting.* Sample buyers are fictional.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY (needed from Phase 3, the AI report)
```

## Run

```bash
.venv/bin/streamlit run app.py
```

## Test

```bash
.venv/bin/pytest -q
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

## Layout

```
app.py                 Streamlit page (tabs: Your lot, Compare options, Report, Buyers, Sources)
biocycle/config.py     model name, units, defaults
biocycle/data.py       YAML/CSV loaders and validation
biocycle/valuation.py  net value, ranges, break-even, flags, tiers
data/                  sources.yaml, outlets.yaml, buyers_sample.csv
tests/                 pytest suite
```

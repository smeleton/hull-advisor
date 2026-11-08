# Evaluation and validation

How we check that the Hull Value Advisor gives right answers, and keep checking as it changes.
There are three layers, each answering a different question:

| Layer | Question | Cost | Command |
|---|---|---|---|
| 1. Math validation | Is the arithmetic and ranking correct? | free, ~5 s | `python -m evals.run math` |
| 2. AI report eval | Does the written report say only true things, clearly? | ~$0.10 per scenario | `python -m evals.run report` |
| 3. Real-user feedback | Is it useful, and do real sale prices land where we predicted? | free | `python -m evals.run feedback` |
| Real-data check | How do results change on real USDA prices, week by week? | free | `python -m evals.run market` |

Every run prints a summary and writes `evals/results/<UTC time>-<kind>.json` and `.md`.
The command exits non-zero if any check fails, so it can gate a release.

## 1. Math validation (`evals/math_checks.py`)

- **Golden cases** (`evals/golden/valuation_cases.yaml`): six lots worked out by hand on paper,
  including the expected ranking, net $/ton, break-even prices, totals versus the local dairy,
  quality ineligibility and the buyer allocation. They are data, not code: if `outlets.yaml`
  changes, re-derive them by hand. Never copy the numbers from the program's output.
- **Invariants** on 300 seeded random lots: low ≤ expected ≤ high; total = net × tons;
  vs-local = net − local net; pricing an outlet at its break-even makes its net equal the local
  dairy's; 10 more miles cost exactly 10 × haul rate; needs-quote and ineligible outlets are
  never ranked above priced, eligible ones; any placeholder input makes the result placeholder.
- **Allocation cross-check**: on 200 random buyer sets, the HiGHS LP total must equal an
  independent greedy solver (fill the best-paying buyers first, which is optimal for this
  one-constraint problem).
- **Backtest** (`data/price_history.yaml`): replay today's recommendation at each sourced
  historical local hull price, and report whether it still holds and by how much.
  This is a robustness check, not proof the advice was right; real outcomes (layer 3) are.
- **Real weekly backtest** (after `python -m biocycle.refresh`): replay every week of USDA
  almond hull prices since 2022 (local = San Joaquin Valley delivered, south = southern
  California delivered, USDA truck rates by distance) and report how often each option wins.
  The first run (181 weeks, Feb 2022 – Nov 2026) found that shipping to southern California beat
  selling locally in 55 weeks, where the demo values said it always lost by about $29/ton.

## 2. AI report eval (`evals/report_graders.py`, `evals/judge.py`, `evals/scenarios.py`)

Seven scenarios (default lot, high fiber, a food buyer quote, worthless kernels, a tested wet lot,
a small lot with expensive hauling, and a lot with no demo values) are each run through the real
report prompt. The raw model output is graded two ways:

**Code graders** (exact, free):

| Check | Pass condition |
|---|---|
| `numbers_traced` | Every number in the report is in the computed results (the in-app guardrail) |
| `sections_in_order` | Exactly the five required headings, in order |
| `closing_line` | Ends with the required disclaimer sentence, verbatim |
| `recommends_top_option` | The first paragraph of the Recommendation names the top eligible, priced option |
| `placeholder_disclosed` | If demo values affect results, the report says so |
| `quality_flags_surfaced` | Aflatoxin and crude-fiber flags are mentioned when present |
| `citations_valid` | Every `[source_id]` cited exists |
| `within_length` | ≤ 600 words of prose |
| `plain_reading_level` | Flesch–Kincaid grade ≤ 10 |

**LLM judge** (`claude-opus-5-5`, a stronger model than the `claude-sonnet-5-5` report writer):
the judge sees the same payload and scores faithfulness, decision clarity, honesty about
uncertainty, risk coverage and plain language from 1 to 5, quoting any problems. The judge
returns schema-validated JSON (structured outputs). A scenario passes only if every criterion
scores 4 or more.

Spend is capped (`--max-spend`, default $2.00): the runner stops before any scenario whose
estimated cost would cross the cap and marks the rest skipped. Use `--no-judge` for a cheaper
code-only run and `--scenario <id>` to run one scenario.

**When to run:** after any change to the report prompt (`biocycle/report.py`), the model in
`biocycle/config.py`, or the payload. Compare pass rates with the last committed result in
`evals/results/`; don't ship a prompt change that lowers them.

## 3. Real-user feedback (`biocycle/feedback.py`, `evals/feedback_summary.py`)

The Compare tab ends with a short form: *Was this useful? What will you do next?* and,
optionally, *Already sold? Record what you got* (option sold to, price received, tons, miles).
Each submission is saved to `logs/feedback.jsonl` with a snapshot of what the app predicted for
that lot. No names or contact details are collected, and the browser session id is stored only
as a one-way hash. `logs/` is gitignored: feedback is user data and is never committed.

`python -m evals.run feedback` reports:

- usefulness rate and intended next steps;
- for every reported sale, the price received against the predicted price range for that option:
  share inside the range, and mean / median / mean absolute error against the midpoint;
- whether people went with the top option, and recent comments.

**This is the real validation.** Layers 1–2 show the app does what we designed. Only reported
outcomes show whether the prices and recommendations match the market. Ask every pilot user to
record their outcome after each sale, and review the summary monthly. When outcomes for an
outlet fall consistently outside its range, update that outlet's numbers in `data/` (with a
source) and re-run layers 1–2.

## Release checklist

1. `pytest -q` (includes the math checks, graders and feedback with a mocked LLM)
2. `python -m evals.run math`
3. If the report prompt, model or payload changed: `python -m evals.run report` and compare with
   the last result in `evals/results/`
4. Monthly during pilots: `python -m evals.run feedback`

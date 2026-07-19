# CLAUDE.md — Financial Analysis Agent

## What this project is

A single-company financial analysis system with two deliverables:

1. **Agent pipeline** (Python, Claude Agent SDK) — pulls filings from SEC EDGAR,
   extracts figures, validates them, writes a tidy long-format `financials.csv`,
   and produces a written trend analysis.
2. **Streamlit dashboard** — single-company deep-dive reading that CSV.

The same CSV also feeds a **manually built Tableau Public dashboard**. Tableau
Public has no publishing API, so the agent produces the data; the human builds
the viz once by hand.

Full requirements live in `SPEC.md`. Read it before building.

## Stack

- Python 3.10+, virtual environment at `venv/`
- `claude-agent-sdk`, `pandas`, `streamlit`, `plotly`, `requests`, `python-dotenv`
- Pin versions in `requirements.txt`

## Directory layout

```
/agents      orchestrator + agent runners
/prompts     one .md per agent role — EDITABLE, no logic here
/data        data_access.py (all I/O), financials.csv, metadata.json,
             tableau_export.csv, sample data generator
/dashboard   Streamlit app
/docs        Tableau guide, sample analysis report
```

## Data contract (non-negotiable)

All financial data is **tidy long format** — one row per company-period-metric.

| Column | Example | Notes |
|---|---|---|
| `company` | Northwind Devices Inc. | |
| `ticker` | NWD | |
| `period` | 2024-09-30 | ISO date, must parse as Date type |
| `fiscal_year` | 2024 | |
| `fiscal_quarter` | Q3 / FY | |
| `frequency` | Annual \| Quarterly | |
| `category` | Profitability | one of the five analysis categories |
| `statement` | income_statement \| balance_sheet \| cash_flow \| ratio | |
| `metric` | Operating Margin | consistent naming across companies |
| `value` | 28.5 | **raw numbers only** |
| `unit` | USD \| % \| x \| USD/share | |
| `source_url` | https://sec.gov/... | every row, always |

**Rules:**

- `value` is a raw number, never a formatted string. `391035000000`, not `"$391B"`.
  Formatting belongs to the presentation layer.
- `period` must be a real ISO date (`YYYY-MM-DD`) so Tableau and pandas type it
  as a date. Time-series features depend on this.
- Never pre-aggregate. Emit atomic rows; let the dashboard sum/average.
- Every row carries a `source_url` back to the filing. Every figure must be auditable.
- Wide format is a bug. Metrics are rows, not columns.

## Data access

**All CSV reads and writes go through `data/data_access.py`.** No other file touches
`financials.csv` directly — not the dashboard, not the orchestrator, not the exporter.
If you are about to write `pd.read_csv` or `.to_csv` outside that file, stop and use
the accessor instead.

Every write stamps `data/metadata.json` with `last_updated`, `source`
(`sample` | `edgar`), `ticker`, and `row_count`. The dashboard displays this as a
freshness indicator.

This abstraction is what makes scheduled refresh and a future database backend
one-function changes rather than refactors. Honor it.

## Analysis rules

- **Quarterly growth is ALWAYS year-over-year** (Q3 vs prior-year Q3), never
  quarter-over-quarter. Seasonality otherwise makes trends meaningless. This is
  the single most common error in this domain — enforce it in code.
- Coverage: 3–5 years, both annual and quarterly.
- The five analysis categories, in this order:
  1. **Growth** — is the business getting bigger
  2. **Profitability** — does the growth make money
  3. **Cash Generation** — are the profits real
  4. **Financial Health & Solvency** — can it survive a bad year
  5. **Capital Allocation** — is management working for shareholders
- Two diagnostic pairings must always be surfaced together, not in isolation:
  - **Net Income vs Operating Cash Flow** (earnings quality)
  - **Revenue Growth vs Operating Margin** (is growth profitable)

## Hard boundary: analysis, not advice

Output is **financial analysis only**. Never investment advice, never buy/sell/hold
recommendations, never price predictions or valuation targets. Interpret what the
numbers show and where trends point; the human makes the decision.

When writing analysis:
- Quantify and cite every claim with figure and period —
  "operating margin fell from 30.1% (FY2022) to 28.5% (FY2024)", not "margins weakened"
- Explicitly separate observation from inference. State when the numbers cannot
  explain a cause and point to the MD&A instead of inventing a reason.
- Emphasize trajectory and inflection points over verdicts.

## Secrets

- API keys come from environment variables **only**. Never hardcoded, never committed,
  never printed to logs.
- Use `python-dotenv`; keep `.env.example` current with every required variable.
- `.gitignore` must cover `.env`, `venv/`, `__pycache__/`, `*.pyc`, `.DS_Store`.
- Never ask the user to paste a key into chat or into source.

## Cost control

- Model is a **per-agent setting** at the top of the orchestrator, clearly commented.
  Cheap model (Haiku) for extraction/validation; stronger model (Sonnet) for analysis.
- `max_budget_usd` hard cap on the agent loop, defaulted low.
- Prompt caching enabled for repeat runs against the same filings.
- Default `--source sample` so development runs against the sample CSV with zero
  live API calls. Only `--source edgar` spends metered credit.

## Agent roles

Each agent's system prompt lives in its own file under `/prompts`. **Prompts are
data, not code** — editing a role means editing that markdown file only. Load them
at runtime; mark the load site with a comment.

- `research_agent.md` — locates filings via SEC EDGAR
- `extraction_agent.md` — maps raw filing data to the schema
- `validation_agent.md` — sanity-checks before write
- `analysis_agent.md` — interprets trends across the five categories

## Working conventions

- Build against sample data first. Everything must run end-to-end before any
  live EDGAR call.
- SEC EDGAR requires a descriptive `User-Agent` header and rate limiting. Respect both.
- Prefer the structured XBRL `companyfacts` endpoint over parsing PDFs.
- Extraction agents output strict JSON matching the schema — no prose, no markdown fences.
- Validation flags anomalies with severity; it does not silently correct figures.
- Handle a missing API key with a clear message, not a crash.
- Commit after each working stage.

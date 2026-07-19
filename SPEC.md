# SPEC.md — Financial Analysis Agent

Build specification. Read `CLAUDE.md` first for conventions and the data contract;
this document defines *what to build*.

Build in the stages below, **in order**. After each stage, stop, show what was
created, and wait for approval before continuing.

Stages 1–3 build and debug against synthetic sample data so development costs
nothing in API credit. **Stage 3b switches to real filings** and is where the system
is proven against actual company data — the project is not done until that passes.

---

## Stage 1 — Scaffold

Create:

```
financial-agent/
├── CLAUDE.md
├── SPEC.md
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── agents/
│   ├── orchestrator.py
│   └── runners.py
├── prompts/
│   ├── research_agent.md
│   ├── extraction_agent.md
│   ├── validation_agent.md
│   └── analysis_agent.md
├── data/
│   ├── data_access.py          single source of truth for reads/writes
│   ├── generate_sample_data.py
│   ├── financials.csv          (generated)
│   ├── metadata.json           (generated) last_updated, source, row counts
│   └── tableau_export.csv      (generated)
├── dashboard/
│   └── app.py
└── docs/
    ├── tableau_public_guide.md
    └── sample_analysis_report.md
```

Set up a virtual environment. Pin dependencies: `claude-agent-sdk`, `pandas`,
`streamlit`, `plotly`, `requests`, `python-dotenv`.

`.gitignore` must cover `.env`, `venv/`, `__pycache__/`, `*.pyc`, `.DS_Store`.

### Data access layer (build this now, not later)

`data/data_access.py` is the **single source of truth for all data reads and writes**.
No other file reads or writes `financials.csv` directly — not the dashboard, not the
orchestrator, not the Tableau exporter. Everything goes through here.

Minimum interface:

```python
load_financials(frequency=None, category=None, metric=None) -> pd.DataFrame
write_financials(df, source: str) -> None   # also writes metadata.json
load_metadata() -> dict                      # last_updated, source, row_count, ticker
```

`write_financials` must always stamp `data/metadata.json` with:

```json
{
  "last_updated": "2026-07-18T14:32:00Z",
  "source": "sample" | "edgar",
  "ticker": "NWD",
  "row_count": 575,
  "periods_covered": ["2021-09-30", "2025-09-30"]
}
```

**Why:** this is the seam that makes every item in the roadmap (§ Future: Live Updates)
a one-function change instead of a refactor. Swapping CSV for SQLite later means
editing this file only. Skipping this abstraction is the single most expensive
shortcut available in this project.

**Acceptance:** `pip install -r requirements.txt` succeeds in a clean venv. Grepping
the codebase for `read_csv` or `to_csv` returns hits in `data_access.py` only.

---

## Stage 2 — Sample dataset

Write `data/generate_sample_data.py` producing a realistic sample `financials.csv`
for one fictional company, in the exact schema from `CLAUDE.md`.

**Coverage:** 5 fiscal years, annual **and** quarterly (~20 quarters). Model
plausible seasonality in quarterly flow items (income statement, cash flow);
balance-sheet items are point-in-time and should not be seasonally scaled.

**Metrics by category:**

**Growth**
Revenue · Revenue YoY Growth % · EPS (Diluted) · EPS YoY Growth %

**Profitability**
Gross Profit · Operating Income · Net Income · Gross Margin · Operating Margin ·
Net Margin · Return on Equity (ROE) · Return on Invested Capital (ROIC) ·
Return on Assets (ROA)

**Cash Generation**
Operating Cash Flow · Capital Expenditures · Free Cash Flow · FCF Margin ·
OCF-to-Net-Income Ratio

**Financial Health & Solvency**
Total Assets · Total Equity · Cash & Equivalents · Total Debt · Net Debt ·
Debt-to-Equity · Current Ratio · Interest Coverage

**Capital Allocation**
Shares Outstanding (Diluted) · Capex as % of Revenue · Dividends Paid · Share Buybacks

Derived metrics must be internally consistent — Gross Profit = Revenue − COGS,
FCF = OCF − Capex, margins = component ÷ Revenue, and so on. The sample data must
pass the Stage 4 validation rules.

Mark the data clearly as synthetic in the company name and a `README` note. It
exists so the system runs before any live API call.

Write output via `data_access.write_financials(df, source="sample")` — not with a
direct `to_csv` call. This exercises the metadata stamping path from day one.

**Acceptance:** running the script prints row count, distinct metric count, periods
covered, and the first 15 rows. Output loads into pandas with `period` as datetime.
`data/metadata.json` exists and shows `"source": "sample"` with a current timestamp.

---

## Stage 3 — Agent pipeline

Four agents, each with its system prompt in `/prompts/<name>.md`. Prompts are loaded
at runtime — no role text embedded in Python. Mark the load site with a comment.

### research_agent
Given a ticker, locates filings via the SEC EDGAR API. Prefer the structured XBRL
`companyfacts` endpoint over parsing PDFs. Covers the last 3–5 years of 10-K
(annual) and 10-Q (quarterly). Sets a descriptive SEC `User-Agent` header and
rate-limits requests. Returns filing references with URLs.

### extraction_agent
Maps raw filing data into the long-format schema. Handles XBRL tag variation across
companies (the same concept appears under different tags). Attaches a `source_url`
to every row. Outputs **strict JSON** matching the schema — no prose, no markdown
fences, no commentary.

### validation_agent
Runs before anything is written to disk. Checks:

- **Internal consistency** — components sum to totals (Gross Profit = Revenue − COGS;
  FCF = OCF − Capex; Net Debt = Total Debt − Cash)
- **Plausibility** — period-over-period swings within believable bounds; margins
  within sane ranges; no sign flips without cause
- **Earnings quality** — Operating Cash Flow tracks Net Income directionally over
  time; flag sustained divergence
- **Units** — consistent units per metric; no mixed scales (thousands vs millions)
- **Completeness** — required metrics present for each period; gaps reported

Flags anomalies **with severity** (info / warning / error). Does **not** silently
correct figures. Errors block the write; warnings annotate it.

### analysis_agent
Interprets trends across the five categories in `CLAUDE.md` order. Produces both
per-category commentary (for the dashboard) and a full written report.

Must:
- Quantify and cite every claim with figure and period
- Surface the two diagnostic pairings (Net Income vs OCF; Revenue Growth vs
  Operating Margin) rather than reporting metrics in isolation
- Note when share count is falling (buybacks) or rising (dilution)
- Separate observation from inference; state when the numbers cannot explain a
  cause and point to the MD&A
- Emphasize trajectory and inflection points
- Give **no investment advice** — no buy/sell/hold, no price targets, no valuation calls

### orchestrator.py
Runs the four agents in sequence. Requirements:

- **Per-agent model config** in a clearly commented block at the top of the file, so
  models can be reassigned without hunting through code. Default: cheap model
  (Haiku) for extraction and validation, stronger model (Sonnet) for analysis.
- **`max_budget_usd`** hard cap on the loop, defaulted low. Abort cleanly when hit.
- **`--source sample|edgar`** flag, defaulting to `sample`. Sample mode makes zero
  live EDGAR calls.
- **`--ticker`** argument for edgar mode.
- **Prompt caching** enabled for repeat runs against the same filings.
- Clear console progress per stage and a final cost summary.
- All persistence goes through `data_access.write_financials()`, which stamps
  `metadata.json`. The orchestrator never writes CSVs directly.
- Must be **invocable headlessly** — importable as a function and runnable as a
  script with no interactive prompts — so it can later be driven by cron or CI
  without modification.

**Acceptance:** `python agents/orchestrator.py --source sample` runs end-to-end and
produces `financials.csv` plus analysis output.

### Stage 3b — Real-ticker smoke test (do this before Stage 4)

Before moving on, prove the pipeline works against **real** data. This is the step
that catches extraction problems while extraction is still the thing in focus.

Run:

```
python agents/orchestrator.py --source edgar --ticker AAPL
```

Then **manually verify** the output against the actual filing. Open the 10-K that
`source_url` points to and check, at minimum:

- **Revenue and Net Income** for the two most recent fiscal years match the filing
  exactly. Not approximately — exactly.
- **Units and scale are right.** Filings often report in thousands or millions;
  confirm no factor-of-1000 errors. This is the most common extraction failure.
- **Fiscal calendar is correct.** Apple's FY ends late September, not December.
  Confirm `period`, `fiscal_year`, and `fiscal_quarter` align with the company's
  actual fiscal calendar, not the calendar year.
- **Quarterly rows are quarterly, not year-to-date.** Some XBRL facts are cumulative
  YTD figures. Confirm Q3 means the three-month period, not nine months.
- **No missing periods** across the 3–5 year window.
- **Derived metrics reconcile** — gross profit, FCF, and margins recompute correctly
  from their components.

If anything is wrong, **edit `prompts/extraction_agent.md` and re-run.** Expect one
or two rounds of this. XBRL tagging varies between companies — the same concept
appears under different tags, restatements shift historical figures, and fiscal
calendars differ. This iteration is normal and is why the role prompts are editable
files rather than embedded strings.

Keep costs contained: use a low `max_budget_usd`, rely on prompt caching for re-runs
against the same filings, and run extraction on the cheap model.

Once AAPL is clean, run **one more ticker with a different fiscal calendar and
industry** (e.g. `MSFT`, FY ending June, or a retailer with a January year-end) to
confirm the extraction generalizes rather than being tuned to a single company.

**Acceptance:** two real tickers produce schema-valid output whose headline figures
have been manually verified against the source filings, with `metadata.json` showing
`"source": "edgar"`.

---

## Stage 4 — API key setup

Create `.env.example` listing every required environment variable with placeholder
values and a comment explaining what each is and where to obtain it.

Then print an explicit setup message to the user stating:
- exactly which file to create (`.env`, copied from `.env.example`)
- exactly which line to edit to add the Anthropic API key
- the terminal command to verify the key is being read
- confirmation that `.env` is gitignored

Never ask the user to paste a key into chat or into source code. Missing key →
clear, actionable message, not a stack trace.

**Acceptance:** running any entry point without a key produces a readable
instruction, not a crash.

---

## Stage 5 — Streamlit dashboard

`dashboard/app.py` — single-company deep-dive reading `data/financials.csv`.

**Layout:**

- **Header** — company name, ticker, period coverage
- **Freshness indicator** — read `last_updated` and `source` from
  `data_access.load_metadata()` and display them (e.g. "Data as of 2026-07-18 ·
  source: sample"). Flag visibly when data is stale or synthetic.
- **KPI strip** — latest Revenue, Net Income, Free Cash Flow, each with YoY change
- **Global controls** — Annual / Quarterly toggle; date-range selector
- **Five sections**, in `CLAUDE.md` order: Growth → Profitability → Cash Generation
  → Financial Health & Solvency → Capital Allocation

**Charts (Plotly):**

- Quarterly views use **YoY** comparison. Never QoQ. Enforce in code.
- **Net Income vs Operating Cash Flow** — dual-axis, dedicated chart (earnings quality)
- **Revenue Growth vs Operating Margin** — dual-axis, dedicated chart (is growth profitable)
- All three margins on one shared chart
- **Shares Outstanding** as its own trend line, annotated: falling = buybacks,
  rising = dilution
- Tooltips include `source_url`

**Interpretation:**

- Below each section, render the `analysis_agent`'s commentary for that category
- Sidebar chat box: user asks a question, `analysis_agent` answers grounded in the
  actual CSV rows. Pass the relevant data slice, not the whole file.
- Display the advice disclaimer once, in the sidebar

**Data loading:** all reads go through `data_access.load_financials()`. The dashboard
contains no `read_csv` call and no hardcoded file path. Wrap the loader in
`@st.cache_data` with a clearly-commented TTL so cache invalidation is a one-line
change when scheduled refresh arrives.

**Acceptance:** `streamlit run dashboard/app.py` renders all five sections against
sample data. Toggling Annual/Quarterly updates every chart without error. The
freshness indicator shows the timestamp from `metadata.json`.

---

## Stage 6 — Tableau outputs

### data/tableau_export.csv
The same long-format data, verified Tableau-ready:
- ISO dates, typed cleanly
- Raw numbers, no formatted strings
- No pre-aggregation
- Consistent metric naming
- `category` column for filtering by analysis group

### docs/tableau_public_guide.md
Step-by-step for **Tableau Desktop Public Edition**, written for a first-time Tableau
user. Cover:

1. Downloading and installing Tableau Desktop Public Edition
2. Connecting to `tableau_export.csv`
3. Verifying field types — `period` as Date, `value` as Measure, everything else as
   Dimension. Fixing them if Tableau guesses wrong.
4. Building each core trend chart click-by-click, naming which field goes to which
   shelf (Columns / Rows / Filters / Color / Tooltip)
5. Using `metric` and `category` as filters
6. Building the dual-axis Net Income vs Operating Cash Flow chart
7. Adding `source_url` to tooltips for auditability
8. Assembling sheets into a dashboard
9. Publishing

**Must include an explicit warning:** Tableau Public saves workbooks to the public
web by default — anything published is visible to anyone. Do not publish data you
consider private.

**Must also state:** updating means replacing the data source and re-publishing
manually. There is no automated refresh on Tableau Public.

### docs/sample_analysis_report.md
The `analysis_agent`'s full written read on the sample data, organized by the five
categories — the companion document to read alongside the Tableau dashboard.
Includes the synthetic-data note and the advice disclaimer.

**Acceptance:** the CSV opens in Tableau with `period` auto-detected as a date, and
a revenue-over-time line chart is buildable in under five drags following the guide.

---

## Future: live updates (NOT in v1 — design for, don't build)

These are planned follow-ons. **Do not implement them now.** They are documented so
v1 leaves the right seams. If a v1 design decision would make one of these harder,
choose differently.

Context that shapes all of this: **filings change four times a year.** Real-time
refresh is the wrong goal for fundamentals data. Scheduled refresh is what "live"
should mean here.

**A. Scheduled refresh (most likely next step).**
A cron job or GitHub Action runs `orchestrator.py --source edgar` weekly; the
dashboard passively reads whatever is current. Requires the orchestrator to be
headless-invocable (Stage 3) and the dashboard to read via `data_access` with a
sane cache TTL (Stage 5). No dashboard changes needed if those hold.

**B. Manual refresh button.**
A sidebar button triggering a pipeline run. Must include a confirmation step and
respect `max_budget_usd`, since each click spends metered credit. Lower priority
than (A) — it invites accidental spend for data that changes quarterly.

**C. Database backend.**
When the flat CSV becomes unwieldy — multiple companies, long history, concurrent
access — swap to SQLite or Postgres. **This must remain an edit to `data_access.py`
alone.** If it requires touching the dashboard or orchestrator, the Stage 1
abstraction was not honored.

**D. Live market data.**
Price and market cap come from a different API than filings. Would sit in its own
dashboard section with its own refresh cadence, clearly separated from
fundamentals. Do not blend the two data sources in one table.

---

## Out of scope

- Automated publishing to Tableau Public (no API exists for it)
- Multi-company comparison views (single-company deep-dive only for v1)
- Real-time or intraday data
- Scheduled/automated refresh (see § Future — design for it, don't build it)
- Database backend (see § Future)
- Valuation modeling, DCF, price targets
- Any buy/sell/hold output

---

## Definition of done

- [ ] `python data/generate_sample_data.py` produces a schema-valid CSV
- [ ] `python agents/orchestrator.py --source sample` runs end-to-end with no live API calls
- [ ] **Two real tickers** (different fiscal calendars) produce schema-valid output
      via `--source edgar`
- [ ] **Headline figures manually verified** against the source filings — revenue and
      net income match exactly, units and scale correct, fiscal periods correctly
      labeled, quarterly rows are quarterly and not YTD
- [ ] **The dashboard has been viewed with real data**, not only sample data
- [ ] **All CSV reads/writes route through `data_access.py`** — grep for `read_csv`
      and `to_csv` returns hits in that file only
- [ ] **`metadata.json` is written on every data write** with `last_updated` and `source`
- [ ] **Dashboard displays data freshness** from `metadata.json`
- [ ] **Orchestrator is headless-invocable** — importable and script-runnable with
      no interactive prompts
- [ ] All four role prompts are editable markdown files under `/prompts`
- [ ] Per-agent model config and `max_budget_usd` are visible at the top of the orchestrator
- [ ] `streamlit run dashboard/app.py` renders all five sections and both diagnostic charts
- [ ] Quarterly growth is YoY everywhere — verified, not assumed
- [ ] `tableau_export.csv` loads into Tableau with `period` typed as Date
- [ ] Tableau guide includes the public-visibility warning and manual-refresh note
- [ ] No secrets in source or git history; `.env` gitignored
- [ ] No investment advice anywhere in output

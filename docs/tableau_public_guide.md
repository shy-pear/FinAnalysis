# Tableau Public Guide — the FinAnalysis dashboard, end to end

Build the full FinAnalysis dashboard in **Tableau Desktop Public Edition**,
mirroring the Streamlit app: five category tabs, KPI banner, fiscal-period
axis labels, a same-quarter-across-years filter, and every core chart — plus
a design pass to make it look professional. Written for a first-time Tableau
user; examples use the Apple dataset (`tableau_export_AAPL.csv`).

The one Streamlit feature with no Tableau equivalent is the "Ask the analyst"
chat (it calls a live model; Tableau Public can't). The analyst's written
commentary still pairs with this dashboard — keep `docs/analysis_report_*.md`
open alongside it.

> ## ⚠️ Read before you start
>
> 1. **Everything you publish to Tableau Public is public.** Workbooks save to
>    the open web — anyone can view *and download* them, and copies may be
>    cached even if you delete later. SEC filing data is already public, so
>    this dataset is fine — but never point this workflow at private data.
> 2. **There is no automatic refresh.** When the pipeline produces new data,
>    you re-open the workbook, refresh the data source, and re-publish by
>    hand (steps at the end). Filings change ~4×/year, so this is a small
>    chore, but it is always manual.

---

## Part 1 — Setup and data

### 1.1 Install

1. Go to <https://public.tableau.com> → **Create** → **Download Tableau
   Desktop Public Edition** (free, Mac/Windows).
2. Install and sign in with a free Tableau Public account (required — Public
   Edition saves to the web, not to local files).

### 1.2 Connect to the data

1. Start page → **Connect → To a File → Text file**.
2. Pick the **per-ticker file** for the company you're building:
   `data/tableau_export_AAPL.csv`. Each pipeline run maintains one file per
   company; other companies' runs never touch it, so your workbook's source
   stays stable. (`tableau_export.csv` always mirrors the *latest* run of
   whatever company — don't publish against it.)
3. The Data Source page shows a preview grid. The AAPL file currently holds
   ~978 rows: FY2021–FY2025 complete, plus the in-progress **FY2026 Q1–Q2**
   quarters from filed 10-Qs.

### 1.3 Verify field types (do this before anything else)

Click the type icon above each column in the preview grid and confirm:

| Field | Type | Role |
|---|---|---|
| `period` | **Date** | Dimension |
| `value` | **Number (decimal)** | **Measure** |
| `fiscal_year` | Number (whole) | **Dimension** (drag out of Measures if needed) |
| everything else (`company`, `ticker`, `fiscal_quarter`, `frequency`, `category`, `statement`, `metric`, `unit`, `source_url`) | String | Dimension |

Then open **Sheet 1** (bottom-left). In the Data pane, confirm `value` sits
under Measures and `fiscal_year` under Dimensions.

**How this data is shaped (read once, saves hours):** every number lives in
one `value` column, one row per company-period-metric. So *every* sheet
filters by `metric`, and *every* sheet needs a `frequency` filter (Annual vs
Quarterly rows must never mix on one axis).

### 1.4 Create the reusable calculated fields

**Analysis → Create Calculated Field**, once each. These give you the fiscal
axis labels and the mixed-unit dual-axis charts the Streamlit app has.

**`Fiscal Period`** — the axis label (`FY2025`, `FY2026 Q1`):

```
IF [Fiscal Quarter] = "FY" THEN "FY" + STR([Fiscal Year])
ELSE "FY" + STR([Fiscal Year]) + " " + [Fiscal Quarter] END
```

Then right-click the new field in the Data pane → **Default Properties →
Sort → Sort By: Field → `period` → Aggregation: Minimum → Ascending**. This
pins the labels to true chronological order everywhere.

**Metric-picker fields** — one per metric you'll pair on a dual axis. The
pattern is always the same; create these to start (add more the same way as
needed):

```
Revenue $        := IF [Metric] = "Revenue" THEN [Value] END
Revenue YoY %    := IF [Metric] = "Revenue YoY Growth %" THEN [Value] END
Net Income $     := IF [Metric] = "Net Income" THEN [Value] END
OCF $            := IF [Metric] = "Operating Cash Flow" THEN [Value] END
Op Margin %      := IF [Metric] = "Operating Margin" THEN [Value] END
FCF $            := IF [Metric] = "Free Cash Flow" THEN [Value] END
FCF Margin %     := IF [Metric] = "FCF Margin" THEN [Value] END
EPS $            := IF [Metric] = "EPS (Diluted)" THEN [Value] END
EPS YoY %        := IF [Metric] = "EPS YoY Growth %" THEN [Value] END
```

**Number formats (professional defaults):** right-click each `... $` field →
Default Properties → Number Format → **Currency (Custom)**, 1 decimal,
**Display units: Billions**. Format the `... %` fields as Number with 1
decimal and a `%` suffix. Do the same for `value` itself (Billions) — you can
override per sheet where a metric is a ratio.

---

## Part 2 — The sheets

General recipe for a single-metric trend (used constantly):

1. New sheet. Drag **Fiscal Period** → Columns, **value** → Rows.
2. Drag **metric** → Filters, tick the one metric. Drag **frequency** →
   Filters, tick Annual *or* Quarterly.
3. Marks card: choose **Bar** (flow items like Revenue, FCF) or **Line**
   (margins, ratios, share count).
4. Name the sheet exactly what it shows.

Build these sheets (grouped by the dashboard tab they'll land on):

### Tab 1 · Growth
- **Revenue & growth** *(the mixed-unit dual-axis pattern — learn it here, reuse everywhere)*:
  Fiscal Period → Columns; **Revenue $** → Rows; **Revenue YoY %** → Rows
  (second pill). Right-click the second pill → **Dual Axis**. On the Marks
  card set Revenue $ to Bar, Revenue YoY % to Line. Do **not** synchronize
  axes (different units). Filter: frequency.
- **EPS & growth** — same pattern with EPS $ / EPS YoY %.
- **Growth decomposition** — metric filter: `Revenue YoY Growth %`,
  `EPS YoY Growth %` (+ Net Income YoY if you add a picker field); all are %,
  so: Fiscal Period → Columns, value → Rows, **metric → Color**. Line marks.

### Tab 2 · Profitability
- **Margins** — metric filter: Gross Margin, Operating Margin, Net Margin,
  **EBITDA Margin**; metric → Color; Line. One shared % axis.
- **Revenue growth vs operating margin** *(diagnostic: is growth
  profitable?)* — dual axis: Revenue YoY % (Bar) vs Op Margin % (Line).
- **Returns on capital** — metric filter: Return on Equity (ROE), Return on
  Invested Capital (ROIC), Return on Assets (ROA); metric → Color;
  **frequency = Annual only** (these are computed on average balances and
  exist only annually).
- **Effective tax rate** — single line, quarterly or annual.

### Tab 3 · Cash Generation
- **Net Income vs Operating Cash Flow** *(diagnostic: earnings quality)* —
  dual axis: Net Income $ (Bar) vs OCF $ (Line). Both are USD, so
  right-click the right axis → **Synchronize Axis** (unsynchronized same-unit
  dual axes mislead).
- **Free cash flow** — dual axis: FCF $ (Bar) vs FCF Margin % (Line). Add
  `SBC-Adjusted FCF` to the metric filter of the bar side if you want the
  stricter FCF view Streamlit shows.
- **OCF ÷ Net Income** — single line, metric `OCF-to-Net-Income Ratio`.
  Label the axis "Ratio (times)" (double-click the axis → Title). ≈1 means
  profits are backed by cash.

### Tab 4 · Financial Health & Solvency
- **Debt vs cash** — metric filter: Total Debt, Cash & Equivalents (Bars,
  metric → Color) + Net Debt (add to the filter; make it a Line via a dual
  axis if you want the exact Streamlit look — or keep all three as bars for
  simplicity).
- **Liquidity & leverage** — metric filter: Current Ratio, Debt-to-Equity,
  Net Debt to EBITDA; metric → Color; Line; axis title "Ratio (times)".
  *(Interest Coverage note: for AAPL this line ends at FY2023 — Apple stopped
  disclosing interest expense. That's the filing, not a bug. Give it its own
  small sheet if you want it.)*
- **Balance sheet size** — Total Assets, Total Equity; Bars; metric → Color.

### Tab 5 · Capital Allocation
- **Shares outstanding** — metric `Shares Outstanding (Diluted)`, Line.
  Annotate the trend: right-click a mid-series point → **Annotate → Point** →
  type "Falling share count → buybacks returning capital".
- **Capital returned vs SBC** — metric filter: Dividends Paid, Share
  Buybacks, Stock-Based Compensation; Bars; metric → Color (stacked is
  fine — Analysis menu → Stack Marks).
- **Reinvestment intensity** — Capex as % of Revenue + R&D as % of Revenue;
  Lines; metric → Color.
- **Payout ratios** — Dividends ÷ NI and (Div+Buybacks) ÷ FCF need calculated
  fields: `SUM(IF [Metric]="Dividends Paid" THEN [Value] END) / SUM(IF
  [Metric]="Net Income" THEN [Value] END)`, formatted as %. **Annual
  frequency only** — quarterly payout ratios are seasonal noise (steady
  dividends ÷ a seasonal FCF quarter means nothing).

### KPI banner (BANs) — three small sheets
For each of Revenue / Net Income / Free Cash Flow:

1. Filters: that metric + frequency (Quarterly gives the freshest number —
   for AAPL that's FY2026 Q2).
2. Drag **Fiscal Period → Detail**, **value → Text**.
3. Drag **value** to Text a *second* time → right-click the pill → **Quick
   Table Calculation → Percent Difference** (this is the YoY change,
   computed along Fiscal Period).
4. Keep only the latest period: create field `Is Last := LAST() = 0`, drag to
   Filters, tick True (right-click the filter pill → Compute Using → Fiscal
   Period).
5. Format: click Text on the Marks card → make the value large/bold and the
   % smaller beneath it, e.g. `<SUM(value)>` on line one, `▲ <% Diff> YoY`
   on line two. Center-align (Format → Alignment).

### Two data-honesty rules to preserve (they're enforced in Streamlit)
- **Quarterly comparisons are year-over-year.** The `...YoY Growth %` metrics
  in the CSV are already computed vs the same quarter a year earlier. Chart
  those; never build a quarter-over-quarter table calc — seasonality makes
  QoQ meaningless.
- **Annual-only metrics stay annual**: ROE/ROIC/ROA, Net Debt to EBITDA, and
  the payout ratios have no valid quarterly form. The CSV simply has no
  quarterly rows for the first two; for payout ratios *you* enforce it with
  the frequency filter.
- The in-progress **FY2026** has quarterly rows only (no 10-K yet) — annual
  charts exclude it automatically. Correct, not a gap.

---

## Part 3 — Tooltips with audit links

Every row carries the SEC filing URL it came from. On each sheet:

1. Drag **source_url** onto **Tooltip** on the Marks card (accept
   ATTR if prompted).
2. Click **Tooltip** and arrange:
   ```
   <metric>
   <Fiscal Period>:  <SUM(value)>
   ─────────────────
   Source filing: <ATTR(source_url)>
   ```
3. Untick "Include command buttons" in the tooltip editor for a cleaner look.

---

## Part 4 — Professional design pass

Do this once, before assembling dashboards — it's what separates "default
Tableau" from polished:

1. **Workbook-wide typography**: Format menu → **Workbook…** → set font to
   Tableau Book 10 for worksheets, 12–14 bold for titles. One font family
   everywhere.
2. **Palette — pick 3 colors and stop.** Suggested: primary blue `#4C90D9`
   (main series), light blue `#A7CDF0` (secondary/context series), amber
   `#E8A33D` (accents like YoY lines). On each sheet: Color → Edit Colors →
   assign consistently — *Revenue-family always primary, margins always
   accent* — so a viewer learns the encoding once. Avoid red except for
   genuinely negative things.
3. **Declutter every sheet**: Format → Borders: none; Format → Lines: keep
   only faint horizontal gridlines; hide the "Fiscal Period" field label
   (right-click column header → Hide Field Labels for Columns); axis titles
   only where the unit isn't obvious — and write units in words:
   "USD (billions)", "Margin (%)", **"Ratio (times)"** — never a bare "x".
4. **Numbers**: everything USD in **billions with 1 decimal** ($94.9B);
   percents with 1 decimal. Consistency here is 70% of "professional".
5. **Legends**: prefer direct color + a small legend placed *below* each
   chart (drag the legend object under the sheet in the dashboard; floating
   legends overlapping axes look amateur — this mirrors a fix we made in
   Streamlit).
6. **Titles as sentences**, not field names: "Net Income vs Operating Cash
   Flow — earnings quality", "Rule: quarterly growth is year-over-year".

---

## Part 5 — Assemble the five dashboards

Mirror the Streamlit navigation: **five dashboards in one workbook**, one per
category, published as clickable tabs.

1. **New Dashboard** (bottom bar) → **Size: Fixed, 1400 × 900** (fixed sizes
   publish predictably; Automatic can reflow badly on the web).
2. Layout skeleton with **tiled Horizontal/Vertical containers** (Objects
   panel): a title band across the top, the KPI BANs row beneath it (Growth
   dashboard only, or repeat on all five), then a 2×2 grid of charts.
3. Title band: a **Text object**: company + ticker, bold 16; below it in
   gray 9pt: "Source: SEC EDGAR XBRL · FY2021–FY2026 YTD · Financial
   analysis, not investment advice." That disclaimer must appear.
4. Name each dashboard exactly: `1 · Growth`, `2 · Profitability`,
   `3 · Cash Generation`, `4 · Financial Health & Solvency`,
   `5 · Capital Allocation`. Drag the tabs into numeric order.
5. **Global filters, mirroring the Streamlit sidebar:**
   - **Frequency** (Annual/Quarterly): show the `frequency` filter on each
     dashboard → filter card dropdown → **Apply to Worksheets → All Using
     This Data Source** → display as **Single Value List**.
   - **Quarter filter** (the same-quarter-across-years view): show the
     `fiscal_quarter` filter → Apply to all worksheets → Single Value List
     with **(All)**. Picking Q1 turns every quarterly chart into a clean
     Q1-vs-Q1-vs-Q1 comparison — Apple's holiday quarters lined up with no
     seasonality in between. (Tip: while a single quarter is selected,
     year-scale charts read best; the BANs will show that quarter's latest
     year.)
   - Optional **date range**: show a `period` filter as a range slider on
     the title band.
6. Padding: every object → Layout pane → **Outer Padding 8–12px**. Cramped
   edges are the #1 giveaway of a rushed dashboard.

---

## Part 6 — Publish

1. **File → Save to Tableau Public As…** (saving *is* publishing in Public
   Edition). Name it e.g. "Apple Inc. — Fundamental Deep Dive (FY2021–FY2026)".
2. In the save dialog, ensure **Show Sheets as Tabs** is ON — that's what
   turns the five dashboards into viewer-clickable tabs, matching the
   Streamlit app's navigation. (Also toggleable later on the workbook's web
   page settings.)
3. After it opens in the browser: hide the raw worksheets from tabs if they
   appear (right-click sheet tabs in Desktop → Hide before publishing, so
   only the five dashboards show), grab the share link, and optionally
   feature it on your profile.
4. Remember the warning at the top: the workbook **and the underlying data**
   are now downloadable by anyone.

---

## Part 7 — Updating when new filings drop

1. Re-run the pipeline for the same ticker:
   `python agents/orchestrator.py --source edgar --ticker AAPL`
   — `tableau_export_AAPL.csv` refreshes **in place, automatically**, with
   the new quarter (other tickers' files untouched).
2. Tableau Desktop → **File → Open from Tableau Public** → your workbook.
3. **Data menu → your data source → Refresh** (same file path — nothing to
   re-point).
4. **File → Save to Tableau Public As…** → overwrite. Done.

Steps 2–4 are manual every time — Tableau Public has no refresh API. The
pipeline's job ends at keeping the CSV current; the two-minute republish is
yours.

---

*Companion reading: `docs/analysis_report_AAPL.md` (if generated) or the
per-company report from your latest run — the written analysis that pairs
with these charts. All output is financial analysis, not investment advice.*

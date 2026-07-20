# Tableau Public Guide — building the FinAnalysis dashboard by hand

This guide walks a first-time Tableau user through building the financial
dashboard from `data/tableau_export.csv` in **Tableau Desktop Public Edition**,
click by click.

> ## ⚠️ Before you start: two things you must know
>
> 1. **Everything you publish to Tableau Public is public.** Tableau Public
>    saves workbooks to the open web by default — anyone on the internet can
>    view and download anything you publish, and copies may be cached or
>    indexed even if you later delete them. Do not publish data you consider
>    private. (SEC filing data, as used here, is already public.)
> 2. **There is no automated refresh on Tableau Public.** When the pipeline
>    produces new data, updating your dashboard means opening the workbook,
>    replacing/refreshing the data source, and re-publishing — by hand, every
>    time. This is a platform limitation, not a project one; it is why the
>    agent's job ends at producing the CSV.

---

## 1. Download and install Tableau Desktop Public Edition

1. Go to <https://public.tableau.com> and click **Create** → **Download
   Tableau Desktop Public Edition** (Windows/macOS, free).
2. Run the installer and sign in with (or create) a free Tableau Public
   account — you'll need it to save or publish, since Public Edition saves to
   the web, not to local files.

## 2. Connect to the data

1. Open Tableau. On the start page, under **Connect → To a File**, click
   **Text file**.
2. Browse to your project folder and select the **per-ticker file** for the
   company you're charting, e.g. `data/tableau_export_GOOG.csv`. Each pipeline
   run writes one file per company; other companies' runs never touch it, so
   your workbook's data source stays stable. (`data/tableau_export.csv` also
   exists and always mirrors the *latest* run, whichever company that was —
   prefer the per-ticker file for anything you plan to publish.)
3. The **Data Source** page opens with a preview grid of the CSV.

## 3. Verify the field types

Tableau guesses each column's type — verify before building anything. In the
preview grid, each column header has a small type icon (📅 calendar, `#`
number, `Abc` text, 🌐 globe):

| Field | Must be | If Tableau guessed wrong |
|---|---|---|
| `period` | **Date** (📅) | Click the type icon above the column → **Date** |
| `value` | **Number (decimal)**, a **Measure** | Click the icon → **Number (decimal)** |
| `fiscal_year` | Number is fine, but treat as **Dimension** (see below) | — |
| everything else (`company`, `ticker`, `fiscal_quarter`, `frequency`, `category`, `statement`, `metric`, `unit`, `source_url`) | **String** (Abc), **Dimension** | Click the icon → **String** |

Then click **Sheet 1** (bottom-left) to open a worksheet. In the left-hand
**Data** pane:

- `value` should be listed under **Measures** (bottom section). If not, drag
  it there.
- `period` should be under **Dimensions** with a calendar icon.
- If `fiscal_year` appears under Measures, drag it up to Dimensions — you
  filter by it, never sum it.

> Because every metric lives in one `value` column (tidy long format), you
> will *always* filter by `metric` when charting. That is by design — it is
> what lets one data source drive every chart.

## 4. Core chart: Revenue over time (5 drags)

1. Drag **period** to the **Columns** shelf. Click the pill's dropdown and
   choose the *continuous* (green) **Month** or **Quarter** — the second
   group in the menu, not the first.
2. Drag **value** to the **Rows** shelf.
3. Drag **metric** to the **Filters** shelf → tick **Revenue** only → OK.
4. Drag **frequency** to the **Filters** shelf → tick **Quarterly** → OK.
5. Right-click the sheet tab → **Rename** → "Revenue".

You now have a revenue line chart. Every other single-metric trend chart
(Net Income, Free Cash Flow, Operating Margin, Shares Outstanding…) is the
same recipe with a different `metric` filter value.

**Margins chart (three lines on one chart):** same as above, but in the
`metric` filter tick **Gross Margin**, **Operating Margin**, and **Net
Margin**, then drag **metric** to **Color** on the Marks card.

## 5. Use `metric` and `category` as filters

- To make a sheet-level filter interactive, right-click the filter pill →
  **Show Filter** — a checkbox panel appears on the right.
- `category` groups metrics by analysis theme (Growth, Profitability, Cash
  Generation, Financial Health & Solvency, Capital Allocation). Drag
  **category** to Filters on any sheet to scope it to one theme, or show it
  as a global filter on the dashboard (filter dropdown → **Apply to
  Worksheets → All Using This Data Source**).
- Keep a `frequency` filter on **every** sheet — mixing Annual and Quarterly
  rows on one axis double-counts. Set it once per sheet: Quarterly for trend
  charts, Annual for year-level charts.

## 6. Dual-axis chart: Net Income vs Operating Cash Flow

The earnings-quality chart — the two series belong together:

1. New worksheet. Drag **period** to **Columns** (continuous Quarter).
2. Drag **metric** to **Filters** → tick **Net Income** and **Operating Cash
   Flow**.
3. Drag **frequency** to **Filters** → **Quarterly**.
4. Drag **value** to **Rows**. Then drag **value** to **Rows** *again* — two
   identical pills, two stacked charts.
5. Right-click the **second** `SUM(value)` pill → **Dual Axis**. The charts
   merge onto shared left/right axes.
6. Right-click the right-hand axis → **Synchronize Axis** (both series are
   USD; unsynchronized dual axes mislead).
7. On the **Marks** card you now have three tabs (All / SUM(value) /
   SUM(value) (2)). On the first SUM(value) tab set the mark type to **Bar**
   and drag **metric** to **Color**; on the second set **Line**.
8. Rename the sheet "Earnings Quality: NI vs OCF".

The same recipe builds **Revenue Growth vs Operating Margin** (filter
`metric` to **Revenue YoY Growth %** + **Operating Margin** — both are `%`,
so synchronize the axes here too).

> **Quarterly growth is always year-over-year.** The `Revenue YoY Growth %`
> and `EPS YoY Growth %` metrics in the CSV are already computed against the
> same quarter one year earlier. Chart those columns; do not build
> quarter-over-quarter table calculations in Tableau — sequential-quarter
> comparisons are distorted by seasonality.

## 7. Add `source_url` to tooltips (auditability)

Every row carries the SEC filing URL it came from. To surface it:

1. On any worksheet, drag **source_url** from the Data pane onto **Tooltip**
   on the Marks card. (If Tableau nags about aggregation, choose
   **ATTR(source_url)** — right-click the pill → **Attribute**.)
2. Click **Tooltip** on the Marks card to edit the text — you'll see
   `<ATTR(source_url)>` inserted. Arrange it under the value, e.g.:

   ```
   <metric>: <SUM(value)>
   Period: <period>
   Source filing: <ATTR(source_url)>
   ```

3. Repeat for each sheet (tooltips are per-sheet). Now every hovered number
   can be traced to the filing it came from.

## 8. Assemble the dashboards — one tab per category

Mirror the Streamlit app's structure: **five dashboards in one workbook, one
per analysis category**, which publish as tabs the viewer clicks through.

1. Click the **New Dashboard** icon (bottom bar, grid-with-plus) and name it
   **1 · Growth** (right-click the tab → **Rename**).
2. Set **Size** (left panel) to **Automatic**, or Fixed 1200 × 900 for a
   consistent published layout.
3. Drag onto it the sheets that answer the Growth question: Revenue trend,
   EPS trend, and their YoY-growth charts.
4. Repeat four more times, one dashboard per category:
   - **2 · Profitability** — Margins chart, Revenue Growth vs Operating
     Margin (dual-axis), returns on capital
   - **3 · Cash Generation** — Net Income vs OCF (dual-axis), Free Cash
     Flow, OCF÷NI
   - **4 · Financial Health & Solvency** — Debt vs Cash, Current
     Ratio/Debt-to-Equity, balance sheet size
   - **5 · Capital Allocation** — Shares Outstanding, Dividends + Buybacks,
     Capex % of revenue
   Order the dashboard tabs left-to-right in this numbering (drag tabs to
   reorder) — it matches the analysis flow: growth → profits → cash →
   resilience → allocation.
5. On each dashboard add global filters: on any placed sheet click the funnel
   icon (**Use as Filter**), or show the `frequency` filter panel and set it
   to apply to all sheets (dropdown → **Apply to Worksheets → All Using This
   Data Source**). A `category` filter is unnecessary in this layout — each
   tab *is* a category; keep each sheet's own `metric` filter doing that work.
6. Add a text object at the top of each dashboard with the company name and
   an "analysis, not advice" note (build it once, copy-paste to the others).

## 9. Publish

1. **File → Save to Tableau Public As…** (in Public Edition, saving *is*
   publishing — there is no local-only save).
2. Sign in, name the workbook, save. A browser window opens with your
   published dashboard.
3. In the publish/save dialog, make sure **Show Sheets as Tabs** is enabled —
   that's what turns your five category dashboards into clickable tabs for
   viewers, matching the Streamlit app's navigation. (If you missed it, the
   toggle is also on the workbook's web page under settings.)
4. On the workbook's web page you can toggle **Show viz on profile**, and
   get share/embed links. Remember: the workbook *and its data* are now
   downloadable by anyone (⚠️ see the warning at the top).

### Updating later (manual, every time)

1. Re-run the pipeline for the **same ticker** your workbook uses
   (`python agents/orchestrator.py --source edgar --ticker GOOG`) — its
   per-ticker CSV (`tableau_export_GOOG.csv`) refreshes in place with the new
   periods; other tickers' files are untouched.
2. Open your workbook in Tableau Desktop Public Edition (**File → Open from
   Tableau Public**).
3. In the **Data** menu → your data source → **Refresh** (same file path).
4. **File → Save to Tableau Public As…** again, overwriting the workbook.

There is no way to automate steps 2–4 on Tableau Public — no publish API
exists. Fundamentals data changes four times a year, so this is a small,
predictable chore rather than a real limitation.

---

*Companion reading: `docs/analysis_report_GOOG.md` — the analysis agent's
written interpretation of the same data this dashboard visualizes (or
`docs/sample_analysis_report.md` for the synthetic sample company).*

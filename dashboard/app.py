"""Streamlit dashboard — single-company financial deep dive.

All data reads go through data/data_access.py (no read_csv, no hardcoded CSV
paths here). Commentary comes from the analysis agent's saved output; the
sidebar chat calls the analysis agent live on a compact slice of the data.

Run:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "data"))
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

import data_access

# Chat settings: which model answers sidebar questions, and the per-question
# spend cap. Each question is a live API call.
CHAT_MODEL = "claude-sonnet-4-6"
CHAT_BUDGET_USD = 0.25

# Data is considered stale after this many days (filings change quarterly).
STALE_AFTER_DAYS = 120

DISCLAIMER = ("**Not investment advice.** This dashboard presents financial "
              "analysis only — no buy/sell/hold recommendations, price "
              "targets, or valuation calls. Interpret trends; decide yourself.")

st.set_page_config(page_title="FinAnalysis", layout="wide")


# ── Data loading (all through data_access) ─────────────────────────────────

# Cache TTL: 600 s. When scheduled refresh (SPEC § Future A) lands, tune this
# one number so the dashboard picks up new pipeline runs — no other change.
@st.cache_data(ttl=600)
def load_data():
    df = data_access.load_financials()
    meta = data_access.load_metadata()
    try:
        analysis = data_access.load_analysis()
    except FileNotFoundError:
        analysis = None
    return df, meta, analysis


try:
    df_all, meta, analysis = load_data()
except FileNotFoundError as e:
    st.error(f"No data yet. {e}")
    st.stop()

COMPANY, TICKER = df_all["company"].iloc[0], df_all["ticker"].iloc[0]


# ── Header + freshness indicator ───────────────────────────────────────────

st.title(f"{COMPANY} ({TICKER})")
periods = meta.get("periods_covered", ["?", "?"])
st.caption(f"Coverage: {periods[0]} → {periods[1]}")

updated = datetime.strptime(meta["last_updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
age_days = (datetime.now(timezone.utc) - updated).days
fresh_line = f"Data as of {updated:%Y-%m-%d %H:%M} UTC · source: {meta['source']}"
if meta["source"] == "sample":
    st.warning(f"{fresh_line} — **synthetic sample data** for a fictional "
               "company. Nothing here describes a real business.")
elif age_days > STALE_AFTER_DAYS:
    st.warning(f"{fresh_line} — **{age_days} days old**; a newer filing may "
               "exist. Re-run the pipeline to refresh.")
else:
    st.info(fresh_line)

if analysis and analysis.get("ticker") != TICKER:
    st.warning(f"Saved commentary is for **{analysis.get('ticker')}**, but the data is "
               f"**{TICKER}** — re-run the pipeline to regenerate the analysis.")
    analysis = None


# ── Sidebar: global controls, chat, disclaimer ─────────────────────────────

with st.sidebar:
    st.header("Controls")
    frequency = st.radio("Frequency", ["Annual", "Quarterly"], horizontal=True)
    quarter_filter = "All"
    if frequency == "Quarterly":
        # Compare one fiscal quarter across years (Q1 vs Q1 vs Q1 ...) —
        # the seasonally-clean way to eyeball a quarterly trend.
        quarter_filter = st.segmented_control(
            "Quarter", ["All", "Q1", "Q2", "Q3", "Q4"], default="All") or "All"
    dmin, dmax = df_all["period"].min().date(), df_all["period"].max().date()
    date_range = st.slider("Date range", min_value=dmin, max_value=dmax,
                           value=(dmin, dmax), format="YYYY-MM")

df = df_all[(df_all["frequency"] == frequency)
            & (df_all["period"].dt.date >= date_range[0])
            & (df_all["period"].dt.date <= date_range[1])]
if quarter_filter != "All":
    df = df[df["fiscal_quarter"] == quarter_filter]

if df.empty:
    st.error("No rows in the selected range — widen the date range.")
    st.stop()


def series(metric: str, frame: pd.DataFrame | None = None) -> pd.DataFrame:
    frame = df if frame is None else frame
    return frame[frame["metric"] == metric].sort_values("period")


def plabel(fy, fq) -> str:
    """Fiscal-period axis label. 'FY2025 Q1' sorts lexically == chronologically."""
    return f"FY{fy}" if fq == "FY" else f"FY{fy} {fq}"


def labels_for(idx, frame: pd.DataFrame | None = None) -> list[str]:
    """Map period timestamps to fiscal labels using the given frame's rows."""
    frame = df if frame is None else frame
    m = {p: plabel(y, q) for p, y, q in zip(frame["period"], frame["fiscal_year"],
                                            frame["fiscal_quarter"])}
    return [m.get(p, str(p)) for p in idx]


def hover(name: str, unit: str) -> str:
    """Tooltip template incl. the source filing URL for auditability."""
    val = "%{y:.2f}" if unit in ("%", "x", "USD/share") else "%{y:,.0f}"
    shown = "×" if unit == "x" else unit  # '29.1×', not a stray letter x
    return (f"<b>{name}</b><br>%{{x}}<br>{val} {shown}"
            "<br>source: %{customdata}<extra></extra>")


def trace(metric: str, name: str | None = None, kind: str = "line",
          frame: pd.DataFrame | None = None):
    s = series(metric, frame)
    if s.empty:
        return None
    name = name or metric
    unit = s["unit"].iloc[0]
    labels = [plabel(y, q) for y, q in zip(s["fiscal_year"], s["fiscal_quarter"])]
    common = dict(x=labels, y=s["value"], name=name,
                  customdata=s["source_url"], hovertemplate=hover(name, unit))
    return go.Bar(**common) if kind == "bar" else go.Scatter(mode="lines+markers", **common)


def chart(traces, title: str, y1: str = "", y2: str | None = None,
          secondary: list | None = None, annotations: list | None = None,
          barmode: str | None = None):
    """Render a (optionally dual-axis) chart from prepared traces."""
    live = [t for t in traces if t is not None]
    live2 = [t for t in (secondary or []) if t is not None]
    if not live and not live2:
        return  # nothing to plot — skip instead of rendering a broken sliver
    if not live:  # only the secondary metric exists — promote it to the main axis
        live, live2 = live2, []
        y1 = y2 or y1
    # Create the secondary axis only when it has data — otherwise Plotly
    # renders a labeled right-hand axis with no tick numbers behind it.
    use_secondary = y2 is not None and bool(live2)
    fig = make_subplots(specs=[[{"secondary_y": use_secondary}]])
    for t in live:
        fig.add_trace(t, secondary_y=False)
    for t in live2:
        fig.add_trace(t, secondary_y=use_secondary)
    # Legend sits below the x-axis labels (yanchor top + negative y), with
    # enough bottom margin that neither overlaps the axis.
    fig.update_layout(title=title, height=400, margin=dict(t=48, b=95),
                      legend=dict(orientation="h", yanchor="top", y=-0.28,
                                  x=0, xanchor="left"),
                      **({"barmode": barmode} if barmode else {}))
    # Financial notation on numeric axes: 400B / 1.2T — never ×10⁹ exponents
    fig.update_yaxes(title_text=y1 if live else "", exponentformat="B",
                     separatethousands=True, secondary_y=False)
    if use_secondary:
        fig.update_yaxes(title_text=y2, exponentformat="B",
                         separatethousands=True, secondary_y=True)
    # Fiscal-period labels, chronological: 'FY2025 Q1' sorts correctly as text
    fig.update_xaxes(type="category", categoryorder="category ascending")
    for a in annotations or []:
        fig.add_annotation(**a)
    st.plotly_chart(fig, width="stretch")


def wide(frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Period-indexed wide view of the filtered data, for derived ratios."""
    frame = df if frame is None else frame
    return frame.pivot_table(index="period", columns="metric",
                             values="value", aggfunc="first").sort_index()


def computed(x, y, name: str, unit: str, kind: str = "line",
             frame: pd.DataFrame | None = None):
    """Trace for a ratio derived in the dashboard from reported metrics."""
    ht = (f"<b>{name}</b><br>%{{x}}<br>%{{y:,.2f}} {unit}"
          "<br><i>computed from reported metrics</i><extra></extra>")
    common = dict(x=labels_for(x, frame), y=y, name=name, hovertemplate=ht)
    return go.Bar(**common) if kind == "bar" else go.Scatter(mode="lines+markers", **common)


def yoy_series(metric: str) -> pd.Series | None:
    """YoY % change vs the same fiscal period one year earlier — never QoQ."""
    s = series(metric)
    if s.empty:
        return None
    prior = s.set_index(["fiscal_year", "fiscal_quarter"])["value"]
    vals = [
        100 * (r.value - p) / abs(p) if (p := prior.get((r.fiscal_year - 1, r.fiscal_quarter)))
        not in (None,) and pd.notna(p) and p != 0 else None
        for r in s.itertuples()
    ]
    return pd.Series(vals, index=list(s["period"]), name=metric).dropna()


def md_safe(text: str) -> str:
    """Escape $ so st.markdown doesn't render money amounts as LaTeX math.

    Pairs like "$5.80 ... $8.04" otherwise become KaTeX formulas. Streamlit's
    escape for a literal dollar sign is a backslash.
    """
    return text.replace("$", "\\$")


def commentary(category: str) -> None:
    if analysis and category in analysis.get("categories", {}):
        with st.expander(f"Analyst commentary — {category}", expanded=False):
            st.markdown(md_safe(analysis["categories"][category]))
    else:
        st.caption("No saved commentary — run `python agents/orchestrator.py` "
                   "to generate the analysis.")


# ── KPI strip: latest Revenue / Net Income / FCF with YoY change ───────────

def kpi(metric: str):
    """Latest value + YoY delta. YoY = same period one year earlier (same
    fiscal quarter for quarterly data) — never the previous quarter."""
    s = series(metric)
    if s.empty:
        return None, None
    last = s.iloc[-1]
    prior = s[(s["fiscal_year"] == last["fiscal_year"] - 1)
              & (s["fiscal_quarter"] == last["fiscal_quarter"])]
    delta = None
    if len(prior) and prior["value"].iloc[0]:
        delta = 100 * (last["value"] - prior["value"].iloc[0]) / abs(prior["value"].iloc[0])
    return last, delta


def fmt_usd(v: float) -> str:
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= cut:
            return f"${v / cut:,.1f}{suffix}"
    return f"${v:,.0f}"


cols = st.columns(3)
for col, metric in zip(cols, ("Revenue", "Net Income", "Free Cash Flow")):
    last, delta = kpi(metric)
    with col:
        if last is None:
            st.metric(metric, "n/a")
        else:
            when = (f"FY{last['fiscal_year']}" if last["fiscal_quarter"] == "FY"
                    else f"{last['fiscal_quarter']} FY{last['fiscal_year']}")
            label = f"{metric} ({when})"
            st.metric(label, fmt_usd(last["value"]),
                      f"{delta:+.1f}% YoY" if delta is not None else None)

if frequency == "Quarterly":
    if quarter_filter == "All":
        st.caption("Quarterly comparisons are **year-over-year** (vs the same "
                   "quarter one year earlier), never sequential quarters — "
                   "seasonality makes QoQ trends meaningless.")
    else:
        st.caption(f"Showing **{quarter_filter} only, across years** — every "
                   "point on every chart is the same fiscal quarter, so the "
                   "trend is seasonally clean by construction.")

st.divider()


# ── Five analysis categories, one tab each ─────────────────────────────────

w = wide()
annual_all = df_all[df_all["frequency"] == "Annual"]

# Category navigation. A segmented control rendering ONLY the active section
# (not st.tabs): Plotly charts first drawn inside a hidden tab get measured at
# zero width and collapse to a sliver showing just the axis title.
SECTIONS = ["1 · Growth", "2 · Profitability", "3 · Cash Generation",
            "4 · Financial Health & Solvency", "5 · Capital Allocation"]
section = st.segmented_control("Category", SECTIONS, default=SECTIONS[0],
                               label_visibility="collapsed") or SECTIONS[0]

if section == "1 · Growth":
    left, right = st.columns(2)
    with left:
        chart([trace("Revenue", kind="bar")], "Revenue", "USD",
              y2="YoY %", secondary=[trace("Revenue YoY Growth %", "Revenue YoY %")])
    with right:
        chart([trace("EPS (Diluted)", kind="bar")], "EPS (Diluted)", "USD/share",
              y2="YoY %", secondary=[trace("EPS YoY Growth %", "EPS YoY %")])
    left, right = st.columns(2)
    with left:
        # TTM needs all four quarters — meaningless when filtered to one quarter
        if frequency == "Quarterly" and quarter_filter == "All" and "Revenue" in w.columns:
            ttm = w["Revenue"].rolling(4).sum().dropna()
            if len(ttm):
                chart([computed(ttm.index, ttm.values, "TTM Revenue", "USD")],
                      "Trailing-12-month revenue (seasonality removed)", "USD")
        else:
            # Rule of 40: growth + FCF margin in one number
            if {"Revenue YoY Growth %", "FCF Margin"} <= set(w.columns):
                r40 = (w["Revenue YoY Growth %"] + w["FCF Margin"]).dropna()
                if len(r40):
                    chart([computed(r40.index, r40.values, "Rule of 40", "pts", kind="bar")],
                          "Rule of 40 (revenue growth + FCF margin)", "points")
    with right:
        # Where EPS growth comes from: revenue vs profit leverage vs buybacks
        decomp = [(m, yoy_series(m)) for m in ("Revenue", "Net Income", "EPS (Diluted)")]
        traces = [computed(s.index, s.values, f"{m} YoY %", "%")
                  for m, s in decomp if s is not None and len(s)]
        if traces:
            chart(traces, "Growth decomposition (all YoY)", "YoY growth (%)")
    commentary("Growth")

if section == "2 · Profitability":
    left, right = st.columns(2)
    with left:
        # All three margins on one shared chart (+ EBITDA margin when extracted)
        chart([trace("Gross Margin"), trace("Operating Margin"), trace("Net Margin"),
               trace("EBITDA Margin")], "Margins", "Margin (%)")
    with right:
        # Diagnostic pairing: is growth profitable?
        chart([trace("Revenue YoY Growth %", "Revenue YoY Growth %", kind="bar")],
              "Revenue Growth vs Operating Margin (is growth profitable?)",
              "Revenue YoY %", y2="Operating Margin %",
              secondary=[trace("Operating Margin")])
    left, right = st.columns(2)
    with left:
        returns = [trace(m, frame=annual_all)
                   for m in ("Return on Equity (ROE)", "Return on Invested Capital (ROIC)",
                             "Return on Assets (ROA)")]
        if any(t is not None for t in returns):
            chart(returns, "Returns on capital (annual — computed on average balances)",
                  "Return (%)")
    with right:
        # DuPont: is ROE coming from margins, asset efficiency, or leverage?
        aw = wide(annual_all)
        if {"Net Margin", "Revenue", "Total Assets", "Total Equity"} <= set(aw.columns):
            turnover = (aw["Revenue"] / aw["Total Assets"]).dropna()
            leverage = (aw["Total Assets"] / aw["Total Equity"]).dropna()
            chart([computed(aw.index, aw["Net Margin"], "Net Margin", "%", frame=annual_all)],
                  "DuPont decomposition of ROE (annual)", "Net Margin %",
                  y2="Turnover / Leverage (times)",
                  secondary=[computed(turnover.index, turnover.values, "Asset Turnover", "x", frame=annual_all),
                             computed(leverage.index, leverage.values, "Equity Multiplier", "x", frame=annual_all)])
    if trace("Effective Tax Rate") is not None:
        chart([trace("Effective Tax Rate")], "Effective tax rate", "Tax rate (%)")
    commentary("Profitability")

if section == "3 · Cash Generation":
    left, right = st.columns(2)
    with left:
        # Diagnostic pairing: earnings quality
        chart([trace("Net Income", kind="bar")],
              "Net Income vs Operating Cash Flow (earnings quality)",
              "Net Income (USD)", y2="OCF (USD)",
              secondary=[trace("Operating Cash Flow")])
    with right:
        chart([trace("Free Cash Flow", kind="bar"), trace("SBC-Adjusted FCF")],
              "Free Cash Flow", "USD",
              y2="FCF Margin %", secondary=[trace("FCF Margin")])
    left, right = st.columns(2)
    with left:
        chart([trace("OCF-to-Net-Income Ratio")],
              "OCF ÷ Net Income (≈1 means profits are backed by cash)",
              "Ratio (times)")
    with right:
        # Can the shareholder-return program be funded from free cash flow?
        # Cumulative sums need every quarter — hidden in single-quarter mode.
        if quarter_filter == "All" and \
                {"Free Cash Flow", "Dividends Paid", "Share Buybacks"} <= set(w.columns):
            cum_fcf = w["Free Cash Flow"].fillna(0).cumsum()
            cum_ret = (w["Dividends Paid"].fillna(0) + w["Share Buybacks"].fillna(0)).cumsum()
            chart([computed(cum_fcf.index, cum_fcf.values, "Cumulative FCF", "USD"),
                   computed(cum_ret.index, cum_ret.values,
                            "Cumulative dividends + buybacks", "USD")],
                  "Cumulative FCF vs capital returned (selected range)", "USD")
    commentary("Cash Generation")

if section == "4 · Financial Health & Solvency":
    left, right = st.columns(2)
    with left:
        chart([trace("Total Debt", kind="bar"), trace("Cash & Equivalents", kind="bar"),
               trace("Net Debt")], "Debt vs Cash", "USD")
    with right:
        chart([trace("Current Ratio"), trace("Debt-to-Equity"),
               trace("Net Debt to EBITDA")],
              "Liquidity & leverage", "Ratio (times)",
              y2="Interest Coverage (times)", secondary=[trace("Interest Coverage")])
    equity_ratio = None
    if {"Total Equity", "Total Assets"} <= set(w.columns):
        er = (100 * w["Total Equity"] / w["Total Assets"]).dropna()
        equity_ratio = [computed(er.index, er.values, "Equity Ratio", "%")]
    chart([trace("Total Assets", kind="bar"), trace("Total Equity", kind="bar")],
          "Balance sheet size", "USD",
          y2="Equity ÷ Assets (%)" if equity_ratio else None, secondary=equity_ratio)
    commentary("Financial Health & Solvency")

if section == "5 · Capital Allocation":
    sh = series("Shares Outstanding (Diluted)")
    left, right = st.columns(2)
    with left:
        note = None
        if len(sh) >= 2:
            direction = sh["value"].iloc[-1] - sh["value"].iloc[0]
            mid = sh.iloc[len(sh) // 2]
            note = [dict(x=plabel(mid["fiscal_year"], mid["fiscal_quarter"]),
                         y=sh["value"].max(), showarrow=False, yshift=18,
                         text=("Falling share count → buybacks returning capital"
                               if direction < 0 else
                               "Rising share count → dilution"))]
        chart([trace("Shares Outstanding (Diluted)", "Shares Outstanding")],
              "Shares Outstanding (Diluted)", "shares", annotations=note)
    with right:
        chart([trace("Dividends Paid", kind="bar"), trace("Share Buybacks", kind="bar"),
               trace("Stock-Based Compensation", kind="bar")],
              "Capital returned vs SBC issued", "USD",
              y2="Reinvestment (% of revenue)",
              secondary=[trace("Capex as % of Revenue"), trace("R&D as % of Revenue")])
    left, right = st.columns(2)
    with left:
        # Payout sustainability: what share of profits/FCF goes back to holders?
        # Always computed on ANNUAL figures — single-quarter payout ratios are
        # seasonal noise (steady dividends ÷ a seasonal FCF quarter misleads).
        aw_pay = wide(annual_all)
        payout = []
        if {"Dividends Paid", "Net Income"} <= set(aw_pay.columns):
            dp = (100 * aw_pay["Dividends Paid"] / aw_pay["Net Income"]).dropna()
            payout.append(computed(dp.index, dp.values, "Dividends ÷ Net Income", "%",
                                   frame=annual_all))
        if {"Dividends Paid", "Share Buybacks", "Free Cash Flow"} <= set(aw_pay.columns):
            tp = (100 * (aw_pay["Dividends Paid"].fillna(0) + aw_pay["Share Buybacks"].fillna(0))
                  / aw_pay["Free Cash Flow"]).dropna()
            payout.append(computed(tp.index, tp.values, "(Dividends + Buybacks) ÷ FCF", "%",
                                   frame=annual_all))
        if payout:
            chart(payout, "Payout ratios (annual — 100% = all FCF returned)", "Payout (%)")
    with right:
        # Where every operating dollar went
        deploy = [trace(m, kind="bar") for m in
                  ("Capital Expenditures", "Dividends Paid", "Share Buybacks")]
        if any(t is not None for t in deploy):
            chart(deploy, "Capital deployment vs operating cash flow", "USD",
                  y2="OCF (USD)", secondary=[trace("Operating Cash Flow")],
                  barmode="stack")
    commentary("Capital Allocation")

if "EBITDA" not in set(df_all["metric"].unique()):
    st.caption("Extended metrics (EBITDA, R&D %, effective tax rate, SBC) are "
               "wired into the pipeline but not yet in this dataset — they "
               "populate on the next pipeline run.")


# ── Sidebar: ask the analysis agent ────────────────────────────────────────

def answer_question(question: str) -> str:
    import runners  # deferred: only needed when a question is asked
    from env_check import get_required
    get_required("ANTHROPIC_API_KEY")
    # Pass a compact digest of the currently-filtered data — never the raw file
    data_slice = runners.build_digest(df_all)
    prompt = (
        "Answer the user's question in a few sentences, grounded ONLY in the "
        "data below. Cite figures and periods for every claim. If the data "
        "cannot answer it, say so and point to the filings/MD&A. Do not write "
        "the full report; no investment advice.\n\n"
        f"=== DATA ===\n{data_slice}\n\n=== QUESTION ===\n{question}"
    )
    text, cost = asyncio.run(
        runners.call_agent("analysis_agent", prompt, CHAT_MODEL, CHAT_BUDGET_USD))
    return f"{text}\n\n*(cost ${cost:.4f})*"


with st.sidebar:
    st.divider()
    st.header("Ask the analyst")
    st.caption("Answers are grounded in the loaded dataset. Each question "
               "makes one metered API call.")
    question = st.text_area("Question", placeholder="e.g. Is margin expansion "
                            "keeping up with revenue growth?", height=80)
    if st.button("Ask", width="stretch") and question.strip():
        with st.spinner("Analyzing…"):
            try:
                st.session_state["chat_answer"] = answer_question(question)
            except SystemExit as e:  # missing API key → readable message
                st.session_state["chat_answer"] = f"⚠️ {e}"
            except Exception as e:
                st.session_state["chat_answer"] = f"⚠️ Agent call failed: {e}"
    if "chat_answer" in st.session_state:
        st.markdown(md_safe(st.session_state["chat_answer"]))

    st.divider()
    st.info(DISCLAIMER)  # the advice disclaimer, shown once

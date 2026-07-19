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
    dmin, dmax = df_all["period"].min().date(), df_all["period"].max().date()
    date_range = st.slider("Date range", min_value=dmin, max_value=dmax,
                           value=(dmin, dmax), format="YYYY-MM")

df = df_all[(df_all["frequency"] == frequency)
            & (df_all["period"].dt.date >= date_range[0])
            & (df_all["period"].dt.date <= date_range[1])]

if df.empty:
    st.error("No rows in the selected range — widen the date range.")
    st.stop()


def series(metric: str, frame: pd.DataFrame | None = None) -> pd.DataFrame:
    frame = df if frame is None else frame
    return frame[frame["metric"] == metric].sort_values("period")


def hover(name: str, unit: str) -> str:
    """Tooltip template incl. the source filing URL for auditability."""
    val = "%{y:.2f}" if unit in ("%", "x", "USD/share") else "%{y:,.0f}"
    return (f"<b>{name}</b><br>%{{x|%Y-%m-%d}}<br>{val} {unit}"
            "<br>source: %{customdata}<extra></extra>")


def trace(metric: str, name: str | None = None, kind: str = "line",
          frame: pd.DataFrame | None = None):
    s = series(metric, frame)
    if s.empty:
        return None
    name = name or metric
    unit = s["unit"].iloc[0]
    common = dict(x=s["period"], y=s["value"], name=name,
                  customdata=s["source_url"], hovertemplate=hover(name, unit))
    return go.Bar(**common) if kind == "bar" else go.Scatter(mode="lines+markers", **common)


def chart(traces, title: str, y1: str = "", y2: str | None = None,
          secondary: list | None = None, annotations: list | None = None):
    """Render a (optionally dual-axis) chart from prepared traces."""
    fig = make_subplots(specs=[[{"secondary_y": y2 is not None}]])
    for t in traces:
        if t is not None:
            fig.add_trace(t, secondary_y=False)
    for t in secondary or []:
        if t is not None:
            fig.add_trace(t, secondary_y=True)
    fig.update_layout(title=title, height=380, margin=dict(t=48, b=8),
                      legend=dict(orientation="h", y=-0.15))
    fig.update_yaxes(title_text=y1, secondary_y=False)
    if y2 is not None:
        fig.update_yaxes(title_text=y2, secondary_y=True)
    for a in annotations or []:
        fig.add_annotation(**a)
    st.plotly_chart(fig, width="stretch")


def commentary(category: str) -> None:
    if analysis and category in analysis.get("categories", {}):
        with st.expander(f"Analyst commentary — {category}", expanded=False):
            st.markdown(analysis["categories"][category])
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
    st.caption("Quarterly comparisons are **year-over-year** (vs the same "
               "quarter one year earlier), never sequential quarters — "
               "seasonality makes QoQ trends meaningless.")

st.divider()


# ── 1. Growth ──────────────────────────────────────────────────────────────

st.header("1 · Growth")
left, right = st.columns(2)
with left:
    chart([trace("Revenue", kind="bar")], "Revenue", "USD",
          y2="YoY %", secondary=[trace("Revenue YoY Growth %", "Revenue YoY %")])
with right:
    chart([trace("EPS (Diluted)", kind="bar")], "EPS (Diluted)", "USD/share",
          y2="YoY %", secondary=[trace("EPS YoY Growth %", "EPS YoY %")])
commentary("Growth")

# ── 2. Profitability ───────────────────────────────────────────────────────

st.header("2 · Profitability")
left, right = st.columns(2)
with left:
    # All three margins on one shared chart
    chart([trace("Gross Margin"), trace("Operating Margin"), trace("Net Margin")],
          "Margins", "%")
with right:
    # Diagnostic pairing: is growth profitable?
    chart([trace("Revenue YoY Growth %", "Revenue YoY Growth %", kind="bar")],
          "Revenue Growth vs Operating Margin (is growth profitable?)",
          "Revenue YoY %", y2="Operating Margin %",
          secondary=[trace("Operating Margin")])
returns = [trace(m, frame=df_all[df_all["frequency"] == "Annual"])
           for m in ("Return on Equity (ROE)", "Return on Invested Capital (ROIC)",
                     "Return on Assets (ROA)")]
if any(t is not None for t in returns):
    chart(returns, "Returns on capital (annual — computed on average balances)", "%")
commentary("Profitability")

# ── 3. Cash Generation ─────────────────────────────────────────────────────

st.header("3 · Cash Generation")
left, right = st.columns(2)
with left:
    # Diagnostic pairing: earnings quality
    chart([trace("Net Income", kind="bar")],
          "Net Income vs Operating Cash Flow (earnings quality)",
          "Net Income (USD)", y2="OCF (USD)",
          secondary=[trace("Operating Cash Flow")])
with right:
    chart([trace("Free Cash Flow", kind="bar")], "Free Cash Flow", "USD",
          y2="FCF Margin %", secondary=[trace("FCF Margin")])
chart([trace("OCF-to-Net-Income Ratio")],
      "OCF ÷ Net Income (≈1 means profits are backed by cash)", "x")
commentary("Cash Generation")

# ── 4. Financial Health & Solvency ─────────────────────────────────────────

st.header("4 · Financial Health & Solvency")
left, right = st.columns(2)
with left:
    chart([trace("Total Debt", kind="bar"), trace("Cash & Equivalents", kind="bar"),
           trace("Net Debt")], "Debt vs Cash", "USD")
with right:
    chart([trace("Current Ratio"), trace("Debt-to-Equity")],
          "Liquidity & leverage", "x",
          y2="Interest Coverage (x)", secondary=[trace("Interest Coverage")])
chart([trace("Total Assets", kind="bar"), trace("Total Equity", kind="bar")],
      "Balance sheet size", "USD")
commentary("Financial Health & Solvency")

# ── 5. Capital Allocation ──────────────────────────────────────────────────

st.header("5 · Capital Allocation")
sh = series("Shares Outstanding (Diluted)")
left, right = st.columns(2)
with left:
    note = None
    if len(sh) >= 2:
        direction = sh["value"].iloc[-1] - sh["value"].iloc[0]
        note = [dict(x=sh["period"].iloc[len(sh) // 2], y=sh["value"].max(),
                     showarrow=False, yshift=18,
                     text=("Falling share count → buybacks returning capital"
                           if direction < 0 else
                           "Rising share count → dilution"))]
    chart([trace("Shares Outstanding (Diluted)", "Shares Outstanding")],
          "Shares Outstanding (Diluted)", "shares", annotations=note)
with right:
    chart([trace("Dividends Paid", kind="bar"), trace("Share Buybacks", kind="bar")],
          "Capital returned to shareholders", "USD",
          y2="Capex % of revenue", secondary=[trace("Capex as % of Revenue")])
commentary("Capital Allocation")


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
        st.markdown(st.session_state["chat_answer"])

    st.divider()
    st.info(DISCLAIMER)  # the advice disclaimer, shown once

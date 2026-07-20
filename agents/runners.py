"""Agent runners — one entry point per agent role.

Each runner loads its role prompt from /prompts at runtime (prompts are data,
not code — see CLAUDE.md), calls Claude via the Claude Agent SDK, and parses
the structured response. Deterministic data checks that should never depend on
a model live here too, alongside the LLM review they feed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"

sys.path.insert(0, str(PROJECT_ROOT / "data"))  # data_access import for callers


class AgentError(RuntimeError):
    pass


def load_prompt(role: str) -> str:
    # PROMPT LOAD SITE — role prompts are editable markdown under /prompts.
    # Changing an agent's behavior means editing prompts/<role>.md, not code.
    path = PROMPTS_DIR / f"{role}.md"
    if not path.exists():
        raise AgentError(f"Missing role prompt: {path}")
    return path.read_text()


async def call_agent(role: str, user_prompt: str, model: str,
                     budget_left_usd: float) -> tuple[str, float]:
    """Single-shot LLM call for one agent role. Returns (text, cost_usd).

    No tools are exposed — these agents are pure reasoning over the prompt.
    Prompt caching is handled by the Agent SDK automatically (stable system
    prompt first, volatile data in the user turn), so repeat runs against the
    same filings hit the cache.
    """
    options = ClaudeAgentOptions(
        system_prompt=load_prompt(role),
        model=model,
        max_turns=1,
        allowed_tools=[],
        tools=[],
        setting_sources=None,  # isolated: no user/project settings leak in
        max_budget_usd=budget_left_usd,  # SDK-enforced hard cap for this call
        cwd=PROJECT_ROOT,
    )
    parts: list[str] = []
    cost = 0.0
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            if message.is_error:
                raise AgentError(
                    f"{role} failed ({message.subtype}): {message.errors or message.result}"
                )
    text = "".join(parts).strip()
    if not text:
        raise AgentError(f"{role} returned an empty response")
    return text, cost


def parse_json_response(role: str, text: str) -> dict:
    """Parse strict-JSON agent output, tolerating fences and stray prose.

    Prompts demand raw JSON, but models occasionally wrap it in a markdown
    fence or append commentary anyway. Recover the first balanced JSON object
    rather than failing a run whose (paid) agent call already succeeded.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n|\n```$", "", cleaned, flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        raise AgentError(f"{role} did not return valid JSON.\n--- output ---\n{text[:2000]}")


# ── Validation: deterministic checks (code, not model) ─────────────────────

CORE_METRICS = ("Revenue", "Net Income", "Operating Cash Flow")
REL_TOL = 0.015  # identity checks tolerate rounding in reported figures


def deterministic_checks(df: pd.DataFrame) -> list[dict]:
    """Mechanical consistency/completeness checks run on every dataset."""
    findings: list[dict] = []

    def add(severity, check, period, metric, detail):
        findings.append({"severity": severity, "check": check, "period": period,
                         "metric": metric, "detail": detail})

    # Units: one unit per metric, no mixed scales
    for metric, units in df.groupby("metric")["unit"].unique().items():
        if len(units) > 1:
            add("error", "units", "-", metric, f"Mixed units {list(units)} for one metric")

    wide = df.pivot_table(index=["frequency", "fiscal_year", "fiscal_quarter"],
                          columns="metric", values="value", aggfunc="first")

    def close(a, b):
        return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1.0)

    for idx, r in wide.iterrows():
        freq, fy, fq = idx
        label = f"{freq} FY{fy} {fq}"
        g = lambda m: r.get(m)

        for m in CORE_METRICS:
            if pd.isna(g(m)):
                add("warning", "completeness", label, m, "Core metric missing for this period")

        pairs = [
            ("Free Cash Flow", "Operating Cash Flow", "Capital Expenditures",
             lambda a, b: a - b, "FCF = OCF - Capex"),
            ("Net Debt", "Total Debt", "Cash & Equivalents",
             lambda a, b: a - b, "Net Debt = Debt - Cash"),
            ("EBITDA", "Operating Income", "Depreciation & Amortization",
             lambda a, b: a + b, "EBITDA = OI + D&A"),
            ("SBC-Adjusted FCF", "Free Cash Flow", "Stock-Based Compensation",
             lambda a, b: a - b, "SBC-adj FCF = FCF - SBC"),
        ]
        for target, x, y, fn, name in pairs:
            t, a, b = g(target), g(x), g(y)
            if pd.notna(t) and pd.notna(a) and pd.notna(b) and not close(t, fn(a, b)):
                add("error", "consistency", label, target,
                    f"{name} violated: {t:,.0f} vs {fn(a, b):,.0f}")

        rev = g("Revenue")
        if pd.notna(rev):
            for margin, num in (("Gross Margin", "Gross Profit"),
                                ("Operating Margin", "Operating Income"),
                                ("Net Margin", "Net Income")):
                mv, nv = g(margin), g(num)
                if pd.notna(mv) and pd.notna(nv) and rev and not close(mv, 100 * nv / rev):
                    add("error", "consistency", label, margin,
                        f"{margin} {mv} inconsistent with {num}/Revenue = {100 * nv / rev:.2f}")
            gm = g("Gross Margin")
            if pd.notna(gm) and not (0 <= gm <= 100):
                add("error", "plausibility", label, "Gross Margin", f"Implausible value {gm}")
            if rev < 0:
                add("error", "plausibility", label, "Revenue", f"Negative revenue {rev:,.0f}")

    # Annual flow rows should equal the sum of their four quarters
    flow_metrics = df[df["statement"].isin(["income_statement", "cash_flow"])]["metric"].unique()
    q = df[df["frequency"] == "Quarterly"]
    a = df[df["frequency"] == "Annual"]
    for metric in flow_metrics:
        qsum = q[q["metric"] == metric].groupby("fiscal_year")["value"].agg(["sum", "count"])
        for fy, row in qsum.iterrows():
            if row["count"] != 4:
                continue
            ann = a[(a["metric"] == metric) & (a["fiscal_year"] == fy)]["value"]
            if len(ann) and not close(ann.iloc[0], row["sum"]):
                add("warning", "consistency", f"FY{fy}", metric,
                    f"Annual {ann.iloc[0]:,.0f} != sum of quarters {row['sum']:,.0f}")

    # YoY growth figures must match the underlying series (YoY, never QoQ)
    for metric, base in (("Revenue YoY Growth %", "Revenue"), ("EPS YoY Growth %", "EPS (Diluted)")):
        sub = df[df["metric"].isin([metric, base])]
        piv = sub.pivot_table(index=["frequency", "fiscal_quarter", "fiscal_year"],
                              columns="metric", values="value", aggfunc="first")
        for (freq, fq, fy), r in piv.iterrows():
            grown = r.get(metric)
            cur = r.get(base)
            prev_row = piv.reset_index()
            prev = prev_row[(prev_row["frequency"] == freq) & (prev_row["fiscal_quarter"] == fq)
                            & (prev_row["fiscal_year"] == fy - 1)]
            if pd.isna(grown) or pd.isna(cur) or prev.empty or pd.isna(prev[base].iloc[0]):
                continue
            expected = 100 * (cur - prev[base].iloc[0]) / abs(prev[base].iloc[0])
            if abs(grown - expected) > 1.0:
                add("error", "yoy", f"{freq} FY{fy} {fq}", metric,
                    f"Reported {grown:.2f}% vs recomputed YoY {expected:.2f}% — "
                    "growth must be year-over-year, never quarter-over-quarter")
    return findings


# ── Digest builders (compact data slices for LLM prompts) ──────────────────

def build_digest(df: pd.DataFrame, max_quarterly_metrics: int = 10) -> str:
    """Compact text tables of the dataset for validation/analysis prompts."""
    annual = df[df["frequency"] == "Annual"].pivot_table(
        index="metric", columns="fiscal_year", values="value", aggfunc="first")
    key = ["Revenue", "Revenue YoY Growth %", "Net Income", "Operating Cash Flow",
           "Free Cash Flow", "Gross Margin", "Operating Margin", "Net Margin",
           "EPS (Diluted)", "Shares Outstanding (Diluted)"][:max_quarterly_metrics]
    qdf = df[(df["frequency"] == "Quarterly") & (df["metric"].isin(key))]
    quarterly = qdf.pivot_table(index="metric", columns=["fiscal_year", "fiscal_quarter"],
                                values="value", aggfunc="first")
    units = df.groupby("metric")["unit"].first()
    company, ticker = df["company"].iloc[0], df["ticker"].iloc[0]
    lines = [
        f"Company: {company} ({ticker})",
        f"Periods: {df['period'].min().date()} .. {df['period'].max().date()}",
        "", "=== ANNUAL (all metrics; raw values, units per metric below) ===",
        annual.round(2).to_string(),
        "", "=== QUARTERLY (key metrics) ===",
        quarterly.round(2).to_string(),
        "", "=== UNITS ===",
        units.to_string(),
    ]
    return "\n".join(lines)


async def run_validation(df: pd.DataFrame, model: str,
                         budget_left_usd: float) -> tuple[dict, float]:
    """Deterministic checks + LLM review. Returns (report, cost)."""
    code_findings = deterministic_checks(df)
    prompt = (
        "Review this dataset before it is written to disk.\n\n"
        f"{build_digest(df)}\n\n"
        "=== DETERMINISTIC FINDINGS (already run by code) ===\n"
        f"{json.dumps(code_findings, indent=1)}\n"
    )
    text, cost = await call_agent("validation_agent", prompt, model, budget_left_usd)
    llm = parse_json_response("validation_agent", text)
    findings = code_findings + llm.get("findings", [])
    errors = [f for f in findings if f.get("severity") == "error"]
    warnings = [f for f in findings if f.get("severity") == "warning"]
    report = {
        "verdict": "fail" if errors else ("pass_with_warnings" if warnings else "pass"),
        "n_errors": len(errors), "n_warnings": len(warnings),
        "findings": findings,
    }
    return report, cost


# ── Analysis ───────────────────────────────────────────────────────────────

CATEGORY_HEADINGS = ["Growth", "Profitability", "Cash Generation",
                     "Financial Health & Solvency", "Capital Allocation"]


def split_report(report_md: str) -> dict:
    """Split the analysis report into per-category commentary by H2 heading."""
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in report_md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current in CATEGORY_HEADINGS:
                sections[current] = "\n".join(buf).strip()
            current, buf = m.group(1), []
        else:
            buf.append(line)
    if current in CATEGORY_HEADINGS:
        sections[current] = "\n".join(buf).strip()
    return sections


async def run_analysis(df: pd.DataFrame, source: str, model: str,
                       budget_left_usd: float) -> tuple[str, dict, float]:
    """Full written analysis. Returns (report_md, categories, cost)."""
    note = ("This is SYNTHETIC sample data for a fictional company — say so in "
            "the overview.\n\n" if source == "sample" else "")
    prompt = f"{note}Analyze the following financial data:\n\n{build_digest(df)}"
    report_md, cost = await call_agent("analysis_agent", prompt, model, budget_left_usd)
    categories = split_report(report_md)
    missing = [c for c in CATEGORY_HEADINGS if c not in categories]
    if missing:
        raise AgentError(f"Analysis report is missing required sections: {missing}")
    return report_md, categories, cost

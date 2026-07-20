"""Pipeline orchestrator — runs the four agents in sequence.

Headless-invocable: import run_pipeline() from code, or run as a script:

    python agents/orchestrator.py --source sample
    python agents/orchestrator.py --source edgar --ticker AAPL

No interactive prompts anywhere, so cron/CI can drive it unmodified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))
sys.path.insert(0, str(PROJECT_ROOT / "data"))

# ═══════════════════════════════════════════════════════════════════════════
# PER-AGENT MODEL CONFIGURATION — edit here, nowhere else.
# Cheap model (Haiku) for mechanical roles; stronger model (Sonnet) for the
# written analysis, where reasoning quality shows up directly in the output.
# ═══════════════════════════════════════════════════════════════════════════
AGENT_MODELS = {
    "research":   "claude-haiku-4-5",   # filing selection — simple, cheap
    "extraction": "claude-haiku-4-5",   # XBRL tag mapping — simple, cheap
    "validation": "claude-haiku-4-5",   # sanity review — simple, cheap
    "analysis":   "claude-sonnet-4-6",  # written trend analysis — stronger
}

# Hard cap on total LLM spend per pipeline run, in USD. The run aborts cleanly
# when the cap is reached. Override per-run with --max-budget-usd.
DEFAULT_MAX_BUDGET_USD = 1.00
# ═══════════════════════════════════════════════════════════════════════════

import data_access
import runners
from env_check import get_required
from generate_sample_data import build_dataframe as build_sample_dataframe


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Tracks per-agent spend against the hard cap."""

    def __init__(self, max_usd: float):
        self.max_usd = max_usd
        self.per_agent: dict[str, float] = {}

    @property
    def spent(self) -> float:
        return sum(self.per_agent.values())

    @property
    def remaining(self) -> float:
        return self.max_usd - self.spent

    def charge(self, agent: str, cost: float) -> None:
        self.per_agent[agent] = self.per_agent.get(agent, 0.0) + cost

    def ensure_available(self, agent: str) -> float:
        if self.remaining <= 0:
            raise BudgetExceeded(
                f"Budget of ${self.max_usd:.2f} exhausted (spent ${self.spent:.4f}) "
                f"before running {agent}. Aborting cleanly — no partial write."
            )
        return self.remaining


def _banner(stage: str) -> None:
    print(f"\n{'─' * 60}\n▶ {stage}\n{'─' * 60}")


async def _build_edgar_dataframe(ticker: str, budget: Budget):
    """Research + extraction against live SEC EDGAR. Returns (df, company)."""
    import edgar  # imported lazily so sample mode never touches it

    _banner(f"Research agent [{AGENT_MODELS['research']}] — locating filings for {ticker}")
    client = edgar.EdgarClient()
    cik, company = client.ticker_to_cik(ticker)
    submissions = client.submissions(cik)
    fye = submissions.get("fiscalYearEnd") or ""
    filings = edgar.recent_filings_summary(submissions, client=client)
    print(f"  CIK {cik} — {company}; fiscalYearEnd={fye!r}; "
          f"{len(filings)} recent 10-K/10-Q filings")

    research_input = json.dumps({
        "ticker": ticker, "cik": f"{cik:0>10}",
        "fiscal_year_end_mmdd": fye, "filings": filings,
    }, indent=1)
    budget.ensure_available("research")
    text, cost = await runners.call_agent(
        "research_agent", f"Select the filings to cover:\n{research_input}",
        AGENT_MODELS["research"], budget.remaining)
    budget.charge("research", cost)
    research = runners.parse_json_response("research_agent", text)
    if "error" in research:
        raise runners.AgentError(f"research_agent: {research['error']}")
    fye_month = int(research.get("fiscal_year_end_month") or int(fye[:2] or 12))
    end_fy = int(research["coverage"]["end_fy"])
    start_fy = max(int(research["coverage"]["start_fy"]), end_fy - 4)
    print(f"  Coverage FY{start_fy}–FY{end_fy}, fiscal year ends month {fye_month}; "
          f"cost ${cost:.4f}")

    _banner(f"Extraction agent [{AGENT_MODELS['extraction']}] — mapping XBRL tags")
    facts = client.companyfacts(cik)
    filtered = edgar.filter_facts(facts, min_year=start_fy - 1)
    summary = edgar.facts_summary_for_agent(filtered)
    print(f"  {len(filtered)} candidate tags with data in window")
    budget.ensure_available("extraction")
    text, cost = await runners.call_agent(
        "extraction_agent",
        f"Available XBRL tags for {company} ({ticker}):\n{json.dumps(summary, indent=1)}",
        AGENT_MODELS["extraction"], budget.remaining)
    budget.charge("extraction", cost)
    mapping = runners.parse_json_response("extraction_agent", text)
    tag_map = mapping.get("tag_map", {})
    mapped = {k: v for k, v in tag_map.items() if v}
    print(f"  Mapped {len(mapped)}/{len(edgar.CONCEPTS)} concepts; cost ${cost:.4f}")
    for note in mapping.get("notes", []):
        print(f"  note: {note}")

    series = edgar.extract_concept_series(filtered, tag_map, fye_month)
    df = edgar.assemble_dataframe(series, company, ticker, cik, fye_month,
                                  start_fy, end_fy)
    if df.empty:
        raise runners.AgentError("Extraction produced no rows — check the tag map")
    print(f"  Assembled {len(df)} rows, {df['metric'].nunique()} distinct metrics")
    return df


async def _run(source: str, ticker: str | None, max_budget_usd: float,
               skip_analysis: bool = False) -> dict:
    budget = Budget(max_budget_usd)

    if source == "sample":
        _banner("Building sample dataset (no EDGAR calls, no extraction cost)")
        df = build_sample_dataframe()
        print(f"  {len(df)} rows for {df['company'].iloc[0]}")
    else:
        df = await _build_edgar_dataframe(ticker, budget)

    _banner(f"Validation agent [{AGENT_MODELS['validation']}] — pre-write checks")
    budget.ensure_available("validation")
    report, cost = await runners.run_validation(df, AGENT_MODELS["validation"],
                                               budget.remaining)
    budget.charge("validation", cost)
    print(f"  Verdict: {report['verdict']} "
          f"({report['n_errors']} errors, {report['n_warnings']} warnings); cost ${cost:.4f}")
    for f in report["findings"]:
        if f["severity"] in ("error", "warning"):
            print(f"    [{f['severity']}] {f['period']} {f['metric']}: {f['detail']}")
    report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["source"] = source
    data_access.write_validation_report(report)

    if report["verdict"] == "fail":
        raise runners.AgentError(
            "Validation errors block the write. financials.csv was NOT updated. "
            "See data/validation_report.json; fix extraction (prompts/extraction_agent.md) "
            "and re-run.")

    _banner("Writing financials.csv via data_access (stamps metadata.json)")
    data_access.write_financials(df, source=source)
    meta = data_access.load_metadata()
    print(f"  {meta['row_count']} rows, periods {meta['periods_covered'][0]} .. "
          f"{meta['periods_covered'][1]}, source={meta['source']}")
    # Keep the Tableau data files in lockstep with every successful write:
    # a per-ticker file (stable source for published workbooks) plus the
    # canonical tableau_export.csv mirroring the latest run.
    tableau_path = data_access.export_tableau()
    print(f"  Tableau exports refreshed: {tableau_path.name} + tableau_export.csv (latest)")

    if skip_analysis:
        _banner("Analysis skipped (--skip-analysis) — data written, no report generated")
        _print_costs(budget)
        return {"source": source, "ticker": meta["ticker"], "rows": meta["row_count"],
                "validation": report["verdict"], "cost_usd": round(budget.spent, 4)}

    _banner(f"Analysis agent [{AGENT_MODELS['analysis']}] — written trend analysis")
    budget.ensure_available("analysis")
    report_md, categories, cost = await runners.run_analysis(
        df, source, AGENT_MODELS["analysis"], budget.remaining)
    budget.charge("analysis", cost)
    data_access.write_analysis({
        "ticker": meta["ticker"], "source": source,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "categories": categories, "report_md": report_md,
    })
    report_path = data_access.export_analysis_report()
    print(f"  Report: {len(report_md):,} chars across {len(categories)} categories; "
          f"cost ${cost:.4f}")
    print(f"  Companion doc saved: {report_path.relative_to(PROJECT_ROOT)}")

    _print_costs(budget)
    return {"source": source, "ticker": meta["ticker"], "rows": meta["row_count"],
            "validation": report["verdict"], "cost_usd": round(budget.spent, 4)}


def _print_costs(budget: Budget) -> None:
    _banner("Cost summary")
    for agent, cost in budget.per_agent.items():
        print(f"  {agent:<12} {AGENT_MODELS.get(agent, '-'):<22} ${cost:.4f}")
    print(f"  {'TOTAL':<12} {'':<22} ${budget.spent:.4f}  (cap ${budget.max_usd:.2f})")


def run_pipeline(source: str = "sample", ticker: str | None = None,
                 max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
                 skip_analysis: bool = False) -> dict:
    """Headless entry point: run the full pipeline and return a summary dict."""
    get_required("ANTHROPIC_API_KEY")  # clear message, not a crash, if missing
    if source == "edgar":
        if not ticker:
            raise SystemExit("--ticker is required with --source edgar")
        get_required("SEC_EDGAR_USER_AGENT")
    return asyncio.run(_run(source, ticker, max_budget_usd, skip_analysis))


def main() -> int:
    parser = argparse.ArgumentParser(description="Financial analysis agent pipeline")
    parser.add_argument("--source", choices=["sample", "edgar"], default="sample",
                        help="sample = synthetic data, zero EDGAR calls (default); "
                             "edgar = live SEC data (spends metered credit)")
    parser.add_argument("--ticker", help="Ticker symbol (edgar mode only)")
    parser.add_argument("--max-budget-usd", type=float, default=DEFAULT_MAX_BUDGET_USD,
                        help=f"Hard cap on LLM spend (default {DEFAULT_MAX_BUDGET_USD})")
    parser.add_argument("--skip-analysis", action="store_true",
                        help="Stop after validation+write; skip the (priciest) analysis agent")
    args = parser.parse_args()
    try:
        summary = run_pipeline(args.source, args.ticker, args.max_budget_usd,
                               args.skip_analysis)
    except BudgetExceeded as e:
        print(f"\nBUDGET EXCEEDED: {e}")
        return 2
    print(f"\nDone: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

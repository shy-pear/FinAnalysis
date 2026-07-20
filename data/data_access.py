"""Single source of truth for all reads and writes of financials.csv.

No other file in this project touches financials.csv, metadata.json, or
tableau_export.csv directly. The dashboard, orchestrator, sample generator,
and Tableau exporter all go through this module. Swapping the CSV for a
database backend later means editing this file only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent
FINANCIALS_CSV = DATA_DIR / "financials.csv"
METADATA_JSON = DATA_DIR / "metadata.json"
TABLEAU_EXPORT_CSV = DATA_DIR / "tableau_export.csv"
ANALYSIS_JSON = DATA_DIR / "analysis.json"
VALIDATION_JSON = DATA_DIR / "validation_report.json"

# Tidy long format — one row per company-period-metric. See CLAUDE.md.
SCHEMA_COLUMNS = [
    "company",
    "ticker",
    "period",
    "fiscal_year",
    "fiscal_quarter",
    "frequency",
    "category",
    "statement",
    "metric",
    "value",
    "unit",
    "source_url",
]

VALID_SOURCES = {"sample", "edgar"}
VALID_FREQUENCIES = {"Annual", "Quarterly"}
VALID_CATEGORIES = [
    "Growth",
    "Profitability",
    "Cash Generation",
    "Financial Health & Solvency",
    "Capital Allocation",
]
VALID_STATEMENTS = {"income_statement", "balance_sheet", "cash_flow", "ratio"}


def load_financials(
    frequency: str | None = None,
    category: str | None = None,
    metric: str | None = None,
) -> pd.DataFrame:
    """Load financials.csv with `period` typed as datetime, optionally filtered.

    Filters are exact matches; None means no filter on that column.
    """
    if not FINANCIALS_CSV.exists():
        raise FileNotFoundError(
            f"{FINANCIALS_CSV} not found. Generate it first with "
            "`python data/generate_sample_data.py` or run the pipeline with "
            "`python agents/orchestrator.py --source sample`."
        )
    df = pd.read_csv(FINANCIALS_CSV, parse_dates=["period"])
    if frequency is not None:
        df = df[df["frequency"] == frequency]
    if category is not None:
        df = df[df["category"] == category]
    if metric is not None:
        df = df[df["metric"] == metric]
    return df.reset_index(drop=True)


def write_financials(df: pd.DataFrame, source: str) -> None:
    """Validate the schema, write financials.csv, and stamp metadata.json.

    Every write records last_updated, source, ticker, row_count, and
    periods_covered so the dashboard can display data freshness.
    """
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {sorted(VALID_SOURCES)}, got {source!r}")

    missing = [c for c in SCHEMA_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame is missing required columns: {missing}")
    extra = [c for c in df.columns if c not in SCHEMA_COLUMNS]
    if extra:
        raise ValueError(f"DataFrame has unexpected columns: {extra}")
    if df.empty:
        raise ValueError("Refusing to write an empty DataFrame")

    df = df[SCHEMA_COLUMNS].copy()

    # period must be a real ISO date — Tableau and pandas time-series depend on it.
    df["period"] = pd.to_datetime(df["period"], errors="raise")

    if not pd.api.types.is_numeric_dtype(df["value"]):
        raise ValueError("`value` must be numeric — raw numbers, never formatted strings")

    bad_freq = set(df["frequency"].unique()) - VALID_FREQUENCIES
    if bad_freq:
        raise ValueError(f"Invalid frequency values: {sorted(bad_freq)}")
    bad_cat = set(df["category"].unique()) - set(VALID_CATEGORIES)
    if bad_cat:
        raise ValueError(f"Invalid category values: {sorted(bad_cat)}")
    bad_stmt = set(df["statement"].unique()) - VALID_STATEMENTS
    if bad_stmt:
        raise ValueError(f"Invalid statement values: {sorted(bad_stmt)}")
    if df["source_url"].isna().any() or (df["source_url"].astype(str).str.strip() == "").any():
        raise ValueError("Every row must carry a source_url")

    df = df.sort_values(["frequency", "period", "category", "metric"]).reset_index(drop=True)

    out = df.copy()
    out["period"] = out["period"].dt.strftime("%Y-%m-%d")
    out.to_csv(FINANCIALS_CSV, index=False)

    tickers = df["ticker"].unique().tolist()
    metadata = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "ticker": tickers[0] if len(tickers) == 1 else tickers,
        "row_count": int(len(df)),
        "periods_covered": [
            df["period"].min().strftime("%Y-%m-%d"),
            df["period"].max().strftime("%Y-%m-%d"),
        ],
    }
    METADATA_JSON.write_text(json.dumps(metadata, indent=2) + "\n")


def load_metadata() -> dict:
    """Return the freshness metadata stamped by the last write_financials call."""
    if not METADATA_JSON.exists():
        raise FileNotFoundError(
            f"{METADATA_JSON} not found — no data has been written yet. "
            "Run `python data/generate_sample_data.py` first."
        )
    return json.loads(METADATA_JSON.read_text())


def write_analysis(payload: dict) -> None:
    """Persist the analysis agent's output (per-category commentary + report).

    Expected keys: ticker, source, generated_at, categories (dict of
    category -> markdown commentary), report_md (full report).
    """
    ANALYSIS_JSON.write_text(json.dumps(payload, indent=2) + "\n")


def load_analysis() -> dict:
    """Return the last analysis payload, or raise with a pointer to the fix."""
    if not ANALYSIS_JSON.exists():
        raise FileNotFoundError(
            f"{ANALYSIS_JSON} not found — run the pipeline first: "
            "`python agents/orchestrator.py --source sample`."
        )
    return json.loads(ANALYSIS_JSON.read_text())


def write_validation_report(payload: dict) -> None:
    """Persist the validation agent's findings for auditability."""
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2) + "\n")


def load_validation_report() -> dict:
    if not VALIDATION_JSON.exists():
        raise FileNotFoundError(f"{VALIDATION_JSON} not found — run the pipeline first.")
    return json.loads(VALIDATION_JSON.read_text())


def restore_from_export(ticker: str) -> dict:
    """Reload a previously-run company from its surviving per-ticker export.

    Free and offline: tableau_export_<TICKER>.csv carries the full schema, so
    restoring is just writing it back through the normal validated path.
    Returns the freshly stamped metadata.
    """
    path = DATA_DIR / f"tableau_export_{ticker}.csv"
    if not path.exists():
        available = sorted(p.stem.replace("tableau_export_", "")
                           for p in DATA_DIR.glob("tableau_export_*.csv"))
        raise FileNotFoundError(
            f"No saved export for {ticker!r} (have: {available or 'none'}). "
            f"Run the pipeline first: python agents/orchestrator.py "
            f"--source edgar --ticker {ticker}")
    df = pd.read_csv(path, parse_dates=["period"])
    source = ("sample" if str(df["source_url"].iloc[0]).startswith("https://example.com")
              else "edgar")
    write_financials(df, source=source)
    export_tableau()  # keep the canonical 'latest' export in step with the restore
    return load_metadata()


def export_analysis_report() -> Path:
    """Render the saved analysis as docs/analysis_report_<TICKER>.md.

    The companion document to the dashboards — same content the dashboard
    shows per category, as one readable file with the audit/advice footer.
    """
    analysis = load_analysis()
    ticker = analysis["ticker"]
    footer = (
        "\n\n---\n\n"
        "> Generated by the analysis agent from SEC EDGAR XBRL data "
        f"({analysis.get('generated_at', '?')}). Every figure is traceable to a "
        f"filing via the source_url column in data/tableau_export_{ticker}.csv.\n>\n"
        "> **Not investment advice:** this document is financial analysis only — "
        "no buy/sell/hold recommendations, price targets, or valuation calls.\n"
    )
    path = DATA_DIR.parent / "docs" / f"analysis_report_{ticker}.md"
    path.write_text(analysis["report_md"] + footer)
    return path


def export_tableau() -> Path:
    """Write the Tableau-ready CSVs and return the per-ticker path.

    Two files, same long-format data:
    - tableau_export_<TICKER>.csv — one per company, never touched by other
      companies' runs. Re-running the same ticker refreshes it in place (new
      quarters), so a published Tableau workbook keeps a stable data source.
    - tableau_export.csv — canonical name, always mirrors the latest run.
    """
    df = load_financials()
    out = df.copy()
    out["period"] = out["period"].dt.strftime("%Y-%m-%d")
    ticker = str(out["ticker"].iloc[0])
    per_ticker = DATA_DIR / f"tableau_export_{ticker}.csv"
    out.to_csv(per_ticker, index=False)
    out.to_csv(TABLEAU_EXPORT_CSV, index=False)
    return per_ticker

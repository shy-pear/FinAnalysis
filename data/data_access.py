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


def export_tableau() -> Path:
    """Write tableau_export.csv — the same long-format data, Tableau-ready.

    Kept as a separate file so the Tableau workflow has a stable artifact
    even while financials.csv is being regenerated.
    """
    df = load_financials()
    out = df.copy()
    out["period"] = out["period"].dt.strftime("%Y-%m-%d")
    out.to_csv(TABLEAU_EXPORT_CSV, index=False)
    return TABLEAU_EXPORT_CSV

# FinAnalysis — Financial Analysis Agent

A single-company financial analysis system with two deliverables:

1. **Agent pipeline** (Python, Claude Agent SDK) — pulls filings from SEC EDGAR,
   extracts figures, validates them, writes a tidy long-format `data/financials.csv`,
   and produces a written trend analysis.
2. **Streamlit dashboard** — single-company deep-dive reading that CSV.

The same CSV also feeds a manually built Tableau Public dashboard
(see `docs/tableau_public_guide.md`).

> **Synthetic data note:** the default dataset is generated sample data for
> **Northwind Devices Inc. (Synthetic)**, ticker `NWD` — a fictional company
> (source: `sample` in `data/metadata.json`). It exists so the whole system runs
> end-to-end with zero live API calls. Nothing in it describes a real company,
> and its `source_url` values point at example.com, not sec.gov.

> **Not investment advice:** all output is financial analysis only — no buy/sell/hold
> recommendations, price targets, or valuation calls.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env — see comments in the file
```

## Usage

```bash
# Generate the sample dataset (no API calls)
python data/generate_sample_data.py

# Run the pipeline against sample data (no live EDGAR calls)
python agents/orchestrator.py --source sample

# Run against live SEC EDGAR data for a real ticker
python agents/orchestrator.py --source edgar --ticker AAPL

# Switch the dashboard back to an already-run company (free, offline, no API key)
python agents/orchestrator.py --restore GOOG

# Launch the dashboard
streamlit run dashboard/app.py
```

## Project layout

```
agents/      orchestrator + agent runners
prompts/     one .md per agent role — editable, no logic
data/        data_access.py (all I/O), financials.csv, metadata.json,
             tableau_export.csv, sample data generator
dashboard/   Streamlit app
docs/        Tableau guide, sample analysis report
```

All CSV reads/writes go through `data/data_access.py` — see `CLAUDE.md` for the
data contract and conventions, `SPEC.md` for the build specification.

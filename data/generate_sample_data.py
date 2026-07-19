"""Generate a realistic sample financials.csv for one fictional company.

Northwind Devices Inc. (ticker NWD) is entirely synthetic — a consumer-hardware
company with an Apple-like September fiscal year-end and holiday-quarter
seasonality. The dataset exists so the whole system runs end-to-end with zero
live API calls.

Economic model, so the numbers tell a coherent story:
- FY2021–FY2022: strong post-launch growth
- FY2023: a revenue dip (demand normalization)
- FY2024–FY2025: reacceleration with expanding margins (services mix)
- Steady buybacks shrink the share count ~1.5%/year; dividends grow ~8%/year.

Internal consistency is enforced by construction:
- Gross Profit = Revenue − COGS; Operating Income = Gross Profit − Opex
- Net Income = (Operating Income − Interest) × (1 − tax rate)
- FCF = OCF − Capex; Net Debt = Total Debt − Cash
- Equity evolves as equity + NI − dividends − buybacks (clean ROE base)
- All margins/ratios are recomputed from the emitted (rounded) components
- Annual flow rows are exact sums of the four quarters
- YoY growth is year-over-year (Q3 vs prior-year Q3) — never QoQ. FY2020 is
  modeled internally (not emitted) so FY2021 growth figures exist.

Balance-sheet items are point-in-time (not seasonally scaled); flow items carry
holiday-quarter seasonality.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import data_access  # same directory; all writes go through the accessor

COMPANY = "Northwind Devices Inc. (Synthetic)"
TICKER = "NWD"
RNG = np.random.default_rng(42)

# Fiscal year ends September 30. Q1 is the December (holiday) quarter.
FISCAL_YEARS = list(range(2020, 2026))  # FY2020 is hidden — YoY base only
EMIT_YEARS = list(range(2021, 2026))

# Annual revenue path in USD billions — growth, dip, reacceleration
ANNUAL_REVENUE_B = {2020: 21.4, 2021: 26.8, 2022: 31.9, 2023: 30.9, 2024: 34.2, 2025: 38.9}
SEASONAL_WEIGHTS = [0.32, 0.23, 0.21, 0.24]  # Q1 (Dec) is the big quarter

TAX_RATE = 0.16


def quarter_end(fy: int, q: int) -> pd.Timestamp:
    """Fiscal quarter end date. FY ends Sep 30, so Q1 ends Dec 31 of fy-1."""
    return pd.Timestamp(
        {1: f"{fy - 1}-12-31", 2: f"{fy}-03-31", 3: f"{fy}-06-30", 4: f"{fy}-09-30"}[q]
    )


def source_url(fy: int, quarter: str) -> str:
    """Clearly fake filing URL — real rows would point at sec.gov."""
    doc = "10-K" if quarter == "FY" else "10-Q"
    return f"https://example.com/synthetic/NWD/{doc}-FY{fy}-{quarter}"


def build_quarter_fundamentals() -> list[dict]:
    """Model one dict of fundamentals per fiscal quarter, FY2020–FY2025."""
    quarters = []
    shares = 5.28e9          # diluted shares at start of FY2020
    equity = 21.0e9
    cash = 7.9e9
    debt = 10.8e9
    dps = 0.055              # dividend per share, grows ~2%/quarter (~25% payout)

    for fy in FISCAL_YEARS:
        rev_fy = ANNUAL_REVENUE_B[fy] * 1e9
        years_in = fy - 2020
        for q in (1, 2, 3, 4):
            noise = RNG.normal(1.0, 0.012)
            revenue = rev_fy * SEASONAL_WEIGHTS[q - 1] * noise

            # Gross margin drifts 41.5% -> 46.5% over six years (services mix)
            gross_margin = 0.415 + 0.0085 * years_in + RNG.normal(0, 0.004)
            cogs = revenue * (1 - gross_margin)
            gross_profit = revenue - cogs

            # Opex ratio eases 22% -> 20.5% of revenue with scale
            opex = revenue * (0.220 - 0.0025 * years_in + RNG.normal(0, 0.003))
            operating_income = gross_profit - opex

            interest = debt * 0.0225 / 4  # ~2.25% annual cost of debt
            net_income = (operating_income - interest) * (1 - TAX_RATE)

            # Cash conversion: OCF runs ~1.25x NI with D&A and working capital;
            # one soft patch in FY2023 keeps the earnings-quality chart honest
            ocf_factor = RNG.normal(1.26, 0.05)
            if fy == 2023 and q in (2, 3):
                ocf_factor = RNG.normal(1.04, 0.03)
            ocf = net_income * ocf_factor
            capex = revenue * RNG.normal(0.055, 0.005)

            dividends = shares * dps
            # Buybacks scale up over time but stay inside FCF with dividends
            buybacks = (400e6 + 80e6 * years_in) * RNG.normal(1.0, 0.10)
            shares = shares * (1 - buybacks / (shares * 65.0))  # implied ~$65 avg price
            dps *= 1.02

            equity = equity + net_income - dividends - buybacks
            cash = cash + ocf - capex - dividends - buybacks + RNG.normal(0, 150e6)
            if q == 4 and fy >= 2022:
                debt -= 600e6  # modest annual deleveraging at year-end

            quarters.append({
                "fy": fy, "q": q, "period": quarter_end(fy, q),
                "revenue": revenue, "gross_profit": gross_profit,
                "operating_income": operating_income, "net_income": net_income,
                "interest": interest, "ocf": ocf, "capex": capex,
                "dividends": dividends, "buybacks": buybacks,
                "shares": shares, "equity": equity, "cash": cash, "debt": debt,
                # Point-in-time items modeled directly (components not emitted)
                "total_assets": equity * 2.18 + RNG.normal(0, 400e6),
                "current_ratio": 1.45 + 0.05 * years_in + RNG.normal(0, 0.04),
            })
    return quarters


def rows_for_period(f: dict, prior: dict | None, fy: int, quarter: str,
                    frequency: str, extra: dict | None = None) -> list[dict]:
    """Emit schema rows for one period from rounded, reconciled fundamentals."""
    url = source_url(fy, quarter)
    period = f["period"]

    # Round USD once, derive every ratio from the rounded values so the
    # emitted data reconciles exactly under the Stage 3 validation rules.
    usd = {k: round(f[k]) for k in ("revenue", "gross_profit", "operating_income",
                                    "net_income", "ocf", "capex", "dividends",
                                    "buybacks", "equity", "cash", "debt",
                                    "total_assets")}
    shares = round(f["shares"])
    fcf = usd["ocf"] - usd["capex"]
    net_debt = usd["debt"] - usd["cash"]
    eps = round(usd["net_income"] / shares, 2)

    def m(category, statement, metric, value, unit):
        return {"company": COMPANY, "ticker": TICKER, "period": period,
                "fiscal_year": fy, "fiscal_quarter": quarter, "frequency": frequency,
                "category": category, "statement": statement, "metric": metric,
                "value": value, "unit": unit, "source_url": url}

    rows = [
        # Growth
        m("Growth", "income_statement", "Revenue", usd["revenue"], "USD"),
        m("Growth", "income_statement", "EPS (Diluted)", eps, "USD/share"),
        # Profitability
        m("Profitability", "income_statement", "Gross Profit", usd["gross_profit"], "USD"),
        m("Profitability", "income_statement", "Operating Income", usd["operating_income"], "USD"),
        m("Profitability", "income_statement", "Net Income", usd["net_income"], "USD"),
        m("Profitability", "ratio", "Gross Margin", round(100 * usd["gross_profit"] / usd["revenue"], 2), "%"),
        m("Profitability", "ratio", "Operating Margin", round(100 * usd["operating_income"] / usd["revenue"], 2), "%"),
        m("Profitability", "ratio", "Net Margin", round(100 * usd["net_income"] / usd["revenue"], 2), "%"),
        # Cash Generation
        m("Cash Generation", "cash_flow", "Operating Cash Flow", usd["ocf"], "USD"),
        m("Cash Generation", "cash_flow", "Capital Expenditures", usd["capex"], "USD"),
        m("Cash Generation", "cash_flow", "Free Cash Flow", fcf, "USD"),
        m("Cash Generation", "ratio", "FCF Margin", round(100 * fcf / usd["revenue"], 2), "%"),
        m("Cash Generation", "ratio", "OCF-to-Net-Income Ratio", round(usd["ocf"] / usd["net_income"], 2), "x"),
        # Financial Health & Solvency
        m("Financial Health & Solvency", "balance_sheet", "Total Assets", usd["total_assets"], "USD"),
        m("Financial Health & Solvency", "balance_sheet", "Total Equity", usd["equity"], "USD"),
        m("Financial Health & Solvency", "balance_sheet", "Cash & Equivalents", usd["cash"], "USD"),
        m("Financial Health & Solvency", "balance_sheet", "Total Debt", usd["debt"], "USD"),
        m("Financial Health & Solvency", "balance_sheet", "Net Debt", net_debt, "USD"),
        m("Financial Health & Solvency", "ratio", "Debt-to-Equity", round(usd["debt"] / usd["equity"], 2), "x"),
        m("Financial Health & Solvency", "ratio", "Current Ratio", round(f["current_ratio"], 2), "x"),
        m("Financial Health & Solvency", "ratio", "Interest Coverage", round(usd["operating_income"] / f["interest"], 1), "x"),
        # Capital Allocation
        m("Capital Allocation", "balance_sheet", "Shares Outstanding (Diluted)", shares, "shares"),
        m("Capital Allocation", "ratio", "Capex as % of Revenue", round(100 * usd["capex"] / usd["revenue"], 2), "%"),
        m("Capital Allocation", "cash_flow", "Dividends Paid", usd["dividends"], "USD"),
        m("Capital Allocation", "cash_flow", "Share Buybacks", usd["buybacks"], "USD"),
    ]

    # YoY growth — always vs the same period one year earlier, never QoQ
    if prior is not None:
        rev_yoy = 100 * (f["revenue"] - prior["revenue"]) / prior["revenue"]
        prior_eps = round(round(prior["net_income"]) / round(prior["shares"]), 2)
        eps_yoy = 100 * (eps - prior_eps) / prior_eps
        rows.append(m("Growth", "ratio", "Revenue YoY Growth %", round(rev_yoy, 2), "%"))
        rows.append(m("Growth", "ratio", "EPS YoY Growth %", round(eps_yoy, 2), "%"))

    # Annual-only return metrics (computed on average balances)
    if extra:
        for metric, value in extra.items():
            rows.append(m("Profitability", "ratio", metric, value, "%"))
    return rows


def aggregate_year(quarters: list[dict], fy: int) -> dict:
    """Annual fundamentals: flows are 4-quarter sums, balances are FY-end (Q4)."""
    qs = [q for q in quarters if q["fy"] == fy]
    q4 = qs[-1]
    flows = {k: sum(q[k] for q in qs) for k in
             ("revenue", "gross_profit", "operating_income", "net_income",
              "interest", "ocf", "capex", "dividends", "buybacks")}
    return {**flows, "period": q4["period"], "shares": q4["shares"],
            "equity": q4["equity"], "cash": q4["cash"], "debt": q4["debt"],
            "total_assets": q4["total_assets"], "current_ratio": q4["current_ratio"]}


def main() -> None:
    quarters = build_quarter_fundamentals()
    years = {fy: aggregate_year(quarters, fy) for fy in FISCAL_YEARS}
    rows: list[dict] = []

    # Quarterly rows — YoY base is the same quarter of the prior fiscal year
    for f in quarters:
        if f["fy"] not in EMIT_YEARS:
            continue
        prior = next(p for p in quarters if p["fy"] == f["fy"] - 1 and p["q"] == f["q"])
        rows.extend(rows_for_period(f, prior, f["fy"], f"Q{f['q']}", "Quarterly"))

    # Annual rows, with annual-only return metrics on average balances
    for fy in EMIT_YEARS:
        y, py = years[fy], years[fy - 1]
        avg_equity = (y["equity"] + py["equity"]) / 2
        avg_assets = (y["total_assets"] + py["total_assets"]) / 2
        avg_invested = (y["equity"] + y["debt"] + py["equity"] + py["debt"]) / 2
        nopat = y["operating_income"] * (1 - TAX_RATE)
        extra = {
            "Return on Equity (ROE)": round(100 * y["net_income"] / avg_equity, 2),
            "Return on Invested Capital (ROIC)": round(100 * nopat / avg_invested, 2),
            "Return on Assets (ROA)": round(100 * y["net_income"] / avg_assets, 2),
        }
        rows.extend(rows_for_period(y, py, fy, "FY", "Annual", extra))

    df = pd.DataFrame(rows)
    data_access.write_financials(df, source="sample")  # stamps metadata.json

    # Acceptance summary
    loaded = data_access.load_financials()
    meta = data_access.load_metadata()
    print(f"Wrote {meta['row_count']} rows for {COMPANY} [{TICKER}]")
    print(f"Distinct metrics: {loaded['metric'].nunique()}")
    print(f"Periods covered:  {meta['periods_covered'][0]} .. {meta['periods_covered'][1]}")
    print(f"period dtype:     {loaded['period'].dtype}")
    print(f"Metadata:         source={meta['source']}, last_updated={meta['last_updated']}")
    print("\nFirst 15 rows:")
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(loaded.head(15).to_string(index=False))


if __name__ == "__main__":
    main()

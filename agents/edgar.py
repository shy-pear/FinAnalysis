"""SEC EDGAR access and XBRL fact assembly for the extraction pipeline.

Pure code — no LLM calls here. The extraction agent (LLM) chooses which XBRL
tags map to which concepts; this module fetches the data, pulls the exact
reported values for the chosen tags, converts year-to-date durations into
true quarters, derives ratio metrics, and emits schema rows. Numbers are
copied mechanically from the filings, never transcribed by a model.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling modules
from env_check import get_required

TICKER_FILE_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"

# Concepts the pipeline extracts; the extraction agent maps each to XBRL tags.
CONCEPTS = [
    "revenue", "cost_of_revenue", "gross_profit", "operating_income",
    "net_income", "eps_diluted", "shares_diluted", "operating_cash_flow",
    "capex", "dividends_paid", "buybacks", "total_assets", "total_equity",
    "cash", "current_assets", "current_liabilities", "debt_current",
    "debt_noncurrent", "interest_expense",
]

# Balance-sheet concepts are point-in-time (instant); everything else is a flow.
INSTANT_CONCEPTS = {
    "total_assets", "total_equity", "cash", "current_assets",
    "current_liabilities", "debt_current", "debt_noncurrent",
}

# Period-average concepts: YTD-subtraction (Q4 = FY − YTD9) is invalid for
# averages — it produces nonsense like negative share counts. Quarterly values
# come only from reported 3-month facts; Q4 uses the FY average as the closest
# reported figure (companies do not file a separate Q4 report).
AVERAGE_CONCEPTS = {"shares_diluted"}

# Superset of tags worth showing the extraction agent — keeps the prompt small.
CANDIDATE_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
    "SalesRevenueNet", "CostOfGoodsAndServicesSold", "CostOfRevenue",
    "CostOfGoodsSold", "GrossProfit", "OperatingIncomeLoss", "NetIncomeLoss",
    "EarningsPerShareDiluted", "WeightedAverageNumberOfDilutedSharesOutstanding",
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets", "PaymentsOfDividends",
    "PaymentsOfDividendsCommonStock", "PaymentsOfDividendsAndDividendEquivalents",
    "PaymentsForRepurchaseOfCommonStock",
    "Assets", "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "CashAndCashEquivalentsAtCarryingValue", "AssetsCurrent",
    "LiabilitiesCurrent", "LongTermDebtCurrent", "DebtCurrent",
    "CommercialPaper", "OtherShortTermBorrowings", "LongTermDebtNoncurrent",
    "InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt",
}


class EdgarClient:
    """Rate-limited EDGAR HTTP client with the SEC-required User-Agent."""

    # SEC fair-access policy allows 10 req/s; stay well under it.
    MIN_INTERVAL_S = 0.25

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = get_required("SEC_EDGAR_USER_AGENT")
        self._last_request = 0.0

    def get_json(self, url: str) -> dict:
        wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def ticker_to_cik(self, ticker: str) -> tuple[int, str]:
        """Return (cik, company title) for a ticker."""
        data = self.get_json(TICKER_FILE_URL)
        for entry in data.values():
            if entry["ticker"].upper() == ticker.upper():
                return int(entry["cik_str"]), entry["title"]
        raise ValueError(f"Ticker {ticker!r} not found in SEC EDGAR company list")

    def submissions(self, cik: int) -> dict:
        return self.get_json(SUBMISSIONS_URL.format(cik=cik))

    def companyfacts(self, cik: int) -> dict:
        return self.get_json(COMPANYFACTS_URL.format(cik=cik))


def accession_url(cik: int, accession: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession.replace('-', '')}/{accession}-index.htm"
    )


def recent_filings_summary(submissions: dict, max_filings: int = 60) -> list[dict]:
    """Trim the submissions feed to 10-K/10-Q references for the research agent."""
    recent = submissions.get("filings", {}).get("recent", {})
    out = []
    for form, accn, fdate, rdate in zip(
        recent.get("form", []), recent.get("accessionNumber", []),
        recent.get("filingDate", []), recent.get("reportDate", []),
    ):
        if form in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
            out.append({"form": form, "accession": accn,
                        "filing_date": fdate, "period_end": rdate})
        if len(out) >= max_filings:
            break
    return out


def filter_facts(companyfacts: dict, min_year: int) -> dict:
    """Keep candidate us-gaap tags with 10-K/10-Q entries ending >= min_year."""
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    filtered: dict = {}
    for tag, body in gaap.items():
        if tag not in CANDIDATE_TAGS:
            continue
        units = {}
        for unit, entries in body.get("units", {}).items():
            kept = [
                {k: e.get(k) for k in ("start", "end", "val", "accn", "form", "fy", "fp")}
                for e in entries
                if e.get("form") in ("10-K", "10-Q", "10-K/A", "10-Q/A")
                and e.get("end", "")[:4].isdigit() and int(e["end"][:4]) >= min_year
            ]
            if kept:
                units[unit] = kept
        if units:
            filtered[tag] = {"label": body.get("label"), "units": units}
    return filtered


def facts_summary_for_agent(filtered: dict) -> dict:
    """Compact per-tag summary so the extraction prompt stays small."""
    summary = {}
    for tag, body in filtered.items():
        units = {}
        for unit, entries in body["units"].items():
            units[unit] = {
                "n_entries": len(entries),
                "period_range": [min(e["end"] for e in entries),
                                 max(e["end"] for e in entries)],
                "sample_values": [e["val"] for e in entries[-3:]],
            }
        summary[tag] = {"label": body["label"], "units": units}
    return summary


def _preferred_unit(units: dict) -> str | None:
    for u in ("USD", "shares", "USD/shares"):
        if u in units:
            return u
    return next(iter(units), None)


def collect_entries(filtered: dict, spec: dict | None) -> list[dict]:
    """Gather fact entries for one concept according to the agent's tag spec.

    prefer_first: for each period, the first listed tag that has data wins.
    sum: values from all listed tags are added per period.
    """
    if not spec or not spec.get("tags"):
        return []
    combine = spec.get("combine", "prefer_first")
    per_tag = []
    for tag in spec["tags"]:
        body = filtered.get(tag)
        if not body:
            continue
        unit = _preferred_unit(body["units"])
        if unit:
            per_tag.append(body["units"][unit])
    if not per_tag:
        return []

    def key(e):  # one value per unique period; later filings win (restatements)
        return (e.get("start"), e["end"])

    if combine == "sum":
        acc: dict = {}
        for entries in per_tag:
            seen_this_tag = {}
            for e in entries:
                seen_this_tag[key(e)] = e  # dedupe within tag, last filing wins
            for k, e in seen_this_tag.items():
                if k in acc:
                    acc[k] = {**acc[k], "val": acc[k]["val"] + e["val"]}
                else:
                    acc[k] = dict(e)
        return list(acc.values())

    merged: dict = {}
    for entries in reversed(per_tag):  # earlier (preferred) tags overwrite later
        for e in entries:
            merged[key(e)] = e
    return list(merged.values())


def fiscal_period(end: pd.Timestamp, fye_month: int) -> tuple[int, int]:
    """Map a period end date to (fiscal_year, fiscal_quarter 1-4).

    Handles 52/53-week calendars by snapping ends in the first week of a month
    back to the prior month (e.g. Oct 2 counts as September).
    """
    month = end.month if end.day > 7 else (end.month - 2) % 12 + 1
    year = end.year if not (end.day <= 7 and end.month == 1) else end.year - 1
    fy = year if month <= fye_month else year + 1
    quarter = 4 - ((fye_month - month) % 12) // 3
    return fy, quarter


def quarterize(entries: list[dict], fye_month: int,
               subtractable: bool = True) -> tuple[dict, dict]:
    """Turn duration facts into true quarterly and annual values.

    Filings often report cash-flow items only as year-to-date (6/9-month)
    durations, and 10-Ks report only the full year. Quarters are recovered by
    subtraction: Q2 = YTD6 − Q1, Q3 = YTD9 − YTD6, Q4 = FY − YTD9 (or FY minus
    the other three quarters). Returns ({(fy, q): {val, accn}}, {fy: {val, accn}}).
    """
    by_fy: dict = {}
    for e in entries:
        if not e.get("start"):
            continue
        start, end = pd.Timestamp(e["start"]), pd.Timestamp(e["end"])
        months = round((end - start).days / 30.44)
        if months not in (3, 6, 9, 12):
            continue
        fy, q = fiscal_period(end, fye_month)
        slot = by_fy.setdefault(fy, {"q": {}, "ytd": {}, "fy": None, "ends": {}})
        rec = {"val": e["val"], "accn": e["accn"], "end": e["end"]}
        if months == 3:
            slot["q"][q] = rec
        elif months == 12:
            slot["fy"] = rec
        else:
            slot["ytd"][months // 3] = rec  # YTD through fiscal Q2 or Q3

    quarterly, annual = {}, {}
    for fy, slot in by_fy.items():
        q, ytd = dict(slot["q"]), slot["ytd"]
        if not subtractable:
            # Averages: only reported 3-month values; Q4 ≈ FY average
            if slot["fy"]:
                annual[fy] = slot["fy"]
                q.setdefault(4, slot["fy"])
            quarterly.update({(fy, qi): rec for qi, rec in q.items()})
            continue
        # Recover missing quarters from YTD chains
        if 2 not in q and 2 in ytd and 1 in q:
            q[2] = {"val": ytd[2]["val"] - q[1]["val"], "accn": ytd[2]["accn"],
                    "end": ytd[2]["end"]}
        if 3 not in q and 3 in ytd:
            base = ytd.get(2) or (
                {"val": q[1]["val"] + q[2]["val"]} if 1 in q and 2 in q else None)
            if base:
                q[3] = {"val": ytd[3]["val"] - base["val"], "accn": ytd[3]["accn"],
                        "end": ytd[3]["end"]}
        if slot["fy"]:
            annual[fy] = slot["fy"]
            if 4 not in q:
                first_three = [q[i]["val"] for i in (1, 2, 3) if i in q]
                ytd9 = ytd.get(3)
                if ytd9 is not None:
                    q[4] = {"val": slot["fy"]["val"] - ytd9["val"],
                            "accn": slot["fy"]["accn"], "end": slot["fy"]["end"]}
                elif len(first_three) == 3:
                    q[4] = {"val": slot["fy"]["val"] - sum(first_three),
                            "accn": slot["fy"]["accn"], "end": slot["fy"]["end"]}
        quarterly.update({(fy, qi): rec for qi, rec in q.items()})
    return quarterly, annual


def instants(entries: list[dict], fye_month: int) -> dict:
    """Point-in-time facts keyed by (fy, quarter); later filings win."""
    out = {}
    for e in sorted(entries, key=lambda x: (x.get("fy") or 0, x["end"])):
        end = pd.Timestamp(e["end"])
        fy, q = fiscal_period(end, fye_month)
        out[(fy, q)] = {"val": e["val"], "accn": e["accn"], "end": e["end"]}
    return out


def extract_concept_series(filtered: dict, tag_map: dict, fye_month: int) -> dict:
    """Apply the extraction agent's tag map: concept -> period-keyed values."""
    series = {}
    for concept in CONCEPTS:
        entries = collect_entries(filtered, tag_map.get(concept))
        if concept in INSTANT_CONCEPTS:
            inst = instants([e for e in entries if not e.get("start")], fye_month)
            series[concept] = {"quarterly": inst,
                               "annual": {fy: rec for (fy, q), rec in inst.items() if q == 4}}
        else:
            quarterly, annual = quarterize(entries, fye_month,
                                           subtractable=concept not in AVERAGE_CONCEPTS)
            series[concept] = {"quarterly": quarterly, "annual": annual}
    return series


# Assumed statutory tax rate for the ROIC NOPAT approximation in edgar mode —
# tax expense is not extracted, so this is an explicit approximation, not data.
ASSUMED_TAX_RATE = 0.21


def assemble_dataframe(series: dict, company: str, ticker: str, cik: int,
                       fye_month: int, start_fy: int, end_fy: int) -> pd.DataFrame:
    """Build tidy long-format schema rows (base + derived metrics) from series."""

    def val(concept, key, kind):
        rec = series.get(concept, {}).get(kind, {}).get(key)
        return None if rec is None else rec["val"]

    def url_of(concept, key, kind):
        rec = series.get(concept, {}).get(kind, {}).get(key)
        return accession_url(cik, rec["accn"]) if rec else None

    def canonical_end(key, kind):
        for c in ("revenue", "net_income", "total_assets"):
            rec = series.get(c, {}).get(kind, {}).get(key)
            if rec:
                return pd.Timestamp(rec["end"])
        return None

    rows: list[dict] = []

    def emit(period, fy, fq, freq, category, statement, metric, value, unit, url):
        if value is None or url is None or pd.isna(value):
            return
        rows.append({"company": company, "ticker": ticker, "period": period,
                     "fiscal_year": fy, "fiscal_quarter": fq, "frequency": freq,
                     "category": category, "statement": statement, "metric": metric,
                     "value": value, "unit": unit, "source_url": url})

    def emit_period(key, prior_key, fy, fq, freq, kind):
        period = canonical_end(key, kind)
        if period is None:
            return
        g = lambda c: val(c, key, kind)
        u = lambda c: url_of(c, key, kind)
        rev, cogs, gp = g("revenue"), g("cost_of_revenue"), g("gross_profit")
        if gp is None and rev is not None and cogs is not None:
            gp = rev - cogs
        oi, ni, eps = g("operating_income"), g("net_income"), g("eps_diluted")
        ocf, capex = g("operating_cash_flow"), g("capex")
        fcf = ocf - capex if ocf is not None and capex is not None else None
        cash, assets, equity = g("cash"), g("total_assets"), g("total_equity")
        debt_parts = [d for d in (g("debt_current"), g("debt_noncurrent")) if d is not None]
        debt = sum(debt_parts) if debt_parts else None
        net_debt = debt - cash if debt is not None and cash is not None else None
        ca, cl, interest = g("current_assets"), g("current_liabilities"), g("interest_expense")
        shares = g("shares_diluted")

        rev_u = u("revenue")
        emit(period, fy, fq, freq, "Growth", "income_statement", "Revenue", rev, "USD", rev_u)
        emit(period, fy, fq, freq, "Growth", "income_statement", "EPS (Diluted)", eps, "USD/share", u("eps_diluted"))
        emit(period, fy, fq, freq, "Profitability", "income_statement", "Gross Profit", gp, "USD", u("gross_profit") or rev_u)
        emit(period, fy, fq, freq, "Profitability", "income_statement", "Operating Income", oi, "USD", u("operating_income"))
        emit(period, fy, fq, freq, "Profitability", "income_statement", "Net Income", ni, "USD", u("net_income"))
        if rev:
            for name, num in (("Gross Margin", gp), ("Operating Margin", oi), ("Net Margin", ni)):
                if num is not None:
                    emit(period, fy, fq, freq, "Profitability", "ratio", name,
                         round(100 * num / rev, 2), "%", rev_u)
        emit(period, fy, fq, freq, "Cash Generation", "cash_flow", "Operating Cash Flow", ocf, "USD", u("operating_cash_flow"))
        emit(period, fy, fq, freq, "Cash Generation", "cash_flow", "Capital Expenditures", capex, "USD", u("capex"))
        emit(period, fy, fq, freq, "Cash Generation", "cash_flow", "Free Cash Flow", fcf, "USD", u("operating_cash_flow"))
        if rev and fcf is not None:
            emit(period, fy, fq, freq, "Cash Generation", "ratio", "FCF Margin",
                 round(100 * fcf / rev, 2), "%", u("operating_cash_flow"))
        if ni and ocf is not None:
            emit(period, fy, fq, freq, "Cash Generation", "ratio", "OCF-to-Net-Income Ratio",
                 round(ocf / ni, 2), "x", u("operating_cash_flow"))
        emit(period, fy, fq, freq, "Financial Health & Solvency", "balance_sheet", "Total Assets", assets, "USD", u("total_assets"))
        emit(period, fy, fq, freq, "Financial Health & Solvency", "balance_sheet", "Total Equity", equity, "USD", u("total_equity"))
        emit(period, fy, fq, freq, "Financial Health & Solvency", "balance_sheet", "Cash & Equivalents", cash, "USD", u("cash"))
        emit(period, fy, fq, freq, "Financial Health & Solvency", "balance_sheet", "Total Debt", debt, "USD", u("debt_noncurrent") or u("debt_current"))
        emit(period, fy, fq, freq, "Financial Health & Solvency", "balance_sheet", "Net Debt", net_debt, "USD", u("debt_noncurrent") or u("debt_current"))
        if equity and debt is not None:
            emit(period, fy, fq, freq, "Financial Health & Solvency", "ratio", "Debt-to-Equity",
                 round(debt / equity, 2), "x", u("total_equity"))
        if ca is not None and cl:
            emit(period, fy, fq, freq, "Financial Health & Solvency", "ratio", "Current Ratio",
                 round(ca / cl, 2), "x", u("current_assets"))
        if oi is not None and interest:
            emit(period, fy, fq, freq, "Financial Health & Solvency", "ratio", "Interest Coverage",
                 round(oi / interest, 1), "x", u("interest_expense"))
        emit(period, fy, fq, freq, "Capital Allocation", "balance_sheet", "Shares Outstanding (Diluted)", shares, "shares", u("shares_diluted"))
        if rev and capex is not None:
            emit(period, fy, fq, freq, "Capital Allocation", "ratio", "Capex as % of Revenue",
                 round(100 * capex / rev, 2), "%", u("capex"))
        emit(period, fy, fq, freq, "Capital Allocation", "cash_flow", "Dividends Paid", g("dividends_paid"), "USD", u("dividends_paid"))
        emit(period, fy, fq, freq, "Capital Allocation", "cash_flow", "Share Buybacks", g("buybacks"), "USD", u("buybacks"))

        # YoY growth — always vs the same period one year earlier, never QoQ
        if prior_key is not None:
            for metric, concept in (("Revenue YoY Growth %", "revenue"),
                                    ("EPS YoY Growth %", "eps_diluted")):
                cur, prev = val(concept, key, kind), val(concept, prior_key, kind)
                if cur is not None and prev:
                    emit(period, fy, fq, freq, "Growth", "ratio", metric,
                         round(100 * (cur - prev) / abs(prev), 2), "%", u(concept))

    for fy in range(start_fy, end_fy + 1):
        for q in (1, 2, 3, 4):
            emit_period((fy, q), (fy - 1, q), fy, f"Q{q}", "Quarterly", "quarterly")
        emit_period(fy, fy - 1, fy, "FY", "Annual", "annual")

        # Annual-only return metrics on average balances (year-end vs prior year-end)
        period = canonical_end(fy, "annual")
        ni = val("net_income", fy, "annual")
        oi = val("operating_income", fy, "annual")
        if period is None or ni is None:
            continue

        def avg_balance(concept):
            cur, prev = val(concept, fy, "annual"), val(concept, fy - 1, "annual")
            if cur is None:
                return None
            return (cur + prev) / 2 if prev is not None else cur

        equity_avg, assets_avg = avg_balance("total_equity"), avg_balance("total_assets")
        debt_avg_parts = [d for d in (avg_balance("debt_current"), avg_balance("debt_noncurrent"))
                          if d is not None]
        url = url_of("net_income", fy, "annual")
        if equity_avg:
            emit(period, fy, "FY", "Annual", "Profitability", "ratio",
                 "Return on Equity (ROE)", round(100 * ni / equity_avg, 2), "%", url)
        if assets_avg:
            emit(period, fy, "FY", "Annual", "Profitability", "ratio",
                 "Return on Assets (ROA)", round(100 * ni / assets_avg, 2), "%", url)
        if oi is not None and equity_avg and debt_avg_parts:
            invested = equity_avg + sum(debt_avg_parts)
            emit(period, fy, "FY", "Annual", "Profitability", "ratio",
                 "Return on Invested Capital (ROIC)",
                 round(100 * oi * (1 - ASSUMED_TAX_RATE) / invested, 2), "%", url)

    return pd.DataFrame(rows)

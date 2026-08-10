"""SEC EDGAR filings — free, no API key, but SEC requires a real identifying
User-Agent header on every request or it will start rejecting calls.
Docs: https://www.sec.gov/os/webmaster-faq#developers
"""

import logging

import httpx

import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{doc}"

HEADERS = {"User-Agent": config.SEC_USER_AGENT}

_ticker_to_cik_cache: dict[str, str] | None = None


def _load_ticker_map() -> dict[str, str]:
    global _ticker_to_cik_cache
    if _ticker_to_cik_cache is not None:
        return _ticker_to_cik_cache
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
        response = client.get(TICKER_MAP_URL)
        response.raise_for_status()
        data = response.json()
    # data is like {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    mapping = {}
    for entry in data.values():
        ticker = str(entry.get("ticker", "")).upper()
        cik = str(entry.get("cik_str", "")).zfill(10)
        if ticker:
            mapping[ticker] = cik
    _ticker_to_cik_cache = mapping
    return mapping


def _resolve_cik(symbol: str) -> str | None:
    try:
        mapping = _load_ticker_map()
    except httpx.HTTPError as exc:
        logger.warning("SEC ticker map fetch failed: %s", exc)
        return None
    return mapping.get(symbol.strip().upper())


def get_latest_filing(symbol: str) -> dict | None:
    """Structured most-recent SEC filing for a ticker, used by filing-watch
    alerts to detect when a NEW filing appears. Returns
    {"id", "form", "date", "url"} or None if nothing could be fetched."""
    symbol = symbol.strip().upper()
    cik = _resolve_cik(symbol)
    if not cik:
        return None

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
            response = client.get(SUBMISSIONS_URL.format(cik=cik))
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("SEC latest-filing fetch failed for %s: %s", symbol, exc)
        return None

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    if not forms:
        return None

    cik_int = str(int(cik))
    accession_nodash = accessions[0].replace("-", "")
    doc = primary_docs[0] if primary_docs else ""
    url = (
        FILING_INDEX_URL.format(cik_int=cik_int, accession_nodash=accession_nodash, doc=doc)
        if doc
        else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    )
    return {"id": accessions[0], "form": forms[0], "date": dates[0], "url": url}


def get_recent_filings(symbol: str, count: int = 5) -> str:
    """Most recent SEC filings (10-K, 10-Q, 8-K, etc.) for a US-listed ticker."""
    symbol = symbol.strip().upper()
    cik = _resolve_cik(symbol)
    if not cik:
        return (
            f"ERROR: no SEC CIK found for '{symbol}'. This tool only covers "
            "companies that file with the US SEC (US-listed equities)."
        )

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, headers=HEADERS) as client:
            response = client.get(SUBMISSIONS_URL.format(cik=cik))
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("SEC submissions fetch failed for %s (CIK %s): %s", symbol, cik, exc)
        return f"ERROR: could not fetch SEC filings for {symbol} right now."

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    if not forms:
        return f"No SEC filings found for {symbol}."

    cik_int = str(int(cik))  # strip leading zeros for the Archives URL
    lines = [f"Recent SEC filings for {data.get('name', symbol)} ({symbol}):"]
    for i in range(min(count, len(forms))):
        accession_nodash = accessions[i].replace("-", "")
        doc = primary_docs[i] if i < len(primary_docs) else ""
        url = (
            FILING_INDEX_URL.format(cik_int=cik_int, accession_nodash=accession_nodash, doc=doc)
            if doc
            else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        )
        lines.append(f"- {forms[i]} filed {dates[i]}: {url}")

    return "\n".join(lines)

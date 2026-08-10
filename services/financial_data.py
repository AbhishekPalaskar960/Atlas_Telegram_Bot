import json
import logging
from datetime import date, timedelta

import httpx

import config

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
YAHOO_BASE_URL = "https://query1.finance.yahoo.com"
REQUEST_TIMEOUT = 15

YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MAX_TOOL_ROUNDS = 4

# Tool schemas exposed to the LLM (OpenAI-compatible format).
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_quote",
            "description": (
                "Get the current live stock price, daily change, and day range "
                "for a ticker symbol (e.g. AAPL, MSFT, RELIANCE.NS, TCS.NS, INFY.NS)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker symbol"}
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_company",
            "description": (
                "Resolve a company name or ticker to its ticker symbol and profile "
                "(legal name, exchange, industry, market cap). Use when the user "
                "mentions a company by name. Works for US and Indian companies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Company name or ticker"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_news",
            "description": (
                "Get the latest news headlines (last ~5 days) for a company "
                "given its ticker symbol."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker symbol"}
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_fundamentals",
            "description": (
                "Get company fundamentals for a ticker symbol: market cap, industry, "
                "IPO date, analyst recommendation consensus, and peer companies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker symbol"}
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_news",
            "description": "Get today's top general market news headlines.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_earnings_calendar",
            "description": (
                "Get the upcoming earnings report dates for a ticker (next ~90 "
                "days), e.g. 'when is NVDA reporting earnings?' or 'any upcoming "
                "earnings on my watchlist?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Stock ticker symbol"}
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_google_sheet",
            "description": (
                "Fetch the contents of a publicly shared Google Sheets link "
                "the user pasted (share with 'Anyone with the link'), and analyze "
                "its rows — KPIs, trends, anomalies, comparisons."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Google Sheets share URL or sheet ID"}
                },
                "required": ["url"],
            },
        },
    },
]


def _get(endpoint: str, params: dict) -> dict | list:
    params = dict(params)
    params["token"] = config.FINNHUB_API_KEY
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(FINNHUB_BASE_URL + endpoint, params=params)
        response.raise_for_status()
        return response.json()


def _yahoo_get(endpoint: str, params: dict) -> dict:
    """Yahoo Finance fallback source (works for Indian + US symbols)."""
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=YAHOO_HEADERS) as client:
        response = client.get(YAHOO_BASE_URL + endpoint, params=params)
        response.raise_for_status()
        return response.json()


def _fmt_money(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _format_quote(symbol: str, price, change, percent, open_, high, low, prev_close) -> str:
    lines = [f"{symbol}: {_fmt_money(price)}"]
    if change is not None:
        sign = "+" if change >= 0 else ""
        pct_str = f" ({sign}{percent:.2f}%)" if percent is not None else ""
        lines.append(f"Change: {sign}{_fmt_money(change)}{pct_str}")
    lines.append(
        f"Open: {_fmt_money(open_)} | High: {_fmt_money(high)} | Low: {_fmt_money(low)} "
        f"| Prev close: {_fmt_money(prev_close)}"
    )
    return "\n".join(lines)


def _yahoo_quote(symbol: str) -> str:
    data = _yahoo_get(
        f"/v8/finance/chart/{symbol}", {"interval": "1d", "range": "5d"}
    )
    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta.get("regularMarketPrice")
    if not price:
        return f"ERROR: no Yahoo quote available for {symbol}."

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = percent = None
    if prev_close:
        change = price - prev_close
        percent = (change / prev_close) * 100 if prev_close else None

    open_ = meta.get("regularMarketOpen")
    high = meta.get("regularMarketDayHigh")
    low = meta.get("regularMarketDayLow")
    if open_ is None:
        try:
            quote_arr = result["indicators"]["quote"][0]
            opens = [v for v in quote_arr.get("open", []) if v is not None]
            if opens:
                open_ = opens[0]
        except (KeyError, IndexError, TypeError):
            pass
    return _format_quote(symbol, price, change, percent, open_, high, low, prev_close)


def _yahoo_search(query: str) -> str:
    data = _yahoo_get(
        "/v1/finance/search",
        {"q": query, "quotesCount": 8, "newsCount": 0},
    )
    quotes = [q for q in (data.get("quotes") or []) if q.get("quoteType") == "EQUITY"]
    if not quotes:
        return f"ERROR: no company found matching '{query}'."

    indian = [q for q in quotes if q.get("exchange") in ("NSI", "BSE")]
    pick = indian[0] if indian else quotes[0]
    exchange = pick.get("exchDisp") or pick.get("exchange", "n/a")
    return (
        f"Symbol: {pick['symbol']} ({pick.get('shortname', 'n/a')}, "
        f"exchange {exchange})"
    )


def _yahoo_news(symbol: str) -> str:
    data = _yahoo_get(
        "/v1/finance/search",
        {"q": symbol, "quotesCount": 0, "newsCount": 5},
    )
    news = data.get("news") or []
    if not news:
        return f"No news found for {symbol}."
    return "\n".join(
        f"- {item.get('title', '')} ({item.get('publisher', '')}) {item.get('link', '')}"
        for item in news[:5]
    )


def get_stock_quote(symbol: str) -> str:
    """Live price + daily change for a ticker (Finnhub first, Yahoo fallback)."""
    symbol = symbol.strip().upper()
    try:
        data = _get("/quote", {"symbol": symbol})
        current = data.get("c")
        if current:
            return _format_quote(
                symbol,
                current,
                data.get("d"),
                data.get("dp"),
                data.get("o"),
                data.get("h"),
                data.get("l"),
                data.get("pc"),
            )
    except httpx.HTTPError:
        pass
    try:
        return _yahoo_quote(symbol)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Yahoo quote failed for %s: %s", symbol, exc)
        return f"ERROR: no quote available for {symbol}. It may be delisted or the symbol may be wrong."


def search_company(query: str) -> str:
    """Resolve a company name/ticker to its symbol + profile (Finnhub first, Yahoo fallback)."""
    try:
        data = _get("/search", {"q": query})
        results = [r for r in data.get("result", []) if r.get("type") == "Common Stock"]
        if results:
            symbol = results[0]["symbol"]
            try:
                profile = _get("/stock/profile2", {"symbol": symbol})
            except httpx.HTTPError:
                profile = {}

            lines = [
                f"Symbol: {symbol} ({results[0].get('description', 'n/a')}, "
                f"exchange {results[0].get('exchange', 'n/a')})"
            ]
            if profile:
                name = profile.get("name")
                if name:
                    lines.append(f"Company: {name}")
                if profile.get("industry"):
                    lines.append(f"Industry: {profile['industry']}")
                if profile.get("marketCapitalization"):
                    lines.append(f"Market cap: {_fmt_money(profile['marketCapitalization'])} USD")
            return "\n".join(lines)
    except httpx.HTTPError:
        pass
    try:
        return _yahoo_search(query)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Yahoo search failed for %r: %s", query, exc)
        return f"ERROR: no company found matching '{query}'."


def get_latest_news_item(symbol: str) -> dict | None:
    """Structured single most-recent news item for a ticker, used by
    news-watch alerts to detect when something NEW shows up (as opposed to
    get_company_news, which returns a formatted block for the LLM to read).
    Returns {"id", "headline", "url"} or None if nothing could be fetched."""
    symbol = symbol.strip().upper()
    today = date.today()
    frm = today - timedelta(days=3)
    try:
        data = _get("/company-news", {"symbol": symbol, "from": frm.isoformat(), "to": today.isoformat()})
        if isinstance(data, list) and data:
            newest = max(data, key=lambda item: item.get("datetime", 0))
            news_id = newest.get("id")
            return {
                "id": str(news_id) if news_id is not None else str(newest.get("url", "")),
                "headline": newest.get("headline", "").strip(),
                "url": newest.get("url", ""),
            }
    except httpx.HTTPError as exc:
        logger.warning("Finnhub company-news failed for %s: %s", symbol, exc)

    try:
        raw = _yahoo_get("/v1/finance/search", {"q": symbol, "quotesCount": 0, "newsCount": 3})
        news = raw.get("news") or []
        if news:
            top = news[0]
            title = (top.get("title") or "").strip()
            link = top.get("link", "")
            if title:
                return {"id": f"{title}|{link}", "headline": title, "url": link}
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        logger.warning("Yahoo news fallback failed for %s: %s", symbol, exc)

    return None


def get_company_news(symbol: str) -> str:
    """Latest headlines for a ticker (Finnhub first, Yahoo fallback)."""
    symbol = symbol.strip().upper()
    try:
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=5)).isoformat()
        data = _get(
            "/company-news",
            {"symbol": symbol, "from": from_date, "to": to_date},
        )
        if data:
            headlines = []
            for item in data[:5]:
                headline = item.get("headline", "")
                source = item.get("source", "")
                url = item.get("url", "")
                headlines.append(f"- {headline} ({source}) {url}".strip())
            return "\n".join(headlines)
    except httpx.HTTPError:
        pass
    try:
        return _yahoo_news(symbol)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Yahoo news failed for %s: %s", symbol, exc)
        return f"No news found for {symbol} in the last 5 days."


def get_company_fundamentals(symbol: str) -> str:
    """Market cap, industry, IPO, valuation, margins, growth, analyst
    consensus, and peers for a ticker — everything the LLM needs to write a
    real fundamental analysis rather than just a company snapshot."""
    symbol = symbol.strip().upper()
    profile = _get("/stock/profile2", {"symbol": symbol})
    if not profile:
        return f"ERROR: no company profile available for {symbol}."

    lines = [f"{profile.get('name', symbol)} ({symbol})"]
    if profile.get("industry"):
        lines.append(f"Industry: {profile['industry']} | Exchange: {profile.get('exchange', 'n/a')}")
    if profile.get("marketCapitalization"):
        lines.append(f"Market cap: {_fmt_money(profile['marketCapitalization'])} USD")
    if profile.get("ipo"):
        lines.append(f"IPO date: {profile['ipo']}")

    try:
        metric = (_get("/stock/metric", {"symbol": symbol, "metric": "all"}) or {}).get("metric", {})
    except httpx.HTTPError:
        metric = {}

    def _pct(key: str) -> str | None:
        value = metric.get(key)
        return f"{value:.1f}%" if isinstance(value, (int, float)) else None

    def _num(key: str, digits: int = 2) -> str | None:
        value = metric.get(key)
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else None

    valuation = {
        "P/E (TTM)": _num("peBasicExclExtraTTM"),
        "P/B": _num("pbAnnual"),
        "Dividend yield": _pct("dividendYieldIndicatedAnnual"),
    }
    valuation_line = " | ".join(f"{k}: {v}" for k, v in valuation.items() if v)
    if valuation_line:
        lines.append(f"Valuation — {valuation_line}")

    margins = {
        "Gross margin": _pct("grossMarginTTM"),
        "Operating margin": _pct("operatingMarginTTM"),
        "Net margin": _pct("netProfitMarginTTM"),
    }
    margins_line = " | ".join(f"{k}: {v}" for k, v in margins.items() if v)
    if margins_line:
        lines.append(f"Margins (TTM) — {margins_line}")

    profitability = {
        "ROE": _pct("roeTTM"),
        "ROA": _pct("roaTTM"),
        "ROI": _pct("roiTTM"),
    }
    profitability_line = " | ".join(f"{k}: {v}" for k, v in profitability.items() if v)
    if profitability_line:
        lines.append(f"Profitability (TTM) — {profitability_line}")

    growth = {
        "Revenue growth YoY": _pct("revenueGrowthTTMYoy"),
        "EPS growth YoY": _pct("epsGrowthTTMYoy"),
    }
    growth_line = " | ".join(f"{k}: {v}" for k, v in growth.items() if v)
    if growth_line:
        lines.append(f"Growth (TTM YoY) — {growth_line}")

    health = {
        "Current ratio": _num("currentRatioQuarterly"),
        "Debt/Equity": _num("totalDebt/totalEquityQuarterly"),
        "Free cash flow/share": _num("freeCashFlowPerShareTTM"),
    }
    health_line = " | ".join(f"{k}: {v}" for k, v in health.items() if v)
    if health_line:
        lines.append(f"Balance sheet — {health_line}")

    week_high, week_low, beta = metric.get("52WeekHigh"), metric.get("52WeekLow"), metric.get("beta")
    if week_high and week_low:
        range_line = f"52-week range: {_fmt_money(week_low)}–{_fmt_money(week_high)}"
        if isinstance(beta, (int, float)):
            range_line += f" | Beta: {beta:.2f}"
        lines.append(range_line)

    try:
        recommendation = _get("/stock/recommendation", {"symbol": symbol})
        if recommendation:
            latest = recommendation[0]
            rec = " / ".join(
                f"{label}{latest.get(key, 0)}"
                for key, label in (
                    ("strongBuy", "strong buy:"),
                    ("buy", "buy:"),
                    ("hold", "hold:"),
                    ("sell", "sell:"),
                )
            )
            lines.append(f"Analyst consensus ({latest.get('period', 'n/a')}): {rec}")
    except httpx.HTTPError:
        pass

    try:
        peers = _get("/stock/peers", {"symbol": symbol})
        if peers:
            lines.append("Peers: " + ", ".join(peers[:8]))
    except httpx.HTTPError:
        pass

    return "\n".join(lines)


def get_market_news() -> str:
    """Top general market headlines."""
    data = _get("/news", {"category": "general"})
    if not data:
        return "No market news right now."
    headlines = []
    for item in data[:6]:
        headline = item.get("headline", "")
        source = item.get("source", "")
        url = item.get("url", "")
        headlines.append(f"- {headline} ({source}) {url}".strip())
    return "\n".join(headlines)


def _earnings_items(symbol: str) -> list:
    """Raw earnings-calendar entries for a symbol (Finnhub). Next ~90 days.
    Entries look like {symbol, date, hour, epEstimate, epsActual, ...}."""
    today = date.today()
    frm = today.isoformat()
    to = (today + timedelta(days=90)).isoformat()
    try:
        data = _get("/calendar/earnings", {"symbol": symbol, "from": frm, "to": to})
        items = (data or {}).get("earningsCalendar") or []
        return [i for i in items if str(i.get("symbol", "")).upper() == symbol]
    except httpx.HTTPError as exc:
        logger.warning("Finnhub earnings calendar failed for %s: %s", symbol, exc)
        return []


def get_earnings_calendar(symbol: str) -> str:
    """Upcoming earnings report dates for a ticker (next ~90 days)."""
    symbol = symbol.strip().upper()
    items = _earnings_items(symbol)
    if not items:
        return f"No upcoming earnings found for {symbol} in the next 90 days."
    lines = [f"Upcoming earnings for {symbol}:"]
    for item in items:
        lines.append(f"- {item.get('date', 'TBD')}: {item.get('symbol', symbol)}")
    return "\n".join(lines)


def get_latest_earnings_event(symbol: str) -> dict | None:
    """Structured next earnings date for a ticker, used by earnings-watch alerts
    to detect when a NEW earnings date is published (id = the report date so a
    change of date re-fires). Returns {"id", "date"} or None if unavailable."""
    symbol = symbol.strip().upper()
    items = _earnings_items(symbol)
    if not items:
        return None
    items.sort(key=lambda i: str(i.get("date", "")))
    d = items[0].get("date") or ""
    return {"id": f"{symbol}|{d}", "date": d}


def get_quote_numbers(symbol: str) -> tuple:
    """Live (price, change_pct) for alert evaluation. Returns (None, None) if unavailable."""
    try:
        data = _get("/quote", {"symbol": symbol})
        price = data.get("c")
        if price:
            return float(price), data.get("dp")
    except httpx.HTTPError:
        pass
    try:
        data = _yahoo_get(
            "/v8/finance/chart/" + symbol, {"interval": "1d", "range": "5d"}
        )
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        if not price:
            return None, None
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        pct = ((price - prev_close) / prev_close * 100) if prev_close else None
        return float(price), pct
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None, None


def _get_google_sheet(url: str) -> str:
    """Fetch a publicly shared Google Sheet and return its flattened rows."""
    from services.sheets_service import fetch_google_sheet

    return fetch_google_sheet(url)


TOOL_HANDLERS = {
    "get_stock_quote": get_stock_quote,
    "search_company": search_company,
    "get_company_news": get_company_news,
    "get_company_fundamentals": get_company_fundamentals,
    "get_market_news": get_market_news,
    "get_earnings_calendar": get_earnings_calendar,
    "get_google_sheet": _get_google_sheet,
}


def execute_tool(name: str, args: dict) -> str:
    """Run one tool by name; never raises — errors become honest LLM context."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"ERROR: unknown tool '{name}'."
    try:
        return str(handler(**args))
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Tool %s(%s) failed: %s", name, args, exc)
        return (
            f"ERROR: could not fetch data for {name}. "
            "Tell the user this data is currently unavailable rather than guessing."
        )

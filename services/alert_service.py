import logging
import re

from sqlalchemy.orm import Session, joinedload

from models import Alert, User
from services.financial_data import get_latest_earnings_event, get_latest_news_item, get_quote_numbers
from services.sec_service import get_latest_filing

logger = logging.getLogger(__name__)

# Condition syntax stored on Alert.condition:
#   price_move_pct_5      daily |% change| >= 5   (one-shot: fires once, then deactivates)
#   price_above_500       price > 500              (one-shot)
#   price_below_100       price < 100              (one-shot)
#       news                  new company news appears (recurring: stays active)
#       filing                new SEC filing appears    (recurring: stays active)
#       earnings              earnings date published/changed (recurring: stays active)

ALERT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_alert",
            "description": (
                "Set a custom alert for the user: a price alert, OR an ongoing "
                "watch for new company news or new SEC filings. Use when the user "
                "asks to be notified if a stock moves a certain percentage, "
                "crosses a price level (e.g. 'alert me if AAPL falls below 200'), "
                "OR whenever there's a major announcement / SEC filing for a "
                "ticker (e.g. 'track TSLA and notify me of any major announcement "
                "or SEC filing')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL)"},
                    "condition": {
                        "type": "string",
                        "description": (
                            "One of: 'price_move_pct_<n>' (daily % move >= n, e.g. "
                            "price_move_pct_5), 'price_above_<n>' (price above n, e.g. "
                            "price_above_200), 'price_below_<n>' (price below n, e.g. "
                            "price_below_180), 'news' (notify on new company news), "
                            "'filing' (notify on new SEC filings), 'earnings' "
                            "(notify when an earnings date is set for the ticker)."
                        ),
                    },
                },
                "required": ["ticker", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "List the user's active alerts.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_alert",
            "description": (
                "Remove an alert the user no longer wants (by ticker, e.g. 'stop "
                "alerting me about AAPL')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker whose alerts should be removed"}
                },
                "required": ["ticker"],
            },
        },
    },
]

PRICE_CONDITION_RE = re.compile(r"^(price_move_pct|price_above|price_below)_(\d+(?:\.\d+)?)$")
WATCH_KINDS = {"news", "filing", "earnings"}  # recurring watches, not one-shot triggers


def parse_condition(condition: str) -> tuple | None:
    """Validate a condition string; returns (kind, value_or_None) or None if invalid."""
    condition = condition.strip().lower()
    if condition in WATCH_KINDS:
        return condition, None
    match = PRICE_CONDITION_RE.match(condition)
    if not match:
        return None
    return match.group(1), float(match.group(2))


def create_alert(session: Session, user: User, ticker: str, condition: str) -> str:
    """Create a new alert from natural-language args. Returns a confirmation string."""
    ticker = ticker.strip().upper()
    condition = condition.strip().lower()
    if not ticker:
        return "ERROR: missing ticker for create_alert."
    if parse_condition(condition) is None:
        return (
            f"ERROR: unknown alert condition '{condition}'. Use price_move_pct_5, "
            "price_above_200, price_below_180, 'news', 'filing', or 'earnings'."
        )
    alert = Alert(user_id=user.id, ticker=ticker, condition=condition)
    session.add(alert)
    session.commit()
    logger.info("Alert created for user %s: %s %s", user.id, ticker, condition)

    if condition in WATCH_KINDS:
        label = {
            "news": "new company news",
            "filing": "new SEC filings",
            "earnings": "earnings dates",
        }[condition]
        return (
            f"Watching {ticker} for {label}. I'll only message you when something "
            "new actually shows up — the very next item won't fire immediately, "
            "since that's usually something you've already seen."
        )
    return (
        f"Alert set: {ticker} ({condition}). I'll check it regularly and message "
        "you once when it triggers."
    )


def list_alerts(session: Session, user: User) -> str:
    """Human-readable list of the user's active alerts."""
    alerts = (
        session.query(Alert)
        .filter(Alert.user_id == user.id, Alert.active.is_(True))
        .order_by(Alert.created_at.desc())
        .all()
    )
    if not alerts:
        return "You have no active alerts. Ask me to set one, e.g. 'alert me if AAPL moves 5% in a day'."
    lines = [f"- {a.ticker}: {a.condition}" for a in alerts]
    return "Active alerts:\n" + "\n".join(lines)


def delete_alert(session: Session, user: User, ticker: str) -> str:
    """Deactivate all alerts for a ticker. Returns a confirmation string."""
    ticker = ticker.strip().upper()
    alerts = (
        session.query(Alert)
        .filter(
            Alert.user_id == user.id,
            Alert.ticker == ticker,
            Alert.active.is_(True),
        )
        .all()
    )
    if not alerts:
        return f"No active alerts for {ticker}."
    for alert in alerts:
        alert.active = False
    session.commit()
    return f"Removed {len(alerts)} alert(s) for {ticker}."


def _evaluate_price_alert(alert: Alert, kind: str, value: float) -> tuple:
    price, change_pct = get_quote_numbers(alert.ticker)
    if price is None:
        return False, ""

    triggered = False
    if kind == "price_move_pct":
        triggered = abs(change_pct or 0) >= value
    elif kind == "price_above":
        triggered = price > value
    elif kind == "price_below":
        triggered = price < value

    if not triggered:
        return False, ""

    reason = {
        "price_move_pct": f"moved {change_pct:+.2f}% today",
        "price_above": f"crossed above {value:g}",
        "price_below": f"dropped below {value:g}",
    }[kind]
    return True, (
        f"Your alert for {alert.ticker} just triggered — the price is "
        f"${price:,.2f} and it {reason}."
    )


def _evaluate_watch_alert(alert: Alert, item: dict | None, label: str) -> tuple:
    """Shared logic for news/filing watches: fire only when the latest item's
    id differs from what we saw last time. First-ever check just establishes
    the baseline (no notification for pre-existing news/filings)."""
    if not item:
        return False, ""

    new_id = item["id"]
    if alert.last_seen_id is None:
        alert.last_seen_id = new_id  # baseline only, don't notify
        return False, ""

    if new_id == alert.last_seen_id:
        return False, ""

    alert.last_seen_id = new_id
    if label == "news":
        message = (
            f"New for {alert.ticker}: {item['headline']}"
            + (f" — {item['url']}" if item.get("url") else "")
        )
    elif label == "earnings":
        message = f"{alert.ticker} has an earnings report scheduled for {item['date']}."
    else:
        message = f"New SEC filing for {alert.ticker}: {item['form']} filed {item['date']} — {item['url']}"
    return True, message


def evaluate_alert(alert: Alert) -> tuple:
    """Check one alert against live data.

    Returns (triggered: bool, message: str, deactivate: bool). message is a
    user-facing notification when triggered, otherwise ''. Price alerts are
    one-shot (deactivate=True on trigger); news/filing watches stay active.
    """
    parsed = parse_condition(alert.condition)
    if parsed is None:
        return False, "", False
    kind, value = parsed

    if kind == "news":
        triggered, message = _evaluate_watch_alert(alert, get_latest_news_item(alert.ticker), "news")
        return triggered, message, False
    if kind == "news":
        triggered, message = _evaluate_watch_alert(alert, get_latest_news_item(alert.ticker), "news")
        return triggered, message, False
    if kind == "filing":
        triggered, message = _evaluate_watch_alert(alert, get_latest_filing(alert.ticker), "filing")
        return triggered, message, False
    if kind == "earnings":
        triggered, message = _evaluate_watch_alert(alert, get_latest_earnings_event(alert.ticker), "earnings")
        return triggered, message, False

    triggered, message = _evaluate_price_alert(alert, kind, value)
    return triggered, message, triggered  # price alerts deactivate once they fire


def check_and_fire(session: Session) -> list:
    """Evaluate all active alerts, deactivate one-shot alerts that triggered.

    Returns a list of (telegram_id, message) pairs ready to send.
    """
    alerts = (
        session.query(Alert)
        .filter(Alert.active.is_(True))
        .options(joinedload(Alert.user))
        .all()
    )
    fired = []
    for alert in alerts:
        try:
            triggered, message, deactivate = evaluate_alert(alert)
        except Exception as exc:
            logger.warning("Alert check failed for %s (%s): %s", alert.ticker, alert.condition, exc)
            continue

        if deactivate:
            alert.active = False
        session.commit()  # persists last_seen_id / active changes either way

        if triggered and alert.user and alert.user.telegram_id:
            fired.append((alert.user.telegram_id, message))
            logger.info("Alert fired for user %s: %s (%s)", alert.user_id, alert.ticker, alert.condition)
    return fired


def handle_alert_tool(session: Session, user: User, name: str, args: dict) -> str:
    """Dispatch an alert tool call. Never raises."""
    try:
        if name == "create_alert":
            return create_alert(session, user, str(args.get("ticker") or ""), str(args.get("condition") or ""))
        if name == "list_alerts":
            return list_alerts(session, user)
        if name == "delete_alert":
            return delete_alert(session, user, str(args.get("ticker") or ""))
    except Exception as exc:
        logger.warning("Alert tool %s failed: %s", name, exc)
        return f"ERROR: alert tool '{name}' failed. Tell the user to try again."
    return f"ERROR: unknown alert tool '{name}'."
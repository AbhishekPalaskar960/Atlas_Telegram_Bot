import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

import config
from database import get_session
from models import User
from services import alert_service, llm_service
from utils.telegram_formatting import send_long_message

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Kolkata"


def _user_timezone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.briefing_timezone or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def register_jobs(application: Application) -> None:
    """Register scheduled jobs: alert checks + daily briefings."""
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("No JobQueue available — alerts and briefings disabled.")
        return

    job_queue.run_repeating(
        check_alerts,
        interval=config.ALERT_CHECK_MINUTES * 60,
        first=config.ALERT_CHECK_MINUTES * 60,
        name="check-alerts",
    )
    job_queue.run_repeating(
        check_briefings,
        interval=60,
        first=30,
        name="check-briefings",
    )
    logger.info(
        "Scheduler registered: alerts every %d min, briefings every minute.",
        config.ALERT_CHECK_MINUTES,
    )


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Evaluate all active alerts and message users whose alerts triggered."""
    session = get_session()
    try:
        fired = alert_service.check_and_fire(session)
    finally:
        session.close()

    for chat_id, message in fired:
        try:
            await send_long_message(context.bot, chat_id, message)
        except Exception as exc:
            logger.error("Failed to send alert to %s: %s", chat_id, exc)


async def check_briefings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send each user's daily briefing when their local time hits briefing_time."""
    now_utc = datetime.now(timezone.utc)
    session = get_session()
    try:
        users = (
            session.query(User)
            .filter(User.briefing_time.isnot(None), User.onboarded.is_(True))
            .all()
        )
        for user in users:
            local = now_utc.astimezone(_user_timezone(user))
            if local.strftime("%H:%M") != user.briefing_time:
                continue
            if user.last_briefing_date == local.date().isoformat():
                continue
            user.last_briefing_date = local.date().isoformat()
            session.commit()
            logger.info("Briefing due for user %s (%s)", user.id, user.briefing_time)
            text = llm_service.generate_briefing(session, user)
            if not text:
                logger.info("Nothing to send for user %s today — staying silent.", user.id)
                continue
            try:
                await send_long_message(context.bot, user.telegram_id, text)
            except Exception as exc:
                logger.error("Failed to send briefing to %s: %s", user.telegram_id, exc)
    finally:
        session.close()

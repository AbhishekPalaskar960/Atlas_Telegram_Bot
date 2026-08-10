import logging

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from bot.handlers import (
    handle_audio,
    handle_document,
    handle_error,
    handle_photo,
    handle_text,
    handle_voice,
)
from config import TELEGRAM_BOT_TOKEN
from database import init_db
from services.google_auth_service import start_oauth_server
from services.scheduler import register_jobs

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Runs once the bot's asyncio event loop is up — safe place to start
    the background Google OAuth callback server."""
    start_oauth_server(application)


def build_application() -> Application:
    """Create the Telegram Application and register all message handlers."""
    init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.add_error_handler(handle_error)

    register_jobs(application)
    return application


def run_bot() -> None:
    """Boot the bot and start polling Telegram for updates."""
    application = build_application()
    logger.info("Atlas online — polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
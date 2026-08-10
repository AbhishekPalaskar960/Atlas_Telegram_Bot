import logging

from bot.telegram_bot import run_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting Atlas...")
    run_bot()


if __name__ == "__main__":
    main()
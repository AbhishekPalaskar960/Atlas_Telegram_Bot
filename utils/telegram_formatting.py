"""Keep outgoing Telegram messages safe and clean:
1. Telegram rejects any message over 4096 characters — long PDF/sheet
   summaries or briefings can exceed that, so we chunk on sensible boundaries.
2. LLMs often output markdown (**bold**, # headers, markdown bullets) but
   plain reply_text() calls don't render it — so raw asterisks/hashes show
   up literally in the chat. This strips/normalizes that into clean,
   readable plain text instead of asking every service to remember to do it.
"""

import re

TELEGRAM_MAX_LEN = 4096
# leave a little headroom below the hard limit
SAFE_CHUNK_LEN = 4000

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADER_RE = re.compile(r"^#{1,6}\s*(.+)$", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_markdown(text: str) -> str:
    """Turn common LLM markdown into clean plain text for a chat that
    doesn't render markdown by default."""
    if not text:
        return text

    text = _HEADER_RE.sub(r"\1", text)                 # "## Title" -> "Title"
    text = _BOLD_RE.sub(r"\1", text)                    # "**x**" -> "x"
    text = _ITALIC_RE.sub(r"\1", text)                  # "*x*" -> "x"
    text = _INLINE_CODE_RE.sub(r"\1", text)             # "`x`" -> "x"
    text = _MD_BULLET_RE.sub(r"\1• ", text)             # "- x" -> "• x"
    text = _BLANK_LINES_RE.sub("\n\n", text)            # collapse 3+ blank lines
    return text.strip()


def chunk_message(text: str, max_len: int = SAFE_CHUNK_LEN) -> list[str]:
    """Clean markdown, then split text into Telegram-safe chunks, preferring
    paragraph/line/word boundaries over hard character cuts."""
    text = clean_markdown((text or "").strip())
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]

        split_at = window.rfind("\n\n")
        if split_at == -1:
            split_at = window.rfind("\n")
        if split_at == -1:
            split_at = window.rfind(". ")
            if split_at != -1:
                split_at += 1  # keep the period with the current chunk
        if split_at == -1:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = max_len  # no natural break found; hard cut

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


async def reply_long_text(message, text: str) -> None:
    """Send a (possibly long) reply to a Telegram message, split into
    multiple messages if needed."""
    for chunk in chunk_message(text):
        await message.reply_text(chunk)


async def send_long_message(bot, chat_id: str, text: str) -> None:
    """Same as reply_long_text but for proactive sends (briefings, alerts)
    where there's no originating message to reply to."""
    for chunk in chunk_message(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
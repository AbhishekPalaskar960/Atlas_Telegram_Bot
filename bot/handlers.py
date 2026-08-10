import logging
import os
import shutil
import tempfile

from sqlalchemy.orm import Session
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import Message, User
from services import document_service, llm_service, sheets_service, vision_service, voice_service
from utils.telegram_formatting import reply_long_text

logger = logging.getLogger(__name__)


def get_or_create_user(session: Session, tg_user) -> User:
    """Find the user by Telegram ID, or register them on first contact."""
    user = session.query(User).filter_by(telegram_id=str(tg_user.id)).first()
    if user is None:
        user = User(telegram_id=str(tg_user.id), name=tg_user.full_name)
        session.add(user)
        session.commit()
        logger.info("New user registered: %s", tg_user.full_name)
    return user


def save_message(session: Session, user: User, role: str, content: str) -> None:
    """Persist one message to the rolling conversation history."""
    session.add(Message(user_id=user.id, role=role, content=content))
    session.commit()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return
    session = get_session()
    try:
        user = get_or_create_user(session, message.from_user)
        logger.info("Text from %s: %s", user.name, message.text[:100])
        await message.reply_chat_action("typing")
        reply = llm_service.generate_reply(session, user, message.text)
        await reply_long_text(message, reply)
    finally:
        session.close()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.voice is None:
        return
    session = get_session()
    try:
        user = get_or_create_user(session, message.from_user)
        logger.info("Voice note from %s (%ss)", user.name, message.voice.duration)
        await message.reply_chat_action("typing")

        temp_dir = tempfile.mkdtemp(prefix="atlas_voice_")
        audio_path = os.path.join(temp_dir, "voice.ogg")
        try:
            file = await context.bot.get_file(message.voice.file_id)
            await file.download_to_drive(audio_path)
            text = voice_service.transcribe(audio_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not text:
            await message.reply_text(
                "I couldn't make out the audio — could you try again?"
            )
            return

        reply = llm_service.generate_reply(session, user, text)
        await reply_long_text(message, reply)
    except voice_service.TranscriptionUnavailable:
        logger.warning("Voice handling skipped for user %s: transcription not configured", user.name)
        await message.reply_text(
            "Voice transcription isn't set up yet — GROQ_API_KEY is missing in .env. "
            "Text messages work fine in the meantime!"
        )
    except Exception as exc:
        logger.error("Voice handling failed for user %s: %s", user.name, exc)
        await message.reply_text(
            "Sorry, I couldn't process that voice note. Transcription service issue?"
        )
    finally:
        session.close()


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle audio files (WAV, MP3, M4A, OGG, etc.) sent as audio messages.

    Telegram distinguishes between voice notes (filters.VOICE, always OGG) and
    audio files (filters.AUDIO, any format). This handler transcribes both the
    same way so users can send pre-recorded .wav/.mp3 files and get the same
    experience as recording in-app.
    """
    message = update.message
    if message is None or message.audio is None:
        return
    session = get_session()
    try:
        user = get_or_create_user(session, message.from_user)
        audio = message.audio
        filename = audio.file_name or "audio"
        logger.info("Audio file from %s: %s (%ss)", user.name, filename, audio.duration)
        await message.reply_chat_action("typing")

        temp_dir = tempfile.mkdtemp(prefix="atlas_audio_")
        # Keep original extension so voice_service picks the right format
        ext = os.path.splitext(filename)[1] or ".ogg"
        audio_path = os.path.join(temp_dir, f"audio{ext}")
        try:
            file = await context.bot.get_file(audio.file_id)
            await file.download_to_drive(audio_path)
            text = voice_service.transcribe(audio_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not text:
            await message.reply_text(
                "I couldn't make out the audio — could you try again or type your question?"
            )
            return

        # If the user attached a caption, prepend it as extra context
        caption = (message.caption or "").strip()
        prompt = f"{caption}\n\n[Voice transcription]: {text}" if caption else text
        reply = llm_service.generate_reply(session, user, prompt)
        await reply_long_text(message, reply)
    except voice_service.TranscriptionUnavailable:
        logger.warning("Audio handling skipped for user %s: transcription not configured", user.name)
        await message.reply_text(
            "Voice transcription isn't set up yet — GROQ_API_KEY is missing in .env."
        )
    except Exception as exc:
        logger.error("Audio handling failed for user %s: %s", user.name, exc)
        await message.reply_text("Sorry, I couldn't process that audio file.")
    finally:
        session.close()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.photo:
        return
    session = get_session()
    try:
        user = get_or_create_user(session, message.from_user)
        logger.info("Photo from %s", user.name)
        await message.reply_chat_action("typing")

        temp_dir = tempfile.mkdtemp(prefix="atlas_photo_")
        image_path = os.path.join(temp_dir, "image.jpg")
        try:
            file = await context.bot.get_file(message.photo[-1].file_id)
            await file.download_to_drive(image_path)
            description = vision_service.analyze_image(image_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if description != vision_service.VISION_MODEL_UNAVAILABLE:
            # Record both sides of the turn so future context is coherent:
            # the model should see it already answered about this photo,
            # not that the user somehow said the description themselves.
            save_message(session, user, "user", "[photo uploaded]")
            save_message(session, user, "assistant", description)
        await reply_long_text(message, description)
    except Exception as exc:
        logger.error("Photo handling failed for user %s: %s", user.name, exc)
        await message.reply_text("Sorry, I couldn't analyze that image.")
    finally:
        session.close()


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.document is None:
        return
    session = get_session()
    try:
        user = get_or_create_user(session, message.from_user)
        document = message.document
        filename = document.file_name or "document"

        if filename.lower().endswith((".csv", ".xlsx", ".xlsm")):
            await _handle_spreadsheet(session, context, user, message, document, filename)
            return

        if not filename.lower().endswith(".pdf"):
            await message.reply_text(
                "I can read PDF documents and CSV/XLSX spreadsheets right now. "
                "Try sending one of those file types."
            )
            return

        logger.info("PDF from %s: %s", user.name, filename)
        await message.reply_chat_action("typing")

        temp_dir = tempfile.mkdtemp(prefix="atlas_doc_")
        pdf_path = os.path.join(temp_dir, filename)
        try:
            file = await context.bot.get_file(document.file_id)
            await file.download_to_drive(pdf_path)
            text = document_service.extract_pdf_text(pdf_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not text or len(text) < 50:
            await message.reply_text(
                "I opened the PDF, but it looks like a scanned/image-only file — "
                "there's no text layer to read. If it's a chart or table, send it "
                "as a photo instead."
            )
            return

        document_service.save_document(session, user, filename, text)
        save_message(session, user, "user", f"[document uploaded: {filename}]")
        caption = (message.caption or "").strip()
        user_prompt = (
            caption
            if caption
            else (
                f"I just uploaded the document '{filename}'. Give me a quick summary "
                "of what it contains and the key numbers, in a few concise points."
            )
        )
        reply = llm_service.generate_reply(session, user, user_prompt)
        await reply_long_text(message, reply)
    except Exception as exc:
        logger.error("Document handling failed for user %s: %s", user.name, exc)
        await message.reply_text("Sorry, I couldn't process that document.")
    finally:
        session.close()


async def _handle_spreadsheet(session, context, user, message, document, filename: str) -> None:
    """Download a CSV/XLSX upload, flatten it, store it, and summarize via LLM."""
    logger.info("Spreadsheet from %s: %s", user.name, filename)
    await message.reply_chat_action("typing")

    temp_dir = tempfile.mkdtemp(prefix="atlas_sheet_")
    sheet_path = os.path.join(temp_dir, filename)
    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(sheet_path)
        text = sheets_service.parse_spreadsheet(sheet_path, filename)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not text:
        await message.reply_text(
            "I couldn't read that spreadsheet. I support CSV and XLSX files (text-based "
            "rows in the first sheet). Try exporting your file as CSV."
        )
        return

    sheets_service.save_sheet(session, user, filename, text)
    save_message(session, user, "user", f"[spreadsheet uploaded: {filename}]")
    caption = (message.caption or "").strip()
    user_prompt = (
        caption
        if caption
        else (
            f"I just uploaded the spreadsheet '{filename}'. Summarize what it covers, "
            "highlight the key KPIs and any trends or unusual values you can spot, "
            "in a few concise points."
        )
    )
    reply = llm_service.generate_reply(session, user, user_prompt)
    await reply_long_text(message, reply)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error, exc_info=True)

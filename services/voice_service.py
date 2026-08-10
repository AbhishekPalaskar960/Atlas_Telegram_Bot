import logging
import os
import subprocess

import httpx
import imageio_ffmpeg

import config

logger = logging.getLogger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
REQUEST_TIMEOUT = 120


class TranscriptionUnavailable(Exception):
    """Raised when voice transcription can't run (e.g. no GROQ_API_KEY set).
    Transcription always goes through Groq's hosted Whisper regardless of
    LLM_PROVIDER, since it doesn't require a heavy local model — but that
    means it needs its own explicit check rather than failing deep inside
    an HTTP call with a confusing error."""


def _to_mp3(audio_path: str, mp3_path: str) -> None:
    """Convert Telegram's ogg/opus voice note to mp3 using bundled ffmpeg."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", mp3_path],
        check=True,
        capture_output=True,
    )


def transcribe(audio_path: str) -> str:
    """Transcribe a voice note via Groq's hosted whisper. Returns text."""
    if not config.GROQ_API_KEY:
        raise TranscriptionUnavailable(
            "GROQ_API_KEY is not set — voice transcription requires it "
            "regardless of LLM_PROVIDER."
        )

    mp3_path = audio_path + ".mp3"
    _to_mp3(audio_path, mp3_path)
    try:
        headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
        data = {"model": config.GROQ_TRANSCRIBE_MODEL}
        with open(mp3_path, "rb") as audio_file:
            files = {"file": (os.path.basename(mp3_path), audio_file, "audio/mpeg")}
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.post(
                    GROQ_TRANSCRIBE_URL, files=files, data=data, headers=headers
                )
                response.raise_for_status()
        return response.json()["text"].strip()
    finally:
        os.remove(mp3_path)

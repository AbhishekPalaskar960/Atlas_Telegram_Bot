import base64
import logging

import httpx

import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120

VISION_MODEL_UNAVAILABLE = (
    "I can't analyze images right now — no vision model is installed. "
    "If you'd like chart/photo analysis, run `ollama pull llama3.2-vision:latest` once, "
    "and I'll use it."
)


def _vision_model_available() -> bool:
    try:
        response = httpx.get(
            f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=3
        )
        response.raise_for_status()
        names = [m["name"].split(":")[0] for m in response.json().get("models", [])]
        return config.VISION_MODEL.split(":")[0] in names
    except (httpx.HTTPError, ValueError):
        return False


def analyze_image(image_path: str) -> str:
    """Describe a financial image using an optional local Ollama vision model."""
    if not _vision_model_available():
        return VISION_MODEL_UNAVAILABLE

    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode()

    payload = {
        "model": config.VISION_MODEL,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "This is a financial image (chart, report, or document) sent by "
                    "a user. Analyze what it shows, highlight key numbers and trends, "
                    "and note anything unusual. Keep it concise."
                ),
                "images": [encoded],
            }
        ],
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(
            f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/chat", json=payload
        )
        response.raise_for_status()
    return response.json()["message"]["content"].strip()
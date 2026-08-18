import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- LLM (Groq API by default, or local Ollama, or OpenRouter) ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# Hybrid mode: a cheap/fast provider runs the tool loop (no Groq quota),
# and Groq is called exactly once at the end to polish the final phrasing.
# Tool-loop provider can be "ollama" or "openrouter".
LLM_HYBRID_MODE = os.getenv("LLM_HYBRID_MODE", "false").lower() == "true"
LLM_HYBRID_TOOL_PROVIDER = os.getenv("LLM_HYBRID_TOOL_PROVIDER", "ollama")
GROQ_POLISH_MODEL = os.getenv("GROQ_POLISH_MODEL", "llama-3.1-8b-instant")

# Tool-calling rounds resend the full tool schema + history every round,
# which is the main token cost driving TPM 429s. Use a lighter/cheaper
# model for those rounds, and save GROQ_MODEL (e.g. 70b) for the final
# natural-language answer only. Defaults to the same model if unset, so
# this is a no-op until you actually set GROQ_TOOL_MODEL.
GROQ_TOOL_MODEL = os.getenv("GROQ_TOOL_MODEL", GROQ_MODEL)
GROQ_TRANSCRIBE_MODEL = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")

# --- OpenRouter (for hybrid tool loop) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-2-9b-it")
OPENROUTER_MAX_TOKENS = int(os.getenv("OPENROUTER_MAX_TOKENS", "1500"))

# --- Local LLM (used for ALL tool-calling rounds in hybrid mode when provider=ollama) ---
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.2:1b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("VISION_MODEL", "llama3.2-vision:latest")

# --- Financial data ---
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# --- SEC EDGAR (no API key needed, but SEC requires an identifying User-Agent) ---
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "Atlas Financial Assistant contact@example.com")

# --- Google (Gmail + Calendar) OAuth ---
# Create an "OAuth client ID" (type: Web application) in Google Cloud Console,
# add GOOGLE_OAUTH_REDIRECT_URI below to its "Authorized redirect URIs".
# Not fatal if unset — Gmail/Calendar tools just stay unavailable.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8765/oauth2callback"
)
GOOGLE_OAUTH_PORT = int(os.getenv("GOOGLE_OAUTH_PORT", "8765"))
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

# --- Scheduler ---
ALERT_CHECK_MINUTES = int(os.getenv("ALERT_CHECK_MINUTES", "3"))

# --- Database ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./assistant.db")

# --- Fail-fast validation ---
# Only the Telegram token is truly non-negotiable to boot the bot.
# LLM_PROVIDER decides which key/URL is required below.
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")

if LLM_PROVIDER == "groq":
    if not GROQ_API_KEY:
        print("[WARNING] GROQ_API_KEY missing — LLM responses will not work until set.")
elif LLM_PROVIDER == "ollama":
    if not OLLAMA_BASE_URL:
        raise RuntimeError("OLLAMA_BASE_URL missing in .env (needed when LLM_PROVIDER=ollama)")
elif LLM_PROVIDER == "openrouter":
    if not OPENROUTER_API_KEY:
        print("[WARNING] OPENROUTER_API_KEY missing — LLM responses will not work until set.")
else:
    raise RuntimeError(f"Unknown LLM_PROVIDER '{LLM_PROVIDER}'. Use 'groq', 'ollama', or 'openrouter'.")

if LLM_HYBRID_MODE:
    if LLM_HYBRID_TOOL_PROVIDER == "ollama":
        if not OLLAMA_BASE_URL:
            raise RuntimeError("LLM_HYBRID_MODE=true with ollama requires OLLAMA_BASE_URL")
    elif LLM_HYBRID_TOOL_PROVIDER == "openrouter":
        if not OPENROUTER_API_KEY:
            raise RuntimeError("LLM_HYBRID_MODE=true with openrouter requires OPENROUTER_API_KEY")
    else:
        raise RuntimeError(f"Unknown LLM_HYBRID_TOOL_PROVIDER '{LLM_HYBRID_TOOL_PROVIDER}'. Use 'ollama' or 'openrouter'.")

if not FINNHUB_API_KEY:
    # Not fatal — bot can still run without live financial data, but warn loudly.
    print("[WARNING] FINNHUB_API_KEY missing — financial data tools will not work until set.")

if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
    print("[WARNING] GOOGLE_CLIENT_ID/SECRET missing — Gmail/Calendar tools will not work until set.")
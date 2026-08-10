import json
import logging
import re
import subprocess
import time

import httpx
from sqlalchemy.orm import Session

import config
from models import Message, User
from prompts.system_prompt import SYSTEM_PROMPT
from services import calendar_service, gmail_service, google_auth_service, memory_service, sheets_service
from services.alert_service import ALERT_TOOLS, handle_alert_tool
from services.document_service import document_context_block
from services.financial_data import MAX_TOOL_ROUNDS, TOOLS, execute_tool
from services.rate_limiter import groq_limiter
from services.sec_service import get_recent_filings
from services.sheets_service import sheet_context_block

logger = logging.getLogger(__name__)

# Fallback parser for Groq models that emit function calls as text tags
# (e.g. <function=get_company_news>{"symbol":"NVDA"}).
FUNCTION_TAG_RE = re.compile(
    r"<function=(?P<name>\w+)>(?P<args>\{.*?\})</function>",
    re.DOTALL,
)


def extract_function_call(text: str) -> dict | None:
    """Return {"name", "arguments"} if text contains a <function=...> tag, else None."""
    match = FUNCTION_TAG_RE.search(text or "")
    if not match:
        return None
    name = match.group("name")
    try:
        arguments = json.loads(match.group("args"))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(arguments, dict):
        return None
    return {"name": name, "arguments": arguments}


def strip_function_tags(text: str) -> str:
    """Remove function-call tags from text before it ever reaches the user."""
    return FUNCTION_TAG_RE.sub("", text or "").strip()


_MD_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|\*\*|\*|__|_)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)

def strip_markdown(text: str) -> str:
    """Remove markdown emphasis/heading/code-fence syntax, keeping the text
    itself. Bullet '•' characters are left untouched."""
    if not text:
        return text
    cleaned = _MD_HEADING_RE.sub("", text)
    cleaned = _MD_INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _MD_BOLD_ITALIC_RE.sub("", cleaned)
    return cleaned.strip()


OLLAMA_CHAT_URL = f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_CHAT_URL = f"{config.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
MAX_HISTORY = 8           # recent messages included as LLM context (token budget)
REQUEST_TIMEOUT = 180     # seconds; bumped from 120 — on this hardware (GTX 1650,
                          # only 13/29 model layers fit in VRAM) generation runs at
                          # ~4 tokens/sec on the CPU-bound layers, so 120s wasn't
                          # enough headroom even with num_predict capping the length.
TEMPERATURE = 0.4         # finance answers: low randomness, fewer hallucinations
OLLAMA_MAX_TOKENS = 300   # caps the LOCAL DRAFT's length only. Lowered from 500:
                          # the draft's job is to gather facts via tool calls, not
                          # write the final essay — Groq's polish step (now fed the
                          # raw tool data directly, see _polish_with_groq) is what
                          # expands it into the detailed final answer the user
                          # sees. A smaller cap keeps per-round wall time bounded
                          # (~300 tokens * 0.246s/token ≈ 74s) without shrinking
                          # the detail level of what actually reaches the user.

# Tool-call rounds on the LOCAL Ollama loop only. Lowered from 4 -> 3: on this
# hardware each round costs ~70-90s (draft generation + tool exec), so 4 rounds
# could push a single reply past 5 minutes. 3 rounds is still enough for the
# common cases (quote+fundamentals+news, or two separate company lookups for
# a comparison) while keeping worst-case latency more predictable. Does NOT
# affect the Groq-only path (_groq_tool_loop), which imports its own
# MAX_TOOL_ROUNDS from financial_data.py.
OLLAMA_MAX_TOOL_ROUNDS = min(MAX_TOOL_ROUNDS, 3)

# OpenRouter tool-loop settings (used in hybrid mode when LLM_HYBRID_TOOL_PROVIDER=openrouter)
OPENROUTER_MAX_TOKENS = 300
OPENROUTER_MAX_TOOL_ROUNDS = min(MAX_TOOL_ROUNDS, 3)

# Comparison-query detection — bump tool-loop rounds when user explicitly
# asks to compare multiple companies. Weak local models (7B) often emit
# only ONE tool call per round, so N companies needs ~2*N rounds to gather
# quote + fundamentals + news for each, plus a final synthesis round.
_COMPARISON_SIGNAL_RE = re.compile(r"\bcompare\b|\bvs\.?\b|\bversus\b", re.IGNORECASE)


def _estimate_tool_rounds(user_text: str, base_rounds: int) -> int:
    if not user_text or not _COMPARISON_SIGNAL_RE.search(user_text):
        return base_rounds
    candidates = re.findall(r"\b[A-Z][A-Za-z.]{1,}\b", user_text)
    stopwords = {"I", "The", "Which", "What", "Compare", "Based", "One", "Has"}
    entities = {c for c in candidates if c not in stopwords}
    company_count = max(2, min(len(entities), 5))
    return max(base_rounds, min(company_count * 2 + 1, 8))

# Tool results are truncated per-call to keep the local model's context small
# (see _ollama_tool_loop / _groq_tool_loop). The FULL, untruncated results are
# separately kept in `tool_results` and handed to the Groq polish step, whose
# context window and quota can comfortably hold them — this is what lets
# polish add real detail instead of just rephrasing the draft.
TOOL_RESULT_TRUNCATE_FOR_DRAFT = 1500
TOOL_RESULT_TRUNCATE_FOR_POLISH = 4000

# Tool names that actually fetch external ground-truth data (as opposed to
# alert/utility tools). Used for the hallucination guardrail below: if every
# one of these that got called this turn came back as an ERROR, there is no
# real data behind the draft, so we must not let the draft (which may have
# invented numbers anyway, since a small local model doesn't always obey the
# "never fabricate" instruction under pressure) get "polished" into something
# that reads as confident and sourced.
CRITICAL_DATA_TOOLS = {
    "get_stock_quote",
    "search_company",
    "get_company_news",
    "get_company_fundamentals",
    "get_market_news",
    "get_earnings_calendar",
    "get_google_sheet",
    "get_sec_filings",
}

# Dummy tool the model must explicitly call when it decides no live data is
# needed for the current question (greetings, clarifying questions, general
# chit-chat, fully answered from history). If the model skips both real data
# tools AND this declaration, it's silently skipping grounding — the guardrail
# catches that as unverified.
NO_DATA_NEEDED_TOOL = {
    "type": "function",
    "function": {
        "name": "no_data_needed",
        "description": (
            "Call this as your first and only action if THIS message does not "
            "require any live/external data to answer correctly — e.g. greetings, "
            "clarifying questions, general conversation, or something already "
            "fully covered by the conversation history. Do NOT call this if your "
            "answer is about to state any specific number, date, or fact about a "
            "company, market, or filing — those always require a real data tool."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

LLM_OFFLINE_REPLY = (
    "My language model seems to be offline right now. "
    "Make sure the LLM provider is configured, then ask me again."
)

FOCUS_INSTRUCTION = (
    "The message below is the user's CURRENT question. Answer THAT question "
    "specifically — do not continue, extend, or blend in content from your "
    "own earlier replies in this conversation, even if the topics seem "
    "adjacent. If the current question is ambiguous or underspecified, ask "
    "ONE clarifying question per the system prompt's rules instead of "
    "producing a long generic answer."
)

PROFILE_EXTRACT_PROMPT = """You are a data extraction assistant. Read the conversation between a user and a financial assistant and extract the user's profile details. Respond with ONLY valid JSON and nothing else — no markdown, no explanation, no code fences.

{
  "role": "the user's role (e.g. investor, analyst, founder, student, finance professional) or null",
  "sectors": ["sectors or industries the user is interested in, empty array if unknown"],
  "watchlist": ["companies or tickers the user wants to track, empty array if unknown"],
  "briefing_time": "the user's preferred daily briefing time in 24h HH:MM format or null",
  "facts": ["any other useful long-term facts about the user, empty array if none"]
}

Use null for unknown strings and empty arrays for unknown lists. Never invent information that is not in the conversation."""


GOOGLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "connect_google",
            "description": (
                "Generate a Google sign-in link so the user can connect Gmail and "
                "Calendar. Use this when the user wants to connect their Google "
                "account, or when Gmail/Calendar tools return an error saying the "
                "account isn't connected yet. Never call gmail/calendar tools "
                "speculatively before the user has asked about email or their "
                "calendar."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_emails",
            "description": (
                "Search the user's connected Gmail inbox for recent emails. Use "
                "when the user asks to check, search, or summarize their email "
                "(e.g. 'any replies from Acme?', 'summarize emails about this "
                "acquisition'). Requires Gmail to be connected first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional Gmail search query, e.g. 'from:acme.com', "
                            "'subject:earnings', 'newer_than:3d'. Leave empty for "
                            "the most recent emails."
                        ),
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": (
                "Get the user's upcoming Google Calendar events. Use for meeting "
                "prep, 'what's on my calendar', or scheduling context. Requires "
                "Calendar to be connected first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "How many days ahead to look (default 7).",
                    }
                },
                "required": [],
            },
        },
    },
]

SEC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_sec_filings",
            "description": (
                "Get the most recent SEC filings (10-K, 10-Q, 8-K, etc.) for a "
                "US-listed ticker, with links. Use when the user asks about "
                "regulatory filings, annual/quarterly reports, or 'any new SEC "
                "filings' for a company."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "US stock ticker symbol"}
                },
                "required": ["symbol"],
            },
        },
    },
]


def _profile_block(user: User) -> str:
    """Compile what we know about the user into a compact context block."""
    parts = []
    if user.role:
        parts.append(f"Role: {user.role}")
    if user.sectors:
        parts.append(f"Sectors of interest: {user.sectors}")
    if user.watchlist:
        parts.append(f"Watchlist: {user.watchlist}")
    if user.briefing_time:
        parts.append(f"Daily briefing at {user.briefing_time} ({user.briefing_timezone})")
    if not parts:
        return ""
    return "User profile:\n" + "\n".join(parts)


def _recent_history(session: Session, user: User) -> list[Message]:
    """Most recent messages, oldest to newest, for natural context."""
    rows = (
        session.query(Message)
        .filter(Message.user_id == user.id)
        .order_by(Message.created_at.desc())
        .limit(MAX_HISTORY)
        .all()
    )
    return list(reversed(rows))


def _dedupe_roles(messages: list[dict]) -> list[dict]:
    """Collapse consecutive same-role turns (Groq/Ollama reject them).

    When the LLM call fails, the assistant turn never gets saved, so the next
    user message stacks as another consecutive 'user' role. Keep only the most
    recent of each run so the payload strictly alternates roles."""
    if not messages:
        return messages
    out = [messages[0]]
    for msg in messages[1:]:
        if out[-1]["role"] == msg["role"]:
            out[-1] = msg
        else:
            out.append(msg)
    return out


def _build_messages(session: Session, user: User, user_text: str) -> list[dict]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    profile = _profile_block(user)
    if profile:
        messages.append({"role": "system", "content": profile})

    if not user.onboarded:
        known = [label for label, value in (
            ("role", user.role),
            ("sectors", user.sectors),
            ("watchlist", user.watchlist),
            ("briefing_time", user.briefing_time),
        ) if value]
        status = (
            "This user is new. Learn their role, sectors or stocks to watch, and "
            "preferred daily briefing time through natural conversation — one or two "
            "questions at a time, and let them skip anything. "
            f"So far collected: {', '.join(known) if known else 'nothing yet'}."
        )
        messages.append({"role": "system", "content": status})

    facts = memory_service.get_facts(session, user)
    if facts:
        fact_block = "Long-term facts I know about this user:\n- " + "\n- ".join(facts)
        messages.append({"role": "system", "content": fact_block})

    doc_block = document_context_block(session, user)
    if doc_block:
        messages.append({"role": "system", "content": doc_block})

    sheet_block = sheet_context_block(session, user)
    if sheet_block:
        messages.append({"role": "system", "content": sheet_block})

    for msg in _recent_history(session, user):
        messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "system", "content": FOCUS_INSTRUCTION})
    messages.append({"role": "user", "content": user_text})
    return _dedupe_roles(messages)


def _ollama_complete(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": OLLAMA_MAX_TOKENS,  # bounds generation length — see
                                                # OLLAMA_MAX_TOKENS comment above
            "num_ctx": 8192,  # ensure prompt and tool results aren't truncated
        },
    }
    if tools:
        payload["tools"] = tools

    start = time.monotonic()
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(OLLAMA_CHAT_URL, json=payload)
        response.raise_for_status()
        data = response.json()
    elapsed = time.monotonic() - start
    logger.warning("Ollama call took %.1fs (model=%s)", elapsed, config.LLM_MODEL)

    try:
        ps_output = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        logger.warning("ollama ps right after call:\n%s", ps_output)
    except Exception as exc:
        logger.warning("Could not run 'ollama ps': %s", exc)

    return data["message"]


def _openrouter_complete(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Call OpenRouter API (OpenAI-compatible) for chat completion."""
    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": OPENROUTER_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/AbhishekPalaskar960/Atlas_Telegram_Bot",
        "X-Title": "Atlas Financial Assistant",
    }

    start = time.monotonic()
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(OPENROUTER_CHAT_URL, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    elapsed = time.monotonic() - start
    logger.warning("OpenRouter call took %.1fs (model=%s)", elapsed, config.OPENROUTER_MODEL)

    return data["choices"][0]["message"]


_last_groq_call_ts = 0.0
_MIN_GROQ_INTERVAL = 2.2   # seconds between calls — proactively avoids most 429s
_MAX_GROQ_RETRIES = 4


def _groq_post(payload: dict) -> dict:
    """POST to Groq with proactive throttling + 429 retry (honoring Retry-After).

    Free-tier Groq rate limits are easy to hit once tool-calling makes several
    calls per user message, so this both spaces calls out ahead of time and
    backs off correctly when a 429 does slip through."""
    global _last_groq_call_ts
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    response = None
    for attempt in range(_MAX_GROQ_RETRIES):
        elapsed = time.monotonic() - _last_groq_call_ts
        if elapsed < _MIN_GROQ_INTERVAL:
            time.sleep(_MIN_GROQ_INTERVAL - elapsed)

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(GROQ_CHAT_URL, json=payload, headers=headers)
        _last_groq_call_ts = time.monotonic()

        if response.status_code != 429:
            response.raise_for_status()
            return response.json()

        _log_groq_rate_limit_headers(response)
        retry_after = response.headers.get("retry-after")
        raw_wait = float(retry_after) if retry_after else min(2 ** attempt * 2, 20)
        # Cap at 30s — if Groq wants us to wait longer (e.g. 1500s for a daily
        # limit), fail fast rather than blocking the bot for 25+ minutes.
        wait = min(raw_wait, 30)
        if raw_wait > 30:
            logger.warning(
                "Groq retry-after %ss exceeds 30s cap — failing fast (quota likely exhausted).",
                raw_wait,
            )
            break  # stop retrying; raise the 429 below
        logger.warning(
            "Groq rate-limited (429), retrying in %.0fs (attempt %d/%d)",
            wait,
            attempt + 1,
            _MAX_GROQ_RETRIES,
        )
        time.sleep(wait)

    response.raise_for_status()  # exhausted retries — surface the 429 to the caller
    return response.json()


def _log_groq_rate_limit_headers(response) -> None:
    h = response.headers
    logger.warning(
        "Groq 429 details | requests: %s/%s remaining | tokens: %s/%s remaining | retry-after: %s",
        h.get("x-ratelimit-remaining-requests"),
        h.get("x-ratelimit-limit-requests"),
        h.get("x-ratelimit-remaining-tokens"),
        h.get("x-ratelimit-limit-tokens"),
        h.get("retry-after"),
    )


def _groq_complete(
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str | None = None,
) -> dict:
    # llama models occasionally emit malformed tool-call syntax, which Groq
    # rejects with 400 tool_use_failed — retry those, but let _groq_post
    # handle rate limiting/backoff.
    payload = {
        "model": model or config.GROQ_MODEL,
        "messages": messages,
        "temperature": TEMPERATURE,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            data = _groq_post(payload)
            return data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400 or "tool_use_failed" not in (exc.response.text or ""):
                raise
            last_error = exc
            logger.warning(
                "Groq tool call rejected (malformed), retry %d/2: %s",
                attempt + 1,
                (exc.response.text or "")[:200],
            )
    raise last_error if last_error is not None else RuntimeError("Groq call failed unexpectedly")


def _parse_tool_args(call: dict) -> dict:
    """Tolerate Groq/Ollama returning arguments as JSON string, dict, or null."""
    arguments = (call.get("function") or {}).get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


ALL_TOOLS = [*TOOLS, *ALERT_TOOLS, *GOOGLE_TOOLS, *SEC_TOOLS]

# Groq free tier caps ~6000 tokens/min. Every call resends the full tool
# schemas, so only include the optional groups (Google/SEC) when the message
# plausibly needs them instead of paying for all four groups every round.
CORE_TOOLS = TOOLS + ALERT_TOOLS          # financial data + alerts: send always
OPTIONAL_TOOL_GROUPS = {
    "google": GOOGLE_TOOLS,
    "sec": SEC_TOOLS,
}


def _select_tools(user_text: str) -> list[dict]:
    """Only include optional tool schemas when the message plausibly needs them,
    instead of sending all four tool groups on every call regardless of relevance."""
    text = user_text.lower()
    tools = list(CORE_TOOLS)
    if any(kw in text for kw in ("email", "gmail", "inbox", "calendar", "meeting", "schedule", "connect google")):
        tools += OPTIONAL_TOOL_GROUPS["google"]
    if any(kw in text for kw in ("sec filing", "10-k", "10-q", "8-k", "filing", "annual report", "quarterly report")):
        tools += OPTIONAL_TOOL_GROUPS["sec"]
    return tools


# Keywords that signal the question needs a live tool call even when a file is
# already in context. If ANY of these appear, we route through the normal
# Ollama/Groq tool loop instead of the file-context fast path.
_LIVE_DATA_KEYWORDS = (
    "price", "quote", "current price", "stock price", "today",
    "latest", "right now", "live", "real-time", "real time",
    "alert", "notify", "notification", "remind",
    "news", "announcement", "headline",
    "earnings", "report date", "when does", "when is",
    "email", "gmail", "inbox", "calendar", "meeting",
    "sec filing", "10-k", "10-q", "8-k", "filing",
    "remember to alert", "remember to notify", "save this", "add to my", "update my",
    "search", "look up", "check", "fetch", "get me",
    "briefing", "watchlist update", "portfolio update",
)


def _needs_live_data(user_text: str) -> bool:
    """Return True if the user's message likely requires a real-time tool call.
    Used to prevent the file-context fast path from intercepting questions that
    need stock quotes, alerts, news, etc."""
    text = user_text.lower()
    return any(kw in text for kw in _LIVE_DATA_KEYWORDS)


_CONVERSATIONAL_SIGNALS = (
    "i mainly", "i follow", "i like", "i prefer", "i am", "i'm", "my name",
    "hello", "hi ", "hey ", "thanks", "thank you", "ok", "okay", "sure",
    "yes", "no", "got it", "sounds good", "great", "nice",
    "what can you", "who are you", "what are you",
)


def _is_conversational(user_text: str) -> bool:
    """Return True when the message is short/casual and unlikely to need tool
    data or heavy file analysis — route these straight to Groq for speed."""
    text = user_text.lower().strip()
    # Explicit conversational openers
    if any(text.startswith(sig) or sig in text for sig in _CONVERSATIONAL_SIGNALS):
        return True
    # Short messages with no analysis keywords are usually conversational
    analysis_kw = ("analyze", "analysis", "compare", "comparison", "explain",
                   "fundamentals", "breakdown", "extract", "summarize", "summary")
    if len(text) < 120 and not any(kw in text for kw in analysis_kw):
        return True
    return False


def _execute_tool(session: Session, user: User, name: str, args: dict) -> str:
    """Run a tool, and persist Google Sheets data so follow-up questions work."""
    if name in ("create_alert", "list_alerts", "delete_alert"):
        return handle_alert_tool(session, user, name, args)

    if name == "connect_google":
        if google_auth_service.is_connected(user):
            return "Google is already connected for this user."
        if not google_auth_service.google_configured():
            return "ERROR: Google integration isn't configured on this bot yet."
        url = google_auth_service.build_auth_url(session, user)
        return (
            "Send the user this link and tell them to open it and sign in to "
            f"connect Gmail and Calendar: {url}"
        )

    if name == "get_recent_emails":
        return gmail_service.search_recent_emails(session, user, str(args.get("query") or ""))

    if name == "get_calendar_events":
        days_ahead = args.get("days_ahead") or 7
        try:
            days_ahead = int(days_ahead)
        except (TypeError, ValueError):
            days_ahead = 7
        return calendar_service.get_upcoming_events(session, user, days_ahead)

    if name == "get_sec_filings":
        return get_recent_filings(str(args.get("symbol") or ""))

    result = execute_tool(name, args)
    if name == "get_google_sheet" and not result.startswith("ERROR"):
        source = str(args.get("url") or "Google Sheet")
        sheets_service.save_sheet(session, user, source, result)
    return result


def _groq_tool_loop(session: Session, user: User, messages: list[dict], user_text: str = "") -> str:
    """Chat loop for Groq: execute requested tools until a final answer arrives.

    Handles BOTH native OpenAI-style tool_calls AND text-tag function calls
    (e.g. <function=get_company_news>{"symbol":"NVDA"}</function>) that some
    Groq-hosted models emit in message content instead of populating the
    native field. Raw tags must never reach the user — they're always parsed
    and executed, and any leftovers are stripped from the final answer.

    Only the tool schemas relevant to the message are sent (see _select_tools),
    and large tool results are truncated to hold the per-call token budget.
    """
    tool_model = config.GROQ_TOOL_MODEL
    # No user_text (e.g. briefing path) -> full toolset; otherwise only the
    # schemas relevant to the message, to keep the token budget low.
    tools_for_call = list(ALL_TOOLS) if not user_text else _select_tools(user_text)

    for _ in range(MAX_TOOL_ROUNDS):
        message = _groq_complete(messages, tools=tools_for_call, model=tool_model)
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            call = extract_function_call(content)
            if call is None:
                return _final_answer(messages, message)
            messages.append({"role": "assistant", "content": strip_function_tags(content)})
            name = call["name"]
            args = call["arguments"]
            result = _execute_tool(session, user, name, args)
            logger.info("Text-tag tool call executed: %s(%s)", name, args)
            if len(result) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                result = result[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The result of {name}({args}) was:\n{result}\n\n"
                        "Answer the user's question using this data. If the data "
                        "is an error or unavailable, say so plainly."
                    ),
                }
            )
            continue

        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            args = _parse_tool_args(call)
            result = _execute_tool(session, user, name, args)
            if len(result) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                result = result[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": result}
            )
    return strip_function_tags(messages[-1].get("content") or "") or LLM_OFFLINE_REPLY


def _final_answer(messages: list[dict], message: dict) -> str:
    """Produce the final natural-language answer.

    When GROQ_TOOL_MODEL differs from GROQ_MODEL, the tool rounds ran on the
    cheap model; hand the completed context to the full model (no tools) for
    a higher-quality final answer. No-op (just returns the draft) when both
    models are the same.
    """
    draft = strip_function_tags(message.get("content") or "")
    if config.GROQ_TOOL_MODEL == config.GROQ_MODEL or not draft:
        return draft or LLM_OFFLINE_REPLY
    messages.append({"role": "assistant", "content": draft})
    final = _groq_complete(messages, tools=None, model=config.GROQ_MODEL)
    return strip_function_tags(final.get("content") or "") or draft


def _ollama_tool_loop(
    session: Session, user: User, messages: list[dict], user_text: str = ""
) -> tuple[str, list[dict], bool]:
    """Chat loop for Ollama: execute requested tools until a final answer arrives.

    Mirrors the Groq loop: handles native tool_calls AND text-tag function
    calls, only sends the schemas relevant to the message, and truncates
    large tool results to hold the DRAFT model's context budget.

    Returns (draft_text, tool_results, no_data_declared). tool_results is a list of
    {"name", "args", "result"} dicts — one per REAL tool call made during the
    loop, with the FULL, untruncated result — so callers can (a) hand real
    data to the polish step instead of just the local draft, and (b) detect
    whether the underlying data actually came back OK before trusting
    anything the draft claims (see _draft_is_unverified / _call_llm).
    no_data_declared is True iff the model explicitly called the dummy
    `no_data_needed` tool, signalling that no live data is required for
    this question. If neither real tools ran nor this flag is set, the
    model silently skipped grounding — the guardrail treats that as unverified.

    NOTE: this appends Ollama-shaped assistant/tool messages (no OpenAI
    `tool_call_id` field) directly into the `messages` list passed in. Do NOT
    forward this same list to Groq afterwards (e.g. for polishing) without a
    clean snapshot taken BEFORE calling this function — Groq's API rejects
    `tool` role messages that don't carry a valid `tool_call_id`, which is
    what caused the 400 Bad Request on the polish step. See `_call_llm`.
    """
    base_tools = list(ALL_TOOLS) if not user_text else _select_tools(user_text)
    tools_for_call = [*base_tools, NO_DATA_NEEDED_TOOL]
    tool_results: list[dict] = []
    no_data_declared = False

    rounds = _estimate_tool_rounds(user_text, OLLAMA_MAX_TOOL_ROUNDS) + 1
    for _ in range(rounds):
        message = _ollama_complete(messages, tools=tools_for_call)
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            call = extract_function_call(content)
            if call is None:
                return strip_function_tags(content) or LLM_OFFLINE_REPLY, tool_results, no_data_declared
            messages.append({"role": "assistant", "content": strip_function_tags(content)})
            name = call["name"]
            args = call["arguments"]
            if name == "no_data_needed":
                no_data_declared = True
                continue
            result = _execute_tool(session, user, name, args)
            logger.info("Ollama text-tag tool call executed: %s(%s)", name, args)
            tool_results.append({"name": name, "args": args, "result": result})
            truncated = result
            if len(truncated) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                truncated = truncated[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The result of {name}({args}) was:\n{truncated}\n\n"
                        "Answer the user's question using this data. If the data "
                        "is an error or unavailable, say so plainly."
                    ),
                }
            )
            continue

        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            if name == "no_data_needed":
                no_data_declared = True
                continue
            args = _parse_tool_args(call)
            result = _execute_tool(session, user, name, args)
            tool_results.append({"name": name, "args": args, "result": result})
            truncated = result
            if len(truncated) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                truncated = truncated[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append({"role": "tool", "name": name, "content": truncated})

    draft = strip_function_tags(messages[-1].get("content") or "") or LLM_OFFLINE_REPLY
    return draft, tool_results, no_data_declared


def _openrouter_tool_loop(
    session: Session, user: User, messages: list[dict], user_text: str = ""
) -> tuple[str, list[dict], bool]:
    """Chat loop for OpenRouter: execute requested tools until a final answer arrives.

    Mirrors the Ollama loop: handles native tool_calls AND text-tag function
    calls, only sends the schemas relevant to the message, and truncates
    large tool results to hold the DRAFT model's context budget.

    Returns (draft_text, tool_results, no_data_declared). tool_results is a list of
    {"name", "args", "result"} dicts — one per REAL tool call made during the
    loop, with the FULL, untruncated result — so callers can (a) hand real
    data to the polish step instead of just the local draft, and (b) detect
    whether the underlying data actually came back OK before trusting
    anything the draft claims (see _draft_is_unverified / _call_llm).
    no_data_declared is True iff the model explicitly called the dummy
    `no_data_needed` tool, signalling that no live data is required for
    this question. If neither real tools ran nor this flag is set, the
    model silently skipped grounding — the guardrail treats that as unverified.

    NOTE: OpenRouter uses OpenAI-compatible tool calling format with tool_call_id,
    so the resulting messages CAN be forwarded to Groq for polishing without
    the 400 Bad Request issue that Ollama has.
    """
    base_tools = list(ALL_TOOLS) if not user_text else _select_tools(user_text)
    tools_for_call = [*base_tools, NO_DATA_NEEDED_TOOL]
    tool_results: list[dict] = []
    no_data_declared = False

    rounds = _estimate_tool_rounds(user_text, OPENROUTER_MAX_TOOL_ROUNDS) + 1
    for _ in range(rounds):
        message = _openrouter_complete(messages, tools=tools_for_call)
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            call = extract_function_call(content)
            if call is None:
                return strip_function_tags(content) or LLM_OFFLINE_REPLY, tool_results, no_data_declared
            messages.append({"role": "assistant", "content": strip_function_tags(content)})
            name = call["name"]
            args = call["arguments"]
            if name == "no_data_needed":
                no_data_declared = True
                continue
            result = _execute_tool(session, user, name, args)
            logger.info("OpenRouter text-tag tool call executed: %s(%s)", name, args)
            tool_results.append({"name": name, "args": args, "result": result})
            truncated = result
            if len(truncated) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                truncated = truncated[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The result of {name}({args}) was:\n{truncated}\n\n"
                        "Answer the user's question using this data. If the data "
                        "is an error or unavailable, say so plainly."
                    ),
                }
            )
            continue

        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            if name == "no_data_needed":
                no_data_declared = True
                continue
            args = _parse_tool_args(call)
            result = _execute_tool(session, user, name, args)
            tool_results.append({"name": name, "args": args, "result": result})
            truncated = result
            if len(truncated) > TOOL_RESULT_TRUNCATE_FOR_DRAFT:
                truncated = truncated[:TOOL_RESULT_TRUNCATE_FOR_DRAFT] + "... [truncated]"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": truncated})

    draft = strip_function_tags(messages[-1].get("content") or "") or LLM_OFFLINE_REPLY
    return draft, tool_results, no_data_declared


def _is_error_result(result: str) -> bool:
    return str(result).strip().upper().startswith("ERROR")


def _all_critical_tools_failed(tool_results: list[dict]) -> bool:
    """True when at least one CRITICAL_DATA_TOOLS call was made this turn and
    every single one of them came back as an ERROR — i.e. there is no real
    ground-truth data behind whatever the draft said. In that case the draft
    cannot be trusted (a small local model doesn't reliably follow "don't
    fabricate" once it fails a tool call) and must not be polished into a
    confident-sounding answer."""
    critical_calls = [r for r in tool_results if r["name"] in CRITICAL_DATA_TOOLS]
    if not critical_calls:
        return False
    return all(_is_error_result(r["result"]) for r in critical_calls)


def _draft_is_unverified(tool_results: list[dict], no_data_declared: bool) -> bool:
    """True when neither a real data tool ran nor did the model explicitly
    declare that no data was needed — i.e. it silently skipped grounding."""
    if tool_results:
        return _all_critical_tools_failed(tool_results)
    return not no_data_declared


def _data_unavailable_reply(tool_results: list[dict]) -> str:
    """Honest, code-generated reply used when every critical data tool call
    failed — bypasses the LLM entirely for this response so there's no
    chance of it papering over the failure with invented numbers."""
    failed = [r for r in tool_results if r["name"] in CRITICAL_DATA_TOOLS and _is_error_result(r["result"])]
    symbols = sorted({str(r["args"].get("symbol") or r["args"].get("query") or "").strip() for r in failed} - {""})
    target = symbols[0] if symbols else "that"
    return (
        f"I couldn't pull real data for {target} right now — the data source "
        "returned an error (wrong/unlisted symbol, or the provider doesn't "
        "cover it). I don't want to guess at numbers, so let's confirm the "
        "exact ticker/exchange, or try again in a bit."
    )


def _format_tool_result_for_polish(entry: dict) -> str:
    """Full-length (only lightly capped) tool result for the Groq polish step,
    separate from the tighter TOOL_RESULT_TRUNCATE_FOR_DRAFT cap used to keep
    the local model's own context small during the tool loop."""
    result = entry["result"]
    if len(result) > TOOL_RESULT_TRUNCATE_FOR_POLISH:
        result = result[:TOOL_RESULT_TRUNCATE_FOR_POLISH] + "... [truncated]"
    return f"{entry['name']}({entry['args']}):\n{result}"


GROUNDING_INSTRUCTION_BASE = """Grounding rules for this reply — follow strictly:
- Use ONLY the "Raw data gathered by tool calls" block below (if present) and
  the conversation itself as your source of facts. Do not add any number,
  date, financial figure, or claim from your own general knowledge, even if
  you believe it to be true — your training data may be stale or wrong, and
  the user needs to trust that everything you say is live and verified.
- If the raw data block is missing, empty, or contains an ERROR for
  something the user asked about, say plainly that you don't have that data
  right now — do not fill the gap from memory, and do not invent a plausible
  approximation.
- The draft below is only a rough outline of what to cover; treat any figure
  in the draft as unverified unless it also appears in the raw data block.
- Do NOT use markdown (**, *, `, #, etc.) — use plain text only. Use ALL CAPS
  for any section title you do include (Telegram has no bold rendering).
  NEVER include generic financial disclaimers like "consult a financial advisor"."""

# Only forced onto messages that are genuinely a fundamentals deep-dive or a
# multi-company comparison (detected below from which tools actually ran).
# Forcing this onto every reply — including plain "what's the price of X"
# lookups — was making the model pad out sections it had no data for with
# lines like "no financial strength metrics are provided in the current
# data", which is exactly the noisy, template-stuffed output the product
# spec explicitly warns against.
GROUNDING_INSTRUCTION_DEEP_DIVE = """
- **THIS IS THE FINAL INSTRUCTION — IT OVERRIDES ALL PRIOR INSTRUCTIONS.**
  This question is a fundamentals deep-dive or a multi-company comparison, so
  follow the section structure EXACTLY as defined in your system prompt
  (Business overview / Financial strength / Growth outlook / Competitive
  position / Profitability / Key risks / Overall takeaway for fundamentals;
  metric-by-metric for comparisons) — but SKIP any section the raw data
  block has nothing for rather than writing a line saying it's missing.
  Do NOT compress into long paragraphs.
  ALWAYS use ALL CAPS for all section titles and bullet metric labels (e.g. "COMPARISON POINTS:", "MARKET CAP:", "VALUATION:", "GROWTH RATES:", "BUSINESS OVERVIEW:").
  ALWAYS include concrete numbers/percentages for metrics rather than qualitative statements."""

GROUNDING_INSTRUCTION_SIMPLE = """
- This is a simple, specific question (e.g. a quote, a single fact, a quick
  lookup) — answer it directly in 1-4 short lines. Do NOT produce a
  multi-section analyst brief, and do NOT mention or list metrics/sections
  that weren't asked for or that the raw data doesn't contain."""


def _needs_deep_dive_template(tool_results: list[dict] | None) -> bool:
    """True only when a fundamentals or comparison-shaped tool call actually
    ran this turn — i.e. the cases the system prompt's own template rules
    (## Fundamental analysis structure / ## Comparing two or more companies)
    are meant for. A plain get_stock_quote lookup should never trigger it."""
    if not tool_results:
        return False
    names = [r.get("name") for r in tool_results]
    if names.count("get_stock_quote") + names.count("get_company_fundamentals") + names.count("get_company_news") >= 4:
        return True  # multiple companies' worth of calls -> comparison
    return "get_company_fundamentals" in names


def _polish_with_groq(messages: list[dict], draft: str, tool_results: list[dict] | None = None) -> str:
    """Hybrid mode: rephrase/expand the local model's draft with Groq, no tools.

    One call per message; uses GROQ_POLISH_MODEL. Falls back to the draft
    verbatim if Groq is unavailable or returns nothing useful.

    IMPORTANT: `messages` here must be a CLEAN conversation (system + prior
    turns + the current user message) with no Ollama-style tool_call/tool
    messages mixed in — Groq's API will 400 on those (missing tool_call_id).
    The caller (`_call_llm`) is responsible for passing a clean snapshot,
    not the same list `_ollama_tool_loop` mutated with tool-call debris.

    `tool_results`, when provided, is the FULL raw output of every tool call
    the local draft made (see _ollama_tool_loop). A GROUNDING_INSTRUCTION is
    always sent alongside it (even when there's no data, or the call failed)
    so Groq is explicitly told not to patch gaps with its own training
    knowledge — this is the main defense against hallucinated financials.
    """
    if not draft:
        return draft or LLM_OFFLINE_REPLY
    try:
        data_block = (
            "\n\n".join(_format_tool_result_for_polish(r) for r in tool_results)
            if tool_results
            else "(no tool data was gathered for this turn)"
        )
        grounding_instruction = GROUNDING_INSTRUCTION_BASE + (
            GROUNDING_INSTRUCTION_DEEP_DIVE
            if _needs_deep_dive_template(tool_results)
            else GROUNDING_INSTRUCTION_SIMPLE
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    grounding_instruction
                    + "\n\nRaw data gathered by tool calls for this question:\n\n"
                    + data_block
                    + "\n\nDraft answer (may be incomplete):\n"
                    + draft
                    + "\n\nPlease rewrite and expand this draft into the final polished answer, strictly following the formatting rules."
                ),
            }
        )
        final = _groq_complete(messages, tools=None, model=config.GROQ_POLISH_MODEL)
        polished = strip_function_tags(final.get("content") or "")
        return polished or draft
    except Exception as exc:
        logger.warning("Groq polish failed, using local draft: %s", exc)
        return draft


def _call_llm(session: Session, user: User, messages: list[dict], user_text: str = "") -> str:
    """Main conversation path: chat + financial tool calling.

    Hybrid mode: local Ollama runs the tool loop (cheap, no Groq quota),
    then Groq polishes the final phrasing once (no tools) — fed the raw tool
    data too, not just the local draft (see _polish_with_groq).

    Hallucination guardrail: the model MUST either (a) call at least one real
    data tool that succeeds, or (b) explicitly declare `no_data_needed` when
    no live data is required. If it does neither — i.e. silently skips tools
    and produces a draft anyway — we bypass the LLM entirely and return a
    plain "data unavailable" message. This exists because a small local model
    does not reliably follow "don't fabricate numbers" when it fails to call
    tools — it's safer to not give it (or Groq afterwards) the chance to
    paper over the failure.

    FIX: `_ollama_tool_loop` appends Ollama-shaped tool-call/tool messages
    (no `tool_call_id`) into whatever `messages` list it's given. If that
    same, now-polluted list were handed to Groq for polishing, Groq's API
    rejects it with 400 Bad Request because its `tool` role messages don't
    carry a valid `tool_call_id` matching a prior assistant tool_calls entry.
    Taking a snapshot (`clean_messages = list(messages)`) BEFORE the tool
    loop runs means Groq only ever sees the original conversation + the raw
    tool data + the final draft appended in `_polish_with_groq` — no Ollama
    tool debris.
    """
    if config.LLM_HYBRID_MODE:
        has_file_context = any(
            m.get("role") == "system"
            and ("[Document: " in m.get("content", "") or "[Spreadsheet: " in m.get("content", ""))
            for m in messages
        )
        needs_live = _needs_live_data(user_text)
        is_conv = _is_conversational(user_text)

        # --- Fast Groq path (no tool loop, no tools) ---
        # Used when: (a) file data is already in context, OR (b) message is short/
        # conversational — as long as no live data tool is needed.
        # Falls back to the hybrid path if Groq fails (e.g. 429 quota exhausted).
        if (has_file_context or is_conv) and not needs_live:
            logger.info(
                "Fast Groq path for user %s (file_ctx=%s, conversational=%s).",
                user.id, has_file_context, is_conv,
            )
            try:
                fast_messages = list(messages) + [
                    {
                        "role": "system",
                        "content": (
                            "IMPORTANT: The full text of every uploaded document or spreadsheet is "
                            "already embedded in the system messages above (look for blocks starting "
                            "with [Document: ...] or [Spreadsheet: ...]). You have complete access to "
                            "all the content. Do NOT say you cannot read or access the file. "
                            "Answer the user's question using ONLY that embedded content (if relevant). "
                            "Do NOT use markdown (**, *, #). "
                            "Do NOT write intro/preamble sentences like 'Here are...' or 'The following...'. "
                            "Use ALL CAPS for every section/metric header (e.g. REVENUE GROWTH:, P/E RATIO:). "
                            "When listing values for multiple companies under a metric, put EACH company "
                            "on its OWN separate bullet line (e.g. '\u2022 NVDA: 70.7%' then newline '\u2022 MSFT: 17.8%'), "
                            "NOT all companies crammed into a single comma-separated line. "
                            "Include concrete numbers and percentages where applicable. "
                            "Never say 'consult a financial advisor'. Answer the question directly and concisely."
                        ),
                    }
                ]
                reply = _groq_complete(fast_messages, tools=None, model=config.GROQ_MODEL)
                return strip_markdown(strip_function_tags(reply.get("content") or "")) or LLM_OFFLINE_REPLY
            except Exception as exc:
                logger.warning(
                    "Fast Groq path failed for user %s (%s), falling back to hybrid tool loop.", user.id, exc
                )
                # Fall through to the normal hybrid path below.

        # --- Normal hybrid path: tool loop (Ollama or OpenRouter) → Groq polish ---
        # Used when live data is needed (stock price, alert, news, etc.)
        # or the fast Groq path failed.
        # Automatic fallback: if tool provider is unreachable, fall back to Groq tool loop.
        tool_provider = config.LLM_HYBRID_TOOL_PROVIDER
        try:
            clean_messages = list(messages)  # snapshot BEFORE the tool loop mutates `messages`
            if tool_provider == "openrouter":
                draft, tool_results, no_data_declared = _openrouter_tool_loop(session, user, messages, user_text=user_text)
            else:  # ollama (default)
                draft, tool_results, no_data_declared = _ollama_tool_loop(session, user, messages, user_text=user_text)
            if _draft_is_unverified(tool_results, no_data_declared) and not has_file_context and not (is_conv and not needs_live):
                logger.warning(
                    "Draft unverified (no tool ran, no no_data_needed call) for user %s — "
                    "returning honest unavailable reply instead of polishing: %r",
                    user.id,
                    draft[:200],
                )
                return _data_unavailable_reply(tool_results)
            return _polish_with_groq(clean_messages, draft, tool_results=tool_results)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            logger.warning(
                "%s unreachable for user %s (%s) — falling back to Groq tool loop.",
                tool_provider.capitalize(), user.id, exc,
            )
            return _groq_tool_loop(session, user, messages, user_text=user_text)
    if config.LLM_PROVIDER == "groq":
        return _groq_tool_loop(session, user, messages, user_text=user_text)
    if config.LLM_PROVIDER == "openrouter":
        # Direct OpenRouter mode (non-hybrid): tool loop on OpenRouter, no Groq polish
        draft, tool_results, no_data_declared = _openrouter_tool_loop(session, user, messages, user_text=user_text)
        has_file_context = any(
            m.get("role") == "system"
            and ("[Document: " in m.get("content", "") or "[Spreadsheet: " in m.get("content", ""))
            for m in messages
        )
        needs_live = _needs_live_data(user_text)
        is_conv = _is_conversational(user_text)
        if _draft_is_unverified(tool_results, no_data_declared) and not has_file_context and not (is_conv and not needs_live):
            return _data_unavailable_reply(tool_results)
        return draft
    draft, tool_results, no_data_declared = _ollama_tool_loop(session, user, messages, user_text=user_text)
    has_file_context = any(
        m.get("role") == "system"
        and ("[Document: " in m.get("content", "") or "[Spreadsheet: " in m.get("content", ""))
        for m in messages
    )
    needs_live = _needs_live_data(user_text)
    is_conv = _is_conversational(user_text)
    if _draft_is_unverified(tool_results, no_data_declared) and not has_file_context and not (is_conv and not needs_live):
        return _data_unavailable_reply(tool_results)
    return draft


def _call_llm_plain(messages: list[dict]) -> str:
    """Structured/simple path (e.g. profile extraction) — no tools, raw content."""
    if config.LLM_PROVIDER == "groq":
        return _groq_complete(messages).get("content", "").strip()
    if config.LLM_PROVIDER == "openrouter":
        return _openrouter_complete(messages).get("content", "").strip()
    return _ollama_complete(messages).get("content", "").strip()


def _parse_json(raw: str) -> dict:
    """Extract a JSON object from a model reply, tolerating code fences."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def _extract_profile(session: Session, user: User) -> dict:
    """Ask the LLM to pull structured profile data from what the USER has said.
    Assistant messages are excluded so the model never stores its own guesses."""
    history = _recent_history(session, user)
    user_lines = [m.content for m in history if m.role == "user"][-6:]
    if not user_lines:
        return {}
    transcript = "\n".join(f"user: {line}" for line in user_lines)
    messages = [
        {"role": "system", "content": PROFILE_EXTRACT_PROMPT},
        {"role": "user", "content": transcript},
    ]
    return _parse_json(_call_llm_plain(messages))


def _apply_profile(session: Session, user: User, data: dict) -> bool:
    """Persist extracted profile fields + facts. Returns True if onboarding finished."""
    changed = False

    role = str(data.get("role") or "").strip()
    if role and not user.role:
        user.role = role[:100]
        changed = True

    sectors = [str(s).strip() for s in (data.get("sectors") or []) if str(s).strip()]
    if sectors and not user.sectors:
        user.sectors = ", ".join(sectors)
        changed = True

    watchlist = [str(s).strip() for s in (data.get("watchlist") or []) if str(s).strip()]
    if watchlist and not user.watchlist:
        user.watchlist = ", ".join(watchlist)
        changed = True

    briefing_time = str(data.get("briefing_time") or "").strip()
    if briefing_time and not user.briefing_time:
        user.briefing_time = briefing_time[:5]
        changed = True

    for fact in (data.get("facts") or []):
        fact_text = str(fact).strip()
        if fact_text:
            memory_service.remember(session, user, fact_text)
            changed = True

    if not user.onboarded:
        exchange_count = (
            session.query(Message)
            .filter(Message.user_id == user.id, Message.role == "user")
            .count()
        )
        has_role = bool(user.role)
        has_interests = bool(user.sectors or user.watchlist)
        if has_role and has_interests and (user.briefing_time or exchange_count >= 6):
            user.onboarded = True
            changed = True
            logger.info("Onboarding complete for user %s", user.id)

    if changed:
        session.commit()
    return changed


def generate_reply(session: Session, user: User, user_text: str) -> str:
    """Run the full conversation loop: context -> LLM -> save both sides -> reply."""
    messages = _build_messages(session, user, user_text)
    session.add(Message(user_id=user.id, role="user", content=user_text))
    session.commit()

    try:
        reply = strip_markdown(_call_llm(session, user, messages, user_text=user_text))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("LLM call failed for user %s: %s", user.id, exc)
        return LLM_OFFLINE_REPLY

    session.add(Message(user_id=user.id, role="assistant", content=reply))
    session.commit()

    if not user.onboarded:
        user_turns = (
            session.query(Message)
            .filter(Message.user_id == user.id, Message.role == "user")
            .count()
        )
        # Extract every other turn, not every turn — halves Groq call volume
        # during onboarding (the heaviest period for hitting rate limits)
        # without meaningfully slowing down onboarding completion.
        if user_turns % 2 == 0:
            try:
                profile = _extract_profile(session, user)
                _apply_profile(session, user, profile)
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                logger.warning("Profile extraction failed for user %s: %s", user.id, exc)

    return reply


BRIEFING_INSTRUCTION = """You are generating the user's personalized daily briefing.
Cover their watchlist and sectors using live-data tools (prices, company news,
market news) whenever available. Explain WHY something matters, not just what
happened. Keep it tight: a short market-open line, then 3-6 bullet points, then
a one-line "watch today" note. If a requested stock is down to no data, skip it
silently rather than guessing. Do not send a long essay.

If, after checking, there is genuinely nothing noteworthy to report (no material
moves, no relevant news, markets flat and quiet), do not manufacture filler —
reply with EXACTLY the single word NOTHING_IMPORTANT and nothing else. Quality
over frequency: silence is the correct answer when nothing matters today."""


def generate_briefing(session: Session, user: User) -> str | None:
    """Build and run a personalized daily briefing. Never raises.
    Returns None when there's nothing worth sending (quality over frequency)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": BRIEFING_INSTRUCTION},
    ]
    profile = _profile_block(user)
    if profile:
        messages.append({"role": "system", "content": profile})

    facts = memory_service.get_facts(session, user)
    if facts:
        fact_block = "Long-term facts I know about this user:\n- " + "\n- ".join(facts)
        messages.append({"role": "system", "content": fact_block})

    doc_block = document_context_block(session, user)
    if doc_block:
        messages.append({"role": "system", "content": doc_block})

    sheet_block = sheet_context_block(session, user)
    if sheet_block:
        messages.append({"role": "system", "content": sheet_block})

    messages.append(
        {"role": "user", "content": "Generate my daily briefing for today."}
    )

    try:
        reply = strip_markdown(_call_llm(session, user, messages))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Briefing generation failed for user %s: %s", user.id, exc)
        return (
            "Your briefing couldn't be generated right now — my language model "
            "seems to be offline. I'll try again tomorrow."
        )

    if reply.strip().upper() == "NOTHING_IMPORTANT":
        logger.info("Briefing for user %s skipped: nothing important today.", user.id)
        return None

    return reply
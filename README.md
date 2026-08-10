# Financial Assistant Bot (Telegram) — Atlas

A personal financial assistant Telegram bot. Local, open-source stack:

- **python-telegram-bot** — Telegram integration
- **Ollama + llama3.2:3b** — local LLM (optional Anthropic provider supported)
- **SQLAlchemy + SQLite** — persistence
- **Finnhub** — financial data (optional)
- **APScheduler / pydub / whisper** — scheduling, audio, transcription (later phases)

## Phase 0 — project skeleton

```
financial-assistant-bot/
├── .env.example
├── requirements.txt
├── README.md
├── config.py
├── database.py
├── models.py
└── prompts/
    └── system_prompt.py
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows; source venv/bin/activate on Linux/macOS

pip install -r requirements.txt
cp .env.example .env           # then fill in your real tokens

# Ollama (run separately):
ollama pull llama3.2:3b
ollama serve
```

Verify Phase 0:

```bash
python -c "import config; print('Config OK, provider:', config.LLM_PROVIDER)"
```

If that prints without errors, Phase 0 is successful.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | BotFather token |
| `LLM_PROVIDER` | no | `ollama` (default) or `anthropic` |
| `LLM_MODEL` | no | Default `llama3.2:3b` |
| `OLLAMA_BASE_URL` | yes (ollama) | Default `http://localhost:11434` |
| `ANTHROPIC_API_KEY` | yes (anthropic) | Needed when provider is anthropic |
| `FINNHUB_API_KEY` | no | Live financial data (warns loudly if missing) |
| `DATABASE_URL` | no | Default `sqlite:///./assistant.db` |

## Models

- `User` — profile + onboarding state (role, sectors, watchlist, briefing time)
- `Message` — rolling conversation history for LLM context
- `MemoryFact` — long-term learned facts about the user
- `Alert` — custom price alerts (later phases)

## Phases

- **Phase 0** ✅ config, DB, models, system prompt
- **Phase 1** ✅ Telegram bot boot + text/voice/photo handlers
- **Phase 2** ✅ LLM conversation loop (Groq/Ollama) + memory
- **Phase 3** ✅ Voice transcription + image/vision handling
- **Phase 4** ✅ Conversational onboarding (profile extraction)
- **Phase 5** ✅ Financial data tool-calling (quotes, news, fundamentals)
- **Phase 6** ✅ PDF document upload → extract → conversational Q&A
- **Phase 7** ✅ Spreadsheets: CSV/XLSX upload + public Google Sheets links → analysis
- **Phase 8** ✅ Daily briefing scheduler + custom price alerts (move % / above / below)
- **Phase 9+** formatting polish, deploy
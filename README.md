
```markdown
# Financial Assistant Bot (Telegram) — Atlas

A personal AI-powered financial assistant Telegram bot for financial research, company analysis, document analysis, spreadsheet analysis, personalization, watchlists, alerts, and daily briefings.

- **python-telegram-bot** — Telegram integration
- **Groq + Llama 3.3 70B Versatile** — AI/LLM
- **SQLAlchemy + SQLite** — persistence and conversation data
- **Finnhub** — financial market data
- **APScheduler** — scheduling, daily briefings, and price alerts
- **Voice transcription** — Telegram voice message processing
- **PDF processing** — financial document analysis
- **CSV/XLSX processing** — spreadsheet and financial data analysis
- **Railway** — production deployment

## Live Demo

Telegram Bot:

https://t.me/Palaskar_bot

GitHub Repository:

https://github.com/AbhishekPalaskar960/Atlas_Telegram_Bot

## Phase 0 — project skeleton

```

financial-assistant-bot/
├── .env.example
├── requirements.txt
├── README.md
├── config.py
├── database.py
├── models.py
├── main.py
├── bot/
│   ├── telegram_bot.py
│   └── handlers.py
├── services/
│   ├── llm_service.py
│   ├── finnhub_service.py
│   └── ...
└── prompts/
└── ...

````

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows; source venv/bin/activate on Linux/macOS

pip install -r requirements.txt
cp .env.example .env           # then fill in your real tokens

python main.py
````

Verify configuration:

```bash
python -c "import config; print('Config OK, provider:', config.LLM_PROVIDER)"
```

If that prints without errors, the configuration is successful.

## Environment variables

| Variable             | Required | Description                        |
| -------------------- | -------- | ---------------------------------- |
| `TELEGRAM_BOT_TOKEN` | yes      | BotFather token                    |
| `GROQ_API_KEY`       | yes      | Groq API key                       |
| `LLM_PROVIDER`       | yes      | `groq`                             |
| `LLM_MODEL`          | yes      | `llama-3.3-70b-versatile`          |
| `FINNHUB_API_KEY`    | yes      | Finnhub financial data API key     |
| `DATABASE_URL`       | no       | Default `sqlite:///./assistant.db` |

## Models

* `User` — profile, preferences, watchlist, and briefing settings
* `Message` — rolling conversation history for LLM context
* `MemoryFact` — long-term learned facts about the user
* `Alert` — custom price alerts and notification preferences

## Features

* Natural language financial conversations
* Fundamental company analysis
* Stock quotes and financial metrics
* Company news
* Analyst recommendations
* Company peer comparison
* Financial risk analysis
* Personalized watchlists
* User memory and preferences
* Telegram voice message support
* Image handling
* PDF document upload and Q&A
* CSV/XLSX spreadsheet analysis
* Financial model analysis
* Trend and anomaly detection
* Daily financial briefings
* Custom price alerts

## Phases

* **Phase 0** ✅ config, DB, models, system prompt
* **Phase 1** ✅ Telegram bot boot + text/voice/photo handlers
* **Phase 2** ✅ LLM conversation loop using Groq + Llama 3.3 70B + memory
* **Phase 3** ✅ Voice transcription + image handling
* **Phase 4** ✅ Conversational onboarding + profile extraction
* **Phase 5** ✅ Financial data integration (quotes, news, fundamentals, analyst recommendations, peers)
* **Phase 6** ✅ PDF document upload → extract → conversational Q&A
* **Phase 7** ✅ Spreadsheets: CSV/XLSX upload → financial analysis
* **Phase 8** ✅ Daily briefing scheduler + custom price alerts (move % / above / below)
* **Phase 9** ✅ Formatting polish + production deployment
* **Phase 10** ✅ Railway deployment + live Telegram bot

## Production Deployment

Atlas is deployed on Railway as a long-running Telegram worker.

```text
Telegram
    ↓
python-telegram-bot
    ↓
Atlas Financial Assistant
    ├── Groq / Llama 3.3 70B
    ├── Finnhub
    ├── SQLite
    └── APScheduler
```

Production start command:

```bash
python main.py
```

Production secrets are configured through Railway environment variables.

## Security

Never commit the real `.env` file or API keys to GitHub.

The following files should remain excluded from Git:

```text
.env
venv/
.venv/
__pycache__/
*.pyc
*.db
*.sqlite
*.sqlite3
```

## Testing

### Fundamental Analysis

```text
Give me a fundamental analysis of NVDA.
```

### Company Comparison

```text
Compare NVIDIA, Microsoft and Google based on growth,
profitability and valuation.
```

### Financial Reasoning

```text
Don't just list NVIDIA's financial metrics.
Explain what they mean and why they matter.
```

### Market Data

```text
What is the current price of NVDA?
```

### Risk Analysis

```text
What are NVIDIA's biggest strengths and risks?
```

### Memory

```text
I mainly follow NVIDIA, Apple, Microsoft and Google.
Remember that.
```

Then:

```text
Which companies do I follow?
```

### Document Analysis

Upload a financial PDF and ask:

```text
Summarize this report and highlight the biggest risks.
```

### Spreadsheet Analysis

Upload a CSV or XLSX file and ask:

```text
Find unusual trends or anomalies in this financial model.
```

### Price Alert

```text
Create an alert if NVDA moves more than 5% in a day.
```

### Voice

Send a Telegram voice message such as:

```text
Give me a detailed fundamental analysis of Nvidia
and explain the biggest risks.
```

## Current Status

* **Telegram Bot:** ✅ Live
* **Groq LLM:** ✅ Working
* **Llama 3.3 70B:** ✅ Integrated
* **Finnhub:** ✅ Integrated
* **Conversation Memory:** ✅ Working
* **Personalized Watchlist:** ✅ Working
* **Voice:** ✅ Implemented
* **PDF Analysis:** ✅ Implemented
* **Spreadsheet Analysis:** ✅ Implemented
* **Financial Research:** ✅ Working
* **Daily Briefings:** ✅ Implemented
* **Price Alerts:** ✅ Implemented
* **Railway Deployment:** ✅ Complete

## Disclaimer

Atlas is an AI-powered financial research assistant.

Financial information and AI-generated analysis are provided for informational and educational purposes only. Atlas does not provide personalized investment, financial, legal, or tax advice.

Users should independently verify important financial information before making investment decisions.

````


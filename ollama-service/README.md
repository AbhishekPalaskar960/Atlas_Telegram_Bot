# Ollama Service for Atlas Bot (Hybrid Mode)

Separate Ollama service for hybrid mode: local LLM runs tool loop, Groq polishes final answer.

## Deploy on Railway (Recommended)

1. **Create new Railway project** → "Deploy from GitHub repo" → select this repo (or push this `ollama-service` folder as separate repo)

2. **Configure service:**
   - Service Name: `ollama`
   - Root Directory: `/ollama-service` (if mono-repo)
   - Dockerfile: `Dockerfile` (auto-detected)

3. **Environment Variables (Railway Variables tab):**
   ```
   OLLAMA_MODEL=qwen2.5:7b
   ```
   *Optional: `llama3.2:1b` for lower RAM (~1.5GB vs ~5GB)*

4. **Deploy** → Wait for build + model pull (~5-10 min first deploy)

5. **Get public URL:** Railway Settings → Domains → `https://ollama-xxx.up.railway.app`

## Update Atlas Bot (Already Deployed)

In your Atlas bot's Railway project → Variables, add/update:

```env
LLM_HYBRID_MODE=true
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=https://ollama-xxx.up.railway.app
GROQ_POLISH_MODEL=llama-3.3-70b-versatile
```

Redeploy Atlas bot.

## Notes

- **RAM:** `qwen2.5:7b` needs ~5GB. Railway free tier (512MB) **will crash**. Use paid plan ($5/mo for 1GB+) or `llama3.2:1b` (~1.5GB).
- **Cold starts:** First request after idle spins up — expect 10-30s delay.
- **Volume persistence:** Railway doesn't persist `/root/.ollama` across deploys by default. Model re-pulls each deploy. For production, attach Railway Volume → mount to `/root/.ollama`.
- **Healthcheck:** Add `/api/tags` endpoint check for Railway healthchecks.

## Quick Test

```bash
curl https://ollama-xxx.up.railway.app/api/tags
# Should return {"models":[{"name":"qwen2.5:7b",...}]}
```
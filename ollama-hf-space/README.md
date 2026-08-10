# Ollama on Hugging Face Space (Free CPU)

Run `qwen2.5:7b` on HF Spaces free tier (16GB RAM, 2 vCPU). Spins down after ~48h inactivity.

## Deploy Steps

1. **Create HF Space:**
   - Go to https://huggingface.co/new-space
   - Owner: your account
   - Space name: `atlas-ollama` (or any)
   - License: MIT / Apache-2.0
   - SDK: **Docker**
   - Hardware: **CPU basic (free)** — 16GB RAM, enough for qwen2.5:7b
   - Private: **Yes** (don't expose model publicly)

2. **Push this folder to the Space:**
   ```bash
   # Clone your new space
   git clone https://huggingface.co/spaces/YOUR_USERNAME/atlas-ollama
   cd atlas-ollama

   # Copy these files (Dockerfile, entrypoint.sh, .gitignore)
   cp /path/to/financial-assistant-bot/ollama-hf-space/* .
   
   # Or if mono-repo: git subtree push --prefix ollama-hf-space ...
   
   git add .
   git commit -m "Add Ollama service for Atlas bot"
   git push
   ```

3. **Wait for build (~10-20 min):**
   - Logs show: `Pulling model: qwen2.5:7b` → downloads ~4.7GB
   - Build succeeds when `Ollama ready with qwen2.5:7b` appears

4. **Get public URL:**
   - Space URL: `https://YOUR_USERNAME-atlas-ollama.hf.space`
   - Ollama API: `https://YOUR_USERNAME-atlas-ollama.hf.space` (HF proxies 7860→11434)
   - Test: `curl https://YOUR_USERNAME-atlas-ollama.hf.space/api/tags`

## Update Atlas Bot (Railway Variables)

```env
LLM_HYBRID_MODE=true
LLM_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=https://YOUR_USERNAME-atlas-ollama.hf.space
GROQ_POLISH_MODEL=llama-3.3-70b-versatile
```

Redeploy Atlas bot.

## Important Notes

| Aspect | Detail |
|--------|--------|
| **Cold start** | First request after spin-down: 30-60s (model loads) |
| **Spin-down** | ~48h inactivity → Space sleeps. Wake via curl or keep pinging |
| **Rate limit** | Free tier: 1000 requests/day per IP. Bot traffic = 1 IP = fine |
| **No GPU** | CPU inference: ~5-15 tokens/s for qwen2.5:7b |
| **Persistence** | Model cached in `/root/.ollama` — persists across restarts |
| **Private Space** | Keep private so only your bot can call it |

## Keep-Alive (Optional)

Add a cron job (GitHub Actions / Railway cron) to ping every 30 min:

```yaml
# .github/workflows/keep-ollama-alive.yml
name: Keep Ollama Alive
on:
  schedule:
    - cron: "*/30 * * * *"
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: curl -sf https://YOUR_USERNAME-atlas-ollama.hf.space/api/tags > /dev/null
```

## If Memory Issues

Switch to lighter model in Space Settings → Variables:
```
OLLAMA_MODEL=llama3.2:3b
```
Then rebuild Space.
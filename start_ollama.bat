@echo off
REM Starts Ollama with Flash Attention + quantized KV cache enabled.
REM This frees up VRAM (roughly 30-50%% off the KV cache) so more of the
REM model's layers fit on the GTX 1650's 4GB instead of falling back to
REM the much slower CPU path. Quality impact is negligible (q8_0).

set OLLAMA_FLASH_ATTENTION=1
set OLLAMA_KV_CACHE_TYPE=q8_0
set OLLAMA_CONTEXT_LENGTH=2048

echo Starting Ollama with:
echo   OLLAMA_FLASH_ATTENTION=%OLLAMA_FLASH_ATTENTION%
echo   OLLAMA_KV_CACHE_TYPE=%OLLAMA_KV_CACHE_TYPE%
echo   OLLAMA_CONTEXT_LENGTH=%OLLAMA_CONTEXT_LENGTH%
echo.

ollama serve
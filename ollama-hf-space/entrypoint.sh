#!/bin/sh
set -e

MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

echo "Starting ollama server..."
ollama serve &
SERVER_PID=$!

# Wait for server to be ready
echo "Waiting for ollama to start..."
sleep 10

# Pull model if not present
if ! ollama list | grep -q "^${MODEL%%:*}"; then
    echo "Pulling model: ${MODEL}..."
    ollama pull "${MODEL}"
else
    echo "Model ${MODEL} already present"
fi

echo "Ollama ready with ${MODEL} on port 11434"
echo "HF Space will proxy via port 7860"

# Keep container alive
wait $SERVER_PID
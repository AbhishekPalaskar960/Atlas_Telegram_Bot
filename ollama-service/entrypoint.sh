#!/bin/sh
set -e

MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"

echo "Starting ollama server..."
ollama serve &
SERVER_PID=$!

# Wait for server to be ready
echo "Waiting for ollama to start..."
sleep 8

# Pull model if not present
if ! ollama list | grep -q "^${MODEL%%:*}"; then
    echo "Pulling model: ${MODEL}..."
    ollama pull "${MODEL}"
else
    echo "Model ${MODEL} already present"
fi

echo "Ollama ready with ${MODEL}"
wait $SERVER_PID
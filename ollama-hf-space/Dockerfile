FROM ollama/ollama:latest

# HF Spaces requires the app to listen on 7860
ENV GRADIO_SERVER_PORT=7860
ENV GRADIO_SERVER_NAME=0.0.0.0

# Copy entrypoint that pulls model then starts serve on 11434
# HF Spaces proxies 7860, but ollama runs on 11434 internally
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
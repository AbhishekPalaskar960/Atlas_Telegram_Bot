FROM ollama/ollama:latest

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Use custom entrypoint that pulls model then starts serve
ENTRYPOINT ["/entrypoint.sh"]
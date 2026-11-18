FROM python:3.13-slim
WORKDIR /app
COPY requirements.lock.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY *.py ./
ENV PYTHONUNBUFFERED=1 GUARDIAN_DB_PATH=/data/guardian.sqlite3
# Persistent storage: attach a Railway Volume mounted at /data
# Default: Telegram bot. For the MCP server override with:
#   python guardian_mcp_server.py --transport streamable-http
CMD ["python", "guardian_telegram.py"]

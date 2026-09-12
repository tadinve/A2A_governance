# Inventory & Purchasing UI. Holds no signing key and no Zoho credential: it
# reaches Zoho through the same MCP connectors the agents use, and asks the
# Auth Broker for any token it needs.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY cloud/inventory_ui/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
COPY config/ /app/config/
# The UI loads the Zoho adapter and reorder policy out of the agent package
# rather than keeping a second copy that can drift from what the agents run.
COPY cloud/inventory_agent/ /app/cloud/inventory_agent/

# Writable state lives here. On Cloud Run this is instance-local and does not
# survive a new revision -- see deploy_inventory_ui.sh, which pins the service
# to a single instance so that at least it is never *inconsistent*.
RUN mkdir -p /app/runtime
ENV PYTHONPATH=/app/src \
    INVENTORY_UI_DB=/app/runtime/inventory_ui.sqlite3 \
    SESSION_SECRET_FILE=/app/runtime/session_secret

EXPOSE 8080
CMD exec uvicorn inventory_ui.app:app --host 0.0.0.0 --port ${PORT:-8080}

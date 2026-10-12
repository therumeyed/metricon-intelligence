#!/bin/sh
# Web service entrypoint: apply migrations, load missing seed rows (never overwrites admin edits), then serve.
set -e
python -m app.cli migrate
python -m app.cli seed
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"

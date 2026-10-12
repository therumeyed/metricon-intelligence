# syntax=docker/dockerfile:1
# Stage 1: build the React UI
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: API + collectors + built UI in one image (web service and cron jobs share it)
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
# Optional: Playwright for sources flagged fetch_mode=browser (build with --build-arg INSTALL_BROWSER=1)
ARG INSTALL_BROWSER=0
COPY backend/requirements-browser.txt ./
RUN if [ "$INSTALL_BROWSER" = "1" ]; then pip install -r requirements-browser.txt && playwright install --with-deps chromium; fi
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/seed ./seed
COPY backend/alembic.ini ./
COPY scripts/start.sh ./start.sh
COPY --from=ui /ui/dist ./static
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["sh", "./start.sh"]

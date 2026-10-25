FROM node:24.14.1-bookworm-slim AS frontend
ENV SCARF_ANALYTICS=false DO_NOT_TRACK=1
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13.2-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --no-cache-dir -r /app/backend/requirements.lock
COPY backend /app/backend
COPY --from=frontend /build/frontend/dist /app/frontend/dist
RUN useradd --uid 10001 --create-home credit && mkdir -p /app/backend/data && chown -R credit:credit /app
USER credit
WORKDIR /app/backend
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health/ready')"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

FROM runtime AS test
USER root
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -r /app/backend/requirements-dev.lock && mkdir -p /app/work && chown -R credit:credit /app
USER credit
WORKDIR /app
CMD ["python", "-m", "pytest", "tests", "-q"]

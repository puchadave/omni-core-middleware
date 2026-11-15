# Stage 1: Build dependencies
FROM python:3.11-alpine AS builder

RUN apk add --no-cache --virtual=.build-deps \
    gcc \
    musl-dev \
    libpq-dev \
    cargo \
    pkgconfig \
    make

WORKDIR /build

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-alpine

RUN apk add --no-cache \
    libpq \
    sqlite-libs \
    tzdata \
    su-exec

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . .

RUN addgroup -S appgroup && adduser -S appuser -G appgroup \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appgroup /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/app/data/middleware.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python3", "-c", "import asyncio; from services.omniroute_client import OmniRouteClient; import os; base_url = os.getenv('OMNI_ROUTE_BASE_URL', 'http://host.docker.internal:20128'); api_key = os.getenv('OMNI_ROUTE_API_KEY', 'test-client'); client = OmniRouteClient(base_url, api_key); asyncio.run(client.check_health())"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--loop", "uvloop"]

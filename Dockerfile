FROM python:3.11-alpine AS builder

# Install system dependencies for asyncpg and pydantic
RUN apk add --no-cache --virtual=.build-deps \
        gcc \
        musl-dev \
        libpq-dev \
        jpeg-dev \
        zlib-dev \
        cargo \
        cargo-doc \
        cargo-html \
    && pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn[standard]==0.32.0 \
        uvloop==0.21.0 \
        httpx==0.27.2 \
        pydantic==2.10.2 \
        pydantic-settings==2.6.0 \
        aiosqlite==0.20.0 \
        jinja2==3.1.4

# Runtime stage - only install production deps, remove build deps
FROM python:3.11-alpine

# Install runtime dependencies
RUN apk add --no-cache \
        libpq \
        tzdata \
        su-exec

# Set working directory
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application code
COPY . .

# Create non-root user for security
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser

# Expose the application port
EXPOSE 8000

# Startup command with uvicorn and uvloop
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
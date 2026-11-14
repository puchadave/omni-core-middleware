FROM python:3.11-alpine AS builder

WORKDIR /app

# Install build dependencies needed for compiling Python packages
RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev cargo pkgconfig

# Copy requirements first for caching
COPY requirements.txt .

# Install dependencies into /install prefix
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-alpine

# Install only runtime dependencies (sqlite-libs, minimal libs)
RUN apk add --no-cache sqlite-libs libffi

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Create non-root user for security
RUN adduser -D -u 1000 appuser
USER appuser

# Expose FastAPI port
EXPOSE 8000

# Start FastAPI with Uvicorn + uvloop
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
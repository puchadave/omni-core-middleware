FROM python:3.11-alpine AS builder

# Install build dependencies needed for compiling Python packages
# Musl-dev is required for musl libc compatibility with uvloop and pydantic
RUN apk add --no-cache --virtual=.build-dependencies \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    cargo \
    pkgconfig

# Set work directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install dependencies into /prefix
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Remove build dependencies to keep image small
RUN apk del .build-dependencies

FROM python:3.11-alpine

# Install only runtime dependencies (sqlite-libs, minimal libs needed)
RUN apk add --no-cache \
    sqlite-libs \
    libffi \
    openssl \
    tzdata

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Start FastAPI with Uvicorn + uvloop
# uvloop provides high-performance event loop for asyncio
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
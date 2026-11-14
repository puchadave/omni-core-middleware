FROM python:3.11-alpine AS builder

WORKDIR /app
RUN apk add --no-cache gcc musl-dev libffi-dev sqlite-dev build-base \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-alpine

RUN apk add --no-cache sqlite-libs libffi

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

RUN adduser -D -u 1000 appuser

USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop"]
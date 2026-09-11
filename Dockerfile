# Multi-stage build (doc §12): wheels are built once, the runtime image carries
# no compiler and runs as a non-root user.
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

# postgresql-client gives us `make backup` and `entrypoint.sh psql` in-container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 iot

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

COPY app        ./app
COPY scripts    ./scripts
COPY migrations ./migrations
COPY templates  ./templates
COPY static     ./static

RUN chmod +x scripts/entrypoint.sh scripts/backup.sh scripts/restore.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

USER iot
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["serve"]

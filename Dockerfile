# Multi-stage build (doc §12): wheels are built once, the runtime image carries
# no compiler and runs as a non-root user.
#
# Both stages name the base image by digest as well as tag: a tag can be
# repointed at different bytes, a digest cannot. To move to a newer Python
# or Debian patch, look the digest up (`docker buildx imagetools inspect
# python:3.12-slim`) and change both lines.
FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS builder

WORKDIR /build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# requirements.txt pins every package and lists its hashes (make lock);
# --require-hashes makes pip refuse anything that does not match them.
COPY requirements.txt .
RUN pip wheel --no-cache-dir --require-hashes --wheel-dir /wheels -r requirements.txt


FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS runtime

# postgresql-client gives us `make backup` and `entrypoint.sh psql` in-container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 iot

WORKDIR /app

COPY --from=builder /wheels /wheels
# Exactly the wheels the builder checked, nothing fetched: the lock is
# complete, so --no-deps leaves no gap for pip to fill on its own.
RUN pip install --no-cache-dir --no-index --no-deps /wheels/*.whl \
 && rm -rf /wheels

COPY app        ./app
COPY scripts    ./scripts
COPY migrations ./migrations
COPY templates  ./templates
COPY static     ./static

RUN chmod +x scripts/entrypoint.sh scripts/backup.sh scripts/restore.sh

# Local device names live here. Created owned by `iot` so that mounting a named
# volume over it inherits that ownership -- Docker seeds a fresh named volume
# from the image path, permissions included, which a bind mount would not.
RUN mkdir -p /app/data && chown iot:iot /app/data

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

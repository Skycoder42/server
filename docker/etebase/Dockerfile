# syntax=docker/dockerfile:1

# Production Etebase server image, built on top of a Docker Hardened image
# (https://hub.docker.com/hardened-images/catalog/dhi/python).
#
# The runtime stage intentionally has no shell and no package manager, so all
# setup happens in the build stage and the final image only copies artifacts
# over and runs a Python entrypoint.

# Build stage: hardened Python dev variant (root, shell, apk, build tools).
FROM dhi.io/python:3.14-alpine3.24-dev AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY . /build

RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --progress-bar off \
        -r requirements.txt \
        'psycopg[binary]'

RUN mkdir -p /data/media /srv/etebase/static \
    && DJANGO_STATIC_ROOT=/srv/etebase/static /opt/venv/bin/python manage.py collectstatic --noinput

# Runtime stage: hardened Python runtime variant - nonroot user 65532, no
# shell, no package manager.
FROM dhi.io/python:3.14-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ETEBASE_EASY_CONFIG_PATH="/data/etebase-server.ini" \
    SECRET_FILE="/data/secret.txt" \
    DATA_DIR="/data" \
    MEDIA_ROOT="/data/media" \
    STATIC_ROOT="/srv/etebase/static" \
    DB_ENGINE="sqlite" \
    DATABASE_NAME="/data/db.sqlite3" \
    ALLOWED_HOSTS="localhost,127.0.0.1,[::1]" \
    PORT="3735"

WORKDIR /app

COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv
COPY --chown=65532:65532 . /app
COPY --chown=65532:65532 --from=builder /srv/etebase/static /srv/etebase/static
COPY --chown=65532:65532 --from=builder /data /data
COPY --chown=65532:65532 docker/etebase/entrypoint.py /entrypoint.py
COPY --chown=65532:65532 docker/etebase/healthcheck.py /healthcheck.py

USER 65532:65532

VOLUME /data

EXPOSE 3735

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "/healthcheck.py"]

ENTRYPOINT ["python", "/entrypoint.py"]

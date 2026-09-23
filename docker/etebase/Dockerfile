# syntax=docker/dockerfile:1

# Production Etebase server image, by default built on top of a Docker
# Hardened image (https://hub.docker.com/hardened-images/catalog/dhi/python).
#
# The base images are configurable via build args: CI builds that cannot reach
# the dhi.io registry (or anyone who prefers) can build on the standard
# official python image instead, e.g.:
#   docker build --build-arg BUILDER_IMAGE=python:3.14-alpine3.24 \
#                --build-arg RUNTIME_IMAGE=python:3.14-alpine3.24 .
# With the DHI defaults the runtime stage has no shell and no package manager
# and runs as nonroot 65532; with other bases that depends on the base image.
# Either way all setup happens in the build stage and the final image only
# copies artifacts over and runs a Python entrypoint.

ARG BUILDER_IMAGE=dhi.io/python:3.14-alpine3.24-dev
ARG RUNTIME_IMAGE=dhi.io/python:3.14-alpine3.24

# Build stage: hardened Python dev variant (root, shell, apk, build tools).
FROM ${BUILDER_IMAGE} AS builder
ARG BUILDER_IMAGE

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY . /build

# The standard python image has no build tools by default, so pull them in
# unless the (pre-provisioned) DHI dev image is being used.
RUN case "${BUILDER_IMAGE}" in dhi.io/*) ;; *) apk add --no-cache build-base libffi-dev openssl-dev ;; esac \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --progress-bar off \
        -r requirements.txt \
        'psycopg[binary]'

RUN mkdir -p /data/media /srv/etebase/static \
    && DJANGO_STATIC_ROOT=/srv/etebase/static /opt/venv/bin/python manage.py collectstatic --noinput

# Runtime stage: hardened Python runtime variant - nonroot user 65532, no
# shell, no package manager (with the DHI default base; see header).
FROM ${RUNTIME_IMAGE}

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

#!/usr/bin/env bash
# Shared helpers for the compatibility test scripts (source this file).
set -euo pipefail

COMPAT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${COMPAT_DIR}/../.." && pwd)"

export COMPAT_DIR REPO_ROOT
export COMPAT_PORT="${COMPAT_PORT:-3785}"
export ETEBASE_IMAGE_LEGACY="${ETEBASE_IMAGE_LEGACY:-docker.io/victorrds/etesync:latest}"
export ETEBASE_IMAGE_NEW="${ETEBASE_IMAGE_NEW:-skycoder42/etebase-server:v0.14.2-10-g4690e0c}"
# The Python interpreter that runs the driver (defaults to the venv from
# AGENTS.md; CI overrides this with its own interpreter).
PYTHON="${PYTHON:-${REPO_ROOT}/.venv/bin/python}"
export PYTHON

COMPAT_PROJECT="etebase-compat"
COMPAT_VOLUME="${COMPAT_PROJECT}_compat_data"
COMPAT_CONTAINER="etebase-compat"
COMPAT_REDIS_CONTAINER="etebase-compat-redis"

compose() {
  docker compose -p "${COMPAT_PROJECT}" -f "${COMPAT_DIR}/compose.yaml" "$@"
}

assert_image_present() {
  local image="$1"
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    echo "ERROR: image '${image}' not found locally (override with ETEBASE_IMAGE_NEW)." >&2
    echo "       Build the fork image with: ./docker/build.sh server <tag>" >&2
    return 1
  fi
}

wait_for_ready() {
  echo "Waiting for the server on 127.0.0.1:${COMPAT_PORT} ..."
  local i
  for i in $(seq 1 90); do
    if curl -fsS --max-time 3 -o /dev/null "http://127.0.0.1:${COMPAT_PORT}/api/v1/authentication/is_etebase/" 2>/dev/null; then
      echo "OK: server is ready"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: server did not become ready" >&2
  docker logs "${COMPAT_CONTAINER}" 2>&1 | tail -60 >&2 || true
  return 1
}

compat_server_url() {
  echo "http://127.0.0.1:${COMPAT_PORT}"
}
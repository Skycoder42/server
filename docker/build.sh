#!/bin/bash
set -euo pipefail

# Build scripts for the Etebase server Docker images.
#
#   ./docker/build.sh server [TAG]        # production image (docker/etebase)
#   ./docker/build.sh server-check [TAG]  # production image + clean-repo/dirty-tree
#                                         # checks and a smoke test
#
# The production image also serves as the development/test image: with the
# right environment it is what client integration tests (e.g. etebase-dart) run
# against, so there is no separate test-server image.
#
# The production image pulls its base images from the `dhi.io` registry and
# needs a login first:  docker login dhi.io
#
# The base images are configurable per build (e.g. CI builds without dhi.io
# credentials can use the standard official python image):
#   BUILDER_IMAGE=python:3.14-alpine3.24 RUNTIME_IMAGE=python:3.14-alpine3.24 \
#     ./docker/build.sh server <tag>
#
# The produced image name is configurable too, e.g. to point at your own
# registry:  IMAGE=registry.example.com/skycoder42/etebase-server

IMAGE="${IMAGE:-skycoder42/etebase-server}"
COMMAND="${1:-server}"
TAG="${2:-}"
BUILDER_IMAGE="${BUILDER_IMAGE:-}"
RUNTIME_IMAGE="${RUNTIME_IMAGE:-}"

_SMOKE_NAME=""
_CLEAN_TMP=""

cleanup() {
  if [ -n "${_SMOKE_NAME}" ]; then
    docker rm -f "${_SMOKE_NAME}" >/dev/null 2>&1 || true
  fi
  if [ -n "${_CLEAN_TMP}" ]; then
    rm -rf "${_CLEAN_TMP}"
  fi
}
trap cleanup EXIT

if [ -z "${TAG}" ]; then
  TAG="$(git describe --tags 2>/dev/null || git rev-parse --short HEAD)"
fi

build_server() {
  local context="${1:-.}"
  local file="${context%/}/docker/etebase/Dockerfile"
  echo "Building working copy to ${IMAGE}:${TAG} (context: ${context})"
  local -a build_args=()
  if [ -n "${BUILDER_IMAGE}" ]; then
    build_args+=(--build-arg "BUILDER_IMAGE=${BUILDER_IMAGE}")
  fi
  if [ -n "${RUNTIME_IMAGE}" ]; then
    build_args+=(--build-arg "RUNTIME_IMAGE=${RUNTIME_IMAGE}")
  fi
  if [ "${#build_args[@]}" -gt 0 ]; then
    docker build "${build_args[@]}" \
      -t "${IMAGE}:${TAG}" \
      -f "${file}" \
      "${context}"
  else
    docker build \
      -t "${IMAGE}:${TAG}" \
      -f "${file}" \
      "${context}"
  fi
}

clean_context_tar() {
  # Build a context that mirrors a clean checkout of the committed tree plus
  # any new (non-ignored) files, i.e. without db.sqlite3, secret.txt, .venv,
  # media/ and other gitignored artifacts.
  git ls-files -z
  git ls-files -z --others --exclude-standard
}

assert_no_secret() {
  # The runtime image has no shell, so probe with python. Non-zero exit means
  # the checked file made it into the image.
  docker run --rm "${1}" python -c "import os,sys; sys.exit(0 if os.path.exists('/app/secret.txt') or os.path.exists('/app/db.sqlite3') else 1)"
}

smoke_test_server() {
  _SMOKE_NAME="etebase-server-smoke"
  echo "Smoke test: starting ${IMAGE}:${TAG} on 127.0.0.1:3735"
  docker rm -f "${_SMOKE_NAME}" >/dev/null 2>&1 || true
  docker run -d --name "${_SMOKE_NAME}" \
    -p 127.0.0.1:3735:3735 \
    "${IMAGE}:${TAG}"

  local healthy=""
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 -o /dev/null http://127.0.0.1:3735/ 2>/dev/null; then
      healthy="yes"
      break
    fi
    sleep 1
  done
  if [ -z "${healthy}" ]; then
    echo "Smoke test failed: server did not become healthy" >&2
    docker logs "${name}" >&2
    exit 1
  fi
  echo "OK: / served"
  curl -fsS -o /dev/null http://127.0.0.1:3735/static/admin/css/base.css
  echo "OK: static files served"
  # Signup is blocked by default (HTTP >= 400, not 201).
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:3735/api/v1/auth/signup/)"
  if [ "${code}" = "201" ]; then
    echo "Smoke test failed: signup should be blocked by default" >&2
    exit 1
  fi
  echo "OK: signup blocked by default (HTTP ${code})"
  echo "Smoke test passed"
}

server_check() {
  _CLEAN_TMP="$(mktemp -d)"

  echo "Building from clean-checkout context"
  to_archive="$(mktemp)"
  clean_context_tar | tar --null -T - -cf "${to_archive}"
  tar -xf "${to_archive}" -C "${_CLEAN_TMP}"
  rm -f "${to_archive}"
  build_server "${_CLEAN_TMP}/"
  echo "OK: clean checkout builds"

  echo "Verifying secrets from a dirty working tree are not baked in"
  build_server "."
  assert_no_secret "${IMAGE}:${TAG}"
  echo "OK: no secret.txt / db.sqlite3 baked into the image"

  smoke_test_server
}

case "${COMMAND}" in
  server) build_server ;;
  server-check) server_check ;;
  *)
    echo "Unknown command '${COMMAND}' (expected: server | server-check)" >&2
    exit 1
    ;;
esac
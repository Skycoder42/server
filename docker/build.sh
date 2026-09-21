#!/bin/bash
set -euo pipefail

# Build scripts for the Etebase server Docker images.
#
#   ./docker/build.sh test-server [TAG]   # development/test image (docker/test-server)
#   ./docker/build.sh server [TAG]        # production image (docker/etebase)
#   ./docker/build.sh server-check [TAG]  # production image + clean-repo/dirty-tree
#                                         # checks and a smoke test
#
# The production image pulls its base images from the `dhi.io` registry and
# needs a login first:  docker login dhi.io

REGISTRY="${REGISTRY:-etesync}"
COMMAND="${1:-server}"
TAG="${2:-}"

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

build_test_server() {
  echo "Building working copy to ${REGISTRY}/test-server:${TAG}"
  docker build \
    --build-arg ETESYNC_VERSION="${TAG}" \
    -t "${REGISTRY}/test-server:${TAG}" \
    -f docker/test-server/Dockerfile \
    .
}

build_server() {
  local context="${1:-.}"
  local file="${context%/}/docker/etebase/Dockerfile"
  echo "Building working copy to ${REGISTRY}/server:${TAG} (context: ${context})"
  docker build \
    -t "${REGISTRY}/server:${TAG}" \
    -f "${file}" \
    "${context}"
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
  echo "Smoke test: starting ${REGISTRY}/server:${TAG} on 127.0.0.1:3735"
  docker rm -f "${_SMOKE_NAME}" >/dev/null 2>&1 || true
  docker run -d --name "${_SMOKE_NAME}" \
    -p 127.0.0.1:3735:3735 \
    "${REGISTRY}/server:${TAG}"

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
  assert_no_secret "${REGISTRY}/server:${TAG}"
  echo "OK: no secret.txt / db.sqlite3 baked into the image"

  smoke_test_server
}

case "${COMMAND}" in
  test-server) build_test_server ;;
  server) build_server ;;
  server-check) server_check ;;
  *)
    echo "Unknown command '${COMMAND}' (expected: test-server | server | server-check)" >&2
    exit 1
    ;;
esac
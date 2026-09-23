#!/usr/bin/env bash
# Phase 2: swap the image from victorrds/etesync to the locally built
# etesync/server fork, keeping all data in the volume untouched.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "== Phase 2: switch to ${ETEBASE_IMAGE_NEW} =="
assert_image_present "${ETEBASE_IMAGE_NEW}"

echo "Stopping the victorrds-based server (volume is preserved)"
docker rm -f "${COMPAT_CONTAINER}" >/dev/null 2>&1 || true

# The victorrds image runs as 373:373, the fork image as 65532:65532. Fix the
# volume ownership from inside a root container (works under rootless podman).
echo "Setting volume ownership to 65532:65532:"
docker run --rm -v "${COMPAT_VOLUME}:/data" docker.io/library/alpine:3 chown -R 65532:65532 /data

compose -f "${COMPAT_DIR}/compose.fork.yaml" up -d etebase
wait_for_ready

echo "Container log tail:"
docker logs "${COMPAT_CONTAINER}" 2>&1 | tail -20
echo "== Phase 2 done =="
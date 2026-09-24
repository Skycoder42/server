#!/usr/bin/env bash
# Phase 3: verify all phase-1 data survived the image switch, then continue on
# it (updates, new shares, accepting pending invitations, …) and re-verify.
set -euo pipefail
# lib.sh always sits next to this script; shellcheck cannot follow the dynamic
# path (SC1091 is informational).
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "== Phase 3: verify continuation on ${ETEBASE_IMAGE_NEW} =="
assert_image_present "${ETEBASE_IMAGE_NEW}"

# Make sure the fork image is the one running. podman normalizes unqualified
# image refs to `localhost/...`, so compare on the normalized name.
norm() { printf '%s' "$1" | sed 's#^localhost/##'; }
RUNNING_IMAGE="$(docker inspect -f '{{.Config.Image}}' "${COMPAT_CONTAINER}" 2>/dev/null || true)"
if [ -z "${RUNNING_IMAGE}" ]; then
  compose -f "${COMPAT_DIR}/compose.fork.yaml" up -d etebase
  wait_for_ready
elif [ "$(norm "${RUNNING_IMAGE}")" != "$(norm "${ETEBASE_IMAGE_NEW}")" ]; then
  echo "ERROR: '${COMPAT_CONTAINER}' is running '${RUNNING_IMAGE}', expected '${ETEBASE_IMAGE_NEW}'" >&2
  exit 1
fi

"${PYTHON}" "${COMPAT_DIR}/driver.py" verify
echo "== Phase 3 done =="
#!/usr/bin/env bash
# Phase 1: seed a variety of data onto the latest published victorrds/etesync image.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "== Phase 1: seed on ${ETEBASE_IMAGE_LEGACY} =="
compose -f "${COMPAT_DIR}/compose.victorrds.yaml" up -d etebase redis
wait_for_ready

"${PYTHON}" "${COMPAT_DIR}/driver.py" seed
echo "== Phase 1 done =="
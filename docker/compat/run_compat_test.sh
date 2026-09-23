#!/usr/bin/env bash
# Run the full e2e compatibility suite: phase1 (seed on victorrds/etesync),
# phase2 (switch to the fork image, keep data), phase3 (continue + verify).
#
# Usage:
#   ./docker/compat/run_compat_test.sh           # reset volume, run 3 phases
#   ./docker/compat/run_compat_test.sh --keep    # leave containers up at end
#   ./docker/compat/run_compat_test.sh --clean   # also remove the data volume
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KEEP=0
CLEAN=0
for arg in "$@"; do
  case "${arg}" in
    --keep) KEEP=1 ;;
    --clean) CLEAN=1 ;;
    *) echo "Unknown argument: ${arg}" >&2; exit 1 ;;
  esac
done

echo "== Resetting the compatibility environment (fresh volume) =="
compose -f "${COMPAT_DIR}/compose.victorrds.yaml" -f "${COMPAT_DIR}/compose.fork.yaml" down -v 2>/dev/null || true
docker rm -f "${COMPAT_CONTAINER}" >/dev/null 2>&1 || true
docker rm -f "${COMPAT_REDIS_CONTAINER}" >/dev/null 2>&1 || true

"${COMPAT_DIR}/phase1_seed.sh"
"${COMPAT_DIR}/phase2_switch.sh"
"${COMPAT_DIR}/phase3_verify.sh"

echo ""
echo "== All phases passed: victorrds/etesync data continues cleanly on the fork image =="

if [ "${CLEAN}" = "1" ]; then
  echo "Cleaning up (removing containers and data volume)"
  compose -f "${COMPAT_DIR}/compose.fork.yaml" down -v
elif [ "${KEEP}" = "1" ]; then
  echo "Leaving the server running on 127.0.0.1:${COMPAT_PORT}"
else
  echo "Stopping the etebase compat server (data kept in volume '${COMPAT_VOLUME}')"
  docker rm -f "${COMPAT_CONTAINER}" >/dev/null 2>&1 || true
  echo "  - restart:  docker compose -p ${COMPAT_PROJECT} -f ${COMPAT_DIR}/compose.yaml -f ${COMPAT_DIR}/compose.fork.yaml up -d etebase"
  echo "  - reset:    ${COMPAT_DIR}/run_compat_test.sh (fresh seed)"
fi
# E2E compatibility test suite

Verifies that data produced by the latest published
[`victorrds/etesync`](https://hub.docker.com/r/victorrds/etesync) image keeps
working, losslessly, on this fork's production image
(`etesync/server:…`, built with `./docker/build.sh server`).

It runs in three sequential phases, each its own script:

1. **`phase1_seed.sh`** — starts `victorrds/etesync:latest` on
   `127.0.0.1:3785` in a persisted compose setup and seeds a *variety* of
   Etebase 2.0 states onto it: multiple users, private/shared/read-only
   collections, collections with pending/declined/canceled/accepted
   invitations, item create/update/trash/restore, external chunk uploads,
   revision histories, member add/remove/leave, a soft-deleted collection, and
   a bunch of state-machine negatives (stale etag/stoken, wrong password,
   non-admin access, …). Everything is recorded to `data/snapshot.json`.
2. **`phase2_switch.sh`** — stops the server, replaces the image with the
   locally built fork image (keeps the `/data` volume untouched), fixes its
   ownership 373→65532 via the fork image's own `ETEBASE_FIX_OWNERSHIP` mode
   (no external image needed), and starts it again. The fork's entrypoint then
   runs `migrate` over the existing database — the real schema-compatibility
   check.
3. **`phase3_verify.sh`** — re-logs-in every user, asserts the read-back of
   every collection/item/revision/chunk/member/invitation matches the phase-1
   snapshot exactly (no data loss), then *continues* on the data (item updates,
   new collections and shares, accepting the pending invitations, member
   changes, a password change, incremental sync via fresh stokens) and
   re-verifies the final state. Stokens are server-secret-signed opaque
   cursors, so across the image switch they are verified by their usefulness
   (that they still drive incremental sync), not by value.

## Prerequisites

- A working `docker` CLI (docker or podman alias, rootless is fine).
- The fork image built: `./docker/build.sh server` → the `ETEBASE_IMAGE_NEW`
  (default `etesync/server:v0.14.2-10-g4690e0c`).
- `curl` and a Python interpreter with `requirements.txt` installed. The
  scripts default to the repo venv (`.venv/bin/python`, per AGENTS.md); set
  `PYTHON=/path/to/python` to use a different one.

## Running

Full run from a clean volume (recommended):

```
./docker/compat/run_compat_test.sh
```

Options: `--keep` leaves the server running at the end, `--clean` also deletes
the data volume.

Individual phases (data survives between them):

```
./docker/compat/phase1_seed.sh      # seed onto victorrds/etesync:latest
./docker/compat/phase2_switch.sh    # swap to the fork image, keep data
./docker/compat/phase3_verify.sh    # read-back + continue + re-verify
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ETEBASE_IMAGE_LEGACY` | `victorrds/etesync:latest` | Phase-1 image (pulled) |
| `ETEBASE_IMAGE_NEW` | `etesync/server:v0.14.2-10-g4690e0c` | Phases 2–3 image (local) |
| `COMPAT_PORT` | `3785` | Host port the servers listen on |
| `PYTHON` | `$REPO_ROOT/.venv/bin/python` | Interpreter running the driver |

## CI

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs the whole suite
as the `compat` job (ubuntu, single python). Because the CI has no `dhi.io`
credentials, it builds the fork image with the *standard* official python base
instead of the hardened DHI images and runs the suite against that build — so
the image is exercised on both base options. The `docker` deploy job waits on
the `compat` job, so a compat failure blocks deployment. Locally you can
reproduce the CI build with:

```
BUILDER_IMAGE=python:3.14-alpine3.24 RUNTIME_IMAGE=python:3.14-alpine3.24 \
  ./docker/build.sh server compat-ci
ETEBASE_IMAGE_NEW=etesync/server:compat-ci ./docker/compat/run_compat_test.sh
```

The `docker/etebase/Dockerfile` accepts `BUILDER_IMAGE` / `RUNTIME_IMAGE`
build args (defaulting to `dhi.io/python:3.14-alpine3.24[-dev]`), and
`./docker/build.sh` forwards the `BUILDER_IMAGE` / `RUNTIME_IMAGE` env vars to
the build.

## Notes / troubleshooting

- The victorrds image runs as UID 373, the fork image as 65532. Phase 2 fixes
  volume ownership with the fork image's own root-only mode:
  `docker run --rm --user 0:0 -e ETEBASE_FIX_OWNERSHIP=1 -v <volume>:/data <fork image>`
  (this was previously done with a one-off `alpine chown -R 65532:65532 /data`,
  which still works as a fallback). The fork entrypoint aborts with that
  command when it detects a non-writable `/data`, so a missed chown surfaces as
  a clear error instead of a confusing DB failure.
- Port 3785 avoids clashing with the etebase-dart integration stack on 3735.
- Everything is wired for signup + Redis (websocket) in both phases.
- Reset fully: `docker compose -p etebase-compat down -v` or
  `./docker/compat/run_compat_test.sh`.
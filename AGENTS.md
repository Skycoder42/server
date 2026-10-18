# AGENTS.md

Etebase 2.0 server (Python). Fork of <https://github.com/etesync/server> (`upstream` remote) — keep changes compatible with upstream conventions.

## Architecture

- Django is only the ORM/admin layer; FastAPI serves the EteSync v2 API. `etebase_server/asgi.py` routes `/api/v1/*` to FastAPI and mounts Django at `/`, so run via `uvicorn etebase_server.asgi:application` — plain Django views never see API traffic.
- Auth is zero-knowledge (PyNaCl + msgpack), not Django auth. Users are created passwordless in the Django admin; API auth is an `Authorization` token resolved in `fastapi/dependencies.py`.
- Signup is blocked by default via `ETEBASE_CREATE_USER_FUNC` in `settings.py`. Config sources: `etebase-server.ini` (repo root, `/etc/etebase-server/`, or `ETEBASE_EASY_CONFIG_PATH`), then the gitignored `etebase_server_settings.py` for final overrides.

## Commands

- Venv from the system Python: `/usr/bin/python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`.
- CI gates on `ruff check .`, `ruff format --check .`, `mypy .` and `.venv/bin/python -m pytest` — keep all green.
- `./docker/build.sh server-check` builds a clean checkout, checks no secrets are baked in and smoke-tests the server.

## Dependencies

- `requirements-src/{base,development,docker}.txt` are the pip-compile sources for `requirements.txt`, `requirements-dev.txt` and `requirements-docker.txt`. Always pass `--upgrade` when regenerating (otherwise existing pins act as constraints), and regenerate `requirements-docker.txt` whenever `requirements.txt` changes — they share pins.
- `docker.txt` is `-r base.txt` + `psycopg[binary]`. psycopg must stay out of `base.txt`: `setup.py` reads it as `install_requires`.
- Never name any directory `*.in`: Dependabot's pip helper globs `**/*.in`, hits the directory and the whole run fails with `dependency_file_not_evaluatable`.
- The package version lives only in `setup.py`; read it with `docker/get_version.py` (never `python setup.py --version` — no setuptools on Python 3.12+ venvs). Docker CI requires tag builds to match it (`v0.14.3`).

## Gotchas

- FastAPI runs outside Django's request cycle, so DB connections are never cleaned up automatically. Every FastAPI dependency/endpoint touching the ORM needs `@django_db_cleanup_decorator` (`fastapi/db_hack.py`).
- Routes use `MsgpackRoute`/`MsgpackResponse` (`fastapi/msgpack.py`). `MsgpackRoute.get_route_handler` must resolve FastAPI's `_effective_route_context_var`: since FastAPI 0.137 the `include_router` prefix isn't on the route, and without it path params (e.g. `collection_uid`) are treated as query params.
- `/api/v1/ws/*` needs Redis (`ETEBASE_REDIS_URI`). `fastapi/routers/websocket.py` relies on Python 3.14 / redis-py 8 behaviour: `asyncio.wait` needs tasks, not coroutines, and `pubsub.get_message` returns a dict.
- ORM managers are annotated `models.manager.Manager["X"]`, not `BaseManager` (lacks `filter`/`get`/`create` in django-stubs).
- `etebase_server/fastapi/sendfile/` is vendored third-party code — don't edit.
- Docker image (`docker/etebase/Dockerfile`) defaults to DHI bases: the runtime stage has no shell or package manager, so all setup happens in the builder. pip is uninstalled from the venv before copying, so scanners don't flag its vendored libs — keep it that way.

## Testing

- `etebase_server/tests/` runs in-process against the ASGI app via a hand-rolled wire-protocol client (`protocol_client.py`); every test needs `pytest.mark.django_db`.
- Live-Redis tests (`test_websocket_redis.py`) are skipped unless `ETEBASE_REDIS_URI` is set, e.g. `podman run -d -p 6390:6379 docker.io/library/redis:8-alpine` + `ETEBASE_REDIS_URI=redis://127.0.0.1:6390/0`. Conversely, `test_subscription_ticket_requires_redis` only runs without it.

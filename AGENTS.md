# AGENTS.md

Etebase 2.0 server (Python). Fork of <https://github.com/etesync/server>; `upstream` remote tracks the original — keep changes compatible with upstream conventions.

## Architecture

- Hybrid Django + FastAPI. Django is the ORM/admin layer; FastAPI serves the actual EteSync v2 API.
- ASGI entrypoint `etebase_server/asgi.py`: the FastAPI app handles `/api/v1/*` and mounts the Django ASGI app at `/` (admin, success page). Run it with `uvicorn etebase_server.asgi:application`; plain Django views will never see API traffic.
- Django apps: `etebase_server.myauth` (custom `User` model, `AUTH_USER_MODEL = "myauth.User"`), `etebase_server.django` (core models + `django.token_auth`). All core models live in `etebase_server/django/models.py`.
- API docs are served by FastAPI at `/docs` when running via uvicorn.
- Signup/login is zero-knowledge (PyNaCl + msgpack), not Django auth. Users are created from the Django admin UI without passwords; API auth is a token in the `Authorization` header, resolved in `fastapi/dependencies.py`.

## Setup & commands

- Create the venv with the system Python (`/usr/bin/python3 -m venv .venv && source .venv/bin/activate`), then `pip install -r requirements.txt -r requirements-dev.txt`. The venv currently runs Python 3.14; deps are Django 5.2.x + FastAPI 0.141.x.
- Dev tools (`ruff`, `mypy`, `django-stubs`) come from `requirements-dev.txt`. Both requirements files now pin a compatible `django==5.2.x`, so installing them together works. `requirements.in/{base,development}.txt` are the pip-compile sources. When regenerating, pass `--upgrade` (e.g. `.venv/bin/pip-compile --upgrade --output-file=requirements.txt requirements.in/base.txt`), otherwise pip-compile treats the existing output pins as constraints and keeps stale versions.
- Deploy steps: `./manage.py migrate`, `./manage.py collectstatic`, then run uvicorn.
- Config is read by `etebase_server/settings.py`: optional `etebase-server.ini` (repo root, `/etc/etebase-server/`, or env `ETEBASE_EASY_CONFIG_PATH`), env `ETEBASE_DB_PATH` for sqlite path, and a gitignored `etebase_server_settings.py` module for final overrides. `secret.txt` (gitignored) is auto-generated for `SECRET_KEY`. Defaults: `DEBUG=True`, sqlite.
- Signup is blocked by default (`ETEBASE_CREATE_USER_FUNC = "etebase_server.django.utils.create_user_blocked"`); comment that line in settings.py to allow it.

## Conventions & gotchas

- FastAPI runs async outside Django's request cycle, so Django never cleans up DB connections. Any new FastAPI dependency/endpoint that touches the ORM must be wrapped with `@django_db_cleanup_decorator` from `etebase_server/fastapi/db_hack.py` — check `dependencies.py` for the pattern.
- Responses and routes use `MsgpackResponse` / `MsgpackRoute` (`fastapi/msgpack.py`), not plain JSON. Errors go through `CustomHttpException`/`HttpError`. `MsgpackRoute` must resolve FastAPI's "effective route context" (`_effective_route_context_var`) in `get_route_handler` — since FastAPI 0.137 the prefix of `include_router` is no longer visible on the route itself, and skipping that makes path params (e.g. `collection_uid`) look like query params.
- The `/api/v1/ws/*` websocket endpoints require Redis (`ETEBASE_REDIS_URI`); they raise `NotSupported` without it. Note two Python 3.14 / redis-py 8 fixes applied in `fastapi/routers/websocket.py`: `asyncio.wait` needs tasks (bare coroutines are rejected in 3.14), and `pubsub.get_message` returns a dict, not a `(channel, data)` tuple.
- `etebase_server/fastapi/sendfile/` is vendored third-party code (own LICENSE/README) — avoid editing. Use `SENDFILE_BACKEND` for file serving.
- Style: ruff + black, line-length 120, double quotes; ruff excludes `migrations/` dirs. Run `ruff check .` and `mypy .` (mypy uses the django-stubs plugin via `mypy.ini`). Note: `mypy .` currently reports ~54 pre-existing errors (mostly django-stubs typing in the routers); they predate this fork's dependency bump.
- Django migrations are checked in under each app's `migrations/` dir; generate new ones with `./manage.py makemigrations`.

## Testing

- Test suite lives in `etebase_server/tests/` (pytest + pytest-django + pytest-asyncio), run in-process against the ASGI app. Run it with `.venv/bin/python -m pytest`; config is in `pytest.ini` (collection is scoped via `testpaths = etebase_server/tests`, `django_debug_mode = keep` so `settings.DEBUG` stays True).
- The suite uses a hand-rolled wire-protocol client (`etebase_server/tests/protocol_client.py`) plus `http_client`/`account` fixtures in `conftest.py`. It needs `settings.ALLOWED_HOSTS=["*"]`, an existing `STATIC_ROOT`, and `ETEBASE_CREATE_USER_FUNC=None` (set in conftest before the app import). Every test uses `pytest.mark.django_db`.
- The intended smoke-test path is the Docker image `etesync/test-server` (`docker/build.sh`): a pre-migrated, DEBUG, signup-enabled server on port 3735 for client testing.
- Live-Redis websocket coverage lives in `etebase_server/tests/test_websocket_redis.py` and is opt-in: it is skipped unless `ETEBASE_REDIS_URI` is set. To run it locally, start a Redis and point the env var at it, e.g. `podman run -d -p 6390:6379 docker.io/library/redis:8-alpine && ETEBASE_REDIS_URI=redis://127.0.0.1:6390/0 .venv/bin/python -m pytest`. Conversely, `test_websocket.py::test_subscription_ticket_requires_redis` only runs *without* `ETEBASE_REDIS_URI`.
- `etebase_server/myauth/tests.py` is still a stub; the real API coverage is the FastAPI-level suite above.
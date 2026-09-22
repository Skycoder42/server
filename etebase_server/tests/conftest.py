import os
import tempfile
import uuid

import pytest
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "etebase_server.settings")

# The ASGI app is built once, at import time, so settings that affect the app
# wiring must be in place before we import it (see etebase_server/asgi.py).
settings.ALLOWED_HOSTS = ["*"]

# StaticFiles mount fails if the directory doesn't exist at mount time.
settings.STATIC_ROOT = tempfile.mkdtemp(prefix="etebase-static-")

# Signup is blocked by default; enable it for the tests. CREATE_USER_FUNC is
# read lazily (and cached for the process), so this must happen before any
# signup request. A None value falls through to User.objects.create_user (this
# is what the docker production image does with AUTO_SIGNUP=true).
settings.ETEBASE_CREATE_USER_FUNC = None

# Opt-in Redis (e.g. for the websocket smoke tests): wire ETEBASE_REDIS_URI
# into settings before the app is built, as redisw/app_settings cache it.
etebase_redis_uri = os.environ.get("ETEBASE_REDIS_URI")
if etebase_redis_uri:
    settings.ETEBASE_REDIS_URI = etebase_redis_uri

from etebase_server.asgi import application  # noqa: E402


@pytest.fixture
async def http_client():
    import httpx

    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def username() -> str:
    return f"user-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def password() -> str:
    return "correct horse battery staple"


@pytest.fixture
async def account(http_client, username, password):
    from .protocol_client import EteClient

    client = EteClient(http_client, username, password)
    resp = await client.signup()
    assert resp.status_code == 200
    return client

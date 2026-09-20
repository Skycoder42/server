import os

import pytest

from .protocol_client import msgpack_unpack

pytestmark = pytest.mark.django_db

no_redis = pytest.mark.skipif(
    os.environ.get("ETEBASE_REDIS_URI") is not None,
    reason="This test assumes Redis is not configured",
)


@no_redis
async def test_subscription_ticket_requires_redis(account):
    """The websocket feature needs Redis; without it the ticket endpoint is 501."""
    assert (await account.create_collection("col-ws")).status_code == 201
    resp = await account.subscription_ticket("col-ws")
    assert resp.status_code == 501
    assert msgpack_unpack(resp.content)["code"] == "not_implemented"


async def test_subscription_ticket_requires_membership(http_client, username, password):
    from .protocol_client import EteClient

    outsider = EteClient(http_client, "outsider-" + username, password)
    assert (await outsider.signup()).status_code == 200
    resp = await outsider.subscription_ticket("col-does-not-exist")
    assert resp.status_code == 404

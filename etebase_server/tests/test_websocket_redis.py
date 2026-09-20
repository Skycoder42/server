"""Live-Redis websocket smoke tests (opt-in).

These exercise the real pub/sub path, so they need a reachable Redis:
set ``ETEBASE_REDIS_URI`` (e.g. ``redis://localhost:6390/0``, see the
podman smoke instructions in AGENTS.md) before running pytest. Without it,
the whole module is skipped (matching the default no-Redis test runs).
"""

import os

import pytest

from .protocol_client import make_item, msgpack_unpack

pytestmark = pytest.mark.django_db

requires_redis = pytest.mark.skipif(
    os.environ.get("ETEBASE_REDIS_URI") is None,
    reason="Set ETEBASE_REDIS_URI to run the live-Redis websocket smoke tests",
)

WS_PATH = "/api/v1/ws"


async def _ws_connect(ticket: str):
    from asgiref.testing import ApplicationCommunicator

    from etebase_server.asgi import application

    path = f"{WS_PATH}/{ticket}/"
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    comm = ApplicationCommunicator(application, scope)
    await comm.send_input({"type": "websocket.connect"})
    return comm


@requires_redis
async def test_websocket_initial_sync_and_pubsub(account):
    from etebase_server.fastapi.redis import redisw

    # The lifespan (which sets up redis) isn't run by the in-process app, so do it here.
    await redisw.setup()
    try:
        collection_uid = "col-ws-redis"
        assert (await account.create_collection(collection_uid)).status_code == 201
        col = msgpack_unpack((await account.collection_get(collection_uid)).content)
        item = make_item("it-ws-redis", content=b"value-redis")
        assert (await account.item_batch(collection_uid, [item], stoken=col["stoken"])).status_code == 200

        resp = await account.subscription_ticket(collection_uid)
        assert resp.status_code == 200
        ticket = msgpack_unpack(resp.content)["ticket"]

        comm = await _ws_connect(ticket)

        # Handshake: accept, then a first sync of existing items.
        accept = await comm.receive_output()
        assert accept["type"] == "websocket.accept"
        first = await comm.receive_output()
        assert first["type"] == "websocket.send"
        payload = msgpack_unpack(first["bytes"])
        assert payload["done"] is True
        assert item["uid"] in [x["uid"] for x in payload["data"]]

        # Pub/sub round-trip: a message published to the collection channel is
        # relayed to the connected websocket.
        await redisw.redis.publish(f"col.{collection_uid}", b"hello-webpush")
        relayed = await comm.receive_output()
        assert relayed["type"] == "websocket.send"
        assert relayed["bytes"] == b"hello-webpush"

        # Clean shutdown: the server closes the socket once the client disconnects.
        await comm.send_input({"type": "websocket.disconnect"})
        await comm.wait(timeout=5)
    finally:
        await redisw.close()

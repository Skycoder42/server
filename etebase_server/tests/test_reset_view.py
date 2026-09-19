import pytest

from .protocol_client import EteClient, msgpack_unpack

pytestmark = pytest.mark.django_db


async def test_reset_requires_debug(http_client, username, password, settings):
    client = EteClient(http_client, "test_user-" + username, password)
    assert (await client.signup()).status_code == 200

    settings.DEBUG = False
    resp = await client.reset()
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "generic"


async def test_reset_only_for_test_users(http_client, username, password):
    client = EteClient(http_client, "regular-" + username, password)
    assert (await client.signup()).status_code == 200

    resp = await client.reset()
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "generic"


async def test_reset_unknown_user(http_client, username, password):
    """Reset requires an existing user.

    The reset view uses ``django.shortcuts.get_object_or_404`` which raises an
    unhandled ``Http404``, so the server currently answers with a 500.
    """
    client = EteClient(http_client, "test_user-" + username, password)
    resp = await client.reset()
    assert resp.status_code == 500


async def test_reset_and_relogin(http_client, username, password):
    client = EteClient(http_client, "test_user-" + username, password)
    assert (await client.signup()).status_code == 200

    assert (await client.create_collection("col-reset")).status_code == 201
    listing = msgpack_unpack((await client.collection_list()).content)
    assert len(listing["data"]) == 1

    # Reset wipes the user's data but keeps the account.
    assert (await client.reset()).status_code == 204
    listing = msgpack_unpack((await client.collection_list()).content)
    assert listing["data"] == []

    # The user can log in again afterwards.
    relogin = EteClient(http_client, "test_user-" + username, password)
    assert (await relogin.login()).status_code == 200

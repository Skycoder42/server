import pytest

from .protocol_client import EteClient, make_item, msgpack_unpack

pytestmark = pytest.mark.django_db

# AccessLevels: 0 READ_ONLY, 1 ADMIN, 2 READ_WRITE


async def _setup_pair(http_client, account, username, password, collection_uid):
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    await account.invite(collection_uid, other.username, access_level=2)

    inv = msgpack_unpack((await other.incoming_list()).content)
    assert len(inv["data"]) == 1
    inv_uid = inv["data"][0]["uid"]
    assert (await other.incoming_accept(inv_uid)).status_code == 201
    return other


async def test_member_list_and_add(account, http_client, username, password):
    collection_uid = "col-members"
    other = await _setup_pair(http_client, account, username, password, collection_uid)

    members = msgpack_unpack((await account.member_list(collection_uid)).content)
    names = {m["username"]: m["accessLevel"] for m in members["data"]}
    assert names[account.username] == 1  # creator is admin
    assert names[other.username] == 2  # invitee is read-write
    assert members["done"] is True


async def test_member_patch_access_level(account, http_client, username, password):
    collection_uid = "col-members-patch"
    other = await _setup_pair(http_client, account, username, password, collection_uid)

    members = msgpack_unpack((await account.member_list(collection_uid)).content)
    other_uid = [m for m in members["data"] if m["username"] == other.username][0]["username"]

    # Downgrade to read-only and verify.
    assert (await account.member_patch(collection_uid, other_uid, 0)).status_code == 204
    after = msgpack_unpack((await account.member_list(collection_uid)).content)
    level = {m["username"]: m["accessLevel"] for m in after["data"]}[other.username]
    assert level == 0

    # A read-only member can no longer write.
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)
    resp = await other.item_batch(collection_uid, [make_item("write-attempt")], stoken=col["stoken"])
    assert resp.status_code == 403
    assert msgpack_unpack(resp.content)["code"] == "no_write_access"


async def test_member_delete_by_admin(account, http_client, username, password):
    collection_uid = "col-members-remove"
    other = await _setup_pair(http_client, account, username, password, collection_uid)

    assert (await account.member_delete(collection_uid, other.username)).status_code == 204
    members = msgpack_unpack((await account.member_list(collection_uid)).content)
    assert all(m["username"] != other.username for m in members["data"])

    # The removed member loses access.
    resp = await other.collection_get(collection_uid)
    assert resp.status_code == 404


async def test_member_leave(account, http_client, username, password):
    collection_uid = "col-members-leave"
    other = await _setup_pair(http_client, account, username, password, collection_uid)

    assert (await other.member_leave(collection_uid)).status_code == 204
    resp = await other.collection_get(collection_uid)
    assert resp.status_code == 404


async def test_members_only_visible_to_admin(account, http_client, username, password):
    collection_uid = "col-members-admin"
    other = await _setup_pair(http_client, account, username, password, collection_uid)

    resp = await other.member_list(collection_uid)
    assert resp.status_code == 403
    assert msgpack_unpack(resp.content)["code"] == "admin_access_required"


async def test_non_member_cannot_list_members(http_client, username, password):
    outsider = EteClient(http_client, "outsider-" + username, password)
    assert (await outsider.signup()).status_code == 200
    resp = await outsider.member_list("col-does-not-exist")
    assert resp.status_code == 404

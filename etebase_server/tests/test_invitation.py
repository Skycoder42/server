import pytest

from .protocol_client import EteClient, msgpack_unpack

pytestmark = pytest.mark.django_db


async def test_invite_flow(account, http_client, username, password):
    collection_uid = "col-inv-flow"
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    inv_uid = "inv-1"
    assert (await account.invite(collection_uid, other.username, uid=inv_uid)).status_code == 201

    # Visible as outgoing for the admin.
    outgoing = msgpack_unpack((await account.outgoing_list()).content)
    assert any(x["uid"] == inv_uid for x in outgoing["data"])

    # Visible (with details) as incoming for the invitee.
    incoming = msgpack_unpack((await other.incoming_list()).content)
    inv = [x for x in incoming["data"] if x["uid"] == inv_uid]
    assert len(inv) == 1
    assert inv[0]["collection"] == collection_uid
    assert inv[0]["fromUsername"] == account.username
    assert inv[0]["signedEncryptionKey"]

    got = msgpack_unpack((await other.incoming_get(inv_uid)).content)
    assert got["uid"] == inv_uid

    # Accepting adds the member.
    collection_key = b"new-member-key" * 2
    assert (await other.incoming_accept(inv_uid, encryption_key=collection_key)).status_code == 201

    members = msgpack_unpack((await account.member_list(collection_uid)).content)
    assert any(m["username"] == other.username for m in members["data"])
    # The invitation is consumed.
    after = msgpack_unpack((await other.incoming_list()).content)
    assert not any(x["uid"] == inv_uid for x in after["data"])

    # The new member sees the collection with the key they supplied.
    fetched = msgpack_unpack((await other.collection_get(collection_uid)).content)
    assert fetched["collectionKey"] == collection_key


async def test_invite_self_rejected(account):
    collection_uid = "col-inv-self"
    assert (await account.create_collection(collection_uid)).status_code == 201
    resp = await account.invite(collection_uid, account.username)
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "no_self_invite"


async def test_invite_unknown_user(account):
    collection_uid = "col-inv-nouser"
    assert (await account.create_collection(collection_uid)).status_code == 201
    resp = await account.invite(collection_uid, "no-such-user")
    assert resp.status_code == 404
    assert msgpack_unpack(resp.content)["code"] == "does_not_exist"


async def test_invite_duplicate(account, http_client, username, password):
    collection_uid = "col-inv-dup"
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    assert (await account.invite(collection_uid, other.username, uid="dup-inv")).status_code == 201
    resp = await account.invite(collection_uid, other.username, uid="dup-inv")
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "invitation_exists"


async def test_incoming_decline(account, http_client, username, password):
    collection_uid = "col-inv-decline"
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    inv_uid = "inv-decline"
    assert (await account.invite(collection_uid, other.username, uid=inv_uid)).status_code == 201

    assert (await other.incoming_delete(inv_uid)).status_code == 204
    after = msgpack_unpack((await other.incoming_list()).content)
    assert not any(x["uid"] == inv_uid for x in after["data"])

    # Declining removes the invitation entirely (it's the same DB row), so the
    # admin no longer sees it as outgoing and no membership was created.
    outgoing = msgpack_unpack((await account.outgoing_list()).content)
    assert not any(x["uid"] == inv_uid for x in outgoing["data"])
    members = msgpack_unpack((await account.member_list(collection_uid)).content)
    assert all(m["username"] != other.username for m in members["data"])


async def test_outgoing_cancel(account, http_client, username, password):
    collection_uid = "col-inv-cancel"
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    inv_uid = "inv-cancel"
    assert (await account.invite(collection_uid, other.username, uid=inv_uid)).status_code == 201

    assert (await account.outgoing_delete(inv_uid)).status_code == 204
    outgoing = msgpack_unpack((await account.outgoing_list()).content)
    assert not any(x["uid"] == inv_uid for x in outgoing["data"])
    incoming = msgpack_unpack((await other.incoming_list()).content)
    assert not any(x["uid"] == inv_uid for x in incoming["data"])


async def test_fetch_user_profile(account, http_client, username, password):
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    profile = msgpack_unpack((await account.fetch_user_profile(other.username)).content)
    assert profile["pubkey"]

    resp = await account.fetch_user_profile("no-such-user")
    assert resp.status_code == 404


async def test_non_admin_cannot_invite(account, http_client, username, password):
    collection_uid = "col-inv-nonadmin"
    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200

    assert (await account.create_collection(collection_uid)).status_code == 201
    # Grant read-write (not admin) via an invitation.
    assert (await account.invite(collection_uid, other.username, access_level=2)).status_code == 201
    inv = msgpack_unpack((await other.incoming_list()).content)["data"][0]
    assert (await other.incoming_accept(inv["uid"])).status_code == 201

    third = EteClient(http_client, "third-" + username, password)
    assert (await third.signup()).status_code == 200
    resp = await other.invite(collection_uid, third.username)
    assert resp.status_code == 403
    assert msgpack_unpack(resp.content)["code"] == "admin_access_required"

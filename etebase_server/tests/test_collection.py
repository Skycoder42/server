import pytest

from .protocol_client import msgpack_unpack

pytestmark = pytest.mark.django_db


async def test_create_collection(account):
    uid = "col-basic-create"
    resp = await account.create_collection(uid)
    assert resp.status_code == 201


async def test_collection_list_and_get(account):
    uid = "col-list-get"
    collection_type = b"calendar"
    collection_key = b"k" * 32
    content = b"hello world"
    assert (
        await account.create_collection(
            uid, collection_type=collection_type, collection_key=collection_key, content=content
        )
    ).status_code == 201

    listing = msgpack_unpack((await account.collection_list()).content)
    assert listing["done"] is True
    assert listing["stoken"] is not None
    found = [x for x in listing["data"] if x["item"]["uid"] == uid]
    assert len(found) == 1

    col = found[0]
    assert col["item"]["uid"] == uid
    assert col["item"]["version"] == 1
    # The creator is an admin.
    assert col["accessLevel"] == 1
    assert col["collectionType"] == collection_type
    assert col["collectionKey"] == collection_key
    # content comes back in the item revision (prefetch=auto).
    assert col["item"]["content"]["chunks"][0][1] == content

    fetched = msgpack_unpack((await account.collection_get(uid)).content)
    assert fetched["item"]["uid"] == uid
    assert fetched["stoken"] == col["stoken"]


async def test_create_collection_duplicate_uid(account):
    uid = "col-dup"
    assert (await account.create_collection(uid)).status_code == 201
    resp = await account.create_collection(uid)
    assert resp.status_code == 409
    assert msgpack_unpack(resp.content)["code"] == "unique_uid"


async def test_list_multi_filters_by_type(account):
    uid = "col-multi"
    collection_type = b"note"
    assert (
        await account.create_collection(uid, collection_type=collection_type, collection_key=b"0" * 32)
    ).status_code == 201

    matching = msgpack_unpack((await account.collection_list_multi([collection_type])).content)
    assert any(x["item"]["uid"] == uid for x in matching["data"])

    other = msgpack_unpack((await account.collection_list_multi([b"other"])).content)
    assert all(x["item"]["uid"] != uid for x in other["data"])


async def test_collection_list_for_other_user(account, http_client, username, password):
    """A collection is only visible to its members."""
    from .protocol_client import EteClient

    uid = "col-private"
    assert (await account.create_collection(uid)).status_code == 201

    other = EteClient(http_client, "other-" + username, password)
    assert (await other.signup()).status_code == 200
    listing = msgpack_unpack((await other.collection_list()).content)
    assert not any(x["item"]["uid"] == uid for x in listing["data"])

    # And fetching it directly fails.
    resp = await other.collection_get(uid)
    assert resp.status_code == 404
    assert msgpack_unpack(resp.content)["code"] == "does_not_exist"


async def test_item_batch_add_and_update(account):
    collection_uid = "col-items"
    assert (await account.create_collection(collection_uid)).status_code == 201
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)
    stoken = col["stoken"]

    from .protocol_client import make_item

    item = make_item("item-1", content=b"v1")
    resp = await account.item_batch(collection_uid, [item], stoken=stoken)
    assert resp.status_code == 200

    items = msgpack_unpack((await account.item_list(collection_uid)).content)
    assert items["done"] is True
    created = [x for x in items["data"] if x["uid"] == "item-1"]
    assert len(created) == 1
    assert created[0]["content"]["chunks"][0][1] == b"v1"
    item_etag = created[0]["content"]["uid"]

    # Update without a valid etag succeeds for batch (only transaction checks etags).
    updated = make_item(
        "item-1",
        etag=None,
        revision={"uid": "rev-new", "meta": b"", "deleted": False, "chunks": [["chunk-new", b"v2"]]},
    )
    assert (await account.item_batch(collection_uid, [updated], stoken=items["stoken"])).status_code == 200

    again_list = msgpack_unpack((await account.item_list(collection_uid)).content)
    current = [x for x in again_list["data"] if x["uid"] == "item-1"][0]
    assert current["content"]["chunks"][0][1] == b"v2"
    assert current["content"]["uid"] != item_etag


async def test_item_transaction_requires_etag(account):
    collection_uid = "col-transact"
    assert (await account.create_collection(collection_uid)).status_code == 201
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)
    stoken = col["stoken"]

    from .protocol_client import make_item

    assert (
        await account.item_batch(collection_uid, [make_item("t-1", content=b"v1")], stoken=stoken)
    ).status_code == 200
    list1 = msgpack_unpack((await account.item_list(collection_uid)).content)
    current = [x for x in list1["data"] if x["uid"] == "t-1"][0]
    etag = current["content"]["uid"]

    # Correct etag: succeeds.
    ok = make_item(
        "t-1", etag=etag, revision={"uid": "rev-ok", "meta": b"", "deleted": False, "chunks": [["chunk-ok", b"v2"]]}
    )
    assert (await account.item_transaction(collection_uid, [ok], stoken=list1["stoken"])).status_code == 200

    # Stale etag: rejected with a nested "wrong_etag" item error. A fresh
    # stoken is required for the request to reach etag validation.
    list2 = msgpack_unpack((await account.item_list(collection_uid)).content)
    stale = make_item(
        "t-1",
        etag="rev-too-old",
        revision={"uid": "rev-stale", "meta": b"", "deleted": False, "chunks": [["chunk-stale", b"v3"]]},
    )
    resp = await account.item_transaction(collection_uid, [stale], stoken=list2["stoken"])
    assert resp.status_code == 409
    content = msgpack_unpack(resp.content)
    assert content["code"] == "item_failed"
    assert content["errors"][0]["code"] == "wrong_etag"


async def test_stale_stoken_rejected(account):
    collection_uid = "col-stoken"
    assert (await account.create_collection(collection_uid)).status_code == 201
    first = msgpack_unpack((await account.collection_get(collection_uid)).content)
    stoken = first["stoken"]

    from .protocol_client import make_item

    # Use the collection, invalidating the stored stoken.
    assert (await account.item_batch(collection_uid, [make_item("s-1")], stoken=stoken)).status_code == 200

    # Re-using the old stoken for a write is now rejected.
    resp = await account.item_batch(collection_uid, [make_item("s-2")], stoken=stoken)
    assert resp.status_code == 409
    assert msgpack_unpack(resp.content)["code"] == "stale_stoken"

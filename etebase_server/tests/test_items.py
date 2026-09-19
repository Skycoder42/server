import pytest

from .protocol_client import make_item, msgpack_unpack

pytestmark = pytest.mark.django_db


async def _collection_with_items(account, collection_uid, n):
    assert (await account.create_collection(collection_uid)).status_code == 201
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)
    items = [make_item(f"it-{i}", content=f"value-{i}".encode()) for i in range(n)]
    assert (await account.item_batch(collection_uid, items, stoken=col["stoken"])).status_code == 200
    return col


async def test_item_list_pagination(account):
    collection_uid = "col-items-page"
    await _collection_with_items(account, collection_uid, 5)

    page1 = msgpack_unpack((await account.item_list(collection_uid, limit=2)).content)
    assert page1["done"] is False
    assert len(page1["data"]) == 2
    page1_stoken = page1["stoken"]
    assert page1_stoken is not None

    page2 = msgpack_unpack((await account.item_list(collection_uid, limit=2, stoken=page1_stoken)).content)
    assert page2["done"] is False
    assert len(page2["data"]) == 2

    page3 = msgpack_unpack((await account.item_list(collection_uid, limit=2, stoken=page2["stoken"])).content)
    assert page3["done"] is True
    assert len(page3["data"]) == 1

    seen = [x["uid"] for x in page1["data"] + page2["data"] + page3["data"]]
    assert seen == [f"it-{i}" for i in range(5)]


async def test_item_get_and_fetch_updates(account):
    collection_uid = "col-items-fetch"
    await _collection_with_items(account, collection_uid, 2)

    item = msgpack_unpack((await account.item_get(collection_uid, "it-0")).content)
    assert item["uid"] == "it-0"
    assert item["content"]["chunks"][0][1] == b"value-0"
    etag = item["content"]["uid"]

    # No update yet: nothing to return.
    no_change = msgpack_unpack(
        (await account.item_fetch_updates(collection_uid, [{"uid": "it-0", "etag": etag}])).content
    )
    assert no_change["data"] == []

    # A different etag means the item changed: it must come back.
    changed = msgpack_unpack(
        (await account.item_fetch_updates(collection_uid, [{"uid": "it-0", "etag": "rev-stale"}])).content
    )
    assert [x["uid"] for x in changed["data"]] == ["it-0"]


async def test_fetch_updates_too_many_items(account):
    collection_uid = "col-items-limit"
    await _collection_with_items(account, collection_uid, 1)

    many = [{"uid": f"nope-{i}", "etag": "x"} for i in range(201)]
    resp = await account.item_fetch_updates(collection_uid, many)
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "too_many_items"


async def test_invalid_stoken_rejected(account):
    collection_uid = "col-items-badstoken"
    await _collection_with_items(account, collection_uid, 1)
    resp = await account.item_list(collection_uid, stoken="not-a-real-stoken")
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "bad_stoken"


async def test_item_revision_history(account):
    collection_uid = "col-items-revs"
    assert (await account.create_collection(collection_uid)).status_code == 201
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)

    assert (
        await account.item_batch(collection_uid, [make_item("r-1", content=b"v1")], stoken=col["stoken"])
    ).status_code == 200
    list1 = msgpack_unpack((await account.item_list(collection_uid)).content)
    etag = [x for x in list1["data"] if x["uid"] == "r-1"][0]["content"]["uid"]

    updated = make_item(
        "r-1",
        etag=etag,
        revision={"uid": "rev-2", "meta": b"", "deleted": False, "chunks": [["chunk-2", b"v2"]]},
    )
    assert (await account.item_transaction(collection_uid, [updated], stoken=list1["stoken"])).status_code == 200

    # Revision listing lives under /item/<uid>/revision/
    resp = await account._request("GET", f"/api/v1/collection/{collection_uid}/item/r-1/revision/")
    assert resp.status_code == 200
    history = msgpack_unpack(resp.content)
    assert history["done"] is True
    uids = [x["uid"] for x in history["data"]]
    assert "rev-2" in uids
    assert uids.index("rev-2") < uids.index(etag)


async def test_chunk_upload_and_download(account):
    collection_uid = "col-items-chunk"
    await _collection_with_items(account, collection_uid, 1)

    chunk_uid = "chunk-uploaded"
    payload = b"the quick brown fox" * 1000
    resp = await account.chunk_upload(collection_uid, "it-0", chunk_uid, payload)
    assert resp.status_code == 201

    # The chunk is stored, and a new revision can reference it.
    col = msgpack_unpack((await account.collection_get(collection_uid)).content)
    new_rev = make_item(
        "it-0",
        revision={"uid": "rev-chunk", "meta": b"", "deleted": False, "chunks": [[chunk_uid, None]]},
    )
    assert (await account.item_batch(collection_uid, [new_rev], stoken=col["stoken"])).status_code == 200

    download = await account.chunk_download(collection_uid, "it-0", chunk_uid)
    assert download.status_code == 200
    assert download.content == payload


async def test_chunk_duplicate_upload_rejected(account):
    collection_uid = "col-items-chunkdup"
    await _collection_with_items(account, collection_uid, 1)

    assert (await account.chunk_upload(collection_uid, "it-0", "chunk-a", b"x")).status_code == 201
    resp = await account.chunk_upload(collection_uid, "it-0", "chunk-a", b"y")
    assert resp.status_code == 409
    assert msgpack_unpack(resp.content)["code"] == "chunk_exists"


async def test_missing_chunk_download_404(account):
    collection_uid = "col-items-chunkmissing"
    await _collection_with_items(account, collection_uid, 1)
    resp = await account.chunk_download(collection_uid, "it-0", "chunk-nope")
    assert resp.status_code == 404

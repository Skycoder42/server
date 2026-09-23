#!/usr/bin/env python3
"""E2E compatibility driver for the skycoder42/etebase-server fork.

Phase 1 (`seed`) drives a *variety* of Etebase 2.0 wire-protocol states into a
server provided by the latest published `victorrds/etesync` image and records a
JSON snapshot of everything (every collection, item, revision, chunk, member,
pending invitation and stoken).

Phase 3 (`verify`) points at the *same data* served by the fork's production
image `skycoder42/etebase-server:v0.14.2-10-g4690e0c`, re-logs-in every user, asserts the
read-back matches the snapshot exactly (no data loss / broken states), and then
*continues on* the data (item updates, trashing/restoring, new collections,
accepting pending invitations, member changes, password change, incremental
sync via the fresh stokens captured during the read-back). Stokens are opaque
server-signed cursors, so after the image switch they are compared only by
their usefulness (that they still work for incremental sync), not by value.

The driver reuses the repo's own wire-level test client
(`etebase_server/tests/protocol_client.py`); content encryption is opaque to
the server, which is exactly what is being verified.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from etebase_server.tests import protocol_client as pc  # noqa: E402
from etebase_server.tests.protocol_client import EteClient, msgpack_unpack  # noqa: E402

PASSWORD = pc.DEFAULT_PASSWORD
PORT = int(os.environ.get("COMPAT_PORT", "3785"))
HOST = "127.0.0.1"
BASE_URL = f"http://{HOST}:{PORT}"
SNAPSHOT_PATH = Path(__file__).parent / "data" / "snapshot.json"


def enc(b: bytes) -> str:
    return base64.b64encode(b).decode()


def dec(s: str) -> bytes:
    return base64.b64decode(s.encode())


def enc_chunks(chunks):
    return [[uid, enc(content) if content is not None else None] for uid, content in chunks]


def collection_key(uid: str) -> bytes:
    return hashlib.sha256(f"key-{uid}".encode()).digest()


def collection_type(uid: str) -> bytes:
    """Unique per-collection type token (real clients generate these randomly)."""
    return b"ctype." + hashlib.sha256(uid.encode()).digest()[:16]


def member_type(uid: str, username: str) -> bytes:
    """Per-member collection type token (real clients generate one per member)."""
    return b"ct." + hashlib.sha256(f"{uid}:{username}".encode()).digest()[:16]


class Ctx:
    def __init__(self):
        self.clients: dict[str, EteClient] = {}
        self.snap: dict = {}
        self.count = 0


CTX = Ctx()


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)
    CTX.count += 1


def assert_same(path: str, actual, expected):
    if actual != expected:
        raise AssertionError(f"{path}: mismatch\n  expected: {expected!r}\n  actual:   {actual!r}")
    CTX.count += 1


def require(resp, status: int, what: str = "", code: str | None = None):
    if resp.status_code != status:
        raise AssertionError(f"{what}: expected HTTP {status}, got {resp.status_code}: {resp.content[:300]!r}")
    if code is not None:
        body = msgpack_unpack(resp.content)
        if body.get("code") != code:
            raise AssertionError(f"{what}: expected code {code!r}, got {body.get('code')!r}: {body!r}")
    CTX.count += 1
    return resp


async def make_client(username: str, password: str = PASSWORD, *, host: str = HOST) -> EteClient:
    http = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)
    return EteClient(http, username, password, host=host)


def col(uid: str) -> dict:
    return CTX.snap["collections"][uid]


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


async def refresh_items(owner: str, uid: str):
    listing = msgpack_unpack((await CTX.clients[owner].item_list(uid)).content)
    items = col(uid)["items"]
    for x in listing["data"]:
        content = x["content"]
        items[x["uid"]] = {
            "version": x["version"],
            "etag": content["uid"],
            "deleted": bool(content["deleted"]),
            "meta": enc(content["meta"]),
            "chunks": enc_chunks(content["chunks"]),
        }
    col(uid)["stoken"] = listing["stoken"]


async def refresh_members(owner: str, uid: str):
    raw = msgpack_unpack((await CTX.clients[owner].member_list(uid)).content)["data"]
    col(uid)["members"] = {m["username"]: m["accessLevel"] for m in raw}


async def create_collection(owner: str, uid: str, ctype: str, *, meta: bytes = b"", content: bytes = b""):
    c = CTX.clients[owner]
    resp = await c.create_collection(
        uid, collection_type=collection_type(uid), collection_key=collection_key(uid), meta=meta, content=content
    )
    require(resp, 201, what=f"create collection {uid}")
    CTX.snap["collections"][uid] = {
        "uid": uid,
        "owner": owner,
        "type": ctype,
        "items": {},
        "revisions": {},
    }
    raw = msgpack_unpack((await c.collection_get(uid)).content)
    item = raw["item"]
    col(uid)["type"] = enc(raw["collectionType"])
    col(uid)["key"] = enc(raw["collectionKey"])
    col(uid)["root"] = {
        "version": item["version"],
        "etag": item["content"]["uid"],
        "deleted": bool(item["content"]["deleted"]),
        "meta": enc(item["content"]["meta"]),
        "chunks": enc_chunks(item["content"]["chunks"]),
    }
    col(uid)["stoken"] = raw["stoken"]
    await refresh_members(owner, uid)


async def add_item(owner: str, uid: str, item_uid: str, *, meta: bytes = b"", content: bytes = b""):
    c = CTX.clients[owner]
    raw = msgpack_unpack((await c.collection_get(uid)).content)
    rev_uid = f"rev-{item_uid}-1"
    revision = {"uid": rev_uid, "meta": meta, "deleted": False, "chunks": [[f"chunk-{item_uid}-i", content]]}
    resp = await c.item_batch(uid, [pc.make_item(item_uid, revision=revision)], stoken=raw["stoken"])
    require(resp, 200, what=f"add item {item_uid} to {uid}")
    await refresh_items(owner, uid)
    col(uid)["revisions"][item_uid] = [rev_uid]


async def update_item(owner: str, uid: str, item_uid: str, rev_uid: str, *, deleted: bool = False, content: bytes = b""):
    c = CTX.clients[owner]
    listing = msgpack_unpack((await c.item_list(uid)).content)
    current = next(x for x in listing["data"] if x["uid"] == item_uid)
    revision = {"uid": rev_uid, "meta": current["content"]["meta"], "deleted": deleted, "chunks": [[f"chunk-{item_uid}-{rev_uid}", content]]}
    resp = await c.item_transaction(
        uid, [pc.make_item(item_uid, etag=current["content"]["uid"], revision=revision)], stoken=listing["stoken"]
    )
    require(resp, 200, what=f"transaction update {item_uid} -> {rev_uid}")
    await refresh_items(owner, uid)
    col(uid)["revisions"][item_uid].insert(0, rev_uid)


async def upload_chunk(owner: str, uid: str, item_uid: str, chunk_uid: str, payload: bytes):
    c = CTX.clients[owner]
    resp = await c.chunk_upload(uid, item_uid, chunk_uid, payload)
    require(resp, 201, what=f"upload chunk {chunk_uid}")
    raw = msgpack_unpack((await c.collection_get(uid)).content)
    revision = {"uid": f"rev-{item_uid}-chunk", "meta": b"", "deleted": False, "chunks": [[chunk_uid, None]]}
    resp = await c.item_batch(uid, [pc.make_item(item_uid, revision=revision)], stoken=raw["stoken"])
    require(resp, 200, what=f"attach chunk {chunk_uid} to {item_uid}")
    await refresh_items(owner, uid)
    CTX.snap["chunks"][chunk_uid] = enc(payload)


async def invite_and_accept(owner: str, uid: str, invitee: str, *, access_level: int = 2) -> str:
    c = CTX.clients[owner]
    invitee_client = CTX.clients[invitee]
    inv_uid = f"inv-{owner}-{invitee}-{uid}"
    resp = await c.invite(uid, invitee, access_level=access_level, uid=inv_uid)
    require(resp, 201, what=f"invite {invitee} to {uid}")
    key = hashlib.sha256(f"{uid}:{invitee}".encode()).digest()
    resp = await invitee_client.incoming_accept(
        inv_uid, collection_type=member_type(uid, invitee), encryption_key=key
    )
    require(resp, 201, what=f"{invitee} accepts {inv_uid}")
    await refresh_members(owner, uid)
    return inv_uid


async def leave_pending(owner: str, uid: str, invitee: str, *, access_level: int = 2) -> str:
    inv_uid = f"inv-{owner}-{invitee}-{uid}"
    resp = await CTX.clients[owner].invite(uid, invitee, access_level=access_level, uid=inv_uid)
    require(resp, 201, what=f"leave pending invite {inv_uid}")
    CTX.snap["pending"].append(
        {"uid": inv_uid, "from": owner, "to": invitee, "collection": uid, "accessLevel": access_level}
    )
    return inv_uid


async def soft_delete_collection(owner: str, uid: str, rev_uid: str):
    """Trash a whole collection by soft-deleting its root item."""
    c = CTX.clients[owner]
    raw = msgpack_unpack((await c.collection_get(uid)).content)
    item = raw["item"]
    revision = {"uid": rev_uid, "meta": item["content"]["meta"], "deleted": True, "chunks": item["content"]["chunks"]}
    resp = await c.item_transaction(
        uid, [pc.make_item(uid, version=item["version"], etag=item["content"]["uid"], revision=revision)],
        stoken=raw["stoken"],
    )
    require(resp, 200, what=f"soft-delete collection {uid}")

    # Record how the seed server treats the deleted collection so phase 3 can
    # assert the exact same behaviour after the image switch.
    get_resp = await c.collection_get(uid)
    if get_resp.status_code == 200:
        await refresh_items(owner, uid)
        col(uid)["stoken"] = msgpack_unpack(get_resp.content)["stoken"]
    in_list = any(x["item"]["uid"] == uid for x in msgpack_unpack((await c.collection_list()).content)["data"])
    col(uid)["terminal"] = {
        "get_status": get_resp.status_code,
        "get_deleted": bool(msgpack_unpack(get_resp.content)["item"]["content"]["deleted"])
        if get_resp.status_code == 200
        else None,
        "in_list": in_list,
    }


async def snapshot_revision_lists(owner: str, uid: str):
    c = CTX.clients[owner]
    for item_uid in list(col(uid)["items"]):
        resp = await c._request("GET", f"{pc.COLLECTION_PATH}/{uid}/item/{item_uid}/revision/")
        require(resp, 200, what=f"revision list for {item_uid}")
        history = msgpack_unpack(resp.content)["data"]
        col(uid)["revisions"][item_uid] = [h["uid"] for h in history]


# ---------------------------------------------------------------------------
# Phase 1: seed
# ---------------------------------------------------------------------------


async def seed_negative_checks():
    c = CTX.clients

    # duplicate signup
    require(await c["alice"].signup(), 409, "duplicate signup", "user_exists")
    # wrong password / unknown user login
    require(await (await make_client("alice", "wrong-" + PASSWORD)).login(), 401, "login wrong password", "login_bad_signature")
    require(await (await make_client("nobody-knows-this-user")).login(), 401, "login unknown user", "user_not_found")
    # logout invalidates the token
    carol_clone = await make_client("carol")
    require(await carol_clone.login(), 200, "carol login for logout test")
    require(await carol_clone.logout(), 204, "carol logout")
    check((await carol_clone.collection_list()).status_code in (401, 403), "logout invalidates token")
    # dashboard_url is not supported
    require(
        await c["alice"]._request("POST", f"{pc.BASE_PATH}/authentication/dashboard_url/"),
        400,
        "dashboard_url",
        "not_supported",
    )
    # host mismatch login is rejected outside debug mode
    challenge = msgpack_unpack((await c["alice"]._request("POST", f"{pc.AUTH_PATH}/login_challenge/", data={"username": "alice"})).content)
    login_key = pc.derive_login_signing_key(PASSWORD)
    message = pc.msgpack_pack(
        {"username": "alice", "challenge": challenge["challenge"], "host": "some-other-host", "action": "login"}
    )
    signature = login_key.sign(message).signature
    require(
        await c["alice"]._request("POST", f"{pc.AUTH_PATH}/login/", data={"response": message, "signature": signature}),
        400,
        "login host mismatch",
        "wrong_host",
    )
    # self-invite and unknown-user invite rejected
    require(await c["alice"].invite("col-calendar", "alice"), 400, "self invite", "no_self_invite")
    require(await c["alice"].invite("col-calendar", "no-such-user"), 404, "invite unknown user", "does_not_exist")
    # fetch user profile
    profile = msgpack_unpack((await c["alice"].fetch_user_profile("bob")).content)
    check(bool(profile["pubkey"]), "fetch_user_profile returns pubkey")
    require(
        await c["alice"]._request(
            "GET", f"{pc.INVITATION_PATH}/outgoing/fetch_user_profile/", params={"username": "no-such-user"}
        ),
        404,
        "fetch_user_profile unknown user",
        "does_not_exist",
    )
    # non-admin cannot invite or list members
    require(await c["carol"].invite("col-shared", "dave"), 403, "non-admin invite", "admin_access_required")
    require(await c["carol"].member_list("col-shared"), 403, "non-admin member list", "admin_access_required")
    # stale etag transaction rejected
    listing = msgpack_unpack((await c["alice"].item_list("col-calendar")).content)
    stale = pc.make_item(
        "cal-event-1", etag="rev-too-old", revision={"uid": "rev-stale", "meta": b"", "deleted": False, "chunks": [["chunk-stale", b"x"]]}
    )
    resp = await c["alice"].item_transaction("col-calendar", [stale], stoken=listing["stoken"])
    body = msgpack_unpack(resp.content)
    check(
        resp.status_code == 409 and body.get("code") == "item_failed" and body["errors"][0]["code"] == "wrong_etag",
        "stale etag rejected",
    )
    # stale stoken rejected on a write
    old_stoken = CTX.snap["page_old_stoken"]
    require(
        await c["alice"].item_batch("col-page", [pc.make_item("stale-write")], stoken=old_stoken),
        409,
        "stale stoken write",
        "stale_stoken",
    )
    # missing chunk -> 404
    require(await c["alice"].chunk_download("col-calendar", "cal-event-1", "chunk-nope"), 404, "missing chunk", "does_not_exist")


async def seed():
    CTX.snap = {
        "users": {},
        "collections": {},
        "chunks": {},
        "pending": [],
        "declined": [],
        "canceled": [],
        "user_stokens": {},
        "notes": {"host": HOST, "port": PORT},
    }

    # ---- users ---------------------------------------------------------
    await signup("alice", email="alice@example.com")
    await signup("bob")
    await signup("carol", email="carol@example.com")
    await signup("dave")

    # ---- alice's private calendar: item updates + trash/restore + chunk -
    await create_collection("alice", "col-calendar", "calendar", content=b"alice's calendar")
    await add_item("alice", "col-calendar", "cal-event-1", content=b"event 1 v1")
    await update_item("alice", "col-calendar", "cal-event-1", "rev-cal-event-1-2", content=b"event 1 v2")
    await add_item("alice", "col-calendar", "cal-event-2", meta=b'{"tz":"eu"}', content=b"event 2")
    await upload_chunk("alice", "col-calendar", "cal-event-long", "chunk-cal-long", b"the quick brown fox" * 500)
    # trash, then restore (both are new revisions that must persist)
    await update_item("alice", "col-calendar", "cal-event-2", "rev-cal-event-2-del", deleted=True, content=b"event 2")
    await update_item("alice", "col-calendar", "cal-event-2", "rev-cal-event-2-restore", content=b"event 2 (kept)")
    await snapshot_revision_lists("alice", "col-calendar")

    # ---- pagination on a small collection ------------------------------
    await create_collection("alice", "col-page", "notes", content=b"pagination")
    CTX.snap["page_old_stoken"] = msgpack_unpack((await CTX.clients["alice"].collection_get("col-page")).content)["stoken"]
    for i in range(5):
        await add_item("alice", "col-page", f"page-{i}", content=f"page item {i}".encode())

    # ---- alice's shared addressbook: bob RW, carol RO, dave pending ----
    await create_collection("alice", "col-shared", "addressbook", content=b"shared addressbook")
    await invite_and_accept("alice", "col-shared", "bob", access_level=2)
    await invite_and_accept("alice", "col-shared", "carol", access_level=0)
    await leave_pending("alice", "col-shared", "dave", access_level=2)
    # carol is read-only: writing must fail
    raw = msgpack_unpack((await CTX.clients["alice"].collection_get("col-shared")).content)
    require(
        await CTX.clients["carol"].item_batch("col-shared", [pc.make_item("carol-forbidden")], stoken=raw["stoken"]),
        403,
        "carol RO write to col-shared",
        "no_write_access",
    )
    await add_item("alice", "col-shared", "shared-note-1", content=b"shared note")

    # ---- alice's notes collection with a peer writer --------------------
    await create_collection("alice", "col-notes", "notes", content=b"alice's notes")
    await invite_and_accept("alice", "col-notes", "bob", access_level=2)
    raw = msgpack_unpack((await CTX.clients["bob"].collection_get("col-notes")).content)
    require(
        await CTX.clients["bob"].item_batch("col-notes", [pc.make_item("bob-note-1", content=b"note from bob")], stoken=raw["stoken"]),
        200,
        "bob writes to col-notes",
    )
    await refresh_items("alice", "col-notes")
    # carol declines a pending invitation (a "minus" state that must persist)
    require(await CTX.clients["alice"].invite("col-notes", "carol", access_level=0, uid="inv-alice-carol-col-notes"), 201, "invite carol (declined)")
    require(await CTX.clients["carol"].incoming_delete("inv-alice-carol-col-notes"), 204, "carol declines invitation")
    CTX.snap["declined"].append("inv-alice-carol-col-notes")

    # ---- bob's collections: joins + removal + canceled invitation ------
    await create_collection("bob", "col-bob-notes", "custom-type-1", content=b"bob's notes")
    await invite_and_accept("bob", "col-bob-notes", "alice", access_level=2)
    await invite_and_accept("bob", "col-bob-notes", "dave", access_level=2)
    require(await CTX.clients["bob"].member_delete("col-bob-notes", "dave"), 204, "bob removes dave")
    await refresh_members("bob", "col-bob-notes")
    await create_collection("bob", "col-bob-share", "custom-type-2", content=b"bob's share trial")
    require(
        await CTX.clients["bob"].invite("col-bob-share", "dave", access_level=2, uid="inv-bob-dave-col-bob-share"),
        201,
        "bob invites dave to col-bob-share",
    )
    require(await CTX.clients["bob"].outgoing_delete("inv-bob-dave-col-bob-share"), 204, "bob cancels invitation")
    CTX.snap["canceled"].append("inv-bob-dave-col-bob-share")

    # ---- carol's collection: pending invitation to bob (for phase 3) ----
    await create_collection("carol", "col-carol", "notes", content=b"carol's notes")
    await leave_pending("carol", "col-carol", "bob", access_level=2)

    # ---- dave's collection with a large external chunk -----------------
    await create_collection("dave", "col-dave", "calendar", content=b"dave's calendar")
    await upload_chunk("dave", "col-dave", "dave-large", "chunk-dave-large", b"dave's big payload chunk " * 2000)

    # ---- a soft-deleted collection whose content is preserved -----------
    await create_collection("bob", "col-deleted", "calendar", content=b"to be deleted")
    await add_item("bob", "col-deleted", "gone-item", content=b"will vanish")
    await soft_delete_collection("bob", "col-deleted", "rev-col-deleted-del")

    await seed_negative_checks()

    # Capture the final user-level collection list stokens after every write.
    for name, client in CTX.clients.items():
        listing = msgpack_unpack((await client.collection_list()).content)
        CTX.snap["user_stokens"][name] = listing["stoken"]

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(CTX.snap, f, indent=2, sort_keys=True)
    print(f"seed OK ({CTX.count} checks); snapshot at {SNAPSHOT_PATH}")


async def signup(name: str, *, email: str | None = None) -> EteClient:
    client = await make_client(name)
    require(await client.signup(email=email), 200, what=f"signup {name}")
    require(await client.collection_list(), 200, what=f"collection list after signup {name}")
    CTX.clients[name] = client
    CTX.snap["users"][name] = {"password": PASSWORD, "email": email}
    return client


# ---------------------------------------------------------------------------
# Phase 3: verify
# ---------------------------------------------------------------------------


async def login_all():
    for name, info in CTX.snap["users"].items():
        client = await make_client(name, info["password"])
        require(await client.login(), 200, what=f"login {name}")
        check(client.token is not None, f"login {name} sets token")
        CTX.clients[name] = client


async def readback_snapshot():
    """Re-state every collection and compare against the phase-1 snapshot."""
    for uid, snap in CTX.snap["collections"].items():
        owner = snap["owner"]
        c = CTX.clients[owner]

        listing = msgpack_unpack((await c.collection_list()).content)
        listed_uids = {x["item"]["uid"] for x in listing["data"]}
        in_list = uid in listed_uids

        if "terminal" in snap:
            get_resp = await c.collection_get(uid)
            exp = snap["terminal"]
            check(get_resp.status_code == exp["get_status"], f"readback {uid}: get status after soft-delete")
            if exp["get_status"] == 200:
                check(
                    bool(msgpack_unpack(get_resp.content)["item"]["content"]["deleted"]) == exp["get_deleted"],
                    f"readback {uid}: deleted flag",
                )
            assert_same(f"readback {uid}.terminal.in_list", in_list, exp["in_list"])
            continue

        members = msgpack_unpack((await c.member_list(uid)).content)["data"]
        assert_same(f"readback {uid}.members", {m["username"]: m["accessLevel"] for m in members}, snap.get("members", {}))
        check(in_list, f"readback {uid}: missing from the collection list of {owner}")

        fetched = msgpack_unpack((await c.collection_get(uid)).content)
        assert_same(f"readback {uid}.type", enc(fetched["collectionType"]), snap["type"])
        assert_same(f"readback {uid}.key", enc(fetched["collectionKey"]), snap["key"])

        expected_root = snap["root"]
        root_content = fetched["item"]["content"]
        assert_same(f"readback {uid}.root.etag", root_content["uid"], expected_root["etag"])
        assert_same(f"readback {uid}.root.deleted", bool(root_content["deleted"]), expected_root["deleted"])
        assert_same(f"readback {uid}.root.meta", enc(root_content["meta"]), expected_root["meta"])
        assert_same(f"readback {uid}.root.chunks", enc_chunks(root_content["chunks"]), expected_root["chunks"])
        assert_same(f"readback {uid}.root.version", fetched["item"]["version"], expected_root["version"])

        items_list = msgpack_unpack((await c.item_list(uid)).content)["data"]
        items_actual = {x["uid"]: x for x in items_list}
        expected_items = snap.get("items", {})
        assert_same(f"readback {uid}.item uids", sorted(items_actual), sorted(expected_items))
        for item_uid, exp in expected_items.items():
            cur = items_actual[item_uid]["content"]
            assert_same(f"readback {uid}.{item_uid}.etag", cur["uid"], exp["etag"])
            assert_same(f"readback {uid}.{item_uid}.deleted", bool(cur["deleted"]), exp["deleted"])
            assert_same(f"readback {uid}.{item_uid}.meta", enc(cur["meta"]), exp["meta"])
            assert_same(f"readback {uid}.{item_uid}.chunks", enc_chunks(cur["chunks"]), exp["chunks"])
            assert_same(f"readback {uid}.{item_uid}.version", items_actual[item_uid]["version"], exp["version"])

        for item_uid, exp_revs in snap.get("revisions", {}).items():
            history = msgpack_unpack(
                (await c._request("GET", f"{pc.COLLECTION_PATH}/{uid}/item/{item_uid}/revision/")).content
            )["data"]
            assert_same(f"readback {uid}.{item_uid}.revisions", [h["uid"] for h in history], exp_revs)

        # Stokens are server-secret-signed opaque cursors, so their exact value is
        # not comparable across the image switch; only their usefulness is (see
        # continue_and_check). Every piece of *content* above already matched.
        stoken_actual = fetched["stoken"]
        check(isinstance(stoken_actual, str) and stoken_actual, f"readback {uid}.stoken present")


async def readback_users_and_invites():
    CTX.snap["_verify_user_stokens"] = {}
    for name in CTX.snap["user_stokens"]:
        listing = msgpack_unpack((await CTX.clients[name].collection_list()).content)
        CTX.snap["_verify_user_stokens"][name] = listing["stoken"]
        check(bool(listing["stoken"]), f"readback user_stokens.{name} present")

    for inv in CTX.snap["pending"]:
        outgoing = msgpack_unpack((await CTX.clients[inv["from"]].outgoing_list()).content)["data"]
        check(any(x["uid"] == inv["uid"] for x in outgoing), f"readback outgoing {inv['uid']} still listed")
        incoming = msgpack_unpack((await CTX.clients[inv["to"]].incoming_list()).content)["data"]
        entry = [x for x in incoming if x["uid"] == inv["uid"]]
        check(len(entry) == 1, f"readback incoming {inv['uid']} still listed")
        assert_same(f"readback incoming {inv['uid']}.collection", entry[0]["collection"], inv["collection"])
        assert_same(f"readback incoming {inv['uid']}.from", entry[0]["fromUsername"], inv["from"])
        assert_same(f"readback incoming {inv['uid']}.accessLevel", entry[0]["accessLevel"], inv["accessLevel"])
        check(bool(entry[0]["signedEncryptionKey"]), f"readback incoming {inv['uid']}.signedEncryptionKey set")

    for uid in CTX.snap.get("declined", []):
        outgoing = msgpack_unpack((await CTX.clients["alice"].outgoing_list()).content)["data"]
        incoming = msgpack_unpack((await CTX.clients["carol"].incoming_list()).content)["data"]
        check(all(x["uid"] != uid for x in outgoing), f"readback declined {uid} absent from outgoing")
        check(all(x["uid"] != uid for x in incoming), f"readback declined {uid} absent from incoming")
    for uid in CTX.snap.get("canceled", []):
        outgoing = msgpack_unpack((await CTX.clients["bob"].outgoing_list()).content)["data"]
        incoming = msgpack_unpack((await CTX.clients["dave"].incoming_list()).content)["data"]
        check(all(x["uid"] != uid for x in outgoing), f"readback canceled {uid} absent from outgoing")
        check(all(x["uid"] != uid for x in incoming), f"readback canceled {uid} absent from incoming")

    require(await CTX.clients["dave"].collection_get("col-shared"), 404, "readback dave access while pending", "does_not_exist")

    for chunk_uid, payload in CTX.snap["chunks"].items():
        if chunk_uid.startswith("chunk-cal"):
            owner, uid, item_uid = "alice", "col-calendar", "cal-event-long"
        else:
            owner, uid, item_uid = "dave", "col-dave", "dave-large"
        resp = await CTX.clients[owner].chunk_download(uid, item_uid, chunk_uid)
        require(resp, 200, f"readback chunk download {chunk_uid}")
        assert_same(f"readback chunk {chunk_uid}.payload", resp.content, dec(payload))


async def readback_stokens_for_continue():
    """Capture stokens AFTER the read-back and BEFORE any continue-write."""
    for uid, snap in CTX.snap["collections"].items():
        if "terminal" in snap:
            continue
        owner = snap["owner"]
        listing = msgpack_unpack((await CTX.clients[owner].item_list(uid)).content)
        snap["_readback_stoken"] = listing["stoken"]


async def continue_and_check():
    alice, bob, carol, dave = (CTX.clients[n] for n in ("alice", "bob", "carol", "dave"))

    # 1. update cal-event-1 (v2 -> v3) and verify the new content is served
    listing = msgpack_unpack((await alice.item_list("col-calendar")).content)
    current = next(x for x in listing["data"] if x["uid"] == "cal-event-1")
    revision = {"uid": "rev-cal-event-1-3", "meta": b"", "deleted": False, "chunks": [["chunk-cal-1-v3", b"event 1 v3"]]}
    require(
        await alice.item_transaction(
            "col-calendar", [pc.make_item("cal-event-1", etag=current["content"]["uid"], revision=revision)], stoken=listing["stoken"]
        ),
        200,
        "continue: cal-event-1 -> v3",
    )
    listing = msgpack_unpack((await alice.item_list("col-calendar")).content)
    check(
        next(x for x in listing["data"] if x["uid"] == "cal-event-1")["content"]["chunks"][0][1] == b"event 1 v3",
        "continue: cal-event-1 content is v3",
    )

    # 2. new item, then trash it, then restore it
    raw = msgpack_unpack((await alice.collection_get("col-calendar")).content)
    require(
        await alice.item_batch(
            "col-calendar",
            [pc.make_item("cal-event-3", revision={"uid": "rev-cal-event-3", "meta": b"", "deleted": False, "chunks": [["chunk-cal-3", b"event 3"]]})],
            stoken=raw["stoken"],
        ),
        200,
        "continue: add cal-event-3",
    )
    listing = msgpack_unpack((await alice.item_list("col-calendar")).content)
    etag3 = next(x for x in listing["data"] if x["uid"] == "cal-event-3")["content"]["uid"]
    require(
        await alice.item_transaction(
            "col-calendar",
            [pc.make_item("cal-event-3", etag=etag3, revision={"uid": "rev-cal-event-3-del", "meta": b"", "deleted": True, "chunks": [["chunk-cal-3", b"event 3"]]})],
            stoken=listing["stoken"],
        ),
        200,
        "continue: trash cal-event-3",
    )
    listing = msgpack_unpack((await alice.item_list("col-calendar")).content)
    check(next(x for x in listing["data"] if x["uid"] == "cal-event-3")["content"]["deleted"] is True, "continue: cal-event-3 in trash")
    etag3 = next(x for x in listing["data"] if x["uid"] == "cal-event-3")["content"]["uid"]
    require(
        await alice.item_transaction(
            "col-calendar",
            [pc.make_item("cal-event-3", etag=etag3, revision={"uid": "rev-cal-event-3-restore", "meta": b"", "deleted": False, "chunks": [["chunk-cal-3", b"event 3"]]})],
            stoken=listing["stoken"],
        ),
        200,
        "continue: restore cal-event-3",
    )

    # 3. brand new collection, invite, accept, write as the new member
    require(
        await alice.create_collection(
            "col-new", collection_type=collection_type("col-new"), collection_key=collection_key("col-new"), content=b"fresh collection"
        ),
        201,
        "continue: create col-new",
    )
    require(await alice.invite("col-new", "carol", access_level=2, uid="inv-alice-carol-col-new"), 201, "continue: invite carol to col-new")
    require(
        await carol.incoming_accept(
            "inv-alice-carol-col-new", collection_type=member_type("col-new", "carol"), encryption_key=collection_key("col-new")
        ),
        201,
        "continue: carol accepts col-new",
    )
    raw = msgpack_unpack((await carol.collection_get("col-new")).content)
    require(
        await carol.item_batch("col-new", [pc.make_item("carol-item", content=b"carol's first write")], stoken=raw["stoken"]),
        200,
        "continue: carol writes into col-new",
    )

    # 4. accept the two invitations left pending in phase 1
    require(
        await dave.incoming_accept(
            "inv-alice-dave-col-shared", collection_type=member_type("col-shared", "dave"), encryption_key=collection_key("col-shared")
        ),
        201,
        "continue: dave accepts pending col-shared invite",
    )
    require(
        await bob.incoming_accept(
            "inv-carol-bob-col-carol", collection_type=member_type("col-carol", "bob"), encryption_key=collection_key("col-carol")
        ),
        201,
        "continue: bob accepts pending col-carol invite",
    )
    raw = msgpack_unpack((await bob.collection_get("col-carol")).content)
    require(
        await bob.item_batch("col-carol", [pc.make_item("bob-in-col-carol", content=b"bob writes to carol's collection")], stoken=raw["stoken"]),
        200,
        "continue: bob writes into col-carol",
    )

    # 5. carol is still read-only in col-shared; then gets upgraded
    raw = msgpack_unpack((await alice.collection_get("col-shared")).content)
    require(
        await carol.item_batch("col-shared", [pc.make_item("carol-still-ro")], stoken=raw["stoken"]),
        403,
        "continue: carol still read-only in col-shared",
        "no_write_access",
    )
    require(await alice.member_patch("col-shared", "carol", 2), 204, "continue: upgrade carol to RW in col-shared")
    raw = msgpack_unpack((await alice.collection_get("col-shared")).content)
    require(
        await carol.item_batch("col-shared", [pc.make_item("carol-now-rw", content=b"after upgrade")], stoken=raw["stoken"]),
        200,
        "continue: carol writes after upgrade",
    )

    # 6. bob leaves col-notes; alice removes carol from col-new
    require(await bob.member_leave("col-notes"), 204, "continue: bob leaves col-notes")
    require(await bob.collection_get("col-notes"), 404, "continue: bob lost access to col-notes", "does_not_exist")
    require(await alice.member_delete("col-new", "carol"), 204, "continue: alice removes carol from col-new")
    require(await carol.collection_get("col-new"), 404, "continue: carol lost access to col-new", "does_not_exist")

    # 7. password change: new works, old stops working
    require(await dave.change_password("a brand new dave password"), 204, "continue: dave changes password")
    require(await (await make_client("dave", PASSWORD)).login(), 401, "continue: dave old password rejected", "login_bad_signature")
    require(await (await make_client("dave", "a brand new dave password")).login(), 200, "continue: dave new password works")

    # 8. the read-back stokens still allow incremental sync after the writes
    expected = {
        "col-calendar": ["cal-event-1", "cal-event-3"],
        "col-shared": ["carol-now-rw"],
        "col-carol": ["bob-in-col-carol"],
    }
    for uid, want in expected.items():
        snap = col(uid)
        listing = msgpack_unpack((await CTX.clients[snap["owner"]].item_list(uid, stoken=snap["_readback_stoken"])).content)
        changed = {x["uid"] for x in listing["data"]}
        for item_uid in want:
            check(item_uid in changed, f"continue: incremental list of {uid} includes {item_uid}")

    # 9. user-level collection-list stoken from read-back syncs col-new
    listing = msgpack_unpack(
        (await alice.collection_list(stoken=CTX.snap["_verify_user_stokens"]["alice"])).content
    )
    check("col-new" in {x["item"]["uid"] for x in listing["data"]}, "continue: col-new seen via read-back collection stoken")


async def verify_final_state():
    for name in CTX.snap["users"]:
        require(await CTX.clients[name].collection_list(), 200, f"final collection list {name}")

    members_new = {m["username"] for m in msgpack_unpack((await CTX.clients["alice"].member_list("col-new")).content)["data"]}
    check("alice" in members_new and "carol" not in members_new, "final: col-new members are {alice} only")
    levels_shared = {
        m["username"]: m["accessLevel"] for m in msgpack_unpack((await CTX.clients["alice"].member_list("col-shared")).content)["data"]
    }
    check(levels_shared.get("bob") == 2 and levels_shared.get("carol") == 2, "final: col-shared members RW/bob, RW/carol")
    check(any(m["username"] == "dave" for m in msgpack_unpack((await CTX.clients["alice"].member_list("col-shared")).content)["data"]), "final: dave member of col-shared")
    levels_notes = {m["username"] for m in msgpack_unpack((await CTX.clients["alice"].member_list("col-notes")).content)["data"]}
    check("bob" not in levels_notes, "final: bob no longer in col-notes")
    require(await CTX.clients["carol"].collection_get("col-new"), 404, "final: carol no access to col-new", "does_not_exist")


async def verify():
    with open(SNAPSHOT_PATH) as f:
        CTX.snap = json.load(f)
    print("Loaded snapshot from", SNAPSHOT_PATH)

    probe = await make_client("probe")
    require(await probe.http.get("/api/v1/authentication/is_etebase/"), 200, "is_etebase")

    await login_all()
    await readback_snapshot()
    await readback_users_and_invites()
    await readback_stokens_for_continue()
    await continue_and_check()
    await verify_final_state()

    # negative checks still behave the same on the fork image
    require(await (await make_client("alice", PASSWORD)).signup(), 409, "duplicate signup (post-switch)", "user_exists")
    require(await (await make_client("alice", "wrong-" + PASSWORD)).login(), 401, "login wrong password (post-switch)", "login_bad_signature")
    require(
        await CTX.clients["alice"]._request("POST", f"{pc.BASE_PATH}/authentication/dashboard_url/"),
        400,
        "dashboard_url (post-switch)",
        "not_supported",
    )
    # websocket subscription ticket with Redis
    require(await CTX.clients["alice"].subscription_ticket("col-calendar"), 200, "websocket subscription ticket (redis)")

    print(f"verify OK ({CTX.count} checks)")


async def main():
    parser = argparse.ArgumentParser(description="Etebase compatibility driver")
    parser.add_argument("command", choices=["seed", "verify"])
    args = parser.parse_args()
    if args.command == "seed":
        await seed()
    else:
        await verify()


if __name__ == "__main__":
    asyncio.run(main())

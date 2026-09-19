"""Minimal Etebase client used by the test-suite.

This is a *server-side* test helper: it implements just enough of the Etebase 2.0
wire protocol (msgpack + PyNaCl) to drive the server through its real
request/response formats. End-to-end content encryption stays opaque (random
bytes), which is fine -- the server is transparent to E2E encryption, and this
lets the tests assert the server's own behaviour rather than any particular
encryption scheme.

The key exchange implemented here mirrors what a real client does:

* Signup stores a salt, the login signing public key and some opaque content.
* Login fetches a challenge (encrypted by the server with a key derived from the
  account salt) and answers it with a signed ``LoginResponse`` message. The
  server validates the signature against the stored login public key and the
  challenge against the stored salt.
"""

import hashlib
import secrets
import typing as t

import msgpack
import nacl.signing

BASE_PATH = "/api/v1"
AUTH_PATH = f"{BASE_PATH}/authentication"
COLLECTION_PATH = f"{BASE_PATH}/collection"
INVITATION_PATH = f"{BASE_PATH}/invitation"

DEFAULT_PASSWORD = "correct horse battery staple"

PBKDF2_ITERATIONS = 100_000


def msgpack_pack(data: t.Any) -> bytes:
    return msgpack.packb(data, use_bin_type=True)


def msgpack_unpack(data: bytes) -> t.Any:
    return msgpack.unpackb(data, raw=False)


def derive_login_signing_key(password: str) -> nacl.signing.SigningKey:
    """Deterministically derive the account login signing key from the password.

    The exact derivation is a client concern in Etebase -- the server only ever
    sees (and verifies against) the resulting public key. It must be a pure
    function of the password, because the login key is re-derived after a
    password change while the account salt stays fixed.
    """
    seed = hashlib.pbkdf2_hmac("sha256", password.encode(), b"etebase-login", PBKDF2_ITERATIONS, dklen=32)
    return nacl.signing.SigningKey(seed)


def make_revision(*, uid: t.Optional[str] = None, meta: bytes = b"", deleted: bool = False, content: bytes = None):
    """Build a ``CollectionItemRevisionInOut``-shaped payload."""
    content = b"content-" + secrets.token_bytes(16) if content is None else content
    return {
        "uid": uid or f"rev-{secrets.token_hex(8)}",
        "meta": meta,
        "deleted": deleted,
        "chunks": [[f"chunk-{secrets.token_hex(8)}", content]],
    }


def make_item(
    uid: t.Optional[str] = None,
    *,
    etag: t.Optional[str] = None,
    version: int = 1,
    encryption_key: t.Optional[bytes] = None,
    revision: t.Optional[dict] = None,
    deleted: bool = False,
    content: t.Optional[bytes] = None,
):
    """Build a ``CollectionItemIn``-shaped payload."""
    return {
        "uid": uid or f"item-{secrets.token_hex(8)}",
        "version": version,
        "etag": etag,
        "encryptionKey": encryption_key,
        "content": revision or make_revision(deleted=deleted, content=content),
    }


class EteClient:
    """A minimal Etebase protocol client wrapping an httpx client."""

    def __init__(self, http, username: str, password: str = DEFAULT_PASSWORD, *, host: str = "testserver"):
        self.http = http
        self.username = username
        self.password = password
        self.host = host
        self.token: t.Optional[str] = None
        self.user: t.Optional[dict] = None
        self._login_key: t.Optional[nacl.signing.SigningKey] = None
        self._salt: t.Optional[bytes] = None

    # -- low level helpers ---------------------------------------------------

    def _headers(self, extra: t.Optional[dict] = None) -> dict:
        headers = {
            "Content-Type": "application/msgpack",
            "Accept": "application/msgpack",
        }
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    async def _request(self, method: str, path: str, *, data=None, raw_body=None, headers=None, params=None):
        payload = raw_body if raw_body is not None else (msgpack_pack(data) if data is not None else None)
        return await self.http.request(method, path, content=payload, headers=self._headers(headers), params=params)

    def _decode(self, response) -> t.Any:
        return msgpack_unpack(response.content)

    # -- authentication ------------------------------------------------------

    def _signup_body(self, salt: bytes, login_key: nacl.signing.SigningKey, *, email: t.Optional[str] = None) -> dict:
        return {
            "user": {"username": self.username, "email": email or f"{self.username}@example.com"},
            "salt": salt,
            "loginPubkey": login_key.verify_key.encode(),
            "pubkey": secrets.token_bytes(32),
            "encryptedContent": secrets.token_bytes(16),
        }

    async def signup(self, *, email: t.Optional[str] = None):
        salt = secrets.token_bytes(32)
        login_key = derive_login_signing_key(self.password)
        resp = await self._request("POST", f"{AUTH_PATH}/signup/", data=self._signup_body(salt, login_key, email=email))
        if resp.status_code < 300:
            content = self._decode(resp)
            self.token = content["token"]
            self.user = content["user"]
            self._login_key = login_key
            self._salt = salt
        return resp

    async def login(self):
        challenge_resp = await self._request("POST", f"{AUTH_PATH}/login_challenge/", data={"username": self.username})
        if challenge_resp.status_code >= 300:
            return challenge_resp
        challenge = self._decode(challenge_resp)
        salt = challenge["salt"]
        login_key = derive_login_signing_key(self.password)
        message = msgpack_pack(
            {
                "username": self.username,
                "challenge": challenge["challenge"],
                "host": self.host,
                "action": "login",
            }
        )
        signature = login_key.sign(message).signature
        resp = await self._request("POST", f"{AUTH_PATH}/login/", data={"response": message, "signature": signature})
        if resp.status_code < 300:
            content = self._decode(resp)
            self.token = content["token"]
            self.user = content["user"]
            self._login_key = login_key
            self._salt = salt
        return resp

    async def logout(self):
        resp = await self._request("POST", f"{AUTH_PATH}/logout/")
        if resp.status_code == 204:
            self.token = None
        return resp

    async def change_password(self, new_password: str):
        challenge_resp = await self._request("POST", f"{AUTH_PATH}/login_challenge/", data={"username": self.username})
        if challenge_resp.status_code >= 300:
            return challenge_resp
        challenge = self._decode(challenge_resp)
        current_key = derive_login_signing_key(self.password)
        new_salt = secrets.token_bytes(32)
        new_key = derive_login_signing_key(new_password)
        message = msgpack_pack(
            {
                "username": self.username,
                "challenge": challenge["challenge"],
                "host": self.host,
                "action": "changePassword",
                "loginPubkey": new_key.verify_key.encode(),
                "encryptedContent": secrets.token_bytes(16),
            }
        )
        signature = current_key.sign(message).signature
        resp = await self._request(
            "POST", f"{AUTH_PATH}/change_password/", data={"response": message, "signature": signature}
        )
        if resp.status_code == 204:
            self.password = new_password
            self._login_key = new_key
            self._salt = new_salt
        return resp

    # -- debug test helper ---------------------------------------------------

    async def reset(self, *, email: t.Optional[str] = None):
        """Call the DEBUG-only /test/authentication/reset/ endpoint."""
        salt = secrets.token_bytes(32)
        login_key = derive_login_signing_key(self.password)
        body = self._signup_body(salt, login_key, email=email)
        return await self._request("POST", f"{BASE_PATH}/test/authentication/reset/", data=body)

    # -- collections ----------------------------------------------------------

    async def create_collection(
        self,
        uid: t.Optional[str] = None,
        *,
        collection_type: t.Optional[bytes] = None,
        collection_key: t.Optional[bytes] = None,
        meta: bytes = b"",
        content: t.Optional[bytes] = None,
    ):
        uid = uid or f"col-{secrets.token_hex(8)}"
        body = {
            "collectionType": collection_type or secrets.token_bytes(8),
            "collectionKey": collection_key or secrets.token_bytes(32),
            "item": {
                "uid": uid,
                "version": 1,
                "content": make_revision(meta=meta, content=content),
            },
        }
        return await self._request("POST", f"{COLLECTION_PATH}/", data=body)

    async def collection_list(self, *, stoken: t.Optional[str] = None, limit: int = 50):
        params = {"limit": str(limit)}
        if stoken:
            params["stoken"] = stoken
        return await self._request("GET", f"{COLLECTION_PATH}/", params=params)

    async def collection_list_multi(self, collection_types: t.List[bytes]):
        return await self._request("POST", f"{COLLECTION_PATH}/list_multi/", data={"collectionTypes": collection_types})

    async def collection_get(self, collection_uid: str):
        return await self._request("GET", f"{COLLECTION_PATH}/{collection_uid}/")

    # -- items -----------------------------------------------------------------

    async def item_batch(self, collection_uid: str, items: t.List[dict], *, stoken: t.Optional[str] = None, deps=None):
        params = {}
        if stoken:
            params["stoken"] = stoken
        return await self._request(
            "POST",
            f"{COLLECTION_PATH}/{collection_uid}/item/batch/",
            data={"items": items, "deps": deps},
            params=params,
        )

    async def item_transaction(self, collection_uid: str, items: t.List[dict], *, stoken: t.Optional[str] = None):
        params = {}
        if stoken:
            params["stoken"] = stoken
        return await self._request(
            "POST",
            f"{COLLECTION_PATH}/{collection_uid}/item/transaction/",
            data={"items": items, "deps": None},
            params=params,
        )

    async def subscription_ticket(self, collection_uid: str):
        """Get a websocket subscription ticket (requires Redis to succeed)."""
        return await self._request("POST", f"{COLLECTION_PATH}/{collection_uid}/item/subscription-ticket/")

    async def item_list(
        self, collection_uid: str, *, stoken: t.Optional[str] = None, limit: int = 50, with_collection: bool = False
    ):
        params = {"limit": str(limit), "withCollection": str(with_collection).lower()}
        if stoken:
            params["stoken"] = stoken
        return await self._request("GET", f"{COLLECTION_PATH}/{collection_uid}/item/", params=params)

    async def item_get(self, collection_uid: str, item_uid: str):
        return await self._request("GET", f"{COLLECTION_PATH}/{collection_uid}/item/{item_uid}/")

    async def item_fetch_updates(self, collection_uid: str, items: t.List[dict], *, stoken: t.Optional[str] = None):
        params = {}
        if stoken:
            params["stoken"] = stoken
        return await self._request(
            "POST", f"{COLLECTION_PATH}/{collection_uid}/item/fetch_updates/", data=items, params=params
        )

    async def chunk_upload(self, collection_uid: str, item_uid: str, chunk_uid: str, content: bytes):
        headers = {"Content-Type": "application/octet-stream"}
        return await self._request(
            "PUT",
            f"{COLLECTION_PATH}/{collection_uid}/item/{item_uid}/chunk/{chunk_uid}/",
            raw_body=content,
            headers=headers,
        )

    async def chunk_download(self, collection_uid: str, item_uid: str, chunk_uid: str):
        headers = {"Accept": "application/octet-stream"}
        return await self._request(
            "GET", f"{COLLECTION_PATH}/{collection_uid}/item/{item_uid}/chunk/{chunk_uid}/download/", headers=headers
        )

    # -- members ---------------------------------------------------------------

    async def member_list(self, collection_uid: str):
        return await self._request("GET", f"{COLLECTION_PATH}/{collection_uid}/member/")

    async def member_delete(self, collection_uid: str, username: str):
        return await self._request("DELETE", f"{COLLECTION_PATH}/{collection_uid}/member/{username}/")

    async def member_patch(self, collection_uid: str, username: str, access_level: int):
        return await self._request(
            "PATCH", f"{COLLECTION_PATH}/{collection_uid}/member/{username}/", data={"accessLevel": access_level}
        )

    async def member_leave(self, collection_uid: str):
        return await self._request("POST", f"{COLLECTION_PATH}/{collection_uid}/member/leave/")

    # -- invitations -----------------------------------------------------------

    async def invite(
        self,
        collection_uid: str,
        username: str,
        *,
        access_level: int = 2,
        uid: t.Optional[str] = None,
        signed_encryption_key: t.Optional[bytes] = None,
    ):
        body = {
            "uid": uid or f"inv-{secrets.token_hex(8)}",
            "version": 1,
            "accessLevel": access_level,
            "username": username,
            "collection": collection_uid,
            "signedEncryptionKey": signed_encryption_key or secrets.token_bytes(32),
        }
        return await self._request("POST", f"{INVITATION_PATH}/outgoing/", data=body)

    async def outgoing_list(self):
        return await self._request("GET", f"{INVITATION_PATH}/outgoing/")

    async def outgoing_delete(self, invitation_uid: str):
        return await self._request("DELETE", f"{INVITATION_PATH}/outgoing/{invitation_uid}/")

    async def incoming_list(self):
        return await self._request("GET", f"{INVITATION_PATH}/incoming/")

    async def incoming_get(self, invitation_uid: str):
        return await self._request("GET", f"{INVITATION_PATH}/incoming/{invitation_uid}/")

    async def incoming_accept(
        self,
        invitation_uid: str,
        *,
        collection_type: t.Optional[bytes] = None,
        encryption_key: t.Optional[bytes] = None,
    ):
        data = {
            "collectionType": collection_type or secrets.token_bytes(8),
            "encryptionKey": encryption_key or secrets.token_bytes(32),
        }
        return await self._request("POST", f"{INVITATION_PATH}/incoming/{invitation_uid}/accept/", data=data)

    async def incoming_delete(self, invitation_uid: str):
        return await self._request("DELETE", f"{INVITATION_PATH}/incoming/{invitation_uid}/")

    async def fetch_user_profile(self, username: str):
        return await self._request(
            "GET", f"{INVITATION_PATH}/outgoing/fetch_user_profile/", params={"username": username}
        )

import pytest

from .protocol_client import DEFAULT_PASSWORD, EteClient, msgpack_unpack

pytestmark = pytest.mark.django_db


async def test_is_etebase(http_client):
    resp = await http_client.get("/api/v1/authentication/is_etebase/")
    assert resp.status_code == 200


async def test_signup(http_client, username, password):
    client = EteClient(http_client, username, password)
    resp = await client.signup()
    # The endpoint returns its own MsgpackResponse so the declared 201 status
    # is overridden by the response's default 200.
    assert resp.status_code == 200
    assert client.token is not None

    content = msgpack_unpack(resp.content)
    assert content["user"]["username"] == username
    assert content["user"]["pubkey"]
    assert content["user"]["encryptedContent"]

    # The returned token grants access to protected endpoints.
    collections = await client.collection_list()
    assert collections.status_code == 200


async def test_signup_duplicate(http_client, username, password):
    client = EteClient(http_client, username, password)
    assert (await client.signup()).status_code == 200
    resp = await client.signup()
    assert resp.status_code == 409
    assert msgpack_unpack(resp.content)["code"] == "user_exists"


async def test_login(http_client, username, password):
    client = EteClient(http_client, username, password)
    assert (await client.signup()).status_code == 200
    await client.logout()

    login = EteClient(http_client, username, password)
    resp = await login.login()
    assert resp.status_code == 200
    assert login.token is not None


async def test_login_wrong_password(http_client, username, password):
    client = EteClient(http_client, username, password)
    assert (await client.signup()).status_code == 200

    login = EteClient(http_client, username, "wrong-" + password)
    resp = await login.login()
    assert resp.status_code == 401
    assert msgpack_unpack(resp.content)["code"] == "login_bad_signature"


async def test_login_unknown_user(http_client, username):
    client = EteClient(http_client, "nobody-" + username, DEFAULT_PASSWORD)
    resp = await client.login()
    assert resp.status_code == 401
    assert msgpack_unpack(resp.content)["code"] == "user_not_found"


async def test_unauthenticated_request_is_rejected(http_client):
    resp = await http_client.get("/api/v1/collection/", headers={"Accept": "application/msgpack"})
    assert resp.status_code in (401, 403)


async def test_logout_invalidates_token(account):
    resp = await account.logout()
    assert resp.status_code == 204
    assert account.token is None

    # The old token must be invalid now.
    resp = await account.collection_list()
    assert resp.status_code in (401, 403)


async def test_change_password(http_client, username, password):
    client = EteClient(http_client, username, password)
    assert (await client.signup()).status_code == 200

    new_password = "even more correct horse"
    resp = await client.change_password(new_password)
    assert resp.status_code == 204

    await client.logout()

    # The new password logs in.
    relogin = EteClient(http_client, username, new_password)
    assert (await relogin.login()).status_code == 200

    # The old password no longer does.
    oldlogin = EteClient(http_client, username, password)
    assert (await oldlogin.login()).status_code == 401


async def test_dashboard_url_not_supported(account):
    resp = await account._request("POST", "/api/v1/authentication/dashboard_url/")
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "not_supported"


async def test_login_host_mismatch_rejected(http_client, username, password, settings):
    client = EteClient(http_client, username, password)
    assert (await client.signup()).status_code == 200

    settings.DEBUG = False

    challenge_resp = await client._request(
        "POST", "/api/v1/authentication/login_challenge/", data={"username": username}
    )
    assert challenge_resp.status_code == 200
    challenge = msgpack_unpack(challenge_resp.content)
    from .protocol_client import derive_login_signing_key, msgpack_pack

    login_key = derive_login_signing_key(password)
    message = msgpack_pack(
        {
            "username": username,
            "challenge": challenge["challenge"],
            "host": "some-other-host",
            "action": "login",
        }
    )
    signature = login_key.sign(message).signature
    resp = await client._request(
        "POST",
        "/api/v1/authentication/login/",
        data={"response": message, "signature": signature},
    )
    assert resp.status_code == 400
    assert msgpack_unpack(resp.content)["code"] == "wrong_host"

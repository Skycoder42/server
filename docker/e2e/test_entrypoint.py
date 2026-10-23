"""E2E tests for the production image's entrypoint, health check and config generation."""

import re

import httpx
import pytest
from harness import CONFIG_PATH, POSTGRES_PASSWORD, parse_ini

from etebase_server.tests.protocol_client import EteClient

IS_ETEBASE = "/api/v1/authentication/is_etebase/"
STATIC_FILE = "/static/admin/css/base.css"
GENERATED_PASSWORD = re.compile(r"generated superuser password: (\S+)")


# -- configuration -------------------------------------------------------------


def test_default_start(etebase, volume):
    server = etebase.start(volume=volume)
    server.wait_healthy()

    assert parse_ini(server.read(CONFIG_PATH)) == {
        "global": {
            "secret_file": "/data/secret.txt",
            "debug": "False",
            "static_root": "/srv/etebase/static",
            "static_url": "/static/",
            "media_root": "/data/media",
            "media_url": "/user-media/",
            "language_code": "en-us",
            "time_zone": "UTC",
        },
        "database": {"engine": "django.db.backends.sqlite3", "name": "/data/db.sqlite3"},
        "allowed_hosts": {"allowed_host1": "localhost", "allowed_host2": "127.0.0.1", "allowed_host3": "[::1]"},
    }
    for path in ("/data/secret.txt", "/data/db.sqlite3", "/data/media"):
        assert server.exists(path), path

    logs = server.logs()
    assert f"generated {CONFIG_PATH}" in logs
    assert "static files copied to '/srv/etebase/static'" in logs
    assert "applying migrations" in logs
    assert "starting uvicorn etebase_server.asgi:application --host 0.0.0.0 --port 3735" in logs

    assert server.python("import os; print(os.stat('/proc/1').st_uid)").strip() == "65532"
    assert server.server_environ()["ETEBASE_EASY_CONFIG_PATH"] == CONFIG_PATH
    assert server.http_status(IS_ETEBASE) == 200
    assert server.http_status(STATIC_FILE) == 200


def test_config_from_environment(etebase, volume):
    config_path = "/data/conf/server.ini"
    server = etebase.start(
        volume=volume,
        environ={
            "DEBUG_DJANGO": "true",
            "LANGUAGE_CODE": "de-de",
            "TIME_ZONE": "Europe/Berlin",
            "STATIC_ROOT": "/data/static",
            "MEDIA_ROOT": "/data/custom-media",
            "SECRET_FILE": "/data/custom-secret.txt",
            "REDIS_URI": "redis://redis.invalid:6379/0",
            "ALLOWED_HOSTS": " etebase.example.com , 127.0.0.1,,",
            "DATABASE_NAME": "/data/sub/dir",
            "ETEBASE_EASY_CONFIG_PATH": config_path,
        },
    )
    server.wait_healthy()

    assert parse_ini(server.read(config_path)) == {
        "global": {
            "secret_file": "/data/custom-secret.txt",
            "debug": "True",
            "static_root": "/data/static",
            "static_url": "/static/",
            "media_root": "/data/custom-media",
            "media_url": "/user-media/",
            "language_code": "de-de",
            "time_zone": "Europe/Berlin",
            "redis_uri": "redis://redis.invalid:6379/0",
        },
        "database": {"engine": "django.db.backends.sqlite3", "name": "/data/sub/dir/db.sqlite3"},
        "allowed_hosts": {"allowed_host1": "etebase.example.com", "allowed_host2": "127.0.0.1"},
    }
    assert f"generated {config_path}" in server.logs()
    assert not server.exists(CONFIG_PATH)
    for path in ("/data/sub/dir/db.sqlite3", "/data/custom-media", "/data/custom-secret.txt"):
        assert server.exists(path), path
    # collectstatic filled the custom STATIC_ROOT, which the server then serves.
    assert server.exists("/data/static" + STATIC_FILE.removeprefix("/static"))
    assert server.http_status(STATIC_FILE) == 200
    assert server.server_environ()["ETEBASE_EASY_CONFIG_PATH"] == config_path
    # The server process runs with the configured time zone.
    assert server.manage_shell("from django.utils import timezone; print(timezone.localtime().tzname())").strip() in (
        "CET",
        "CEST",
    )


def test_existing_config_is_kept_unless_regenerated(etebase, volume):
    first = etebase.start(volume=volume, environ={"LANGUAGE_CODE": "de-de"})
    first.wait_healthy()
    ini = first.read(CONFIG_PATH)
    secret = first.read("/data/secret.txt")

    second = etebase.replace(first, volume=volume, environ={"LANGUAGE_CODE": "fr-fr"})
    second.wait_healthy()
    assert f"using existing {CONFIG_PATH}" in second.logs()
    assert second.read(CONFIG_PATH) == ini

    third = etebase.replace(second, volume=volume, environ={"LANGUAGE_CODE": "fr-fr", "REGEN_INI": "true"})
    third.wait_healthy()
    assert f"generated {CONFIG_PATH}" in third.logs()
    assert parse_ini(third.read(CONFIG_PATH))["global"]["language_code"] == "fr-fr"
    assert third.read("/data/secret.txt") == secret


def test_empty_variables_count_as_unset(etebase, volume):
    server = etebase.start(
        volume=volume,
        environ={
            "DB_ENGINE": "",
            "DATABASE_NAME": "",
            "LANGUAGE_CODE": "",
            "PORT": "",
            "ETEBASE_EASY_CONFIG_PATH": "",
            "AUTO_MIGRATE": "",
            "SUPER_USER": "admin",
            "SUPER_PASS": "",
            "SUPER_PASS_FILE": "",
        },
    )
    server.wait_healthy()

    ini = parse_ini(server.read(CONFIG_PATH))
    assert ini["database"] == {"engine": "django.db.backends.sqlite3", "name": "/data/db.sqlite3"}
    assert ini["global"]["language_code"] == "en-us"
    assert server.server_environ()["ETEBASE_EASY_CONFIG_PATH"] == CONFIG_PATH

    logs = server.logs()
    assert "applying migrations" in logs
    assert "--port 3735" in logs
    match = GENERATED_PASSWORD.search(logs)
    assert match, logs
    assert server.authenticates("admin", match.group(1))


def test_file_variants(etebase, volume):
    etebase.python_on_volume(
        volume,
        "import os\n"
        "os.makedirs('/data/secrets')\n"
        "open('/data/secrets/user', 'w').write('admin\\n')\n"
        "open('/data/secrets/pass', 'w').write('from file\\n')\n",
    )
    server = etebase.start(
        volume=volume,
        environ={
            "SUPER_USER_FILE": "/data/secrets/user",
            # An empty variable does not conflict with its _FILE variant.
            "SUPER_PASS": "",
            "SUPER_PASS_FILE": "/data/secrets/pass",
        },
    )
    server.wait_healthy()
    assert server.authenticates("admin", "from file")
    assert "generated superuser password" not in server.logs()


def test_variable_and_file_variant_are_exclusive(etebase):
    result = etebase.run(environ={"SUPER_USER": "admin", "SUPER_USER_FILE": "/data/user"})
    assert result.returncode == 1
    assert "both SUPER_USER and SUPER_USER_FILE are set" in result.stderr


def test_unsupported_db_engine(etebase):
    result = etebase.run(environ={"DB_ENGINE": "mysql"})
    assert result.returncode == 1
    assert "DB_ENGINE 'mysql' is not supported" in result.stderr


# -- superuser -----------------------------------------------------------------


def test_superuser_is_created_once(etebase, volume):
    password = 'it\'s "q" 100%'
    first = etebase.start(volume=volume, environ={"SUPER_USER": "admin", "SUPER_PASS": password})
    first.wait_healthy()
    assert first.authenticates("admin", password)
    assert "generated superuser password" not in first.logs()

    # Usernames are case-insensitive, and an existing user is never modified.
    second = etebase.replace(first, volume=volume, environ={"SUPER_USER": "ADMIN", "SUPER_PASS": "other"})
    second.wait_healthy()
    assert "superuser 'ADMIN' already exists; skipping" in second.logs()
    assert second.authenticates("admin", password)
    assert not second.authenticates("admin", "other")
    count = second.manage_shell("from etebase_server.myauth.models import User; print(User.objects.count())")
    assert count.strip() == "1"


def test_superuser_generated_password(etebase, volume):
    first = etebase.start(volume=volume, environ={"SUPER_USER": "admin"})
    first.wait_healthy()
    match = GENERATED_PASSWORD.search(first.logs())
    assert match, first.logs()
    assert first.authenticates("admin", match.group(1))

    second = etebase.replace(first, volume=volume, environ={"SUPER_USER": "admin"})
    second.wait_healthy()
    assert "superuser 'admin' already exists; skipping" in second.logs()
    assert "generated superuser password" not in second.logs()
    assert second.authenticates("admin", match.group(1))


# -- migrations ----------------------------------------------------------------


def test_auto_migrate_disabled(etebase, volume):
    environ = {"AUTO_MIGRATE": "false", "SUPER_USER": "admin", "SUPER_PASS": "secret"}
    # A fresh database is always initialized, so the superuser can be created.
    first = etebase.start(volume=volume, environ=environ)
    first.wait_healthy()
    assert "AUTO_MIGRATE disabled, but the database is empty; applying initial migrations" in first.logs()
    assert first.authenticates("admin", "secret")

    second = etebase.replace(first, volume=volume, environ=environ)
    second.wait_healthy()
    assert "AUTO_MIGRATE disabled; skipping migrations" in second.logs()
    assert "applying" not in second.logs()


@pytest.mark.parametrize("auto_migrate", ["true", "false"])
def test_unreachable_database(etebase, auto_migrate):
    result = etebase.run(
        environ={"DB_ENGINE": "postgres", "DATABASE_HOST": "database.invalid", "AUTO_MIGRATE": auto_migrate}
    )
    assert result.returncode != 0
    if auto_migrate == "false":
        assert "cannot access the database" in result.stderr


# -- PostgreSQL ----------------------------------------------------------------


def test_postgres_defaults(etebase, postgres, volume):
    # Only the password is set: name, user, host and port use their defaults.
    environ = {"DB_ENGINE": "postgres", "DATABASE_PASSWORD": POSTGRES_PASSWORD, "SUPER_USER": "admin"}
    first = etebase.start(volume=volume, network=postgres.network, environ={**environ, "SUPER_PASS": "secret"})
    first.wait_healthy()

    raw = first.read(CONFIG_PATH)
    assert "p%%ss" in raw
    assert parse_ini(raw)["database"] == {
        "engine": "django.db.backends.postgresql",
        "name": "etebase",
        "user": "etebase",
        "password": POSTGRES_PASSWORD,
        "host": "database",
        "port": "5432",
    }
    assert first.authenticates("admin", "secret")
    assert "django_etebase" in postgres.psql("SELECT DISTINCT app FROM django_migrations")

    # The restart that used to crash on the existing superuser.
    second = etebase.replace(first, volume=volume, network=postgres.network, environ=environ)
    second.wait_healthy()
    assert "superuser 'admin' already exists; skipping" in second.logs()


def test_postgres_custom_settings(etebase, postgres, volume):
    postgres.psql("CREATE ROLE ete_custom LOGIN PASSWORD 'c%stom'")
    postgres.psql("CREATE DATABASE ete_custom_db OWNER ete_custom")
    etebase.python_on_volume(
        volume,
        "open('/data/db-engine', 'w').write('postgres\\n')\nopen('/data/db-pass', 'w').write('c%stom\\n')\n",
    )
    server = etebase.start(
        volume=volume,
        network=postgres.network,
        environ={
            "DB_ENGINE_FILE": "/data/db-engine",
            "DATABASE_NAME": "ete_custom_db",
            "DATABASE_USER": "ete_custom",
            "DATABASE_PASSWORD_FILE": "/data/db-pass",
            "DATABASE_HOST": postgres.name,
            "DATABASE_PORT": "5432",
        },
    )
    server.wait_healthy()
    assert parse_ini(server.read(CONFIG_PATH))["database"] == {
        "engine": "django.db.backends.postgresql",
        "name": "ete_custom_db",
        "user": "ete_custom",
        "password": "c%stom",
        "host": postgres.name,
        "port": "5432",
    }
    assert "django_etebase" in postgres.psql("SELECT DISTINCT app FROM django_migrations", database="ete_custom_db")


# -- signup --------------------------------------------------------------------


@pytest.mark.parametrize("auto_signup", [None, "true"])
async def test_signup(etebase, auto_signup):
    server = etebase.start(publish=True, environ={"AUTO_SIGNUP": auto_signup} if auto_signup else {})
    server.wait_healthy()
    if auto_signup:
        assert server.server_environ()["ETEBASE_CREATE_USER_FUNC"] == ""
    else:
        assert "ETEBASE_CREATE_USER_FUNC" not in server.server_environ()

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.published_port()}", timeout=30) as http:
        response = await EteClient(http, "alice", host="127.0.0.1").signup()
        if not auto_signup:
            assert response.status_code >= 400
            return
        assert response.status_code == 200
        assert (await EteClient(http, "alice", host="127.0.0.1").login()).status_code == 200


# -- static files, port and health check ---------------------------------------


def test_read_only_root_filesystem(etebase, volume):
    server = etebase.start(volume=volume, extra=["--read-only"])
    server.wait_healthy()
    assert "STATIC_ROOT /srv/etebase/static is not writable; skipping collectstatic" in server.logs()
    # The static files baked into the image are still served.
    assert server.http_status(STATIC_FILE) == 200


def test_uncreatable_static_root(etebase):
    # A directory below a regular file can never be created.
    result = etebase.run(environ={"STATIC_ROOT": "/app/manage.py/static"})
    assert result.returncode == 1
    assert "STATIC_ROOT /app/manage.py/static does not exist and cannot be created" in result.stderr


def test_custom_port(etebase):
    server = etebase.start(environ={"PORT": "8080"})
    server.wait_healthy()
    assert "--port 8080" in server.logs()
    assert server.http_status(IS_ETEBASE, port=8080) == 200


@pytest.mark.parametrize(
    "allowed_hosts, status_without_host",
    [
        ("localhost,127.0.0.1,[::1]", 200),
        ("*", 200),
        ("etebase.example.com", 400),
        ("*.example.com", 400),
        ("[::1],etebase.example.com", 400),
    ],
)
def test_healthcheck_uses_an_allowed_host(etebase, allowed_hosts, status_without_host):
    server = etebase.start(environ={"ALLOWED_HOSTS": allowed_hosts})
    server.wait_healthy()
    # Shows whether the health check needed its Host header to pass.
    assert server.http_status(IS_ETEBASE) == status_without_host


def test_healthcheck_fails_when_no_host_is_reachable(etebase):
    # The server matches the Host up to the first colon, so IPv6 hosts never match.
    server = etebase.start(environ={"ALLOWED_HOSTS": "[::1]"})
    server.wait_for_log("Uvicorn running on")
    assert server.healthcheck() != 0


# -- users and data ownership --------------------------------------------------


def test_refuses_to_run_as_root(etebase):
    result = etebase.run(user="0:0")
    assert result.returncode == 1
    assert "refusing to run as root" in result.stderr


def test_fix_ownership_requires_root(etebase):
    result = etebase.run(environ={"ETEBASE_FIX_OWNERSHIP": "1"})
    assert result.returncode == 1
    assert "ETEBASE_FIX_OWNERSHIP requires running as root" in result.stderr


def test_unwritable_data_and_fix_ownership(etebase, volume):
    # Simulate a volume written by another image's user, including nested files.
    etebase.python_on_volume(
        volume,
        "import os\n"
        "os.makedirs('/data/nested/deeper')\n"
        "open('/data/nested/deeper/file.txt', 'w').write('x')\n"
        "for root, dirs, files in os.walk('/data'):\n"
        "    for name in [root, *(os.path.join(root, n) for n in dirs + files)]:\n"
        "        os.chown(name, 1234, 1234)\n",
        user="0:0",
    )

    result = etebase.run(volume=volume)
    assert result.returncode == 1
    assert "data directory /data is not writable by uid 65532" in result.stderr
    assert "-e ETEBASE_FIX_OWNERSHIP=1 -v <volume>:/data" in result.stderr
    assert "chown -R 65532:65532 /data" in result.stderr

    # The hint names the user actually running, and how to chown to it.
    result = etebase.run(volume=volume, user="4321:4321")
    assert result.returncode == 1
    assert "not writable by uid 4321" in result.stderr
    assert "-e ETEBASE_FIX_OWNERSHIP=1 -e ETEBASE_UID=4321 -e ETEBASE_GID=4321 -v" in result.stderr
    assert "chown -R 4321:4321 /data" in result.stderr

    result = etebase.run(
        volume=volume,
        user="0:0",
        environ={"ETEBASE_FIX_OWNERSHIP": "1", "ETEBASE_UID": "4321", "ETEBASE_GID": "4321"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ownership fixed" in result.stdout
    owners = etebase.python_on_volume(
        volume,
        "import os\n"
        "owners = set()\n"
        "for root, dirs, files in os.walk('/data'):\n"
        "    for name in [root, *(os.path.join(root, n) for n in dirs + files)]:\n"
        "        owners.add((os.lstat(name).st_uid, os.lstat(name).st_gid))\n"
        "print(sorted(owners))\n",
    )
    assert owners.strip() == "[(4321, 4321)]"

    server = etebase.start(volume=volume, user="4321:4321")
    server.wait_healthy()


# -- command passthrough -------------------------------------------------------


def test_command_is_run_instead_of_the_server(etebase):
    result = etebase.run("python", "-c", "import os; print('hello from', os.getcwd(), os.geteuid())")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hello from /app 65532"
    assert "[entrypoint]" not in result.stdout + result.stderr


def test_command_may_run_as_root(etebase):
    result = etebase.run("python", "-c", "import os; print(os.geteuid())", user="0:0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_manage_command_uses_the_server_config(etebase, volume):
    server = etebase.start(volume=volume)
    server.wait_healthy()
    server.remove()
    # Fails if the command misses /data's config and migrated database.
    result = etebase.run("python", "manage.py", "migrate", "--check", volume=volume)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unknown_command(etebase):
    result = etebase.run("does-not-exist")
    assert result.returncode == 1
    assert "cannot run 'does-not-exist'" in result.stderr

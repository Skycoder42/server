#!/usr/bin/env python3
"""Etebase server container entrypoint.

The hardened runtime image has no shell, so this Python entrypoint replaces a
classic shell script: it generates the `etebase-server.ini` configuration from
the environment (or uses an existing one), applies pending migrations, can
create a superuser, and finally starts uvicorn. It refuses to run as root.

When the container is given a command (e.g. `docker run <image> python
manage.py shell`), that command is executed as-is instead of the server.

Two extra modes back the upgrade path from an image that wrote the `/data`
volume as a different user (e.g. uid 373 for the older victorrds/etesync image):

* `ETEBASE_FIX_OWNERSHIP=1` (requires running as root, `--user 0:0`) recursively
  chowns the data directory to the server's nonroot user and exits — the one-off
  step that must happen once when migrating an existing volume.
* On a normal start, the entrypoint detects that the data directory is not
  writable by the current (nonroot) user and aborts with that fix command
  instead of failing cryptically on the first write.

Environment variables follow the docker-etebase conventions
(https://github.com/victor-rds/docker-etebase); an empty variable counts as
unset. Variables that take secrets also support a `_FILE` variant for Docker
secrets, e.g. `DATABASE_PASSWORD` and `DATABASE_PASSWORD_FILE`.
"""

import configparser
import os
import secrets
import subprocess
import sys


def env(var, default=None):
    """Return $var, treating an empty value like an unset one."""
    return os.environ.get(var) or default


APP_DIR = "/app"
DATA_DIR = env("DATA_DIR", "/data")
CONFIG_PATH = env("ETEBASE_EASY_CONFIG_PATH", "/data/etebase-server.ini")
MANAGE = os.path.join(APP_DIR, "manage.py")

# User the server normally runs as (matches `USER 65532:65532` in the
# Dockerfile); overridable for rootless setups that remap UIDs.
RUN_UID = int(env("ETEBASE_UID", "65532"))
RUN_GID = int(env("ETEBASE_GID", "65532"))
FIX_OWNERSHIP = env("ETEBASE_FIX_OWNERSHIP", "").lower() in ("true", "1", "yes")


def log(message):
    print(f"[entrypoint] {message}", flush=True)


def error(message):
    print(f"[entrypoint] ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def file_env(var, default=None):
    """Return $var, or $var_FILE content (Docker secrets), falling back to `default`."""
    value = env(var)
    value_file = env(var + "_FILE")
    if value is not None and value_file is not None:
        error(f"both {var} and {var}_FILE are set (but are exclusive)")
    if value is not None:
        return value
    if value_file is not None:
        with open(value_file) as f:
            return f.read().strip() or default
    return default


def ensure_dir(path, description):
    try:
        os.makedirs(path, mode=0o750, exist_ok=True)
    except OSError as e:
        error(f"cannot create {description} directory {path}: {e}")


def gen_config():
    debug_django = env("DEBUG_DJANGO", "false").lower() in ("true", "1", "yes")
    language_code = env("LANGUAGE_CODE", "en-us")
    time_zone = env("TIME_ZONE", "UTC")
    db_engine = file_env("DB_ENGINE", "sqlite")
    static_root = env("STATIC_ROOT", "/srv/etebase/static")
    media_root = env("MEDIA_ROOT", "/data/media")
    secret_file = env("SECRET_FILE", "/data/secret.txt")

    redis_uri = env("REDIS_URI")

    # Values are escaped below, so build the file without interpolation.
    config = configparser.ConfigParser(interpolation=None)
    config["global"] = {
        "secret_file": secret_file,
        "debug": str(debug_django),
        "static_root": static_root,
        "static_url": "/static/",
        "media_root": media_root,
        "media_url": "/user-media/",
        "language_code": language_code,
        "time_zone": time_zone,
    }
    if redis_uri:
        config["global"]["redis_uri"] = redis_uri

    if db_engine == "sqlite":
        db_name = file_env("DATABASE_NAME", "/data/db.sqlite3")
        if not db_name.endswith(".sqlite3"):
            db_name = os.path.join(db_name, "db.sqlite3")
        config["database"] = {"engine": "django.db.backends.sqlite3", "name": db_name}
    elif db_engine == "postgres":
        config["database"] = {
            "engine": "django.db.backends.postgresql",
            "name": file_env("DATABASE_NAME", "etebase"),
            "user": file_env("DATABASE_USER", "etebase"),
            "password": file_env("DATABASE_PASSWORD", "etebase"),
            "host": env("DATABASE_HOST", "database"),
            "port": env("DATABASE_PORT", "5432"),
        }
    else:
        error(f"DB_ENGINE '{db_engine}' is not supported (use 'sqlite' or 'postgres')")

    allowed_hosts = os.environ.get("ALLOWED_HOSTS", "")
    ahosts = [host.strip() for host in allowed_hosts.split(",") if host.strip()]
    if ahosts:
        config["allowed_hosts"] = {"allowed_host%d" % (i + 1): host for i, host in enumerate(ahosts)}

    # settings.py reads the file with interpolation, where a literal % is %%.
    for section in config.sections():
        for key, value in config[section].items():
            config[section][key] = value.replace("%", "%%")

    with open(CONFIG_PATH, "w") as f:
        config.write(f)
    log(f"generated {CONFIG_PATH}")


def run_manage(args, environ=None, check=True):
    return subprocess.run([sys.executable, "manage.py", *args], cwd=APP_DIR, env=environ, check=check)


def run_shell(code, environ=None):
    """Run `code` in the Django shell and return its exit code."""
    return run_manage(["shell", "--no-imports", "-c", code], environ=environ, check=False).returncode


# Exit code of DB_STATE_CODE when no migration was ever applied.
DB_EMPTY = 3
DB_STATE_CODE = f"""
import sys
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
recorder = MigrationRecorder(connection)
if not recorder.has_table() or not recorder.applied_migrations():
    sys.exit({DB_EMPTY})
"""


def is_db_empty():
    """Return whether the database was never migrated; abort if it is unreachable."""
    returncode = run_shell(DB_STATE_CODE)
    if returncode not in (0, DB_EMPTY):
        error(f"cannot access the database (exit code {returncode}); check the connection settings")
    return returncode == DB_EMPTY


# Exit code of SUPERUSER_CODE when the user already exists.
SUPERUSER_EXISTS = 3
# Credentials come in via the environment, so quotes in them can't break the code.
SUPERUSER_CODE = f"""
import os, sys
from etebase_server.myauth.models import User
username = os.environ["ETEBASE_SUPER_USER"]
if User.objects.filter(username__iexact=username).exists():
    sys.exit({SUPERUSER_EXISTS})
User.objects.create_superuser(username, None, os.environ["ETEBASE_SUPER_PASS"])
"""


def create_superuser(username, password):
    """Create the superuser unless it exists; return whether it was created."""
    environ = {**os.environ, "ETEBASE_SUPER_USER": username, "ETEBASE_SUPER_PASS": password}
    returncode = run_shell(SUPERUSER_CODE, environ=environ)
    if returncode == SUPERUSER_EXISTS:
        return False
    if returncode != 0:
        error(f"creating superuser '{username}' failed (exit code {returncode})")
    return True


def is_writable(path):
    return os.path.isdir(path) and os.access(path, os.W_OK)


def fix_data_ownership():
    """Recursively chown DATA_DIR to the server's nonroot user.

    Only meaningful when running as root (see ETEBASE_FIX_OWNERSHIP). Symlinks
    are chowned themselves, never followed, so nothing outside /data can be
    touched.
    """
    ensure_dir(DATA_DIR, "data")
    log(f"fixing ownership of {DATA_DIR} to {RUN_UID}:{RUN_GID}")
    os.chown(DATA_DIR, RUN_UID, RUN_GID, follow_symlinks=False)
    for root, dirs, files in os.walk(DATA_DIR):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in dirs:
            os.chown(os.path.join(root, name), RUN_UID, RUN_GID, follow_symlinks=False)
        for name in files:
            os.chown(os.path.join(root, name), RUN_UID, RUN_GID, follow_symlinks=False)


def assert_data_writable():
    """Abort with the ownership fix command if /data is not usable by us."""
    if os.access(DATA_DIR, os.W_OK) and os.access(DATA_DIR, os.X_OK):
        return
    uid, gid = os.geteuid(), os.getegid()
    # The fix mode chowns to ETEBASE_UID/GID, so point it at the actual user.
    uid_env = "" if (uid, gid) == (RUN_UID, RUN_GID) else f"-e ETEBASE_UID={uid} -e ETEBASE_GID={gid} "
    error(
        f"data directory {DATA_DIR} is not writable by uid {uid} (it is likely "
        "owned by another user, e.g. from a previous etesync/victorrds image).\n"
        "Fix the volume ownership once, e.g. with:\n"
        f"  docker run --rm --user 0:0 -e ETEBASE_FIX_OWNERSHIP=1 {uid_env}-v <volume>:{DATA_DIR} <this image>\n"
        f"  (or: docker run --rm -v <volume>:{DATA_DIR} alpine chown -R {uid}:{gid} {DATA_DIR})"
    )


def main():
    if len(sys.argv) > 1:
        try:
            os.execvp(sys.argv[1], sys.argv[1:])
        except OSError as e:
            error(f"cannot run '{sys.argv[1]}': {e}")

    if FIX_OWNERSHIP:
        if os.geteuid() != 0:
            error("ETEBASE_FIX_OWNERSHIP requires running as root, e.g. docker run --user 0:0 <this image>")
        fix_data_ownership()
        log("ownership fixed; exiting (start the server normally as the nonroot user)")
        sys.exit(0)

    if os.geteuid() == 0:
        error("refusing to run as root; use the image's default nonroot user (65532)")

    ensure_dir(DATA_DIR, "data")
    config_dir = os.path.dirname(CONFIG_PATH)
    if config_dir:
        ensure_dir(config_dir, "config")
    assert_data_writable()
    # settings.py reads the path itself; an empty value must not hide the default.
    os.environ["ETEBASE_EASY_CONFIG_PATH"] = CONFIG_PATH

    regen = os.environ.get("REGEN_INI", "") == "true"
    if os.path.isfile(CONFIG_PATH) and not regen:
        log(f"using existing {CONFIG_PATH}")
    else:
        gen_config()

    # Recreate writable directories (bind mounts may be empty) and refresh
    # the baked-in static files when the configured STATIC_ROOT allows it.
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    media_root = config["global"].get("media_root", "/data/media")
    ensure_dir(media_root, "media")
    # Only sqlite's name is a file path; for postgres it is the database name.
    if "database" in config and config["database"].get("engine", "").endswith("sqlite3"):
        db_dir = os.path.dirname(config["database"].get("name", ""))
        if db_dir:
            ensure_dir(db_dir, "database")

    static_root = config["global"].get("static_root", "/srv/etebase/static")
    # The server refuses to start without the directory, so fail early.
    try:
        os.makedirs(static_root, mode=0o750, exist_ok=True)
    except OSError as e:
        error(f"STATIC_ROOT {static_root} does not exist and cannot be created: {e}")
    if is_writable(static_root):
        run_manage(["collectstatic", "--noinput"])
    else:
        log(f"STATIC_ROOT {static_root} is not writable; skipping collectstatic")

    # Signup defaults to disabled (create_user_blocked). AUTO_SIGNUP=true maps
    # to ETEBASE_CREATE_USER_FUNC= (empty -> None -> Django's default create_user,
    # see settings.py).
    if env("AUTO_SIGNUP", "false").lower() in ("true", "1", "yes"):
        os.environ["ETEBASE_CREATE_USER_FUNC"] = ""

    if env("AUTO_MIGRATE", "true").lower() in ("true", "1", "yes"):
        log("applying migrations")
        run_manage(["migrate"])
    elif is_db_empty():
        # Nothing to protect yet, and the server can't run on an empty schema.
        log("AUTO_MIGRATE disabled, but the database is empty; applying initial migrations")
        run_manage(["migrate"])
    else:
        log("AUTO_MIGRATE disabled; skipping migrations")

    super_user = file_env("SUPER_USER")
    if super_user:
        super_pass = file_env("SUPER_PASS")
        generated = super_pass is None
        super_pass = super_pass or secrets.token_urlsafe(32)
        log("creating superuser")
        if not create_superuser(super_user, super_pass):
            log(f"superuser '{super_user}' already exists; skipping")
        elif generated:
            print(f"[entrypoint] INFO: generated superuser password: {super_pass}", flush=True)

    port = env("PORT", "3735")
    command = ["uvicorn", "etebase_server.asgi:application", "--host", "0.0.0.0", "--port", port]
    log("starting " + " ".join(command))
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()

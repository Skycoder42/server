#!/usr/bin/env python3
"""Etebase server container entrypoint.

The hardened runtime image has no shell, so this Python entrypoint replaces a
classic shell script: it generates the `etebase-server.ini` configuration from
the environment (or uses an existing one), applies pending migrations, can
create a superuser, and finally starts uvicorn. It refuses to run as root.

Environment variables follow the docker-etebase conventions
(https://github.com/victor-rds/docker-etebase). Variables that take secrets
also support a `_FILE` variant for Docker secrets, e.g. `DATABASE_PASSWORD`
and `DATABASE_PASSWORD_FILE`.
"""

import configparser
import os
import secrets
import subprocess
import sys

APP_DIR = "/app"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.environ.get("ETEBASE_EASY_CONFIG_PATH", "/data/etebase-server.ini")
MANAGE = os.path.join(APP_DIR, "manage.py")


def log(message):
    print(f"[entrypoint] {message}", flush=True)


def error(message):
    print(f"[entrypoint] ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def file_env(var, default=None):
    """Return $var, or $var_FILE content (Docker secrets), falling back to `default`."""
    value = os.environ.get(var)
    value_file = os.environ.get(var + "_FILE")
    if value is not None and value_file is not None:
        error(f"both {var} and {var}_FILE are set (but are exclusive)")
    if value is not None:
        return value
    if value_file is not None:
        with open(value_file) as f:
            return f.read().strip()
    return default


def ensure_dir(path, description):
    try:
        os.makedirs(path, mode=0o750, exist_ok=True)
    except OSError as e:
        error(f"cannot create {description} directory {path}: {e}")


def gen_config():
    debug_django = os.environ.get("DEBUG_DJANGO", "false").lower() in ("true", "1", "yes")
    language_code = os.environ.get("LANGUAGE_CODE", "en-us")
    time_zone = os.environ.get("TIME_ZONE", "UTC")
    db_engine = file_env("DB_ENGINE", "sqlite")
    static_root = os.environ.get("STATIC_ROOT", "/srv/etebase/static")
    media_root = os.environ.get("MEDIA_ROOT", "/data/media")
    secret_file = os.environ.get("SECRET_FILE", "/data/secret.txt")

    redis_uri = os.environ.get("REDIS_URI")

    config = configparser.ConfigParser()
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
            "host": os.environ.get("DATABASE_HOST", "database"),
            "port": os.environ.get("DATABASE_PORT", "5432"),
        }
    else:
        error(f"DB_ENGINE '{db_engine}' is not supported (use 'sqlite' or 'postgres')")

    allowed_hosts = os.environ.get("ALLOWED_HOSTS", "")
    ahosts = [host.strip() for host in allowed_hosts.split(",") if host.strip()]
    if ahosts:
        config["allowed_hosts"] = {"allowed_host%d" % (i + 1): host for i, host in enumerate(ahosts)}

    with open(CONFIG_PATH, "w") as f:
        config.write(f)
    log(f"generated {CONFIG_PATH}")


def run_manage(args):
    return subprocess.run([sys.executable, "manage.py", *args], cwd=APP_DIR, check=True)


def is_writable(path):
    return os.path.isdir(path) and os.access(path, os.W_OK)


def main():
    if os.geteuid() == 0:
        error("refusing to run as root; use the image's default nonroot user (65532)")

    ensure_dir(DATA_DIR, "data")
    ensure_dir(os.path.dirname(CONFIG_PATH), "config")

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
    if "database" in config:
        db_name = config["database"].get("name", "")
        if db_name:
            ensure_dir(os.path.dirname(db_name), "database")

    static_root = config["global"].get("static_root", "/srv/etebase/static")
    if is_writable(static_root):
        run_manage(["collectstatic", "--noinput"])
    else:
        log(f"STATIC_ROOT {static_root} is not writable; skipping collectstatic")

    # Signup defaults to disabled (create_user_blocked). AUTO_SIGNUP=true maps
    # to ETEBASE_CREATE_USER_FUNC= (empty -> None -> Django's default create_user,
    # see settings.py).
    if os.environ.get("AUTO_SIGNUP", "false").lower() in ("true", "1", "yes"):
        os.environ["ETEBASE_CREATE_USER_FUNC"] = ""

    if os.environ.get("AUTO_MIGRATE", "true").lower() in ("true", "1", "yes"):
        log("applying migrations")
        run_manage(["migrate"])
    else:
        log("AUTO_MIGRATE disabled; skipping migrations")

    super_user = file_env("SUPER_USER")
    if super_user:
        super_pass = file_env("SUPER_PASS")
        generated = super_pass is None
        super_pass = super_pass or secrets.token_urlsafe(32)
        log("creating superuser")
        code = (
            "from etebase_server.myauth.models import User;"
            f" User.objects.create_superuser('{super_user}', None, '{super_pass}')"
        )
        run_manage(["shell", "-c", code])
        if generated:
            print(f"[entrypoint] INFO: generated superuser password: {super_pass}", flush=True)

    port = os.environ.get("PORT", "3735")
    command = ["uvicorn", "etebase_server.asgi:application", "--host", "0.0.0.0", "--port", port]
    log("starting " + " ".join(command))
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()

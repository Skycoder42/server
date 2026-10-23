import configparser
import os
import sys
import urllib.request

# Same lookup order as settings.py (the working directory is /app).
CONFIG_LOCATIONS = [
    os.environ.get("ETEBASE_EASY_CONFIG_PATH") or "/data/etebase-server.ini",
    "etebase-server.ini",
    "/etc/etebase-server/etebase-server.ini",
]


def host_header():
    """Return a Host the server accepts, or None to keep the default (127.0.0.1).

    The server rejects requests whose Host is not in ALLOWED_HOSTS, which often
    lists only the public domain.
    """
    config = configparser.ConfigParser()
    config.read(CONFIG_LOCATIONS)
    if "allowed_hosts" not in config:
        return None
    for _, host in config.items("allowed_hosts"):
        if host.startswith("*"):
            return "healthcheck" + host[1:]
        # The server matches the Host up to its first colon, so IPv6 never matches.
        if not host.startswith("["):
            return host
    return None


def main():
    port = os.environ.get("PORT") or "3735"
    url = f"http://127.0.0.1:{port}/api/v1/authentication/is_etebase/"
    host = host_header()
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        status = urllib.request.urlopen(request, timeout=3).status
    except Exception:
        sys.exit(1)
    if status != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()

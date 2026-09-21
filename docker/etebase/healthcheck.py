import os
import sys
import urllib.request


def main():
    port = os.environ.get("PORT", "3735")
    url = f"http://127.0.0.1:{port}/api/v1/authentication/is_etebase/"
    try:
        status = urllib.request.urlopen(url, timeout=3).status
    except Exception:
        sys.exit(1)
    if status != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()

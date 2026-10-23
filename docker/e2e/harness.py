"""Helpers driving the production image through the `docker` CLI.

See README.md in this directory for prerequisites and how to run the suite.
"""

import configparser
import os
import subprocess
import sys
import time
import typing as t
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def default_image() -> str:
    """Same default tag as docker/build.sh: the package version from setup.py."""
    get_version = [sys.executable, str(REPO_ROOT / "docker" / "get_version.py")]
    version = subprocess.run(get_version, capture_output=True, text=True, check=True).stdout.strip()
    return f"skycoder42/etebase-server:v{version}"


DOCKER = os.environ.get("DOCKER", "docker")
IMAGE = os.environ.get("ETEBASE_IMAGE") or default_image()
POSTGRES_IMAGE = os.environ.get("POSTGRES_IMAGE", "docker.io/library/postgres:18-alpine")
# Contains % and quotes: both used to break the generated ini or the superuser code.
POSTGRES_PASSWORD = "p%ss'w\"rd"
CONFIG_PATH = "/data/etebase-server.ini"
START_TIMEOUT = 120


def docker(*args: str, check: bool = True, timeout: float = START_TIMEOUT) -> subprocess.CompletedProcess:
    result = subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"`docker {' '.join(args)}` failed ({result.returncode}):\n{result.stdout}{result.stderr}")
    return result


def unique(prefix: str) -> str:
    return f"etebase-e2e-{prefix}-{uuid.uuid4().hex[:8]}"


def run_args(
    *,
    environ: t.Optional[t.Dict[str, str]] = None,
    volume: t.Optional[str] = None,
    user: t.Optional[str] = None,
    network: t.Optional[str] = None,
    extra: t.Sequence[str] = (),
) -> t.List[str]:
    args: t.List[str] = []
    for key, value in (environ or {}).items():
        args += ["-e", f"{key}={value}"]
    if volume:
        args += ["-v", f"{volume}:/data"]
    if user:
        args += ["--user", user]
    if network:
        args += ["--network", network]
    return [*args, *extra]


class Container:
    """A detached container of the image, typically running the server."""

    def __init__(self, name: str):
        self.name = name

    def logs(self) -> str:
        result = docker("logs", self.name, check=False)
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        return docker("inspect", "-f", "{{.State.Running}}", self.name).stdout.strip() == "true"

    def exec(self, *command: str, environ: t.Optional[t.Dict[str, str]] = None) -> subprocess.CompletedProcess:
        env_args = [arg for key, value in (environ or {}).items() for arg in ("-e", f"{key}={value}")]
        return docker("exec", *env_args, self.name, *command, check=False)

    def python(self, code: str, environ: t.Optional[t.Dict[str, str]] = None) -> str:
        result = self.exec("python", "-c", code, environ=environ)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def healthcheck(self) -> int:
        return self.exec("python", "/healthcheck.py").returncode

    def wait_for_log(self, text: str, timeout: float = START_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.logs():
                return
            if not self.is_running():
                pytest.fail(f"{self.name} exited before logging {text!r}:\n{self.logs()}")
            time.sleep(1)
        pytest.fail(f"{self.name} did not log {text!r} in time:\n{self.logs()}")

    def wait_healthy(self, timeout: float = START_TIMEOUT):
        """Wait until the image's own healthcheck passes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthcheck() == 0:
                return
            if not self.is_running():
                pytest.fail(f"{self.name} exited before becoming healthy:\n{self.logs()}")
            time.sleep(1)
        pytest.fail(f"{self.name} did not become healthy in time:\n{self.logs()}")

    def http_status(self, path: str, *, port: int = 3735, host: t.Optional[str] = None) -> int:
        """Request `path` from inside the container and return the status code."""
        headers = {"Host": host} if host else {}
        return int(
            self.python(
                "import urllib.error, urllib.request\n"
                f"req = urllib.request.Request('http://127.0.0.1:{port}{path}', headers={headers!r})\n"
                "try:\n"
                "    print(urllib.request.urlopen(req, timeout=5).status)\n"
                "except urllib.error.HTTPError as e:\n"
                "    print(e.code)\n"
            )
        )

    def server_environ(self) -> t.Dict[str, str]:
        """The environment of PID 1, i.e. the uvicorn the entrypoint exec'd."""
        raw = self.python("import sys; sys.stdout.write(open('/proc/1/environ').read())")
        return dict(item.split("=", 1) for item in raw.split("\0") if item)

    def read(self, path: str) -> str:
        return self.python(f"import sys; sys.stdout.write(open({path!r}).read())")

    def exists(self, path: str) -> bool:
        return self.python(f"import os; print(os.path.exists({path!r}))").strip() == "True"

    def manage_shell(self, code: str) -> str:
        """Run `code` in the Django shell with the server's configuration."""
        result = self.exec(
            "python",
            "manage.py",
            "shell",
            "--no-imports",
            "-c",
            code,
            environ={"ETEBASE_EASY_CONFIG_PATH": self.server_environ()["ETEBASE_EASY_CONFIG_PATH"]},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def authenticates(self, username: str, password: str) -> bool:
        code = (
            "from django.contrib.auth import authenticate\n"
            f"user = authenticate(username={username!r}, password={password!r})\n"
            "print(bool(user and user.is_superuser and user.is_staff))\n"
        )
        return self.manage_shell(code).strip() == "True"

    def published_port(self) -> int:
        return int(docker("port", self.name, "3735/tcp").stdout.splitlines()[0].rsplit(":", 1)[1])

    def remove(self):
        docker("rm", "-f", self.name, check=False)


class Etebase:
    """Factory for containers of the image under test; cleans them up afterwards."""

    def __init__(self):
        self.containers: t.List[Container] = []

    def start(self, *, publish: bool = False, **kwargs) -> Container:
        """Start a detached container; kwargs as for `run_args`."""
        container = Container(unique("server"))
        extra = list(kwargs.pop("extra", ()))
        if publish:
            extra += ["-p", "127.0.0.1::3735"]
        docker("run", "-d", "--name", container.name, *run_args(extra=extra, **kwargs), IMAGE)
        self.containers.append(container)
        return container

    def replace(self, old: Container, **kwargs) -> Container:
        """Stop `old` and start a new container (e.g. on the same volume)."""
        old.remove()
        return self.start(**kwargs)

    def run(self, *command: str, entrypoint: t.Optional[str] = None, **kwargs) -> subprocess.CompletedProcess:
        """Run a container to completion and return its result."""
        extra = list(kwargs.pop("extra", ()))
        if entrypoint:
            extra += ["--entrypoint", entrypoint]
        return docker("run", "--rm", *run_args(extra=extra, **kwargs), IMAGE, *command, check=False)

    def python_on_volume(self, volume: str, code: str, user: t.Optional[str] = None) -> str:
        """Run Python code next to a volume, bypassing the entrypoint."""
        result = self.run("-c", code, entrypoint="python", volume=volume, user=user)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def cleanup(self):
        for container in self.containers:
            container.remove()


class Postgres:
    def __init__(self, name: str, network: str):
        self.name = name
        self.network = network

    def psql(self, sql: str, database: str = "etebase") -> str:
        return docker(
            "exec", self.name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "etebase", "-d", database, "-c", sql
        ).stdout


def parse_ini(text: str) -> t.Dict[str, t.Dict[str, str]]:
    """Parse an ini the way settings.py does (with interpolation)."""
    config = configparser.ConfigParser()
    config.read_string(text)
    return {section: dict(config[section]) for section in config.sections()}

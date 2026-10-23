"""Fixtures for the entrypoint e2e suite; see README.md in this directory."""

import time
import typing as t

import pytest
from harness import IMAGE, POSTGRES_IMAGE, POSTGRES_PASSWORD, START_TIMEOUT, Etebase, Postgres, docker, unique


@pytest.fixture(scope="session")
def image() -> str:
    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        pytest.fail(f"image {IMAGE} not found; build it with ./docker/build.sh server (or set ETEBASE_IMAGE)")
    return IMAGE


@pytest.fixture
def etebase(image) -> t.Iterator[Etebase]:
    factory = Etebase()
    yield factory
    factory.cleanup()


@pytest.fixture
def volume() -> t.Iterator[str]:
    name = unique("data")
    docker("volume", "create", name)
    yield name
    docker("volume", "rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def network() -> t.Iterator[str]:
    name = unique("net")
    docker("network", "create", name)
    yield name
    docker("network", "rm", "-f", name, check=False)


@pytest.fixture(scope="session")
def postgres(network) -> t.Iterator[Postgres]:
    """A PostgreSQL server reachable as `database` (the entrypoint's default host)."""
    name = unique("postgres")
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        "database",
        "-e",
        "POSTGRES_USER=etebase",
        "-e",
        f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
        POSTGRES_IMAGE,
        timeout=600,
    )
    try:
        deadline = time.monotonic() + START_TIMEOUT
        # The init phase runs a server on the unix socket only, so wait for TCP.
        while docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "etebase", check=False).returncode != 0:
            if time.monotonic() > deadline:
                pytest.fail("postgres did not become ready:\n" + docker("logs", name, check=False).stderr)
            time.sleep(1)
        yield Postgres(name, network)
    finally:
        docker("rm", "-f", name, check=False)

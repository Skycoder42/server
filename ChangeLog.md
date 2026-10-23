# Changelog

## Version 0.14.6
- Docker image: fix the entrypoint crashing on restart when `SUPER_USER` is set and the superuser already exists; it is now only created once. Credentials containing quotes no longer break superuser creation.
- Docker image: fix the health check failing when `ALLOWED_HOSTS` does not contain `127.0.0.1`.
- Docker image: fix PostgreSQL defaulting to the database name `/data/db.sqlite3` instead of `etebase`, and `DB_ENGINE_FILE`/`DATABASE_NAME_FILE` always failing as "exclusive".
- Docker image: fix config generation failing for values containing `%`.
- Docker image: create a custom `STATIC_ROOT` that does not exist yet instead of crashing on start.
- Docker image: empty environment variables now count as unset, so e.g. an empty `SUPER_PASS` generates (and prints) a password.
- Docker image: an empty database is initialized even with `AUTO_MIGRATE=false`.
- Docker image: a command passed to the container is now run instead of the server.
- Docker image: the "data directory not writable" hint now names the user actually running.
- Add an e2e test suite for the Docker image's entrypoint (`docker/e2e/`).

## Version 0.14.5
- Docker image: fix the entrypoint failing with `cannot create database directory` when the config uses PostgreSQL or a sqlite path without a directory.

## Version 0.14.4
- Docker image: install the PostgreSQL driver from a pinned `requirements-docker.txt` and remove pip from the runtime image, so image scanners no longer flag its vendored libraries.
- Fix all mypy errors and enforce type checking in CI.
- Move the pip-compile sources to `requirements-src/` and add Dependabot for automated dependency updates.

## Version 0.14.3
- Fix server error when passing null collection types.
- Upgrade Django to 5.2 (LTS) and modernize all dependencies: FastAPI 0.141.x, Pydantic 2, redis-py 8, with support for Python 3.11-3.14.
- Add a production-oriented Docker image `skycoder42/etebase-server` (built on hardened DHI base images, runs as non-root, no shell) whose entrypoint generates the config file, applies migrations and supports in-place upgrades from the `victorrds/etesync` image.
- Add a full FastAPI test suite and GitHub Actions CI (lint/typecheck, tests across Python versions and OSes, image build + smoke checks) plus an e2e compatibility suite verifying that data produced by the `victorrds/etesync` image continues losslessly on the new image.

## Version 0.14.2
- Fix issue with some requests failing in some scenarios with the JS client.
  - The JS client was omitting optional fields which were accidentally made to be required. It happened because pydantic v2 changed the behavior in a few ways (for the better fwiw) and we missed a few places when upgrading.

## Version 0.14.1
- Fix issue with serializing non utf8 422 errors
- Appease django warnings about default auto field

## Version 0.14.0
- Upgrade to latest FastAPI and Pydantic for compatibility with Python 3.12

## Version 0.13.1
- Fix requirements definition for django.

## Version 0.13.0
- Update Django dependency to 4 (LTS)

## Version 0.12.0
- Optimize some database queries fixing performance degradation with large installations.
- Ensure environment variables always override config

## Version 0.11.0
- Update deps for Python 3.11

## Version 0.10.0
- Replace the deprecated aioredis with redis-py
- Optimize how we fetch the current (latest) revision of items

## Version 0.9.1
- Update pinned Django version (only matters if using `requirements.txt`).

## Version 0.9.0
- Add LDAP support for checking the validity of a username
- Allow specifying engine-specific database options
- Fix crash on shutdown when redis isn't used
- Reorganize the code to be a valid Python package

## Version 0.8.3
- Fix compatibility with latest fastapi

## Version 0.8.2
- Update dependencies again

## Version 0.8.1
* Fix Error `404 Not Found` for Static Files
* Fix Django 3.2 warnings
* Update dependencies while (keep Django 3.2 LTS)

## Version 0.8.0
* Update django dep.
* Fix issue with comparing ports in hostname verification with self-hosted servers.
* Fix sendfile settings to be more correct.
* Improve easy config (make it clear media_root needs to be set)
* Handle stoken being the empty string
* Fix mysql/mariadb support
* Switch to FastAPI for the server component

## Version 0.7.0
* Chunks: improve the chunk download endpoint to use sendfile extensions
* Chunks: support not passing chunk content if exists
* Chunks: fix chunk uploading media type to accept everything
* Gracefull handle uploading the same revision
* Pass generic context to callbacks instead of the whole view
* Fix handling of some validation errors

## Version 0.6.1
* Collection: save the UID on the model to use the db for enforcing uniqueness

## Version 0.6.0
* Fix stoken calculation performance - was VERY slow in some rare cases
* Fix issues with host verification failing with a custom port - part 2

## Version 0.5.3
* Add missing migration

## Version 0.5.2
* Fix issues with host verification failing with a custom port
* Add env variable to change configuration file path.
* Change user creation to not ask for a password (and clarify the readme).

## Version 0.5.1
* Enforce collections to always have a collection type set
* Collection saving: add another verification for collection UID uniqueness.

## Version 0.5.0
* First Etebase-server release (was EteSync-server before)

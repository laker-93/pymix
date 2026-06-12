# Development

## Stack
- Python **3.11** (Dockerfile base; `pyrekordbox`/`beets`/`pytaglib` pin compat).
- FastAPI + uvicorn, `dependency-injector`, SQLAlchemy 2.0 (sync) + Alembic, Postgres.
- `python-on-whales` to drive Docker; `pytaglib`/`music-tag`/`mediafile` for tags;
  `pyrekordbox` for RB XML; `pyserato` for Serato crates; `watchfiles` for the watcher.
- Deps in `requirements.txt` (no pyproject/poetry). System lib `libtag1-dev` needed
  for `pytaglib`.

## Running
This app expects to run **inside Docker** with the Docker socket mounted (it shells
out to per-user containers) and the `/subbox` host volume mounted (compose files,
env, rendered configs). It cannot fully run standalone.

```bash
# Full stack (postgres + pymix); image is prebuilt as laker93/pymix:latest
docker compose up -d            # uses docker-compose.yml

# Entry point inside the container:
python /app/pymix/runner.py -e $APP_ENVIRONMENT   # env ∈ {dev, prod}
```

Config is merged from `pymix/config/config.base.yaml` + `config.{env}.yaml`
(`registration.get_config`). DB creds come from env: `POSTGRES_DB/USER/PASSWORD`
(see `factories/create_db_session.py`). Migrations auto-run on startup with retry
(`PYMIX_DB_INIT_MAX_ATTEMPTS`, `PYMIX_DB_INIT_RETRY_SLEEP_S`).

## Tests
- pytest, config at `pymix/pytest.ini`. Async tests use `anyio` (`anyio_backend`
  fixture → asyncio; mark with `@pytest.mark.anyio`).
- Run: `python -m pytest pymix/tests` (or `pytest pymix/tests`).
- Pattern: build the real DI `Container`, then `container.<provider>.override(mock)`
  to swap collaborators. See `pymix/tests/controllers/test_rekordbox_xml_controller.py`
  and fixtures in `pymix/tests/conftest.py` / `pymix/tests/fixtures/container.py`.
- Coverage is light (subsonic client/orchestrator, one controller). When adding a
  feature, add a test in the matching `tests/<layer>/` dir and reuse the override
  pattern rather than hitting real containers.

## Migrations (manual)
Alembic config: `alembic.ini` (`script_location = pymix/migrations`). The DB URL is
injected at runtime via a live connection (not the ini), so for manual ops set
`POSTGRES_*` env and run from repo root:
```bash
alembic revision -m "add <table>"     # then edit upgrade()/downgrade()
alembic upgrade head                  # usually unnecessary — startup does it
```
Always pair a migration with the ORM change in `model/db_tables.py`.

## dev_sandbox/
Ad-hoc scripts to exercise the app against a real `dev` container set (NOT tests —
they create users, hit Navidrome, mutate the DB). Each builds a `Container` via
`create_container('dev')` and pokes a provider. Useful as runnable examples of how
to wire and call a layer in isolation. Highlights:
- `backfill_subbox_id.py` — backfill `SUBBOX_ID` tags + `subbox_beets_map_table` for
  tracks imported before tagging existed (`--apply` to write, dry-run by default).
- `dev_create_services.py` — example of `ServicesOrchestrator.create`.
- `dev_db_controller.py`, `dev_xml_controller.py`, `dev_serato_controller.py`,
  `dev_test_match.py`, `dev_navidrome_client.py`, `dev_get_playlist_tracks.py`.
- `run_map_subbox_id_beet_id.py`, `tag_subbox_ids.py`, `recreate_meta/`.

Treat these as throwaway dev tooling — don't import them from `pymix/`.

## Gotchas
- The app talks to **per-user** containers by name (`beets{user}`, `navidrome{user}`);
  nothing works without those containers up and the docker socket mounted.
- Navidrome must have **"report real path"** enabled, or `pymix_path` resolution and
  thus `subbox_id` lookups fail (README explains the setting).
- Many filesystem ops use `shutil` (not pathlib) because staging dirs are on Docker
  volumes spanning filesystems — keep that when moving files cross-volume.
- `constants.py` is stale Kafka boilerplate; `version`/`title` there feed the FastAPI
  app metadata only.

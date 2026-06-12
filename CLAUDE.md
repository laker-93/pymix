# pymix / subbox — Claude guide

pymix is the FastAPI backend of **subbox**, an ETL service that converts a user's
music library and playlist structure **between DJ software (Rekordbox, Serato) and
Subsonic** (served by Navidrome). Each user gets their own isolated stack of
Docker containers (Navidrome, beets, filebrowser); pymix orchestrates them.

> `pymix` is a legacy package name (`constants.py` still mentions Kafka — ignore
> that, it is dead). The product is "subbox" / `sub-box.net`.

## Read this first when adding a feature

1. This file (architecture + conventions).
2. `.claude/docs/architecture.md` — layers, request flow, container topology.
3. `.claude/docs/api.md` — every HTTP endpoint and what it does.
4. `.claude/docs/data-model.md` — DB tables, domain models, the `subbox_id` concept.
5. `.claude/docs/workflows.md` — the import/export/sync/watch flows end to end.
6. `.claude/docs/dev.md` — how to run, test, migrate, and the dev_sandbox scripts.

Only then read source, guided by the docs.

## The one concept you must understand: `subbox_id`

Every audio file is tagged with a `SUBBOX_ID` (a UUID written into the file's
metadata via `taglib`). This is the **stable cross-system identity** of a track —
it survives transcoding, re-tagging, moving between Rekordbox/Serato/Navidrome,
and beets re-imports. Almost all track-level logic keys off it.

- Written/read in `pymix/utils/tag_subbox_id.py` (`tag_subbox_id`, `get_subbox_id`).
- Mapped to a beets DB id per user in `subbox_beets_map_table` (see `DbController`).
- Used to look up cue/loop metadata in `library_table`.

## Architecture in one paragraph

Layered, dependency-injected (`dependency-injector`). **Routers** (`pymix/routers/`)
are thin HTTP handlers that resolve the user (via `session_id` cookie or `username`
query) and delegate to **controllers** (`pymix/controllers/`). Controllers
orchestrate **orchestrators** (`pymix/orchestrators/`, business logic over a single
domain) which call **clients** (`pymix/clients/`, HTTP to Navidrome/beets/Subsonic)
and **handlers** (`pymix/handlers/`, filesystem + docker side effects). Everything
is wired in `pymix/containers.py`; the app is built in `pymix/registration.py`;
entry point is `pymix/runner.py`. DB access is centralized in `DbController`
(SQLAlchemy, sync sessions, Postgres). Per-user beets commands are run by
`docker.execute(...)` against that user's `beets{username}` container.

## Conventions (follow these — they are load-bearing)

- **DI everywhere.** New collaborators are constructor-injected and registered as
  providers in `containers.py`, then `@inject`ed into routers via
  `Depends(Provide[Container.x])`. Add the router's module to the `wire(...)` list
  in `registration.py` if it isn't already.
- **User resolution pattern**: endpoints accept either a `session_id` cookie or a
  `username` (query/body). Resolve with `db_controller.get_user_by_session_id` /
  `get_user`. Copy the existing guard blocks — they are repeated verbatim across
  routers on purpose.
- **Return shape**: most endpoints return a plain dict `{"success": bool,
  "reason": str, ...}` and swallow exceptions into `reason`. Newer endpoints
  (`track.py`, `sync.py`) use Pydantic response models and raise `HTTPException`.
  Match the style of the router you are editing.
- **Long work runs in the background**: imports/exports use FastAPI
  `BackgroundTasks` + a job row (`DbController.create_import_job/create_export_job`,
  polled via `/beets/import/progress` and `/export/progress`).
- **Blocking calls** (beets via docker, taglib, filesystem walks) are pushed off
  the event loop with `anyio.to_thread.run_sync` / `anyio.to_process.run_sync`.
- **beets is driven via the CLI inside the user's container**, never as a library
  in-process for user data. Pattern: build a `beet ...` string, `docker.execute(
  f"beets{username}", cmd.split(), stream=True)`, iterate/log the streamed output.
- **Per-user containers** are named `navidrome{username}`, `beets{username}`; the
  public/shared beets container is `beets`. `public=True` selects the shared one.
- **Config**: `config.base.yaml` + `config.{env}.yaml` merged in `get_config`.
  Paths use `{user}`/`{port}` `.format()` placeholders. Don't hardcode paths —
  add a config key and thread it through the container.
- **Migrations**: schema changes require a new Alembic revision in
  `pymix/migrations/versions/` AND the matching ORM model in `model/db_tables.py`.
  Migrations auto-run on startup (`create_db_session`, `run_migrations=True`).

## Known rough edges (don't be surprised)

- `RekordboxXMLController` is explicitly overloaded — it contains beets/duplicate
  logic that has `# todo move this elsewhere` markers. New beets-only logic ideally
  belongs in a beets controller, but match existing placement if extending.
- `_import_to_beets` is duplicated in `RekordboxXMLController` and
  `SeratoController` (both flagged with todos).
- `constants.py` describes a Kafka app — it's stale boilerplate, ignore it.
- Several `mkdir(exist_ok=True)` have `# todo change to false when launch` notes.

## Do NOT

- Don't write to the running production DB/containers without explicit user
  confirmation. There is no separate prod safety harness here — treat any live
  instance as precious.
- Don't bypass `subbox_id` tagging when introducing a new ingest path; untagged
  tracks break matching, dedup, and metadata sync downstream.

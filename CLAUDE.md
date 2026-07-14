# CLAUDE.md — pymix (subbox backend)

pymix is subbox's FastAPI backend: an ETL service that converts a user's library and
playlists between DJ software (Rekordbox, Serato) and Subsonic (served by Navidrome),
and orchestrates a per-user stack of Docker containers (Navidrome, beets, filebrowser).
The package name `pymix` is legacy; the product is "subbox" (`sub-box.net`). Cross-repo
context lives in `../subbox-workspace/`.

## Read before adding a feature

1. `docs/architecture.md` — layers, DI wiring, lifespan/watcher, per-user container
   topology, external services, a request-flow example.
2. `docs/data-model.md` — DB tables, domain models, the `subbox_id` lifecycle, the
   add-a-table checklist.
3. `docs/api.md` — every endpoint by router + the add-an-endpoint checklist.
4. `docs/workflows.md` — import/export/sync/watch flows end to end.
5. `docs/dev.md` — run, test, migrate, dev_sandbox scripts, and gotchas.

Only then read source, guided by the docs.

## Invariants

- **`subbox_id` is the cross-system track identity** — a `SUBBOX_ID` UUID tagged into
  each file (`utils/tag_subbox_id.py`) that survives transcoding, re-tagging, and moves
  between Rekordbox/Serato/Navidrome. Almost all track-level logic keys off it. **Never
  add an ingest path that skips tagging** — the beets-map, dedup, presence and metadata
  steps silently fail to associate. Full lifecycle: `docs/data-model.md`.
- **DI everywhere.** Collaborators are constructor-injected, registered as providers in
  `containers.py`, and `@inject`ed into routers; a new router must be added to both
  `create_app` and the `wire(...)` list in `registration.py`. Details: `docs/architecture.md`.
- **Treat any live instance as production.** There is no separate prod safety harness —
  don't write to a running DB or user containers without explicit user confirmation.

## Rough edges (don't be surprised)

- `RekordboxXMLController` is overloaded (mixes beets/dedup logic); `_import_to_beets`
  is duplicated in it and `SeratoController` — see the `# todo` markers, and match
  existing placement when extending rather than "fixing" it in passing.
- `constants.py` is stale Kafka boilerplate (it only feeds FastAPI app metadata) — ignore it.

# Architecture

## Layers (outer → inner)

```
HTTP request
   │
routers/         thin FastAPI handlers: parse input, resolve user, delegate, shape response
   │
controllers/     coordinate a use-case across multiple orchestrators/handlers/clients
   │
orchestrators/   business logic over ONE domain (subsonic, rekordbox xml, serato crates, services)
   │
services/        cross-cutting logic that isn't one domain and often has no HTTP caller
                 (wishlist resolve/reconcile, sheet sync, track matching, link parsing
                  via yt-dlp, MusicBrainz/YouTube matching, automatch, import progress)
   │
clients/         async wrappers over external services (Navidrome/Subsonic REST HTTP, beets CLI via docker exec)
handlers/        side-effecting helpers (filesystem staging, docker compose, env/zip files)
   │
factories/       construct stateful objects (DB session, aiohttp session, rekordbox xml)
model/           SQLAlchemy ORM rows + plain domain dataclasses/pydantic models
utils/           pure helpers (subbox_id tagging, port allocation, string cleaning)
```

Dependencies point inward. A router never touches the filesystem directly; it goes
through a handler. A controller never builds a URL; it goes through a client.

## Wiring (dependency-injector)

- `pymix/containers.py` — `Container(DeclarativeContainer)` declares every provider
  (mostly `Singleton`, some `Factory`/`Resource`). Constructor args are pulled from
  `config.*` paths or other providers.
- `pymix/registration.py`:
  - `create_container(env)` loads config, inits logging, calls `container.init_resources()`,
    and `container.wire(modules=[...])` so `@inject` works in those modules.
  - `create_app(container)` builds the `FastAPI` app, adds CORS, includes all routers,
    and installs the `lifespan` context manager.
- `pymix/runner.py` — argparse `--env` (default `dev`), creates container, runs uvicorn.

**When you add a router**: include it in `create_app`'s `app.include_router(...)`
list AND add its module to the `wire(...)` list in `create_container`.

## Lifespan / background loops

`registration.lifespan` starts **six** long-lived anyio tasks in one task group. The
first two are connected by a memory object stream and form the watch-dir import path:

- `poll_watchdir` (in `handlers/filebrowser_file_handler.py`) — watches
  `/user-updownloads/<user>/watch/` for new audio (debounced 15s, stability-checked),
  enforces per-user storage limits, and sends the username downstream.
- `trigger_processing` — receives usernames and runs
  `RekordboxXMLController.consume_from_filebrowser(user, public=False, watch=True)`,
  wrapped in a job row.

That is the "drop files in a folder and they auto-import" path, distinct from the
explicit `/rekordbox/import` endpoint. The other four poll on their own intervals
(configured under `google_sheets` / `wishlist` / `memory_watch` in `config.*.yaml`):

- `sheet_sync_loop` (`services/sheet_sync_service.py`) — pulls wishlist rows from each
  user's attached Google Sheet.
- `wishlist_reconcile_loop` (`services/wishlist_reconcile_service.py`) — flips open
  wishlist items to `available` once the track shows up in the user's library.
- `wishlist_resolve_loop` (`services/wishlist_resolve_service.py`) — resolves pending
  free-text items to a canonical MusicBrainz match before anything searches for them.
- `mem_watch_loop` (`handlers/mem_watch_handler.py`) — logs one RSS/allocator/cgroup
  sample per interval, escalating to WARNING past `warn_fraction` of the container's
  memory limit. It exists because the kernel SIGKILLs at that limit with no warning and
  no traceback, so the only diagnosis that survives an OOM kill is one already written
  to the log before it (laker-93/pymix#125). `utils/memdiag`'s `/admin/*` endpoints
  answer far more, but only while something is alive to ask them.

**None of these has an HTTP caller**, so a failure inside one is invisible to the user
(they just see "nothing happened"). Keep them defensive, and log loudly.

## Logging

Console (stdout) always; `initialise_logger` in `registration.py` adds a rotating file
handler when `disable_file_handler: false` in that env's config. Prod turns it on and
points `logs_directory` at `/subbox/logs` — a **host bind mount**, deliberately. Docker's
own json-file log survives a container *restart* (the container id is unchanged, so the
file is appended to) but not a `docker compose up -d`, which recreates the container and
discards its log — i.e. the deploy you make to investigate an incident destroys that
incident's logs. Dev stays console-only; `docker logs pymix` is right there.

An unwritable `logs_directory` degrades to console-only with an ERROR, never a crash
loop: pymix runs as uid 1000 against a host-owned mount.

## Container topology (per user)

subbox runs **one set of containers per user**, created by `ServicesOrchestrator`
on `/user/create`:

| Container         | Name pattern        | Port (internal) | Role |
|-------------------|---------------------|-----------------|------|
| Navidrome         | `navidrome{user}`   | 4533            | Subsonic server / library + playlists |
| beets             | `beets{user}`       | —               | Tag/import engine, queried entirely via CLI (`docker exec`) |
| beets (public)    | `beets`             | —               | Shared/public library (`public=True`) |
| filebrowser       | `filebrowser`       | —               | Single shared up/download UI; per-user accounts |
| pymix             | `pymix`             | 8002            | This app |
| postgres          | `pymix-postgres`    | 5432            | pymix's own DB |

- pymix talks to per-user services over the Docker network using
  `host.format(user=..., port=...)` (see `config.dev.yaml` `containers.*.host`).
- pymix runs `beet` commands by `docker.execute("beets{user}", ...)` via
  `python_on_whales` (the docker socket is mounted into the pymix container).
- **pymix renders the per-user compose files itself** (`handlers/compose_file_handler.py`
  + `pymix/templates/compose/*.j2`) and writes them to `<mount>/generated/compose/`.
  They used to be checked into `laker-93/subbox` with one branch per host. Everything
  that varied between those branches is now the `host:` block in
  `config.{dev,prod}.yaml`. filebrowser is not among them: pymix never brings it up,
  only `docker inspect`s and `exec`s into it
  (`ServicesOrchestrator._create_filebrowser_account`).
- **Whose filesystem a path belongs to matters.** `docker compose -f` is run by the
  Docker CLI inside the pymix container, but bind sources are resolved by the *host*
  daemon. So the compose file can live anywhere pymix can write, while every bind
  source in it must be spelled in host terms — that is what `host.bind_root` /
  `host.pymix_mount` and `ComposeFileHandler.host_path()` are for.
- **Navidrome's data is a named volume** (`navidrome-data-{user}`), on every
  environment. Its `navidrome.toml` is injected into that volume before first start;
  see `_write_navidrome_config`. Existing users on the old host bind are moved over by
  `POST /admin/navidrome/{user}/migrate`.
- Templates rendered in-app (beets config, navidrome.toml, the compose files, the beets
  s6 override) are in `pymix/templates/`; static code assets shipped in the image (the
  empty Rekordbox collection stub every export starts from) are in `pymix/resources/`.

## External services & data sources

- **Navidrome** = the Subsonic API implementation. pymix uses standard Subsonic
  endpoints: `getPlaylists`, `getPlaylist`, `search2/search3`, `createPlaylist`,
  `deletePlaylist`, `setRating`, `startScan`, plus Navidrome's `/auth/createAdmin`.
  Auth uses the Subsonic token+salt scheme (`SubsonicClient._calculate_token`).
  **Requires Navidrome's "report real path" option** so pymix can map a Subsonic
  track back to its on-disk path (`pymix_path`).
- **beets** = music importer/tagger. Driven entirely via CLI in the container
  (`docker exec`, `python_on_whales`) — no `web` plugin. Running a Flask server
  per user container was too much memory overhead for the resource-limited DO
  droplet, so even the track-count lookup (`BeetsClient.get_number_of_tracks`)
  parses `beet stats` output instead of calling a `/stats` HTTP endpoint.
- **Rekordbox XML** = parsed/written via `pyrekordbox` (wrapped by
  `RekordboxXMLFactory` / `RekordboxXMLOrchestrator`).
- **Serato crates** = read/written via `pyserato` (`SeratoCrateOrchestrator`).

## Key request flow example (Rekordbox export)

`POST /rekordbox/export`
→ `routers/rb_import_export.rekordbox_export`
→ `RekordboxXMLController.create_rekordbox_xml_from_subsonic_playlists`
→ `SubsonicOrchestrator.get_subsonic_playlists` → `SubsonicClient.get_playlists`/`get_playlist_tracks`
→ `RekordboxXMLOrchestrator` builds folders/playlists/tracks into the XML (via `pyrekordbox`)
→ enriched with stored `path_components` from `playlist_path_table` for lossless folder structure
→ XML written to the user's filebrowser `downloads/` dir for the client to fetch.

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
clients/         async HTTP to external services (Navidrome/Subsonic REST, beets web API)
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

## Lifespan / background watcher

`registration.lifespan` starts two long-lived anyio tasks connected by a memory
object stream:
- `poll_watchdir` (in `handlers/filebrowser_file_handler.py`) — watches
  `/user-updownloads/<user>/watch/` for new audio (debounced 15s, stability-checked),
  enforces per-user storage limits, and sends the username downstream.
- `trigger_processing` — receives usernames and runs
  `RekordboxXMLController.consume_from_filebrowser(user, public=False, watch=True)`,
  wrapped in a job row.

This is the "drop files in a folder and they auto-import" path, distinct from the
explicit `/rekordbox/import` endpoint.

## Container topology (per user)

subbox runs **one set of containers per user**, created by `ServicesOrchestrator`
on `/user/create`:

| Container         | Name pattern        | Port (internal) | Role |
|-------------------|---------------------|-----------------|------|
| Navidrome         | `navidrome{user}`   | 4533            | Subsonic server / library + playlists |
| beets             | `beets{user}`       | 8337            | Tag/import engine, queried via CLI + web API |
| beets (public)    | `beets`             | 8337            | Shared/public library (`public=True`) |
| filebrowser       | `filebrowser`       | —               | Single shared up/download UI; per-user accounts |
| pymix             | `pymix`             | 8002            | This app |
| postgres          | `pymix-postgres`    | 5432            | pymix's own DB |

- pymix talks to per-user services over the Docker network using
  `host.format(user=..., port=...)` (see `config.dev.yaml` `containers.*.host`).
- pymix runs `beet` commands by `docker.execute("beets{user}", ...)` via
  `python_on_whales` (the docker socket is mounted into the pymix container).
- Compose files / env templates for the per-user containers live OUTSIDE this repo,
  under the mounted `/subbox` volume (`docker_compose_file`, `env_file` config keys).
  Templates rendered in-app (beets config, navidrome.toml) are in `pymix/templates/`.

## External services & data sources

- **Navidrome** = the Subsonic API implementation. pymix uses standard Subsonic
  endpoints: `getPlaylists`, `getPlaylist`, `search2/search3`, `createPlaylist`,
  `deletePlaylist`, `setRating`, `startScan`, plus Navidrome's `/auth/createAdmin`.
  Auth uses the Subsonic token+salt scheme (`SubsonicClient._calculate_token`).
  **Requires Navidrome's "report real path" option** so pymix can map a Subsonic
  track back to its on-disk path (`pymix_path`).
- **beets** = music importer/tagger. Driven via CLI in the container; the only web
  API call is `/stats` (`BeetsClient.get_number_of_tracks`).
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

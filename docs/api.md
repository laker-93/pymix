# HTTP API

App `root_path="/pymix"` (so behind the proxy everything is prefixed `/pymix`).
CORS allows the feishin/sub-box web origins with credentials; methods limited to
GET/POST/DELETE/OPTIONS. Auth is by the `session_id` cookie (set on create/login) and
nothing else; `/user/storage_check` also accepts that session id as a Bearer token.

User-scoped endpoints resolve their caller with the `require_user` / `require_username`
dependency in `routers/auth.py`, which returns **401** when the cookie is missing,
unknown or expired. Endpoints used to accept an explicit `username` (query/body) as an
alternative identity; it was never verified, so any caller could act as any user by
naming them. It has been removed — `username` now appears only where it is an argument
rather than a claim of identity (`/user/create`, `/user/login`, and the `[db]` lookup
helpers).

All endpoints live in `pymix/routers/`. Tags in brackets are the OpenAPI tags.

## Users & sessions — `routers/user.py`
| Method/Path | Purpose |
|---|---|
| POST `/user/create` | Create user + spin up their navidrome/beets/filebrowser containers (`ServicesOrchestrator.create`). Requires a valid signup `token`. Sets `session_id` cookie. |
| POST `/user/login` | Create/return a session for username+password. Sets cookie. |
| GET `/user/is_valid_token` | Check a signup token is valid (unused tokens gate registration). |
| GET `/user/library_size` | Sum of bytes under `/private-music/{user}`. |
| GET `/user/storage_check` | Whether an upload of `uploadSizeBytes` fits in quota; accepts Bearer or cookie. |
| GET `/user/delete` | Delete user row. |
| GET `/user/get_by_username`, GET `/user/get_by_session_id` | Lookup helpers. |

## Maintenance — `routers/maintenance.py`
| GET `/healthcheck` | Liveness. |

## Rekordbox import/export — `routers/rb_import_export.py`
| Method/Path | Purpose |
|---|---|
| POST `/rekordbox/import` | Ingest the user's uploaded RB XML (+ optional audio zip) → beets import → create Navidrome playlists + import cue/rating metadata. Runs as a **background job**; returns `job_id`. Body `playlistNames: list[list[str]]` filters which playlist paths to import. Enforces storage quota. |
| POST `/rekordbox/export` | Build a Rekordbox XML from the user's Navidrome playlists. Body `user_root` (client-side music root for path rewriting) + optional `playlistIds`. Writes XML into the user's downloads dir. |

## Serato import/export — `routers/serato_import_export.py`
| POST `/serato/import` | Ingest uploaded Serato crates (+ optional audio) → beets import → Navidrome playlists/metadata. Background job. |
| POST `/serato/export` | Build Serato crates from the user's Navidrome playlists into downloads dir. |

## beets — `routers/beets_import.py`
| Method/Path | Purpose |
|---|---|
| POST `/beets/import` | Lower-level: import staged files from filebrowser into beets (`consume_from_filebrowser`). |
| GET `/beets/import/progress` | Poll an import job's progress. |
| GET `/beets/import/tracks_imported` | Count of tracks currently in beets (`BeetsClient.get_number_of_tracks`). |
| GET `/beets/import/tracks_to_be_imported` | Count of staged tracks awaiting import. |
| GET `/beets/duplicates` | List duplicate tracks (`beet duplicates`). |
| DELETE `/beets/duplicates` | Remove duplicates. |

## Export progress — `routers/export_progress.py`
| GET `/export/progress` | Poll an export job. |

## Track matching — `routers/sync.py`
| Method/Path | Purpose |
|---|---|
| POST `/sync/match_tracks` | Match a list of tracks against the user's Navidrome library; returns matched/unmatched flags. |
| POST `/sync/map_meta` | Tag staged uploads with `subbox_id` and persist original metadata; 400s if any track can't be tagged. |
| POST `/sync/plan` | Compute a sync plan: which requested tracks are already present vs missing on server, download size, metadata updates. Read-only. |
| POST `/sync` and POST `/sync/tracks` | Resolve requested tracks on the server and zip them into the user's downloads dir for download. `/sync/tracks` uses a more lenient multi-stage matcher. |
| POST `/sync/playlists` | Zip the tracks of selected server playlists, excluding ones the client already has. |

## Tracks & metadata — `routers/track.py`
| Method/Path | Purpose |
|---|---|
| POST `/tracks/presence` | Given `subbox_ids` (≤1000), return `{id: bool}` of which are already in the user's library. Lets the client skip re-uploading. |
| POST `/track/metadata/update` | Versioned update of a track's cue/loop metadata (`cuedata` validated against `cue_schema`). `source_app` ∈ {serato, rekordbox}, `change_type` ∈ {upload, edit, sync, merge}. |
| GET `/track/metadata/{track_id}` | Fetch latest cue/loop metadata for a track. |
| DELETE `/track` | Delete tracks (by `subbox_id` list) from DB tables + remove from beets. |

## Conventions for adding an endpoint
1. Put the route in the topical router (or create a new module and register it — see
   architecture.md "When you add a router").
2. Identify the caller with `user: dict = Depends(require_user)` (or
   `username: str = Depends(require_username)`) from `routers/auth.py`. Never take a
   `username` param as the caller's identity — it is unauthenticated. Don't hand-roll a
   guard block; the dependency 401s on its own, so the handler body can assume a user.
3. `@inject` your collaborators with `Depends(Provide[Container.x])`.
4. Delegate to a controller/orchestrator — keep the router thin.
5. Match the response style of the router you're in (plain dict vs Pydantic model).
6. Long-running work → `BackgroundTasks` + a job row.

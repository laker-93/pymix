# Data model

## Database

- Postgres (`pymix-postgres`), one DB for pymix itself (separate from each user's
  Navidrome/beets internal DBs).
- ORM models: `pymix/model/db_tables.py` (SQLAlchemy `declarative_base`).
- All access goes through `DbController` (`pymix/controllers/db_controller.py`),
  using **sync** sessions from a `sessionmaker` (`factories/create_db_session.py`).
  Sessions are short-lived `with self._session_factory() as session:` blocks.
- Migrations: Alembic, `pymix/migrations/versions/`, config `alembic.ini`
  (`script_location = pymix/migrations`). Migrations **auto-apply on startup**
  with retry (see `create_db_session`, gated by `run_migrations=True`).

### Tables (`db_tables.py`)

| Table | Model | Key columns | Notes |
|---|---|---|---|
| `user_table` | `UserRow` | username (unique), password, email, user_id (uuid), beets_port, subsonic_port, max_library_size | One row per user. Ports allocated via `utils/get_available_port`. |
| `session_table` | `SessionRow` | session_id (uuid), user_id | One active session per user (enforced in code). |
| `user_token_table` | `UserTokenRow` | token, user_id | Signup tokens; `user_id=''` until claimed at create. |
| `subbox_beets_map_table` | `SubboxBeetsMapRow` | user_id, subbox_id, beet_id | Maps the cross-system `subbox_id` to that user's beets track id. Presence here = track is in the user's library. |
| `library_table` | `LibraryRow` | user_id, subbox_id, cuedata (JSON), source_app, updated_at, version | Latest cue/loop metadata per track, versioned. |
| `meta_history_table` | `MetaHistoryRow` | user_id, subbox_id, version, cuedata, source_app, change_type, changed_at | Append-only history of every metadata change. |
| `original_track_meta_map_table` | `OriginalTrackMetaRow` | user_id, subbox_id, user_location, staging_location, original_name/artist/album | Records a track's original (client-side) identity so re-imports can be skipped/matched. |
| `user_job_table` | `UserJobRow` | user_id, job_id | Links a user to their jobs. |
| `job_table` | `JobRow` | job_id, name (import/export), counts, in_progress, result | Drives the import/export progress endpoints. At most one in-progress job per user (asserted). |
| `playlist_path_table` | `PlaylistPathRow` | user_id, display_name, path_components (JSON) | Stores a playlist's folder path components so export can rebuild nested folder structure losslessly (Subsonic playlists are flat). Added in migration 002. |

## Domain models (not DB rows)

- `model/subboxtrack.py` `SubBoxTrack` (dataclass) — the in-memory track. Notable fields:
  - `path` — child path relative to a music root (client-facing).
  - `pymix_path` — absolute path on the pymix/server filesystem.
  - `track_id` — Rekordbox XML TrackID; `sub_track_id` — Subsonic id; `subbox_id` — the UUID tag.
  - `__eq__` compares only name+artist.
- `model/subboxplaylist.py` `SubBoxPlaylist` (pydantic dataclass) — name, tracks,
  `subsonic_id`, `path_components` (folder hierarchy).
- `model/original_track_meta.py` `OriginalTrackMeta` / `OriginalTracks` (pydantic) —
  payload for `/sync/map_meta`, persisted to `original_track_meta_map_table`.

## The `subbox_id` lifecycle

1. On upload/staging, `tag_subbox_id(file)` writes a new `SUBBOX_ID` UUID tag if
   absent (or returns the existing one) using `taglib`.
2. After a beets import, `_map_subbox_id_beet_id` reads the tag from each imported
   file and writes a `subbox_beets_map_table` row + sets the `subbox_id` field in
   the beets DB (`beet modify ... subbox_id=...`).
3. Cue/loop metadata is stored against `subbox_id` in `library_table`.
4. Subsonic tracks are resolved back to a file via `pymix_path` (needs Navidrome
   "report real path"), then `get_subbox_id(pymix_path)` re-reads the tag.

If you add a new ingest path, you MUST tag files with `subbox_id` early, or the
beets-map, dedup, presence, and metadata steps will silently fail to associate.

## Adding/altering a table — checklist

1. Edit/add the model in `model/db_tables.py`.
2. Create a new Alembic revision in `pymix/migrations/versions/` (follow the
   numbering of `001_…`, `002_…`). Implement `upgrade()` and `downgrade()`.
3. Add the matching `DbController` methods (keep all SQL in `DbController`).
4. Migrations run automatically on next startup; for local manual runs see dev.md.

# Core workflows (end to end)

These are the use-cases the codebase exists to serve. Trace one before changing it.

## Storage / staging paths

| Logical area | Path template | Where set |
|---|---|---|
| User up/download root (filebrowser) | `/user-updownloads/{user}/{uploads,watch,downloads}` | `config.*.yaml containers.filebrowser` |
| Served music (Navidrome reads here) | `/private-music/{user}` (`serving_music_path_base`) | `containers.subsonic` |
| beets import staging (private) | `/private-staged/{user}/` | `containers.beets.data` |
| beets import staging (public) | `/public-staged` | `containers.beets.data_public` |

Flow of a file: **filebrowser uploads → staged to beets data dir → `beet import`
moves it into `/private-music/{user}` → Navidrome scans → playlists created.**

## 1. User creation (`POST /user/create`)
`ServicesOrchestrator.create`:
1. Quota check (`max_number_of_users`).
2. `create_user` in DB (allocates beets/subsonic ports, claims signup token).
3. mkdir music/backup/staging dirs.
4. `docker compose up` a `navidrome{user}` and `beets{user}` stack (env file rendered
   by `DockerEnvFileHandler`, config files rendered from `pymix/templates/`).
5. Create a filebrowser account for the user (with the db.bak workaround).
6. Retry-create the Navidrome admin account (`NavidromeClient.create_account`).
On any failure the user + session are rolled back.

## 2. Rekordbox import (`POST /rekordbox/import`) — background job
`RekordboxXMLController.create_subsonic_playlists_from_xml`:
1. Router resolves user, checks storage quota, counts tracks, creates an import job,
   schedules `run_import_task` as a `BackgroundTask`.
2. `fb_file_handler.get_xml_data_path` finds the uploaded RB XML (+ optional audio
   zip) in the user's `uploads/`.
3. If audio present → `_import_to_beets` (stage → `beet import --group-albums --set
   user=… -q /downloads` in `beets{user}` → cleanup → dedup tag → subbox_id↔beet map).
4. `SubsonicOrchestrator.scan` triggers a Navidrome rescan.
5. `_set_data_from_xml`:
   - `_create_playlists_from_xml` — parse XML playlists → `SubBoxPlaylist`s →
     persist `path_components` → resolve each track's `sub_track_id` via Subsonic
     search → `create_playlists` in Navidrome.
   - `_set_metadata_from_xml` — set ratings, write BPM into beets, and store
     cue/loop metadata in `library_table` keyed by `subbox_id`.
6. Remove the filebrowser upload dir (only on success).
Progress polled via `/beets/import/progress`.

## 3. Rekordbox export (`POST /rekordbox/export`)
`create_rekordbox_xml_from_subsonic_playlists`:
1. Fetch Navidrome playlists + their tracks.
2. Optionally filter by `playlistIds`.
3. Sort by name; enrich with stored `path_components` (from `playlist_path_table`)
   so nested folder structure is rebuilt (Subsonic playlists are flat).
4. Build the Rekordbox XML via `RekordboxXMLOrchestrator` (`pyrekordbox`).
5. Tracks not in any playlist go into a `NOPLAYLIST` playlist (when not filtering).
6. Save XML into the user's `downloads/` for the client to import into Rekordbox.

## 4. Serato import/export (`/serato/import`, `/serato/export`)
Mirrors the Rekordbox flows but reads/writes Serato `.crate` files via `pyserato`
(`SeratoController` + `SeratoCrateOrchestrator`). Crate folder hierarchy ↔
`path_components` the same way.

## 5. Watch-dir auto-import (no endpoint)
Started in `lifespan`. `poll_watchdir` watches `/user-updownloads/<user>/watch/`:
- Debounces 15s after the last add/modify, and confirms file mtimes are stable
  (guards against partial downloads).
- Enforces per-user storage quota using the **sum of pending files** (a past bug
  double-counted these — see commit `2e54187`; be careful editing the accounting).
- Sends the username to `trigger_processing`, which runs
  `consume_from_filebrowser(..., watch=True)` (files are **moved**, not copied, so
  new arrivals mid-import are left for the next cycle), then beets import + mapping.

## 6. Sync (`/sync/plan`, `/sync`, `/sync/tracks`, `/sync/playlists`)
Client tells the server which tracks/playlists it has; server computes what's
missing and zips the missing server-side tracks into `downloads/` for download.
Matching is fuzzy (`SubsonicClient.get_track_match` / `_find_best_match`) with
escalating fallbacks: title+artist → title → per-token → bracket-stripped, each
with a lower similarity threshold. `subbox_id` presence is the fast path
(`/tracks/presence`) before falling back to fuzzy matching.

## 7. Metadata (cues/loops) — `/track/metadata/*`
Cue/loop data is validated against `cue_schema` (in `routers/track.py`), stored
versioned in `library_table`, with full history in `meta_history_table`. Keyed by
`(user_id, subbox_id)`. `source_app` records origin (serato/rekordbox); `change_type`
∈ {upload, edit, sync, merge}.

## Track matching notes
- Navidrome track titles can embed artist/album; `extract_track_name` strips them.
- `SubsonicClient` cleans strings (lowercase, strip non-word, drop "remix" token for
  titles) before `difflib` similarity scoring.
- A match returns `(SubBoxTrack, similarity)`. Many callers take `match[0]`.

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

Progress polled via `/beets/import/progress`. An import is **three** phases, not one
(`ImportPhase` in `services/import_progress.py`): `importing_audio` (step 3's `beet
import`), `mapping_ids` (the subbox_id↔beet map), `applying_metadata` (step 5's BPM +
cue/loop pass), then `complete`. Only the first is visible in beets' track count, so
the job row carries the current phase and that phase's own n/total and the endpoint
composes one percentage from both — reporting the beets count alone pinned the bar at
100% for the whole tail (#51). Steps 3 and 5 batch their beets writes into a single
`docker exec` each (`utils/beets_batch.py`); they used to shell in once per track,
which cost ~13 min on a 100-track import.

Step 5's three passes all resolve the same XML tracks against Navidrome, so they
share one `TrackMatcher` (`services/track_matcher.py`) for the job: it resolves each
distinct `(title, artist, album)` once and runs the remaining lookups
`IMPORT_MATCH_CONCURRENCY` (16) at a time. Before that, a track in two playlists was
looked up once per playlist *and* again in each tail pass — ~4 sequential Subsonic
round trips per track, which is invisible locally and 12-32 s on a prod RTT (#104).

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
Import mirrors the Rekordbox flow but reads Serato `.crate` files via `pyserato`
(`SeratoController` + `SeratoCrateOrchestrator`). Crate folder hierarchy ↔
`path_components` the same way.

Export does **not** mirror it: `/serato/export` returns the playlist and track
structure and writes no files, because the client is the side that knows where
the tracks are. See `docs/api.md`.

Where it does **not** mirror Rekordbox is track identity. An RB XML carries each
track's metadata; a `.crate` carries only an absolute path on the user's machine,
which pymix never sees and which changes whenever the user moves a file. So the
client resolves each crate entry to a `subbox_id` from the local file's tag and
sends `track_identities`; the `user_location` row from `/sync/map_meta` is the
fallback, and an entry neither knows is skipped with a reason rather than failing
the import. Details and the resolution order are in `docs/api.md`.

The same asymmetry applies to cues. pymix can only read them off *its* copy of a
track, which is frozen at whatever was uploaded, so for a track the library
already had, every cue set in Serato since is invisible to it. The client reads
the file the user is actually cueing and sends the result in `track_identities`;
where it does, that reading wins and the server's copy is not read at all.

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

`/sync/playlists` always prepares exactly **one** file, named in the response's
`downloadFilename`: the tracks zip, the same zip with `subbox_rb_export.xml` added
(`includeRekordboxXml`), or the XML alone (`includeTracks: false` — a metadata-only
export, which skips all of the matching above, since none of it feeds the XML).
It's one file because a browser drops a second programmatic download silently (it
allows one per user gesture), so the client was losing the XML.

The zip has exactly **one top-level entry**, `music/`, and the XML goes *inside*
it — not beside it. macOS' Archive Utility wraps a multi-entry archive in a folder
named after it, so with the XML at the root the tracks extracted to
`<dir>/music/music/<artist>/…` while the XML's Locations said `<dir>/<artist>/…`
and Rekordbox resolved nothing. Callers name the extra via
`FileBrowserFileHandler.get_name_in_export_zip`; don't add a second root entry.

The XML's Locations are `user_root / <path relative to the user's music root>`, so
**`user_root` must be the folder containing the artist folders** — i.e. the
extraction dir *plus* `music`. Desktop sends `appPath/music` and unzips into
`appPath`; the web client appends the segment itself.

A *hit* costs one Subsonic query; a *miss* walks every tier — 2 + one query per
title token — so the widen is what the fan-out endpoints pay for. The token tier's
queries run together rather than in series, and `/sync/match_tracks` (the client's
pre-upload preview, whose normal first-time answer is "you have none of this")
spends one `getScanStatus` instead of a doomed widen per track when Navidrome has
indexed nothing yet and isn't scanning — `TrackMatcher(skip_if_library_empty=True)`,
which the import deliberately does not set (#105).

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

# HTTP API

App `root_path="/pymix"` (so behind the proxy everything is prefixed `/pymix`).
CORS allows the feishin/sub-box web origins with credentials; methods limited to
GET/POST/DELETE/PATCH/OPTIONS. Auth is by the `session_id` cookie (set on create/login)
and nothing else; `/user/storage_check` also accepts that session id as a Bearer token.

User-scoped endpoints resolve their caller with the `require_user` / `require_username`
dependency in `routers/auth.py`, which returns **401** when the cookie is missing,
unknown or expired. Endpoints used to accept an explicit `username` (query/body) as an
alternative identity; it was never verified, so any caller could act as any user by
naming them. It has been removed — `username` now appears only where it is an argument
rather than a claim of identity (`/user/create`, `/user/login`, and the `[db]` lookup
helpers).

Two variants exist for the `demo` account, which has no container stack of its own
(also in `routers/auth.py`):

- **`require_reader`** — library *reads*, i.e. `/sync*`, `/rekordbox/export`,
  `/serato/export`. Resolves `demo` to demoadmin's user row, so every downstream client
  targets demoadmin's containers.
- **`require_uploader`** — library *writes*, i.e. `/rekordbox/import`, `/serato/import`,
  `/beets/import`, `/beets/reimport`, `/beets/import/progress`, `/sync/match_tracks`,
  `/sync/map_meta`. **403s `demo` outright.** (The remaining `/beets/*` read-only count
  and duplicate endpoints just use `require_username`.)

Both are hardcoded username checks, not a role system — `demo` is a one-off account.

Three endpoints sit outside the session cookie entirely: `/admin/*` (a shared-secret
header — see "Admin" below), `GET /metrics` (a bearer token — see "Metrics"), and
`POST /invite-request`, an unauthenticated write because its caller has no account by
definition (see "Beta invites"). None is a precedent for anything else.

All endpoints live in `pymix/routers/`. Tags in brackets are the OpenAPI tags.

## Users & sessions — `routers/user.py`
| Method/Path | Purpose |
|---|---|
| POST `/user/create` | Create user + spin up their navidrome/beets/filebrowser containers (`ServicesOrchestrator.create`). Requires a valid signup `token`. Sets `session_id` cookie. |
| POST `/user/login` | Create/return a session for username+password. Sets cookie. |
| GET `/user/is_valid_token` | Check a signup token is valid (unused tokens gate registration). A pre-flight check for the signup form only — `/user/create` enforces single use itself, and does not trust that this was called. |
| GET `/user/library_size` | Bytes counted against the quota: the `bytes_used` library counter + staging (`docs/workflows.md` § Storage quota). |
| GET `/user/storage_check` | Whether an upload of `uploadSizeBytes` fits in quota; accepts Bearer or cookie. `currentUsageBytes` is the total; `trashBytes` is the part of it that is deleted tracks awaiting the reaper, and `libraryBytes` the rest (#200). |
| GET `/user/get_by_username`, GET `/user/get_by_session_id` | Operator lookup helpers. **Admin-gated** (`X-Admin-Token`) — they answer about whoever is *named* in the request, not about the caller, so they cannot authenticate anyone. The password is stripped from the response whoever asks. |

## Maintenance — `routers/maintenance.py`
| GET `/healthcheck` | Liveness. |

## Metrics — `routers/metrics.py`
| Method/Path | Purpose |
|---|---|
| GET `/metrics` | Prometheus exposition for pymix itself. Scraped by vmagent on the droplet — see `../subbox-workspace/docs/monitoring.md`. |

What it exposes, in six groups:

- **Traffic** — request rate and latency by route template, plus
  `pymix_http_requests_in_flight` (the saturation signal a duration histogram cannot
  give: on 1 vCPU a slow route queues everything behind it).
- **State**, sampled at scrape time — users against `max_number_of_users`, signup
  tokens claimed/unclaimed, invite requests by status and DJ software, sessions, jobs
  by state, wishlist items by status, and the cgroup/allocator memory figures
  `mem_watch_loop` logs.
- **The funnel**, counted as it happens — `pymix_invite_request_submissions_total`,
  `pymix_user_signups_total`, `pymix_user_logins_total`. See "Beta invites" below for
  why the submission counter exists at all.
- **Where the time goes** — `pymix_job_duration_seconds` (import/export, creation to
  completion), `pymix_dependency_request_duration_seconds` (outbound calls to a user's
  Navidrome/Subsonic), `pymix_beets_exec_duration_seconds` and
  `pymix_beets_write_lock_wait_seconds`. The request histogram is close to useless for
  the expensive routes — an import returns a job id in milliseconds and then works for
  ten minutes — so these time the work rather than the request.
- **Engagement, per user** — `pymix_user_requests_total{username}` and
  `pymix_user_last_request_timestamp_seconds{username}`, recorded in `require_user`.
  One series per user is bounded by `max_number_of_users`; a deleted user's series
  persists until restart. These are *API* activity only: playback goes straight from
  the client to the user's Navidrome and never touches pymix, so listening shows up on
  the Navidrome side of the dashboard, not here.
- **Storage quota** (#183) — `pymix_user_storage_used_bytes{username}` and
  `pymix_user_storage_limit_bytes{username}`, sampled from `user_table` (the
  `bytes_used` counter, so staging is excluded, and a never-measured user's usage is
  absent rather than 0); `pymix_storage_quota_refusals_total{path}` (`watch`,
  `rekordbox`, `serato`, `beets`, `storage_check`); and
  `pymix_user_library_drift_bytes{username}`, the signed gap between the disk and the
  counter at each reconcile, with `pymix_library_reconcile_last_completed_timestamp_seconds`
  saying how fresh that is. Large or growing drift means a write path is bypassing the
  quota recorders (`docs/workflows.md` § Storage quota).

Authenticated with `Authorization: Bearer $PYMIX_METRICS_TOKEN`, and **it has to be**:
pymix is published at `pymix.sub-box.net`, so unlike the per-user Navidrome `/metrics`
endpoints (only addressable on the Docker network) anything mounted here is reachable
from the open internet. Its own env var rather than `PYMIX_ADMIN_TOKEN` because the
holder is a scraper that needs to read one page, and `PYMIX_ADMIN_TOKEN` can recreate any
user's containers. Unset token ⇒ 503, never "no auth required".

Gauges are sampled at scrape time by a collector, not updated from business logic, so
they cannot drift from the database; each sample is guarded individually so a failing
one is omitted rather than 500ing the whole scrape. Counters are the opposite by
necessity — they record events that leave no state to sample. Request labels use the
route *template* (`/beets/{username}/status`), and anything unmatched collapses to
`<unmatched>` — otherwise every 404 from an internet scanner would mint a permanent
series.

## Rekordbox import/export — `routers/rb_import_export.py`
| Method/Path | Purpose |
|---|---|
| POST `/rekordbox/import` | Ingest the user's uploaded RB XML (+ the audio the last `/sync/map_meta` tagged; nothing else in uploads/, and no audio zip, #38) → beets import → create Navidrome playlists + import cue/rating metadata. Runs as a **background job**; returns `job_id`. Body `playlistNames: list[list[str]]` filters which playlist paths to import. Enforces storage quota. |
| POST `/rekordbox/export` | Build a Rekordbox XML from the user's Navidrome playlists. Body `user_root` (client-side music root for path rewriting) + optional `playlistIds`. Writes XML into the user's downloads dir. Kept for clients that fetch the XML as its own download; current ones ask `/sync/playlists` for it instead so the whole export is a single file. |

## Serato import/export — `routers/serato_import_export.py`
| Method/Path | Purpose |
|---|---|
| POST `/serato/import` | Ingest uploaded Serato crates (+ optional audio) → beets import → Navidrome playlists/metadata. Background job; returns `job_id`. Audio is scoped to the last `/sync/map_meta`'s files like `/rekordbox/import`, and `uploads/` is cleared whatever the outcome (#38). Reads the crates from a file named exactly **`all-crates.zip`** in the user's uploads dir, with the `.crate` files at the **root of the zip** — `parse_crates_from_root_path` uses `iterdir()`, not `rglob()`, so a Finder "Compress" of the SubCrates folder parses to zero crates. Optional body `track_identities: [{crate_path, subbox_id, cues?}]` — see below. |
| POST `/serato/export` | Return the user's playlists as the crates the **client** will write: `{crates: [{path_components, display_name, tracks: [{relative_path, title, artist, album, rating, subbox_id, cues}]}]}`. Optional body `playlistIds` (empty = all). Writes nothing — see below. |

### Why export returns data and not files
It used to write the `.crate` files itself, against a `user_root` the client sent.
A crate stores an absolute path per track and nothing else, so those files were a
prediction about a filesystem the server has never seen — and a wrong prediction
produces crates that parse perfectly and resolve nothing. The client has just
downloaded the tracks, so it knows where they landed; `relative_path` is the
track's path inside the download zip minus the `music/` prefix, taken from the
same music root the zip's own entry names are built from so the two cannot drift.
Writing crates client-side is also the only way cues can be written into the
user's real audio files.

### Who does a crate entry refer to? (`track_identities`)
A `.crate` file stores an absolute path on the **user's** machine and nothing
else. pymix never sees that file, so it cannot read its `SUBBOX_ID` tag, and the
path alone is not an identity — moving and renaming files is most of the point of
using crates.

So the client resolves each crate entry to a `subbox_id` locally (the tag is on
the file, because subbox put it there on the way out) and posts the result as
`track_identities`. Server-side resolution order, per entry:

1. `track_identities[crate_path]` — survives the user moving the file.
2. `get_meta_by_user_location(crate_path)` — the row `/sync/map_meta` wrote during
   an upload. Covers the Rekordbox-first user and anything uploaded in this same
   import.
3. Neither: the track is **skipped**, with a reason, and the import carries on.

A DJ's crates are full of records that were never uploaded to subbox, so an entry
that resolves to nothing is the normal case, not an error. It used to be an
`assert` inside a background task, which took the whole import down and surfaced
as a stack trace. Skips are counted into the job's `warnings` (see
`/beets/import/progress`) so the user is told the playlist came back short rather
than being shown a clean success. A crate whose every entry was skipped produces
*no* playlist rather than an empty one.

Two things are still hard failures, because neither is recoverable by carrying on:
a zip that parses to zero crates, and a zip where nothing at all matched.

## beets — `routers/beets_import.py`
| Method/Path | Purpose |
|---|---|
| POST `/beets/import` | Lower-level: import staged files from filebrowser into beets (`consume_from_filebrowser`). |
| GET `/beets/import/progress` | Poll an import job's progress. Returns `phase` (`importing_audio`/`mapping_ids`/`applying_metadata`/`complete`) plus `phase_n_processed`/`phase_n_total`; `percentage_complete` composes them and only reads 100 once the job is finished (#51). `reason` is why a **failed** job failed; `warnings` is what a **succeeded** job still needs to say (e.g. a Serato import that left unmatched tracks out of the playlists) — they are separate so the client can render an error and a notice differently. `trash_batch_id`, once the job has finished, names the batch that undoes the playlists a re-import replaced (POST `/trash/{id}/restore`, #208); null if it replaced none. |
| GET `/beets/import/tracks_imported` | Count of tracks currently in beets (`BeetsClient.get_number_of_tracks`). |
| GET `/beets/import/tracks_to_be_imported` | Count of staged tracks awaiting import. |
| GET `/beets/duplicates` | List duplicate tracks (`beet duplicates`). |
| DELETE `/beets/duplicates` | Remove duplicates. |
| POST `/beets/reimport` | Reimport the caller's own tracks against MusicBrainz, scoped to `query` (raw beets query syntax, e.g. `path:Artist/Album`). **Synchronous** — no job/progress polling; it's the deliberate, small-scope escape hatch (#95) that replaced the removed background automatch sweep. Returns `{matched, nomatch}`. |

## Export progress — `routers/export_progress.py`
| GET `/export/progress` | Poll an export job. |

## Track matching — `routers/sync.py`
| Method/Path | Purpose |
|---|---|
| POST `/sync/match_tracks` | Match a list of tracks against the user's Navidrome library; returns matched/unmatched flags. |
| POST `/sync/map_meta` | Tag staged uploads with `subbox_id`, persist original metadata, and record the files as the upload attempt the next import stages (#38); 400s if any track can't be tagged. |
| POST `/sync/plan` | Compute a sync plan: which requested tracks are already present vs missing on server, download size, metadata updates. Read-only. |
| POST `/sync` and POST `/sync/tracks` | Resolve requested tracks on the server and zip them into the user's downloads dir for download. `/sync/tracks` uses a more lenient multi-stage matcher. |
| POST `/sync/playlists` | Prepare **one** file for the client to download from selected server playlists. Tracks (excluding ones the client already has) zipped into the user's downloads dir; with `includeRekordboxXml: true` the XML goes *inside* that zip; with `includeTracks: false` the XML is the whole download and no zip is built. `user_root` is the XML's client-side music root, as on `/rekordbox/export`. Response `downloadFilename` is what to pass to `/sync/download` — clients must not assemble it. 400s if both are false; a failed XML fails the whole call rather than returning a zip silently missing it. One file because a browser only reliably saves one download per user gesture — a second is dropped silently. |
| GET `/sync/download/{filename}` | Stream back a file a previous call wrote to the user's downloads dir (zip or Rekordbox XML). Exists so the client fetches through its pymix session instead of filebrowser directly — which is what makes `demo` able to download at all, since its own filebrowser credential can't see demoadmin's dir (#66). Responds `Cache-Control: no-store, private` and **must keep doing so**: the path is identical for every user (`/sync/download/music.zip`), so anything that caches it serves one user's zip to the next caller — Cloudflare did exactly that in prod, caching by `.zip` extension for 4h. |

## Tracks & metadata — `routers/track.py`
| Method/Path | Purpose |
|---|---|
| POST `/tracks/presence` | Given `subbox_ids` (≤1000), return `{id: bool}` of which are already in the user's library. Lets the client skip re-uploading. |
| POST `/track/metadata/update` | Versioned update of a track's cue/loop metadata (`cuedata` validated against `cue_schema`). `source_app` ∈ {serato, rekordbox}, `change_type` ∈ {upload, edit, sync, merge}. |
| GET `/track/metadata/{track_id}` | Fetch latest cue/loop metadata for a track. |
| DELETE `/track` | Delete tracks (by `subbox_id` list) into the user's **trash** (#200): each file is moved to `/private-music/_trash/{user}/{batch_id}/` after a snapshot, `beet rm -f`, then the DB rows. The response carries `trash_batch_id` (null when nothing moved). It then starts a Navidrome scan of just the deleted files' folders (`startScan?target=1:<folder>`, `utils/navidrome_scan.py`), so the tracks read as missing in under a second: nothing else would tell Navidrome, whose scheduled scans can be off. See `services/trash.py`. |

## Trash — `routers/trash.py`
Every route is `require_uploader`: `demo` has no trash. Design: `design-playlists-and-undo.md` §8, §12 in `subbox-workspace`.

| Method/Path | Purpose |
|---|---|
| GET `/trash` | The user's restorable batches, newest first: `batch_id`, `kind`, `label`, `bytes`, `deleted_at`, `expires_at`, `state` (computed from the items), and each item's `subbox_id`/`relative_path`/`size`/`state`, plus `playlist_name` on a `playlist_entries` item and `node: {node_id, kind, name}` on a `nodes` item. Plus `trash_bytes`. |
| DELETE `/trash/{batch_id}` | Purge one batch now. 404 if it isn't the caller's. For a `nodes` batch this is when its playlists are deleted from Navidrome (`deletePlaylist`, "not found" counting as done), then their nodes (#207). One Navidrome keeps is reported in `errors` and retried by the next purge. |
| DELETE `/trash` | Purge everything restorable. |
| POST `/trash/{batch_id}/restore` | Restore a batch. A `track` batch (#209) starts a job, since it waits on a library scan: `{job_id, n_tracks}`. A `playlist_entries` batch (#208) **undoes a re-import, synchronously**: `{success, batch_id, restored[], failed[], not_restored[]}`. Each `restored` playlist has `n_entries`, `n_in_trash` (entries put back hidden because their track is in the trash; they reappear when it is restored) and `edits_discarded`, meaning the playlist had changed since the import and those changes are gone. `not_restored` lists entries whose track could not be found by subbox_id, media_file id or path. A `nodes` batch (#207) restores **synchronously**: `{success, batch_id, restored[], moved[], shrunk[], lost[]}`. Every node goes back at its old position (clamped) with its `node_id`, and every playlist with its **same Navidrome id**. `moved` is a node whose parent was deleted separately, now at the end of its nearest live ancestor or the root, with a `message`. `shrunk` is a playlist with `n_tracks_lost` fewer entries than when deleted: a track in it was purged meanwhile. `lost` is one deleted outside subbox meanwhile; `success` is false only then. It doesn't wait for jobs, and it answers 409 `tree_not_enabled` for a user without a tree. 409 while another job runs (not for `nodes`), or when nothing in the batch is restorable. |
| GET `/trash/restore/progress?job_id=` | A restore job's `in_progress`, `result`, `reason`, `warnings`, `phase` and `phases` (`checking`, `restoring_files`). |

Purging a track batch unlinks its files (the quota drops by exactly their size), then purges the tracks' missing rows from Navidrome with `DELETE /api/missing` — Navidrome runs `PurgeMissing = "never"`, so nothing else would.

Restoring one (`TrashService.restore_tracks`):
- refuses a track already back in beets (uploaded again meanwhile);
- moves each file back to its exact path and checks its sha256;
- re-adds it to beets **in place** with a plugin-free script (`utils/beets_items.py`) that only reads the file and writes back every field the item and its album had, rejoining the item's own album by id or recreating it;
- writes back the pymix rows, which brings back cues and beat grids;
- scans just the folders it put tracks back into (a targeted scan, not the whole library), and checks each track has its old Navidrome `media_file.id`.

With that id come its star, rating, play count and playlist entries. A track that came back under a new id is named in `warnings` with what it lost. `scripts/trash_restore_roundtrip.py` checks every row of the design's §13 contract against the local dev stack.

## Playlist tree — `routers/playlists.py`
`require_uploader`: `demo` gets 403, and the client draws its flat list. Design:
`design-playlists-and-undo.md` §4 and §10 in `subbox-workspace`. Every route here
answers **409 `{"detail": "tree_not_enabled"}`** while the user's `playlist_tree_state`
is `none`, which since #211 is only `demo`, which gets 403 first. A delete's restore is POST `/trash/{id}/restore` (#207).

These are the **structural** verbs. The content verbs (rename a playlist, add, reorder
or remove its tracks) stay client → Navidrome, unchanged: the tree is keyed by
Navidrome id, not by name, so they don't disturb it.

| Endpoint | Purpose |
|---|---|
| GET `/playlists/tree` | The user's playlist tree (#201). **409 `{"detail": "tree_not_enabled"}`** while their `playlist_tree_state` is `none`; the client treats that, a 403 and a 404 all as "no tree". Every read first reconciles against one `getPlaylists`, filtered to the user's own playlists: a playlist with no node is adopted at the root, and a node whose playlist has gone is dropped, with its children moved up into its place. So a playlist made or deleted directly in Navidrome shows up on the next read. |
| POST `/playlists/folders` | `{name, parent_id?, position?}` → a folder (#206), under `parent_id` (the root if absent) at `position` (the end if absent), later siblings shifting along. Returns the node. It has no `source_path`, so an import never takes it over. 400 for a blank name, 404 for a parent that isn't one of the user's live nodes. |
| POST `/playlists` | `{name, parent_id?, song_ids?}` → a Navidrome playlist with those songs in order, **and** its node at the end of `parent_id`'s children, in one call (#206): "New playlist inside" a folder. Both writes happen under the tree lock, so a tree read can't adopt the playlist at the root in between. A bad parent is refused (404) before Navidrome is asked; 502 if Navidrome refuses. Returns the node with `navidrome_playlist_id`. Upstream's create modal still creates directly in Navidrome, and that playlist is adopted at the root. |
| POST `/playlists/nodes/delete` | `{node_ids}` → `{trash_batch_id, label, deleted: {node_ids, folders, playlists}}` (#207). The nodes and **every live node under them** go into one `nodes` trash batch, in one transaction; siblings close up. **Nothing is deleted from Navidrome**: the playlists are hidden (`hidden_playlist_ids`, and left out of pymix's own lists and exports) until the batch is restored or purged. So it can't partly fail. The one Navidrome call, besides reconciling, reads each playlist's entry count through the native API, for the restore's `shrunk`. getPlaylists' `songCount` can't serve: Navidrome refreshes it only when the playlist is written. `label` is "Folder House · 6 playlists", "Playlist Deep" or "2 folders · 5 playlists". 404, and nothing deleted, if any id isn't one of the user's live nodes; 422 for an empty list. `demo` (403) deletes through Navidrome directly, with no undo. |
| PATCH `/playlists/nodes/{id}` | `{name?, parent_id?, position?}`, any together, in one commit (#206). `name` renames a **folder**; a playlist is renamed in Navidrome (400). `parent_id` moves the node and its subtree, `null` meaning the root; **absent** means "stay under the current parent", so a body of just `position` is a reorder. `position` is where it ends up among its siblings, not counting itself, clamped to the end; absent on a move means the end. A move under the parent the node already has, with no `position`, changes nothing. 400 for a move into its own subtree or an empty body, 404 for a node or parent that isn't one of the user's live nodes. Returns the node; a playlist's `name` is `null` here. |

```ts
z.object({
  nodes: z.array(z.object({          // live nodes, depth-first, siblings by position
    node_id: z.string().uuid(),
    parent_id: z.string().uuid().nullable(),   // null = root
    position: z.number().int(),                // 0..n-1 among its siblings
    kind: z.enum(['folder', 'playlist']),
    name: z.string().nullable(),               // a folder's; a playlist's is Navidrome's, read live
    navidrome_playlist_id: z.string().nullable(), // playlists only
    child_count: z.number().int(),
  })),
  hidden_playlist_ids: z.array(z.string()),  // trashed playlists: leave them out of every list
})
```

The write routes return one node, shaped as in `nodes` above without `child_count`.

Every playlist listing pymix itself makes (exports, sync) goes through
`SubsonicOrchestrator._visible`. It leaves out other users' public playlists, and
the user's hidden ones.

## Wishlist — `routers/wishlist.py`

The wishlist is "tracks the user wants but doesn't have yet". Statuses and the
resolve/reconcile lifecycle are in `docs/design-wishlist-library-automatch.md`; the
`../subbox-slskd` downloader is a second client of these routes.

| Method/Path | Purpose |
|---|---|
| GET `/wishlist` | List the caller's items. Optional `status` / `resolve_state` query filters (validated against the enums; 400 on an unknown value). |
| POST `/wishlist`, POST `/wishlist/bulk` | Create one / many items. |
| GET `/wishlist/{id}`, PATCH `/wishlist/{id}`, DELETE `/wishlist/{id}` | Fetch, update (status transitions land here — `download_wishlist.py` uses PATCH to flip an item to `downloaded`), delete. |
| POST `/wishlist/parse-link` | Extract artist/title metadata from a pasted YouTube/Bandcamp/SoundCloud URL via `LinkParseService` (yt-dlp). 400 if nothing usable comes back. |
| POST `/wishlist/match-metadata` | Resolve free-text (hand-typed, or an `inbox` note) to a canonical MusicBrainz artist/title. |
| POST `/wishlist/{id}/match-youtube` | Find a YouTube match for one item. |
| POST `/wishlist/reconcile` | Run the library reconcile for the caller now, instead of waiting for the background loop: flips open items to `available` when the track has appeared in their library. |
| PATCH `/wishlist/sheet`, GET `/wishlist/sheet/status` | Attach a Google Sheet as a wishlist source, and read its last sync status/error. |

## Admin — `routers/admin.py`

**Not part of the client-facing API** — operator-only infra maintenance, gated by an
`X-Admin-Token` header compared against the `PYMIX_ADMIN_TOKEN` env var
(`require_admin_token`). It **fails closed**: an unset env var 503s every admin request
rather than opening the routes. There is no admin role in the user model; this is
deliberately the minimal gate, not a general auth scheme.

| Method/Path | Purpose |
|---|---|
| GET `/admin/beets/{username}/status` | Read-only audit of that user's beets container: beet version, plugins, stats, one-track `subbox_id` spot check. No lock, no mutation. |
| POST `/admin/beets/{username}/migrate` | Recreate that user's beets container on the current template/version (#76). Backs up `musiclibrary.blb` first and returns before/after status with `stats_match`/`sample_match`. If the existing container belongs to a different compose project it is removed first, and the project it came from is reported as `removed_foreign_project` — see below. |
| GET `/admin/playlists/tree-state` | Read-only: each user's `playlist_tree_state` (demo aside) and `still_none`, without touching any Navidrome. Migration 025 (#211) won't run, and pymix won't start, until `still_none` is 0. The migration pass that moved users to `live`, POST `/admin/playlists/migrate` (#205), was removed by #211 along with `playlist_path_table`. |
| GET `/admin/memory` | Full allocator-level snapshot of the pymix process: kernel RSS (incl. `rss_peak_mb` = `VmHWM`), the container's `cgroup` limit and charge, glibc `mallinfo2`, arena heaps, and a verdict separating a native leak from free-list retention. `?objects=true` adds a gc type histogram (expensive — it walks every tracked object; leave it off when sampling). |
| POST `/admin/memory/trim` | `malloc_trim(0)` — hand entirely-unused free-list pages back to the kernel, reporting RSS either side. A POST because the before/after delta only means anything the first time. |
| POST `/admin/memory/tracemalloc/start` | Start recording Python-level allocation sites, with a baseline. Real overhead while active. |
| GET `/admin/memory/tracemalloc/top` | Allocation sites that grew most since `start`. Flat here means "not Python-level", not "no growth" — a raw `malloc` in a C extension is invisible to tracemalloc. |

`/admin/memory` is pull-based: it answers only while someone is asking. The `mem_watch_loop`
background task (see `architecture.md`) is the push half — it logs a sample every
`memory_watch.interval_s`, so an OOM kill leaves a trajectory in the log instead of a gap.

### Migrating a container compose doesn't own

Compose matches containers to services by the `com.docker.compose.project`/`service`
labels, not by name, so a beets container created any way other than by pymix — e.g. a
host-side `docker compose up -d` run from `docker-compose/beets/` without `-p`, which
lands in project `beets` with service `beets{user}` — is invisible to
`compose --project-name beets{user} up`. It tries to *create* rather than recreate and
the daemon rejects the duplicate name. `migrate` now detects that and removes the old
container before bringing the service up; this is safe because all beets state lives on
the `/config` bind mount and the `private-music`/`private-staged` volumes, and it happens
only after the "before" status has been read and the library backed up.

## Vestigial — `routers/create.py`

`GET /create/subsonic` and `GET /create/xml` are pre-product leftovers with hardcoded
`/Users/lajp/...` paths. Nothing calls them and they can't work as deployed. Don't
extend them; the live equivalents are `/rekordbox/import` and `/rekordbox/export`.

## Beta invites — `routers/invite_request.py`
| Method/Path | Purpose |
|---|---|
| POST `/invite-request` | **Unauthenticated.** Capture a prospective beta tester: `{email, dj_software, dj_software_other?}` where `dj_software` ∈ {rekordbox, serato, other}. Upserts on email; always returns `{"status": "ok"}`. |

The one route that must not resolve a caller: it exists precisely to capture someone with
no account (the demo → beta funnel, subbox-app#69), so `require_user` cannot apply. What
that costs, and how it's paid for:

- **Abuse controls replace the cookie.** A per-IP cap (`check_rate_limit`, 5/hour) and a
  4 KB body cap, both in `routers/invite_request.py`. The IP comes from the *rightmost*
  `X-Forwarded-For` entry — Traefik appends the peer it saw, so anything further left is
  client-supplied. The limiter is in-process, so it resets on restart; it's a speed bump,
  not a security boundary. No captcha until it's actually abused.
- **400, not 422.** The body is parsed by a `Depends(parse_invite_request)` rather than a
  `body:` parameter, so bad input comes back as one flat 400 the client can render inline
  against the email field (and so the size cap runs before anything is parsed).
- **No membership oracle.** The response is identical whether the address was new or
  already on the list, so the route can't leak who has signed up.
  `create_invite_request` *does* return which branch of the upsert it took
  (`'created'` / `'refreshed'`), but only to feed an aggregate counter — no address
  reaches the metric, and the response body is unchanged.

### Measuring the funnel

Every submission is counted at the point it is decided, as
`pymix_invite_request_submissions_total{outcome}` — `created`, `refreshed`, `invalid`,
`too_large`, `rate_limited`, `error`. The reason it exists: the row count alone cannot
show a drop, because a submission that never became a row leaves nothing behind to
sample. `submissions - created` is exactly the set of people who pressed the button and
are not in the table, and the largest term in it is normally `refreshed` — the upsert
means a second submission is a success that adds no row.

Two more, sampled: `pymix_invite_request_oldest_new_age_seconds` (how long somebody has
been waiting on the manual fulfilment step; absent, not zero, when the queue is empty)
and `pymix_invite_request_last_arrival_timestamp_seconds` (the funnel-went-quiet
signal).

**What pymix still cannot see** is a click that never became a request — a browser that
blocked it, a network that dropped it, client-side validation that stopped it. The
nearest proxy is the CORS preflight: browsers send `OPTIONS /invite-request` before the
POST and both land in `pymix_http_requests_total`, so OPTIONS materially exceeding POST
means the form reaches the server and the submission does not. Anything earlier than
that needs telemetry in subbox-app.

There is deliberately **no listing endpoint**. (`require_admin_token` has since arrived
for `/admin/*`, so one is now *possible* — it still isn't worth it while fulfilment is a
handful of rows read by hand.) Fulfilment is manual: read `invite_request_table`,
mint a `user_token_table` row, then set the request's `status` to `invited`/`declined` by
hand. If that becomes routine, add a script under `scripts/` rather than an
unauthenticated route.

## Conventions for adding an endpoint
1. Put the route in the topical router (or create a new module and register it — see
   architecture.md "When you add a router").
2. Identify the caller with `user: dict = Depends(require_user)` (or
   `username: str = Depends(require_username)`) from `routers/auth.py`. Never take a
   `username` param as the caller's identity — it is unauthenticated. Don't hand-roll a
   guard block; the dependency 401s on its own, so the handler body can assume a user.
   If the route reads a library use `require_reader`, if it writes one use
   `require_uploader` — picking the wrong one either breaks `demo` or lets it write.
3. `@inject` your collaborators with `Depends(Provide[Container.x])`.
4. Delegate to a controller/orchestrator — keep the router thin.
5. Match the response style of the router you're in (plain dict vs Pydantic model).
6. Long-running work → `BackgroundTasks` + a job row.

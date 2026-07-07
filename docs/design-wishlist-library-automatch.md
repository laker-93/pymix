# Design: Wishlist ↔ library auto-match

Status: **implemented**

## Goal

Resolve any open wishlist items whose track now exists in the user's library —
flipping them to `available` and stamping the matched `subbox_id` so the client can
deep-link to play the track. This runs **both** automatically when an import completes
and on demand via an endpoint, so an item a user wishlisted before acquiring the track
no longer stays stuck at `wishlist` forever.

## Lifecycle context

Wishlist statuses (`model/wishlist.py`, `WishlistStatus`): `inbox → wishlist →
downloaded → available → ignored`.

- `inbox` — raw note, not yet curated into artist/title.
- `wishlist` — curated, want to acquire (artist + title known).
- `downloaded` — file has landed but not yet in beets.
- `available` — in the collection / playable now.

Because **Navidrome serves directly off the beets library** (the `subsonicupdate`
beets plugin pings Navidrome to rescan as part of `beet import`), "in beets" already
means "playable now". There was previously a separate `imported` state, but it meant
exactly the same real-world thing as `available`, so the two were **collapsed into
`available`** (migration `010`). The auto-match sets `available` directly.

The status is a native Postgres enum (`wishlist_status`), so the set of states is
explicit at the DB level. Adding a state later needs an `ALTER TYPE wishlist_status ADD
VALUE …` migration.

## Matching

`WishlistReconcileService.reconcile_user(user)`
(`services/wishlist_reconcile_service.py`):

1. Load the user's open wishlist items — status in `{wishlist, downloaded}`. Skip
   `inbox` (no clean artist/title to query on), and never re-scan `available` /
   `ignored`.
2. For each item, ask the user's **Navidrome** whether the track already exists, via
   `SubsonicClient.get_track_match(user, title, artist, album)`
   (`clients/subsonic_client.py`). This asks the *same question* as
   `scripts/download_wishlist.py`'s `is_in_collection` — "is this track in the user's
   Navidrome?" — but is **not** the same implementation. The download script is
   deliberately stdlib-only (it runs under a bare `python3` on a user's laptop), so it
   carries its own single-query `SequenceMatcher` check; pymix, with no such
   constraint, uses its richer multi-stage matcher (`get_track_match`: query by
   title+artist, then title, then title tokens, with weighted title/artist/album
   similarity and bracket-stripping fallbacks). The two are intentionally-parallel
   presence checks that should stay behaviourally aligned; they can't share code across
   the stdlib boundary. (The pure "Artist - Title" title split *is* mirrored between
   the two — see `link_parse_service._split_artist_title` and the script's
   `split_youtube_artist_title`.)
3. On a hit → set the item's status to `available` and `linked_subbox_id` to the
   matched track's `subbox_id`. The search result carries `pymix_path` but not the
   tag-derived `subbox_id`, so the service reads it with `get_subbox_id(track.pymix_path)`
   (the same call `SubsonicClient._parse_tracks` uses).

### Why Navidrome search rather than a beets `fuzzy` query

An earlier draft proposed matching with the beets `fuzzy` plugin via `beet ls
artist:~ title:~` per item. We instead use `SubsonicClient.get_track_match`: it
already exists, applies the same "search Navidrome" presence check the wishlist
download script relies on, and needs no per-user beets-config migration (the `fuzzy`
plugin would have had to be enabled and every user's `beets{user}` container
re-rendered). Navidrome is the user's library index, so it is the right source of truth
for "do I have this yet?".

### Edge cases

- **Multiple library hits** for one item → `get_track_match` returns the best single
  match; do not fan out.
- **No hit** → leave the item untouched; a later import or a manual `PATCH
  /wishlist/{id}` resolves it.
- **A single failed search** is logged and skipped — it must never abort the sweep or
  the import that triggered it.

## Triggers

**On demand:** `POST /wishlist/reconcile` (`routers/wishlist.py`) — resolves the
caller's username, loads the user record, and runs `reconcile_user`. Returns
`{"resolved": <count>}`. This is the in-pymix equivalent of running
`download_wishlist.py`, minus the Soulseek download step.

**Automatically on import completion**, once `subbox_id`s are mapped into beets:

- Rekordbox / beets import + watch-dir auto-import — in
  `RekordboxXMLController.consume_from_filebrowser`, after the sync
  `_consume_from_filebrowser` (which runs `_map_subbox_id_beet_id`) returns. This single
  hook covers the manual `POST /beets/import` path (`routers/beets_import.py →
  run_import_task`) and the watch-dir path (`handlers/filebrowser_file_handler.py →
  trigger_processing`), since both funnel through this method. Skipped for `public`
  imports (they don't land in a user's Navidrome).
- Serato import — in `SeratoController.create_subsonic_playlists_from_crates`, after the
  Navidrome scan + `_set_data_from_crates`. Serato has its own import path that does not
  pass through `consume_from_filebrowser`, hence the separate hook.

`WishlistReconcileService` is registered in the DI container (`containers.py`) and
injected into both controllers and the wishlist router.

### Cost

One Subsonic search (per the matcher, sometimes a small number of fallback searches)
per **open** wishlist item, per import. Acceptable at personal scale, and the
open-status filter shrinks the set as items get matched.

## Code surface

- **`WishlistStatus` enum** (`model/wishlist.py`) — replaces the free-string status
  tuple; `imported` removed. `WISHLIST_STATUSES` is kept as a tuple of its values for
  the router's membership validation.
- **`WishlistRow.status`** (`model/db_tables.py`) — now a SQLAlchemy `Enum` bound to
  `WishlistStatus`.
- **`WishlistReconcileService`** (`services/wishlist_reconcile_service.py`) — deps
  `DbController` + `SubsonicClient`.
- **`mark_wishlist_item_imported` → `mark_wishlist_item_available`**
  (`controllers/db_controller.py`) — sets `available`.
- **Migration `010`** — collapses `imported → available` and converts the column to the
  `wishlist_status` enum.

## Out of scope / future

- **Provenance-based matching** (embedding `wishlist_id` / `youtube_video_id` into the
  file at download time for exact matching) — would require a server-side download step
  that does not exist yet. `youtube_match_service` only *searches* YouTube today.
- **Add-time matching** ("do I already own this?" the moment an item is wishlisted) —
  `reconcile_user` is structured so it can be reused for this later.
- **Client follow-up** (`subbox-app`): surface `linked_subbox_id` as a play / deep-link
  action on the wishlist card. Small, separate change.

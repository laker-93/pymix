# Design: Wishlist ↔ library auto-match on import

Status: **proposed** (not yet implemented)

## Goal

When an import completes, automatically resolve any open wishlist items whose track
now exists in the user's beets collection — flipping them to `available` and stamping
the matched `subbox_id` so the client can deep-link to play the track.

Today the wishlist lifecycle has the plumbing for this (`mark_wishlist_item_imported`,
`get_wishlist_items_by_status` in `controllers/db_controller.py`) but **nothing calls
it**: no import path reconciles imported tracks against the wishlist, so an item a user
wishlisted before acquiring the track stays stuck at `wishlist` forever.

## Lifecycle context

Wishlist statuses (`model/wishlist.py`): `inbox → wishlist → downloaded → imported →
available → ignored`.

- `inbox` — raw note, not yet curated into artist/title.
- `wishlist` — curated, want to acquire (artist + title known).
- `downloaded` — file has landed but not yet in beets.
- `imported` / `available` — in the collection / playable.

Because **Navidrome serves directly off the beets library** (the `subsonicupdate`
beets plugin pings Navidrome to rescan as part of `beet import`), "in beets" already
means "playable now". So `imported` and `available` collapse into one real-world
state, and the auto-match sets **`available`** directly — there is no meaningful
intermediate to occupy.

## Trigger

Reconciliation runs **once per import, on completion**.

Hook point: `RekordboxXMLController._consume_from_filebrowser`
(`controllers/rekordbox_xml_controller.py`), in the `finally` block **after**
`_map_subbox_id_beet_id`. That ordering is required: `_map_subbox_id_beet_id` is what
guarantees every imported track carries its `subbox_id` in beets, which is the value
the wishlist item links to.

This single hook covers **both** import paths, since both funnel through
`_consume_from_filebrowser`:

- manual `POST /beets/import` → `run_import_task`
- watch-dir auto-import → `trigger_processing`

`username` is in scope at this point, so the sweep is naturally scoped to the user
whose import just finished.

## Matching

A new `WishlistReconcileService.reconcile_user(username)`:

1. Load the user's open wishlist items — status in `{wishlist, downloaded}`. Skip
   `inbox` (no clean artist/title to query on), and never re-scan `imported` /
   `available` / `ignored`.
2. For each item, run a fuzzy query in the user's beets container (`beets{username}`),
   using the **beets `fuzzy` plugin** to match against artist and title:

   ```
   beet ls -f '$subbox_id' artist:~'<artist>' title:~'<title>'
   ```

   e.g. `beet ls artist:~'Binary Digat' title:~'overdozza'`. The two `~` field terms
   AND together (both must clear the fuzzy threshold); `-f '$subbox_id'` returns the
   matched track's `subbox_id` directly, so no second lookup is needed.
3. On a hit → set the item's status to `available` and `linked_subbox_id` to the
   matched `subbox_id`.

The same `docker.execute(container_name, beets_command.split())` pattern already used
by `RekordboxXMLController.get_path_by_subbox_id` applies here.

### Cost

One `beet ls` (one `docker exec`) per **open** wishlist item, per import. Acceptable
at personal scale, and the open-status filter shrinks the set as items get matched.

Reserve fallback if N ever grows large: a single `beet ls -f '$subbox_id::$artist::$title'`
dump + rapidfuzz matching in Python — same algorithm (difflib-style ratio), one exec.

### Edge cases

- **Multiple library hits** for one item → take the first; do not fan out.
- **No hit** → leave the item untouched; a later import or a manual `PATCH
  /wishlist/{id}` resolves it.
- **A single failed beet query** is logged and skipped — it must never abort the
  sweep or the import itself.

## Code surface

- **Generalize `mark_wishlist_item_imported`** (`controllers/db_controller.py`). It
  currently hardcodes `status="imported"`; make the target status a parameter (or add
  a sibling `mark_wishlist_item_available`) so the auto-match can set `available`.
- **New `WishlistReconcileService`** (deps: `DbController` + docker exec), registered
  in the DI container (`containers.py`), invoked from the import `finally` block.
- Use the user-scoped `get_wishlist_items(username, status)` for loading candidates.
  The cross-user `get_wishlist_items_by_status` helper is no longer needed and can be
  dropped (or repurposed if a cross-user sweep is ever added).

## Beets config + migration

The `fuzzy` plugin must be enabled per user. **This is a required change** — the
`artist:~` / `title:~` query syntax does not work unless the plugin is loaded.

Edit `templates/beets/config.yaml`:

- Add `fuzzy` to the `plugins:` line:

  ```yaml
  # before
  plugins: web fetchart lyrics lastgenre embedart duplicates info subsonicupdate
  # after
  plugins: web fetchart lyrics lastgenre embedart duplicates info subsonicupdate fuzzy
  ```

- Add a `fuzzy` config block to set the threshold:

  ```yaml
  fuzzy:
      threshold: 0.7   # default; tune from here
  ```

The threshold is **global** — beets has no per-query override — so one value governs
all fuzzy matching.

**Existing user containers have a baked-in rendered config** and will not pick up the
plugin from a template edit alone. Re-rendering + restarting each user's `beets{user}`
container is a distinct migration task that must ship with this feature.

> The `artist:~'<artist>' title:~'<title>'` field-scoped fuzzy syntax has been tested
> and confirmed working against the beets version in use.

## Out of scope / future

- **Provenance-based matching** (embedding `wishlist_id` / `youtube_video_id` into the
  file at download time for exact matching) — would require a server-side download step
  that does not exist yet. `youtube_match_service` only *searches* YouTube today.
- **Add-time matching** ("do I already own this?" the moment an item is wishlisted) —
  the `reconcile_*` unit is designed so it can be reused for this later.
- **Client follow-up** (`subbox-app`): surface `linked_subbox_id` as a play / deep-link
  action on the wishlist card. Small, separate change.

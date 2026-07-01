# Design: As-is import + async background automatch

Status: **proposed** (not started)

## Goal

Split the current beets import into two phases so the interactive import is fast,
deterministic, and offline:

1. **Import as-is** — no autotagger on the critical path. Files land in the library
   immediately with whatever tags they arrived with.
2. **Background automatch** — an async, off-the-critical-path sweep that reimports
   already-landed tracks and attempts MusicBrainz matches, applying confident ones and
   leaving the rest as-is.
3. **(Future) UI-driven correction** — let a user edit artist / title / MusicBrainz id
   for a track in the client; pymix does a targeted reimport to correct the tags.

"Offline" here means **off the user's interactive critical path**, not airgapped: the
background pass still calls the real MusicBrainz / AcoustID web services, it just does so
asynchronously (decision confirmed with the user — a local MB mirror is out of scope).

## Current state (what this replaces)

Import today (`controllers/rekordbox_xml_controller.py`, `_consume_from_filebrowser`):

```
beet import --group-albums --set user={username} --set public={public} -q /downloads
```

`-q` (quiet) plus the config (`quiet_fallback: asis`, `strong_rec_thresh: 0.20`,
`match.ignored: missing_tracks`) means beets **already hits MusicBrainz online for every
album on the critical path**, and only falls back to as-is when it can't get a strong
match. So the interactive import is online, slow, and non-deterministic — exactly what
this design pulls apart.

### The cross-system identity, and how it flows through beets

`SUBBOX_ID` is the durable cross-system track identity (see workspace `CLAUDE.md` and
`../pymix/CLAUDE.md`). It exists in **two independent stores**, and the distinction is
load-bearing for this design:

- **File tag `SUBBOX_ID`** — a raw tag (TXXX for MP3, Vorbis comment for FLAC, an
  iTunes freeform atom for MP4) written by pymix via taglib/mutagen at staging time
  (`utils/tag_subbox_id.py`). **beets has no plugin or `types` mapping for it** — beets
  is oblivious to this tag.
- **beets DB flexattr `subbox_id`** — bridged in *after* import by
  `_map_subbox_id_beet_id`: it lists freshly imported tracks (`beet list subbox_id::^$`),
  reads the **file tag** back off each imported file with `get_subbox_id(path)`, and
  mirrors the value into the beets DB with `beet modify -y id:{beet_id} subbox_id=…`.
  This also populates the `subbox_id → beet_id` map pymix relies on.

The bridge only works because the file tag survives import. See the verification below
for why it currently does, and the trap that could silently break it.

## Design

### Phase 1 — as-is import

Change the import command to genuinely skip the tagger:

```
beet import -A --group-albums --set user={username} --set public={public} /downloads
```

`-A` (`--noautotag`) imports as-is with no MusicBrainz round-trips and no prompting; drop
`-q` (redundant under `-A`). The whole `match:` config block stops mattering for import
(it's only used by the background reimport now). `_map_subbox_id_beet_id` and the wishlist
reconcile hook are unaffected — they key off the SUBBOX_ID file tag, which as-is import
preserves.

This is independently shippable and the highest-value part; ship it before the async
machinery.

### Phase 2 — background automatch sweep

Mirror the existing `wishlist_reconcile_loop` (`handlers/wishlist_reconcile_handler.py`):
a new handler + service registered in `containers.py` / `runner.py`, running on a poll
interval from config. Per cycle, per user, sequentially:

1. **Select unmatched tracks.** Stamp each track with an `automatch_state` beets flexattr
   at import time (`--set automatch_state=pending`). The loop queries
   `beet list automatch_state:pending` inside the `beets{username}` container. States:
   `pending → matched | nomatch`. `nomatch` (tried, no confident MB hit — normal for
   white labels / heavily edited files) is terminal so tracks aren't reprocessed forever.
2. **Reimport in place** using beets *library reimport*, which re-runs the tagger on
   tracks already in `/music` without re-staging:
   ```
   beet import -L -q automatch_state:pending
   ```
   With autotag on + `quiet_fallback: asis`, confident matches are applied and files
   renamed per the `paths:` templates; the rest stay as-is.
3. **Re-map subbox_id → beet_id explicitly.** The existing `_map_subbox_id_beet_id` only
   processes tracks whose `subbox_id` flexattr is *empty*, so it will **skip** already-mapped
   tracks and cannot repair the map after a reimport. The sweep needs its own re-map step.
4. **Re-stamp `automatch_state`** — `matched` where `mb_trackid` is now non-empty, else
   `nomatch`.
5. **One Navidrome rescan at the end of the batch**, not per track.

### Phase 3 — UI-driven correction (future)

Endpoint keyed on `subbox_id` (the identity everything else uses; the client already has
it on the track):

- **User supplies an MBID** → exact correction:
  `beet import -L --search-id {mbid} subbox_id:{id}`. beets fetches that specific
  release/recording and applies it — no guessing.
- **User edits artist / title only** → `beet modify -y subbox_id:{id} artist=… title=…`,
  then set `automatch_state=pending` so the next sweep re-matches using the corrected
  fields as hints (much better hit rate).
- Set `automatch_state=matched` afterwards (user-corrected is authoritative) and trigger a
  Navidrome rescan.

This is the cross-repo piece: pymix endpoint + subbox-app pymix-API client/UI, shipped
together per `docs/integration.md`. Current values (artist/title/MBID) are already readable
by the client from Navidrome/Subsonic, so the endpoint only needs `subbox_id` + new values.

## Verification (done before committing to the design)

Two properties the whole design rests on were tested empirically, partly against the live
local `beetstest260526` container.

### 1. Does anything strip the SUBBOX_ID file tag on import/reimport? → No in prod, but a trap exists

- beets' **`scrub` plugin destroys the SUBBOX_ID file tag**: `_scrub` calls mutagen
  `f.delete()` (removes *all* tags), then restores only beets/MediaFile-known fields —
  and SUBBOX_ID is not one. Reproduced locally (FLAC and MP3) with `scrub` in the plugins
  list: the tag was gone after import, and the `_map_subbox_id_beet_id` bridge then reads
  `None` and skips the track (no mapping, no DB flexattr).
- **Production is safe** because the deployed config does **not load the scrub plugin**.
  The plugins line is `web fetchart lyrics lastgenre embedart duplicates info
  subsonicupdate musicbrainz` — no `scrub`. The `scrub: auto: yes` block in the template
  is **inert dead config**. Confirmed on a real imported file in `beetstest260526`:
  file tag `SUBBOX_ID=67014efc-…` and DB flexattr `subbox_id=67014efc-…` both present.
- **Trap:** because a `scrub: auto: yes` block sits in the config looking active, anyone
  enabling the scrub plugin would silently destroy SUBBOX_ID on every import and reimport.
  **Recommendation: delete the dead `scrub:` block from `templates/beets/config.yaml`.**

### 2. Does library reimport (`-L`) preserve the beet item id and tags? → Yes

Controlled `-L` reimport with a forced rename:

```
before:   id 1 | Track One     | subbox_id=CTRL-1234
after -L: id 1 | Renamed Title  | subbox_id=CTRL-1234 | file SUBBOX_ID=['CTRL-1234']
```

Item **id stayed stable**, the `subbox_id` DB flexattr survived, and the file tag survived
the rename. So `subbox_id → beet_id` stays valid across reimport.

Caveats (not yet exercised live):
- Verified for an **as-is** `-L` (`-A`). A `-L` that *applies a MusicBrainz match* is
  beets' documented id-stable reimport behavior, but wasn't tested live (needs network +
  a matchable track). Worth one live test before shipping Phase 2.
- Even if an id ever did change, step 3 above (explicit re-map) is the safety net; don't
  rely on the import-time mapper, which skips already-mapped tracks.

### Environment note

`requirements.txt` pins `beets~=2.2.0`, but the deployed container runs **beets 2.7.1**
(`lscr.io/linuxserver/beets:latest`). Local behavior repros won't match the deployment
unless run against the container.

## Concurrency & churn

- **Per-user lock.** A foreground `beet import` and the background reimport must never
  touch the same `beets{username}` container concurrently — SQLite `.blb` lock contention
  plus concurrent file moves within `/music`. The existing import-job table is the natural
  place for a per-user "beets busy" flag; take it in both paths.
- **Throttle** like the wishlist loop: one user at a time, sequential, low frequency.
- **Rescan batching:** reimport renames files → Navidrome rescans; batch and rescan once
  per user per cycle.

## Build order

1. Phase 1 (`-A` import) — independent, shippable, immediate win.
2. Remove the dead `scrub:` block from the beets config template (cheap, removes a
   footgun; do it with Phase 1).
3. `automatch_state` flexattr + background `automatch_loop` (Phase 2), reusing the
   wishlist-loop scaffolding, with the explicit re-map step and per-user lock.
4. Phase 3 correction endpoint + subbox-app UI (cross-repo, per `docs/integration.md`).

## Code surface (anticipated)

- `controllers/rekordbox_xml_controller.py` — `_consume_from_filebrowser` import command
  (`-A`, `--set automatch_state=pending`); a reusable re-map that doesn't depend on an
  empty `subbox_id` flexattr.
- `templates/beets/config.yaml` — remove the inert `scrub:` block.
- New `handlers/automatch_handler.py` + service (mirrors
  `handlers/wishlist_reconcile_handler.py` / `services/wishlist_reconcile_service.py`).
- `containers.py` / `runner.py` — register the loop + poll interval config.
- New router for Phase 3 correction (`routers/track.py` already handles per-`subbox_id`
  track ops — likely extends there).
- Import-job table / `db_controller` — per-user beets lock.

## Out of scope / future

- Local MusicBrainz mirror + local AcoustID fingerprinting (airgapped matching). Possible
  later (there are `acoustid-fork` / chromaprint siblings in the workspace) but a much
  larger per-container infra lift; explicitly deferred.
- Reconciling the `requirements.txt` beets pin (2.2.0) with the deployed image (2.7.1).

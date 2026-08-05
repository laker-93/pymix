# Design: As-is import + async background automatch

Status: **Phase 2's background sweep removed 2026-08-05, superseded by a manual
reimport endpoint** (`POST /beets/reimport`, `AutomatchService.manual_reimport` —
laker-93/pymix#95). The idle-detection/chunking/wall-clock-budget machinery this doc
describes below existed to let the sweep run safely against every enabled user's
whole library unattended; in practice its main real-world use was reimporting a
track a human had already noticed was mismatched (#95), which a synchronous,
caller-scoped endpoint does more directly with none of that machinery. The rest of
this doc (Phase 1's as-is import, the Navidrome rename-preservation gate, the
`automatch.yaml` overlay) is still current — only the *background, unattended*
sweep described in *Build order* step 7 onward and *Phase 2* is gone. Phase 3
(UI-driven correction) is now moot as originally scoped; a manual query is the UI
surface for now.

Status (historical): **Phase 1 and Phase 2 shipped** (concurrency approach decided 2026-08-02; premise
corrected and open questions resolved 2026-08-02 — see *Revision note*; Navidrome rename gate
run 2026-08-04, failed — see *Renames and Navidrome* — sweep ships with `move: no`; Phase 2
sweep landed 2026-08-04, see *Build order* step 7). Phase 3 (UI-driven correction) is the
only piece not yet built.

## Revision note (read this first if you read the earlier draft)

The first draft was built on a false premise: it claimed the current import "already hits
MusicBrainz online for every album on the critical path". **It does not.** beets ≥ 2.3 moved
MusicBrainz into a plugin, the deployed containers run beets 2.7.1, and the shipped template
does not load that plugin — so the autotagger currently has *zero* candidate sources and
every album already falls back to as-is. Details and evidence in *Current state* below.

What that changes:

- **Phase 1 (`-A`) is not the speed win it was sold as.** The speed is already banked. It is
  still worth doing — it makes the as-is behaviour explicit and deterministic instead of an
  accident of plugin configuration — but expect no visible import speedup.
- **Phase 2 is where MusicBrainz is introduced to the platform for the first time.** Enabling
  the `musicbrainz` plugin is a Phase 2 prerequisite, not an implementation detail. It was
  entirely absent from the first draft.
- **A config/version migration is now required**, because per-user beets configs and beets
  versions are frozen at provisioning time and have already drifted (see *Prerequisites*).
- The "remove the dead `scrub:` block" work item is **already done** (commit `369b25f`) and
  has been dropped. The *never enable scrub* warning is retained.

The first draft's verification section read its config off `beetstest260526` — a container
provisioned before commit `ff5e6d4` (which removed the `web` plugin), whose config also has a
hand-added `musicbrainz` line — and described it as "the deployed config". Its plugin line
matches the old draft verbatim, `web` included, which is the tell. **Anything asserted about
live behaviour must be verified against a container provisioned from the current template, or
against the droplet itself — not against a long-lived local test container.**

## Goal

Split the current beets import into two phases so the interactive import is fast,
deterministic, and offline:

1. **Import as-is** — no autotagger on the critical path. Files land in the library
   immediately with whatever tags they arrived with.
2. **Background automatch** — an async, off-the-critical-path sweep that reimports
   already-landed tracks and attempts MusicBrainz matches, applying confident ones and
   leaving the rest as-is. It runs only while the user is idle.
3. **(Future) UI-driven correction** — let a user edit artist / title / MusicBrainz id
   for a track in the client; pymix does a targeted reimport to correct the tags.

"Offline" here means **off the user's interactive critical path**, not airgapped: the
background pass still calls the real MusicBrainz / AcoustID web services, it just does so
asynchronously (decision confirmed with the user — a local MB mirror is out of scope).

## Current state (what this replaces)

Import today runs from three separate call sites, each with its own command string:

| Call site | Command |
|---|---|
| `controllers/rekordbox_xml_controller.py:330` (`_consume_from_filebrowser`) | `beet import --group-albums --set user={username} --set public={public} -q /downloads` |
| `controllers/rekordbox_xml_controller.py:434` (`_import_to_beets`) | `beet import --group-albums --set user={username} -q /downloads` |
| `controllers/serato_controller.py:115` | `beet import --group-albums --set user={username} -q /downloads` |

**Every change in this design that touches the import command must touch all three.**

### The autotagger is already inert — the `musicbrainz` plugin is not loaded

`templates/beets/config.yaml:4` reads:

```
plugins: fetchart lyrics lastgenre embedart duplicates info subsonicupdate
```

No `musicbrainz`. In beets ≥ 2.3 the MusicBrainz metadata source is a plugin, and the
autotagger sources *all* candidates from loaded metadata-source plugins:

- `beets/autotag/match.py:320` → `metadata_plugins.candidates(...)`
- `beets/metadata_plugins.py` → `find_metadata_source_plugins()` returns
  `[p for p in find_plugins() if hasattr(p, "data_source")]` — loaded plugins only.

No plugin ⇒ no candidates ⇒ `quiet_fallback: asis` fires for every album. The `match:` block
(`strong_rec_thresh: 0.20`, `ignored: missing_tracks`, the distance weights) is currently dead
config; it only starts mattering once the plugin is enabled for the Phase 2 sweep.

Verified empirically against the two live local containers:

| Container | Provisioned | beets | Loaded plugins |
|---|---|---|---|
| `beetstest260526` | pre-`ff5e6d4`, config hand-edited | 2.7.1 | `duplicates, embedart, fetchart, info, lastgenre, lyrics, musicbrainz, subsonic, web` |
| `beetstest300726` | current template | 2.7.1 | `duplicates, embedart, fetchart, info, lastgenre, lyrics, subsonic` |

Both run the same `lscr.io/linuxserver/beets:latest` tag and both report 2.7.1, while the
current latest stable beets is **2.13.1** — `:latest` resolves at pull time, so each container
is frozen at whatever was current when it was created. Config *and* beets version drift
per-container. That is what the migration in *Prerequisites* exists to fix.

### The cross-system identity, and how it flows through beets

`SUBBOX_ID` is the durable cross-system track identity (see workspace `CLAUDE.md` and
`../pymix/CLAUDE.md`). It exists in **two independent stores**, and the distinction is
load-bearing for this design:

- **File tag `SUBBOX_ID`** — a raw tag (TXXX for MP3, Vorbis comment for FLAC, an
  iTunes freeform atom for MP4) written by pymix via taglib/mutagen at staging time
  (`utils/tag_subbox_id.py`). **beets has no plugin or `types` mapping for it** — beets
  is oblivious to this tag.
- **beets DB flexattr `subbox_id`** — bridged in *after* import by
  `_map_subbox_id_beet_id` (`rekordbox_xml_controller.py:198`): it lists freshly imported
  tracks (`beet list subbox_id::^$`), reads the **file tag** back off each imported file with
  `get_subbox_id(path)`, and mirrors the value into the beets DB with
  `beet modify -y id:{beet_id} subbox_id=…`. This also populates the `subbox_id → beet_id`
  map pymix relies on.

The bridge only works because the file tag survives import. See *Verification* for why it
currently does, and the trap that could silently break it.

## Prerequisites (new — blocking for Phase 2)

### P1. Pin beets to the latest stable version

Decided: pin to **beets 2.13.1**, the current latest stable (PyPI, released 2026-07-31).

- `requirements.txt` — `beets~=2.2.0` → `beets==2.13.1`. This pin governs pymix's own
  in-process beets usage (`Item.from_path` in `_get_duplicates`), not the containers.
- `../subbox`, `docker-compose/beets/docker-compose.yml` on **each host branch**
  (`droplet` = prod, plus the dev-machine branches) — `lscr.io/linuxserver/beets:latest` →
  `lscr.io/linuxserver/beets:2.13.1-ls345`. This is the pin that actually matters; it is a
  cross-repo change, per `docs/repositories.md`.

Risk to manage: existing containers run 2.7.1, so the migration below moves them 2.7.1 →
2.13.1 in one step, across six minor releases, against live user libraries. Roll it out one
user at a time on prod (see P3), and check the beets changelog for library-DB migrations
between those versions before touching a real user. Take a copy of
`/subbox/users/{user}/beets/config/musiclibrary.blb` before the first prod user is migrated.

### P2. Enable the `musicbrainz` plugin — done (2026-08-04)

Added `musicbrainz` to the `plugins:` line in `templates/beets/config.yaml`. Without this the
Phase 2 sweep runs, finds zero candidates, marks everything `nomatch`, and looks like a
working feature that does nothing.

Verified live against a scratch container provisioned from the updated template (beets
2.13.1, isolated — no shared volumes, no interaction with any real user's containers): `beet
version` lists `musicbrainz`; an `-A` as-is import of a real-metadata track ("The Beatles –
Come Together" / "Abbey Road") left `mb_trackid` empty and completed in ~1.5s, confirming the
interactive path is still fast and as-is; a subsequent `beet import -L automatch_state:pending`
found the real MusicBrainz release (`musicbrainz.org/release/6bb3793b-…`) at 91.7% confidence
and applied a genuine `mb_trackid`. Only affects newly-provisioned containers — existing users
still need the P3 migration to pick this up.

Note the interaction with Phase 1: enabling the plugin re-arms the autotagger for the
*interactive* import too. Phase 1's `-A` is what keeps the fast path fast once the plugin is
on — so **P2 must not ship before Phase 1**, or interactive imports get slower than they are
today. Order matters here in a way it didn't in the first draft.

### P3. One-off migration for existing containers

Decided: an **explicit, re-runnable admin migration** (endpoint or script), not an automatic
re-render on pymix startup — a deploy must not silently recreate every user container at once
on the droplet.

Per user, the migration:

1. Re-renders `templates/beets/config.yaml` into that user's `config_file_dst`
   (`/subbox/users/{user}/beets/config/config.yaml`, per `config/config.prod.yaml:31`) using
   the same Jinja render as `orchestrators/services_orchestrator.py:190`.
2. Recreates the user's beets container from
   `/subbox/docker-compose/beets/docker-compose.yml` (`config.prod.yaml:29`) so it picks up
   the pinned image.
3. Verifies: `beet version` reports 2.13.1 and lists `musicbrainz`; `beet stats` returns the
   same track count as before; a spot-checked file still has its `SUBBOX_ID` file tag and
   `subbox_id` flexattr.

It must take that user's beets lock (see below), be safe to re-run, and be driveable for a
single named user so prod can be migrated incrementally.

## Design

### Phase 1 — as-is import

Change the import command at **all three call sites** to genuinely skip the tagger:

```
beet import -A --group-albums --set user={username} --set public={public} \
  --set automatch_state=pending /downloads
```

`-A` (`--noautotag`) imports as-is with no MusicBrainz round-trips and no prompting; drop
`-q` (redundant under `-A`). `_map_subbox_id_beet_id` and the wishlist reconcile hook are
unaffected — they key off the `SUBBOX_ID` file tag, which as-is import preserves.

`--set automatch_state=pending` is what makes a track visible to the Phase 2 sweep. Stamping
it in Phase 1 (rather than with Phase 2) means tracks imported between the two ships are
picked up by the first sweep instead of being invisible to it forever. All three call sites
need it, including the two that don't currently set `public`.

This is independently shippable and safe to ship before the plugin is enabled.

### Phase 2 — background automatch sweep

Mirror the existing `wishlist_reconcile_loop` (`handlers/wishlist_reconcile_handler.py`):
a new handler + service registered in `containers.py` / `runner.py`, running on a poll
interval from config. Per cycle, per **idle** user, sequentially:

1. **Skip non-idle users.** Decided idle test: the user has *no* in-progress import job (the
   jobs table `db_controller` already maintains) **and** no Navidrome play activity within a
   configured recency window. Both checks are cheap and neither needs new infrastructure.
2. **Select unmatched tracks** — `beet list automatch_state:pending`. States:
   `pending → matched | nomatch | error`. `nomatch` (tried, no confident MB hit — normal for
   white labels and heavily edited files) is terminal. `error` is *not*: see step 6.
3. **Reimport in place, in chunks** using beets library reimport, which re-runs the tagger on
   tracks already in `/music` without re-staging:
   ```
   beet import -L -q automatch_state:pending
   ```
   With autotag on + `quiet_fallback: asis`, confident matches are applied and files renamed
   per the `paths:` templates; the rest stay as-is. **Chunk the query** (one album, or N
   tracks, per invocation) and release the user's lock between chunks — see *Concurrency*.
4. **Re-map `subbox_id → beet_id` explicitly.** The existing `_map_subbox_id_beet_id` only
   processes tracks whose `subbox_id` flexattr is *empty*, so it will **skip** already-mapped
   tracks and cannot repair the map after a reimport. The sweep needs its own re-map step
   that keys off the swept ids rather than emptiness.
5. **Re-run duplicate detection for the swept tracks.** A newly-matched track gains
   `mb_trackid`/`mb_albumid`, which are the first two `duplicates.keys` — so a match can turn
   two previously-distinct-looking items into a detectable duplicate pair. Decide explicitly
   rather than leaving it implicit: re-run `_get_duplicates` (tag `dup=1`) over the swept
   set, but never `duplicates -d` from the background loop — deletion stays user-initiated.
6. **Re-stamp `automatch_state`** — `matched` where `mb_trackid` is now non-empty, `nomatch`
   where the tagger ran and found nothing confident, `error` where the attempt failed
   (network, timeout, MB 503/rate-limit). `error` carries an attempt counter and is retried on
   later cycles up to a cap, then becomes terminal. Without this split, one flaky MB night
   permanently writes off a batch of tracks, and there's no cheap way to find them again.
7. **One Navidrome rescan at the end of the batch**, not per track.

**Sweep-specific config override.** Run the sweep as `beet -c /config/automatch.yaml import -L …`
with `fetchart.auto` and `embedart.auto` set to `no`. Otherwise every reimport re-downloads and
re-embeds cover art — extra network, extra file writes, and extra Navidrome churn on tracks
that didn't even match. Also `move: no` — decided by the rename gate, see *Renames and
Navidrome*.

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

## Renames and Navidrome (blocking gate on Phase 2)

**Gate result (verified 2026-08-04, dev stack, against real Navidrome 0.60.3): FAIL.**
The sweep must ship with **`move: no`** (tags-only, no path churn). Renaming is deferred —
see *Evidence* below.

The exposure: `import.move: yes` plus the `paths:` templates means a successful match moves
the file. Navidrome playlists, favourites, ratings and play counts all key on `media_file`
ids. If the deployed Navidrome (**0.60.3**, pinned in `../subbox/docker-compose/navidrome/`)
treats a moved file as delete + add rather than detecting the move, then the background sweep
quietly destroys user playlists — a far worse outcome than imperfect tags. Nothing about the
existing import exercised this before this gate, because nothing had ever renamed a file
*after* it landed.

### Evidence

Verification run against `test300726` / `beetstest300726` / `navidrometest300726` — a
container provisioned from the current template, per the standing rule above about not
trusting drifted containers.

1. Used 2 already-landed QA-fixture tracks. Via the Subsonic API: added both to a new
   playlist, starred one (`Quiet Tide`, Subsonic id `zI7K3JwhOmvRJeXJBvwAGy`), and scrobbled
   it twice (`playCount: 2`).
2. Forced a rename with `beet modify -y -m -a album:Discovery albumartist:"Job Jobse"
   album="Discovery Renamed For Gate77"` (album-level modify — a plain item-level `modify`
   updates the item's own fields but not the associated `Album` row, and `beet move` computes
   its destination from the `Album`, so it reports "already in place" and doesn't move
   anything; this is a real footgun worth remembering for any future tooling that renames via
   beets). The file physically moved: `Job Jobse/Discovery/…` → `Job Jobse/Discovery Renamed
   For Gate77/…`.
3. Triggered a full Navidrome rescan (`/rest/startScan?fullScan=true`) and confirmed
   completion via `/rest/getScanStatus`. Navidrome's own scan log shows it *attempted* move
   detection (`Scanner: Found potential moves count=1`) but the result was a fresh record, not
   a preserved one.
4. Checked all four criteria by Subsonic id:

   | Check | Result |
   |---|---|
   | Subsonic track id unchanged | **FAIL** — new id `aAQHlR8bSmiTGqbTajeOXh` minted at the new path; the old id `zI7K3JwhOmvRJeXJBvwAGy` persists as an orphaned "missing" record instead of being reused |
   | Playlist membership intact | **FAIL** — the playlist dropped to `songCount: 1`; the renamed track was pruned and never re-added under its new id |
   | Star intact | **FAIL** — `getStarred2` on the new id returns empty; the star stayed attached to the orphaned old id |
   | Play count intact | **FAIL** — the new id has no `playCount`; the `playCount: 2` stayed stranded on the orphaned old id |

   In short: Navidrome 0.60.3 does **not** detect same-library renames as a move for
   already-scanned files with existing user data attached — it silently creates a new
   `media_file` and orphans the old one, exactly the failure mode this gate exists to catch.
5. Cleaned up afterward: deleted the test playlist, reverted the album name (which moved the
   file back), and re-scanned — confirmed `test300726`'s QA fixtures are back to their
   pre-test state.

Aside, not gating: `beet modify -m`/`-a` both hit `subsonic: Error: Expecting value: line 1
column 1 (char 0)` from the `subsonicupdate` plugin's own post-write rescan hook in this
container — a pre-existing, unrelated break in that plugin's HTTP call. It didn't block the
rename or this verification (the explicit Subsonic `startScan` call was used instead), but the
plugin's auto-rescan-on-write is currently silently broken and worth its own issue.

### Decision

The sweep (Phase 2, not yet built — see *Build order* step 7) must run with `move: no` in its
config override (alongside `fetchart.auto: no` / `embedart.auto: no`, already decided in
*Sweep-specific config override*). Matched tracks get corrected tags but keep their existing
path and `media_file` id — playlists, stars, and play counts survive a match. The
`$albumartist/$album/…` path reorganization is deferred indefinitely, not just for this
gate: revisit only if Navidrome ships real rename/move detection for this case, or if
path-churn stops mattering because moves become op-in/manual.

This also simplifies the sweep's remap step (Phase 2 step 4): `_map_subbox_id_beet_id`
reconstructing host paths from `beet list` output no longer needs to worry about paths
changing under it from the sweep's own reimport — `_resolve_path_with_special_chars` still
matters for the *original* import's paths, but the sweep itself won't be moving anything.

### Future option — `subbox_id`-anchored reconciliation (not built, deliberately deferred)

`subbox_id` survives a rename (both the file tag and the beets flexattr — see *Verification*
below), so it's tempting to use it to *allow* renaming and repair Navidrome state afterward
instead of just avoiding renames. It doesn't get there for free: Navidrome's Subsonic API has
no concept of `subbox_id` at all, and playlists/stars/play counts all key on Navidrome's own
`media_file` id — the one that changes. Making this work would mean, per rename: resolving the
new Navidrome id (no "get by tag" call exists, so this falls back to the same fuzzy
path/artist/title matching `_resolve_path_with_special_chars` already does), then replaying
star and playlist membership onto it via the API. Play count has no clean answer — Subsonic
only exposes `scrobble`, not "set count to N", so reconciling it means replaying N scrobbles or
accepting it resets. There's also a real window, per track, where the data is genuinely absent
from the user's perspective until reconciliation runs. Worth a real design pass if the
`$albumartist/$album` reorganization turns out to matter enough later; not worth building for
this gate.

## Concurrency & churn

Decided 2026-08-02, revised the same day after tracing the actual call sites.

### Lock scope: writes only, reads exempt

**Revised from the original "all beets CLI calls".** Writes — `import`, `import -L`,
`modify`, `rm`, `duplicates -d` — serialize per user. Pure reads — `beet stats`, `beet list`,
`duplicates -p` — do not take the lock.

The original blanket rule breaks the import progress bar. `/beets/import/progress` computes
progress from `beet stats` (`routers/beets_import.py:150` → `clients/beets_client.py:29`) and
the client polls it in a loop for the entire duration of the import
(`subbox-app`, `src/renderer/features/sync/components/sync-rekordbox.tsx:271-292`). Under a
lock covering reads, every poll blocks until the in-flight `beet import` exec returns: the bar
jumps 0 → 100 with a dead UI in between, and each poll holds an HTTP connection open for the
length of the import. That read is not an occasional unlucky contender — it is the one read
guaranteed to contend, on every import.

Exempting reads is safe: SQLite handles concurrent readers fine. The "database is locked"
errors this design exists to prevent come from concurrent *writers*.

### Lock granularity: per operation, not per exec

The lock must be taken around a whole **logical operation**, not around each `docker exec`.
The import path is composite: `_consume_from_filebrowser` (`rekordbox_xml_controller.py:314`)
runs `beet import`, then `_get_duplicates`, then `_map_subbox_id_beet_id` — which is itself a
`beet list` plus one `beet modify` per track. With the lock inside the exec helper, the
automatch sweep's `beet import -L` can interleave *between* those steps and reimport tracks
whose `subbox_id` has not yet been bridged.

So: an explicit `with beets_write_lock(username):` around the import job, the automatch cycle,
the watch cycle, and the migration — with the exec helper itself either lock-free or
reentrancy-aware. Note `asyncio.Lock` is **not** reentrant, so a naive "lock in the helper
*and* hold across the operation" deadlocks.

### Lock type: threading, not asyncio

**Revised.** `asyncio.Lock` cannot sit where the first draft put it. Every beets call site is
synchronous and runs inside `anyio.to_thread.run_sync` — `consume_from_filebrowser:265`,
`remove_duplicates:89`, `BeetsClient.get_number_of_tracks:23` — and a helper owning
`docker exec` cannot `await` an asyncio lock from inside a worker thread.

Use a per-user `threading.Lock` in a plain `dict[str, threading.Lock]` held in app state.
Everything still runs in one process — `runner.py` starts a single `uvicorn.Server` with no
worker fan-out — so an in-process lock remains sufficient, and there is no stuck-row cleanup
to worry about: if the process dies mid-import, the lock and the in-flight `beet` command go
away together.

Two call sites must move onto a thread as part of this work:
`get_present_subbox_ids` (`rekordbox_xml_controller.py:130`) and `remove_tracks`
(`:149`) are `async def` but call `docker.execute` **directly on the event loop**. That is
already a latent stall; adding a blocking lock there would stall the whole app behind another
user's import.

### Behaviour under contention: wait, don't reject — but the sweep must yield

Callers `await`/block on the per-user lock and then proceed; no 409s, no retry-later contract
for API callers. None of the operations in scope are latency-sensitive enough to need
fail-fast.

**This is only safe if the background sweep cannot hold the lock for long.** A `beet import -L`
batch is network-bound (MusicBrainz, plus art plugins if not disabled) and can run for minutes;
with wait-don't-reject, a user hitting Import queues behind it and the fast as-is import is
fast only after an unbounded wait — defeating the point of the whole design. The sweep
therefore must:

- **chunk** — one album (or N tracks) per lock acquisition, releasing between chunks;
- **yield** — before each chunk, re-check the idle test from Phase 2 step 1 and abandon the
  cycle for that user if foreground work has appeared;
- **cap** — a wall-clock budget per user per cycle.

### Centralize the exec call sites

Add one beets-exec helper that owns `docker exec beets{username} beet ...` (via
`python_on_whales`), classified read vs write, with writes taking the per-user lock unless the
caller already holds it. Every existing call site routes through it — the ~10 invocations in
`rekordbox_xml_controller.py` plus the duplicated Serato-side equivalent that `CLAUDE.md`
"Rough edges" already flags. One seam instead of ten, and it incidentally fixes the flagged
duplication.

### Throttle and rescan batching

One user at a time, sequential, low frequency, as the wishlist loop does. Reimport renames
files → Navidrome rescans; batch and rescan once per user per cycle.

## Verification

### 1. Does anything strip the SUBBOX_ID file tag on import/reimport? → No, but keep the trap in mind

- beets' **`scrub` plugin destroys the SUBBOX_ID file tag**: `_scrub` calls mutagen
  `f.delete()` (removes *all* tags), then restores only beets/MediaFile-known fields —
  and SUBBOX_ID is not one. Reproduced locally (FLAC and MP3) with `scrub` in the plugins
  list: the tag was gone after import, and the `_map_subbox_id_beet_id` bridge then reads
  `None` and skips the track (no mapping, no DB flexattr).
- The template does not load `scrub`, and the `scrub:` config block was removed in commit
  `369b25f`. **There is no work item here** — the first draft's "delete the dead `scrub:`
  block" step was already complete when it was written.
- **Standing rule:** never add `scrub` to the plugins list without first protecting SUBBOX_ID
  (register it as a beets field, or re-stamp after write).

### 2. Does library reimport (`-L`) preserve the beet item id and tags? → Yes

Controlled `-L` reimport with a forced rename:

```
before:   id 1 | Track One     | subbox_id=CTRL-1234
after -L: id 1 | Renamed Title  | subbox_id=CTRL-1234 | file SUBBOX_ID=['CTRL-1234']
```

Item **id stayed stable**, the `subbox_id` DB flexattr survived, and the file tag survived
the rename. So `subbox_id → beet_id` stays valid across reimport.

Caveats:
- Verified for an **as-is** `-L` (`-A`). A `-L` that *applies a MusicBrainz match* is
  beets' documented id-stable reimport behavior, but has not been tested live (needs network,
  a matchable track, and the plugin enabled). Worth one live test before shipping Phase 2.
- Even if an id ever did change, the sweep's explicit re-map (Phase 2 step 4) is the safety
  net; don't rely on the import-time mapper, which skips already-mapped tracks.
- Verified under beets 2.7.1. Re-check after the 2.13.1 pin lands (P1).

### 3. Outstanding — must be done before Phase 2 ships

- ~~**Navidrome rename tolerance** — the gate described in *Renames and Navidrome*.~~ Done —
  failed; sweep ships with `move: no`.
- **Prod config audit** — confirm what each prod user's
  `/subbox/users/{user}/beets/config/config.yaml` and beets version actually are. Local test
  containers are not evidence; two containers on this machine already disagree with each other
  and with the template.
- ~~**A live `-L` match** under the pinned 2.13.1 with `musicbrainz` enabled.~~ Done — see P2's
  verification (2026-08-04): a genuine `mb_trackid` applied via `-L` reimport.

Note both remaining items here are prod-rollout operations (audit + P3 migration run), not code
— the Phase 2 sweep itself (step 7) is unit-tested and merged; it just hasn't reimported a real
user's tracks yet, since no prod user has been migrated onto the `musicbrainz`-enabled template
(P3) or the pinned image (`../subbox` P1) yet.

## Build order

1. ~~**Centralized beets-exec helper + per-user `threading.Lock`**~~, wrapping all existing call
   sites, with the write/read split and the two event-loop-blocking call sites moved onto a
   thread (see *Concurrency & churn*). Done — `clients/beets_exec.py`.
2. ~~**Phase 1 (`-A` + `--set automatch_state=pending`)** at all three import call sites.~~ Done
   — `_consume_from_filebrowser`, `_import_to_beets` (both rekordbox and serato) all set it.
3. ~~**P1 — pin beets 2.13.1**~~ — `requirements.txt` done (`beets==2.13.1`). The
   `../subbox` per-host compose pin (`lscr.io/linuxserver/beets:2.13.1-ls345`) is still
   outstanding — a separate, cross-repo change per `docs/repositories.md`; every host still
   provisions new containers off `:latest`.
4. ~~**P3 — migration endpoint/script**~~ Done — the per-user re-render + recreate admin
   endpoint; migrate real dev/prod users and verify is an operational follow-up, not a code
   change.
5. ~~**Navidrome rename verification** (the gate).~~ Done — failed; `move: no` added to the
   sweep override (see *Sweep-specific config override*), renaming recorded as deferred.
6. ~~**P2 — enable the `musicbrainz` plugin** in the template.~~ Done — template updated;
   existing users still need the P3 migration to pick it up.
7. ~~**Phase 2 — `automatch_state` sweep**~~ Done (2026-08-04) — `handlers/automatch_handler.py`
   + `services/automatch_service.py`, reusing the wishlist-loop scaffolding and the
   lock/helper from step 1: chunked locking (`chunk_size`), the idle test (in-progress job +
   Navidrome `getNowPlaying` within `idle_recency_window_s`, re-checked between chunks so a
   foreground import is never queued behind more than one chunk), a wall-clock budget per
   user per cycle, an explicit id-keyed re-map (`RekordboxXMLController.remap_subbox_id_for_ids`,
   not the empty-flexattr-only `_map_subbox_id_beet_id`), duplicate re-tagging
   (`retag_duplicates`), the `pending -> matched | nomatch | error` state machine with an
   `automatch_attempts` retry cap, a single end-of-batch Navidrome rescan, and the
   `templates/beets/automatch.yaml` `-c` overlay (`fetchart.auto`/`embedart.auto: no`,
   `move: no`) rendered per-user alongside `config.yaml`.
8. **Phase 3** correction endpoint + subbox-app UI (cross-repo, per `docs/integration.md`) —
   not started; the only remaining piece of this design.

Steps 1–7 are all shipped. Step 8 (Phase 3) is a cross-repo change and was always scoped
separately from the async-automatch chain that steps 1–7 form.

## Code surface

Everything below is built except the last item (Phase 3, not started).

- ~~**New beets-exec helper**~~ — `clients/beets_exec.py`'s `BeetsExec` — owns `docker exec
  beets{username} beet ...`, the read/write classification, and the per-user `threading.Lock`
  dict; every existing call site routes through it.
- ~~`controllers/rekordbox_xml_controller.py` call sites switched to the shared helper~~; the
  import commands are `-A` + `--set automatch_state=pending`; ~~a reusable re-map that doesn't
  depend on an empty `subbox_id` flexattr~~ — `remap_subbox_id_for_ids` (#79), plus a
  synchronous `retag_duplicates` entry point the sweep uses under its own held lock.
- ~~`controllers/serato_controller.py` — the third import command~~ also sets `-A` +
  `automatch_state=pending`.
- ~~`templates/beets/config.yaml` — `musicbrainz` added to `plugins:`~~ (P2).
- ~~**New sweep config** `templates/beets/automatch.yaml`~~ — `fetchart.auto`/`embedart.auto:
  no`, `move: no` (rename gate, failed); rendered per-user alongside `config.yaml` in both
  `_create_beets` and `_migrate_beets_container` (`orchestrators/services_orchestrator.py`).
- ~~**New migration** (endpoint)~~ — re-renders config + recreates container per user (P3,
  landed with #76, predates the automatch.yaml addition above but re-renders it too).
- ~~**New `handlers/automatch_handler.py` + `services/automatch_service.py`**~~ (mirrors
  `handlers/wishlist_reconcile_handler.py` / `services/wishlist_reconcile_service.py`), using
  the shared helper for its `beet -c automatch.yaml import -L` calls, and
  `SubsonicClient.get_now_playing` (new) for the idle test's recency check.
- ~~`containers.py` / `runner.py` — register the loop~~, plus `config/config.{dev,prod}.yaml`'s
  new `automatch:` section (`poll_interval_s`, `idle_recency_window_s`, `chunk_size`,
  `wall_clock_budget_s`, `error_retry_cap`).
- ~~`requirements.txt` — `beets==2.13.1`~~ (P1).
- `../subbox`, `docker-compose/beets/docker-compose.yml`, **every host branch** —
  `lscr.io/linuxserver/beets:2.13.1-ls345` (P1) — still outstanding, cross-repo.
- New router for Phase 3 correction (`routers/track.py` already handles per-`subbox_id`
  track ops — likely extends there) — not started.

## Out of scope / future

- Local MusicBrainz mirror + local AcoustID fingerprinting (airgapped matching). Possible
  later (there are `acoustid-fork` / chromaprint siblings in the workspace) but a much
  larger per-container infra lift; explicitly deferred.
- AcoustID fingerprinting (`chroma` plugin) as a second candidate source for tracks that come
  back `nomatch` on tags alone. A natural Phase 2.5 — it would lift the hit rate on exactly
  the white-label/edited material that tag matching fails on — but it needs chromaprint in
  each container.

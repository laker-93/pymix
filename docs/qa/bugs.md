# Bug log (pymix)

See `README.md` for the conservative fix policy and the rule against
one-sided fixes for client-coupled issues.

## OPEN

### (informational, low priority) 5 orphaned beets entries at `/downloads/…` paths, no `subbox_id`, re-warn on every import

Added: 2026-07-09. Surfaced while driving the watch-dir import (sub-step 3 of
the phone/Discord wishlist directive; see `features/watch-dir-import.md`).
Workflow: `poll_watchdir` → `_map_subbox_id_beet_id`
(`pymix/controllers/rekordbox_xml_controller.py`).

**Symptom.** Every watch-dir import logs, before the real work:

```
Found 6 tracks with unset subbox_id.
WARNING Could not resolve path for beet_id=105, skipping.
WARNING Could not resolve path for beet_id=106, skipping.
WARNING Could not resolve path for beet_id=107, skipping.
WARNING Could not resolve path for beet_id=87,  skipping.
WARNING Could not resolve path for beet_id=162, skipping.
```

**Evidence (verified).** `beet list -f '$id | $path' 'subbox_id::^$'` in
`beetstest260526` returns exactly these 5 entries, all pointing at
`/downloads/…` paths (a staging/transient location, not `/music/…`):

```
105 | /downloads/cloud formation master 04.06.23.mp3
106 | /downloads/points break at break points master 30.05.23.mp3
107 | /downloads/road runner master 04.06.23.mp3
 87 | /downloads/Sacred (88Ninety's 'StellarMix8' Vox Remix).mp3
162 | /downloads/LITE SPOTS.mp3
```

The `/downloads/` files no longer exist in the container, so
`_map_subbox_id_beet_id` can't stat them and skips — meaning these 5 library
entries are **permanently stuck without a `subbox_id`**. Consequences:
- Recurring WARNING noise on every single import (cosmetic but misleading).
- If the real audio for any of these lives elsewhere in `/music`, it can't be
  matched via the `subbox_id` fast path (sync would fall back to fuzzy, or miss).
- They're phantom rows: beets counts them, but Navidrome's `media_file` (77)
  doesn't, since the files aren't under `/music` — part of the beets-vs-navidrome
  count skew.

**Why not fixed this cycle.** Pre-existing data hygiene, not a regression and
not caused by the import under test. The right fix is a design call — purge
beets entries whose path no longer resolves (a `beet remove` sweep, or making
`_map_subbox_id_beet_id` prune unresolvable rows instead of skipping them
every time) — and touches destructive beets-DB ops, which is above the
conservative auto-fix bar. Flagging for the user / a future cycle to decide.

Single-repo (pymix), likely `rekordbox_xml_controller._map_subbox_id_beet_id`.

Issue: https://github.com/laker-93/pymix/issues/24

<!-- One entry per bug: date, workflow/endpoint, repro (request + response or
     client symptom that led here), hypothesis, which repo(s) need to change,
     and an `Issue: <github url>` line (every bug gets a qa-bug tracking issue —
     see README hard rules / skill Step 1½). -->

### `subbox_id_divergence` ERROR over-fires on ordinary not-yet-downloaded tracks

Added: 2026-07-09. Endpoint: `POST /sync/plan` (`pymix/routers/sync.py`
line ~366 and the ERROR at ~434). Found while driving sub-step 3 of the
sync-matching directive (fuzzy fallback for untagged locals) against
`test260526`'s "Kodzo" playlist (9 tracks, 7 present locally in the isolated
`subbox-dev/music`, 2 missing).

**Symptom.** Every "Preview Download" on this healthy, partially-downloaded
playlist logs `ERROR sync_plan subbox_id_divergence: count=2 tagged tracks
ended up missing despite fuzzy fallback — likely a stale/duplicate local
SUBBOX_ID tag, will be re-downloaded`. Of those 2, only **one** (the genuine
"Damager (Hamdi Edit)" duplicate — see the informational entry in
feishin-qa's `bugs.md`) is a real stale/duplicate case. The other is
**"Oleo" by Pat Martino**, a track the user simply hasn't downloaded yet (it
was moved out of `subbox-dev/music` in an earlier pruning test; no local
copy, no metadata-identical local counterpart). Downloading Oleo is the
correct, desired outcome — there is nothing stale or duplicate about it, yet
it trips the ERROR with that misleading "stale/duplicate local SUBBOX_ID
tag" wording.

**Root cause.** The missing-track branch flags divergence purely from the
*server* track's own tag:

```python
if track.subbox_id:            # line ~366
    subbox_id_tagged_missing.add(track.subbox_id)
```

Every server track that has been through pymix ingest carries a `subbox_id`,
so `track.subbox_id` is truthy for essentially all missing tracks. The
signal therefore fires ERROR for *any* tagged server track the user doesn't
have locally — i.e. for normal, not-yet-downloaded tracks. On a fresh sync
(nothing downloaded), `count` would equal ~the whole playlist and every sync
would log ERROR — the exact "library-wide noise" that the pymix#22 fix (see
FIXED below) set out to eliminate. The `count=1 correctly scoped` conclusion
in that FIXED entry held only because, at that moment, the single missing
track happened to be the genuine duplicate; it wasn't tested with a plain
absent-but-tagged track also missing.

**Verified** (live, shared `pymix` container on `laker93/pymix:qa-local`,
this branch's code): two consecutive `sync/plan` runs both logged
`count=2`; the count is independent of a separate untagged-local test I ran
in the same cycle (that track fuzzy-matched and stayed present). Oleo's
server subbox_id (`f948441b-…`) has no local file carrying it.

**Why not fixed this cycle (needs a design call, not a conservative fix).**
The comment at line ~428 states the intended meaning: "tracks the user
*already has* … but ended up missing anyway." Distinguishing that from a
plain absent track is non-trivial:
- A bare intersection with local subbox_ids would make the signal ~never
  fire — a local file carrying the matching id would already have matched via
  the fast path, so a *missing* server track's id is by construction not on
  any local file.
- The genuinely useful signal (the Damager case) is "a missing tagged server
  track whose *metadata* matches a local file the user already has, but a
  differing subbox_id forced a separate re-download." Detecting that means
  fuzzy-matching missing tracks against the local set — new logic, arguably a
  redesign, outside the conservative-fix bar.

**Recommended options for the user to choose from** (any is a small change;
picking one is the design call): (a) downgrade to INFO / reword to drop the
"stale/duplicate" claim, since "tagged server track not present locally" is
normal; or (b) restrict the ERROR to the metadata-duplicate case above; or
(c) drop the signal entirely and rely on the existing `missing` count.

Single-repo (pymix only). Logging-only; the sync *plan* output itself is
correct (7 present / 2 missing is right — Oleo *should* be downloaded).

Issue: https://github.com/laker-93/pymix/issues/23

## FIXED

<!-- One entry per fix: date, one-line description, commit SHA on this
     branch, how it was re-verified. -->

### `subbox_id_match_summary` logged ERROR on almost every normal sync, not just real divergence

Found: 2026-07-09. Fixed: 2026-07-09. Endpoints: `POST /sync/plan` and
`POST /sync/playlists` (`pymix/routers/sync.py`).

**Original bug** (live-verified against test account `test260526`, 774 real
local tracks, 1 playlist "Kodzo" with 9 tracks): the summary log added in
pymix #21 computed "tagged locals never matched by id" across the client's
*entire local library* (759 tagged tracks) rather than scoping to the
playlist(s) actually requested, so it logged ERROR
(`tagged_locals=759 matched_by_id=5 never_matched_by_id=754`) on this
completely healthy sync — the other 750 tagged tracks simply aren't in this
9-track playlist, which is normal, not divergence. In practice this fires
ERROR on nearly every real sync, defeating the log's purpose.

**Fix, iterated twice**:
1. First pass scoped the denominator to server tracks in the requested
   playlist(s) instead of the whole library (`754` → `3` in this repro) —
   better, but still imprecise: 2 of those 3 "unmatched by id" tracks were
   actually found fine via the pre-existing fuzzy fallback (matched to an
   untagged local file), so they weren't real divergence either.
2. Final fix: only flag a track when it's tagged **and** still ends up
   classified `missing` after *both* the subbox_id fast path and the fuzzy
   fallback have had a chance to match it — the one combination that
   actually causes a re-download/re-export of a track the user already has.
   New logs: `subbox_id_summary` (INFO, informational count of tagged
   tracks in the request) and `subbox_id_divergence` (ERROR, only the
   precise signal).

**Re-verified live** (not just re-reading the diff): rebuilt
`laker93/pymix:qa-local` from this branch twice (once per fix iteration),
redeployed to the shared local `pymix` container, re-ran the identical
"Preview Download" flow via the Electron app. Sync result unchanged both
times (9 requested / 8 present / 1 missing / 1 metadata update — the fix
only touches logging, not matching behavior). Final log output:
`subbox_id_summary: server_tracks_tagged=8` (INFO) and
`subbox_id_divergence: count=1` (ERROR) — `count=1` correctly correlates
with the one track actually classified missing/to-download, not the
library-wide noise from before.

`pytest pymix/tests` (excluding two pre-existing, unrelated collection
errors — `test_rekordbox_xml_controller.py` /
`test_subsonic_orchestrator.py`, both jinja2-version issues reproduced
identically on the unmodified `../pymix` venv too): 37 passed, 10 skipped,
same as before this change.

Single-repo (pymix only) — no subbox-app change needed.

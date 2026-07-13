# Bug log (pymix)

See `README.md` for the conservative fix policy and the rule against
one-sided fixes for client-coupled issues.

## OPEN

(none)

<!-- One entry per bug: date, workflow/endpoint, repro (request + response or
     client symptom that led here), hypothesis, which repo(s) need to change,
     and an `Issue: <github url>` line (every bug gets a qa-bug tracking issue —
     see README hard rules / skill Step 1½). -->

## FIXED

<!-- One entry per fix: date, one-line description, commit SHA on this
     branch, how it was re-verified. -->

### `subbox_id_divergence` ERROR over-fired on ordinary not-yet-downloaded tracks

Found: 2026-07-09. Fixed: 2026-07-13. Endpoints: `POST /sync/plan` and
`POST /sync/playlists` (`pymix/routers/sync.py`). Issue #23.

**Bug.** The missing-track branch added a track to `subbox_id_tagged_missing`
purely from `if track.subbox_id:`. Essentially every ingested server track is
tagged, so the `sync_plan subbox_id_divergence` ERROR ("likely a stale/duplicate
local SUBBOX_ID tag") fired for *any* tagged track the user simply hadn't
downloaded yet — on a fresh sync, ~the whole playlist. The `/sync/playlists`
export path carried the identical bug. Logging-only; the plan/export output was
already correct.

**Fix.** Downgraded ERROR → INFO and renamed `subbox_id_divergence` →
`subbox_id_missing` in both handlers, reworded to what it actually measures
("N tagged server tracks not present locally, will be downloaded"), and dropped
the false stale/duplicate wording. Distinguishing a genuine stale/duplicate-tag
re-download would require fuzzy-matching every missing track against the full
local set (a redesign, deliberately not done — noted in comments). Shipped as
pymix PR #26 (`Closes #23`), branch `fix/sync-subbox-id-missing-info-log` off
`main`.

**Re-verified.** `pytest pymix/tests` (excluding the two pre-existing jinja2
collection errors): 37 passed, 10 skipped — identical to baseline. `grep` confirms
no `subbox_id_divergence` / no `logger.error` divergence call remains in sync.py.

### 5 orphaned beets entries at `/downloads/…` paths re-warned on every import

Found: 2026-07-09. Fixed: 2026-07-13.
`rekordbox_xml_controller._map_subbox_id_beet_id`. Issue #24.

**Bug.** 5 beets rows (ids 105, 106, 107, 87, 162) in `beetstest260526` pointed at
deleted `/downloads/*.mp3` staging paths, so `_map_subbox_id_beet_id` couldn't
stat them and re-logged `Found N tracks with unset subbox_id` +
`Could not resolve path for beet_id=…, skipping` on every import. Phantom rows
(beets-vs-Navidrome count skew). Pre-existing data hygiene, not a regression.

**Fix (data cleanup, no code change).** Confirmed the `/downloads/` files are gone
in-container (dir empty), then removed the 5 rows DB-only: `beet remove -f id:<n>`
(no `-d`, so nothing on disk was touched). No real audio orphaned — the rows had
no subbox_id and any real re-import lands as a separate `/music/` row. No
auto-prune logic was added to the hot import path (running destructive
`beet remove` every import to clean 5 one-off phantoms wasn't worth the risk).

**Re-verified.** The exact query the import runs,
`beet list -f '$id:$path' 'subbox_id::^$'`, now returns zero rows; `path::/downloads/`
returns zero rows. Next import logs "Found 0 tracks with unset subbox_id" with no
per-row warnings. Total library items: 560. Issue closed with a resolution
comment.

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

# Bug log (pymix)

See `README.md` for the conservative fix policy and the rule against
one-sided fixes for client-coupled issues.

## OPEN

<!-- One entry per bug: date, workflow/endpoint, repro (request + response or
     client symptom that led here), hypothesis, which repo(s) need to change. -->

_(none open — see FIXED)_

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

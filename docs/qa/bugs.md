# Bug log (pymix)

See `README.md` for the conservative fix policy and the rule against
one-sided fixes for client-coupled issues.

## OPEN

<!-- One entry per bug: date, workflow/endpoint, repro (request + response or
     client symptom that led here), hypothesis, which repo(s) need to change. -->

### `subbox_id_match_summary` logs ERROR on almost every normal sync, not just real divergence

Added: 2026-07-09. Found validating pymix #21 + subbox-app #14 (see
`../feishin-qa/docs/qa/directives.md`).

**Endpoint**: `POST /sync/plan` (same logic duplicated in `sync_playlists`,
`pymix/routers/sync.py`).

**Repro** (live-verified, not just read from source): rebuilt pymix from
`claude/continuous-ux` (`docker buildx ... -t laker93/pymix:qa-local --load`,
pointed `../traefik/docker-compose.yml` at it), drove a real "Preview
Download" for one playlist ("Kodzo", 9 tracks) via the Electron app against
test account `test260526` (774 local tracks, most already carrying a
SUBBOX_ID tag from prior real usage — not synthetic test data). Result was
correct (9 requested / 8 present / 1 missing / 1 metadata update, identical
to the pre-PR fuzzy-only result), but pymix logged:

```
sync_plan subbox_id_match_summary: user=test260526 tagged_locals=759
matched_by_id=5 never_matched_by_id=754 (... a nonzero count may mean
subbox_id divergence and a re-downloaded duplicate)
```

logged at **ERROR** level (see `routers/sync.py` around `sync_plan():410`:
`log = logger.error if n_subbox_id_never_matched else logger.info`).

**Root cause (confirmed by reading the code alongside this result)**:
`tagged_locals` / `never_matched_by_id` are computed once per **request**
across the client's *entire local library scan* (`n_subbox_id_locals` /
`subbox_id_matched` in `sync_plan()` / `sync_playlists()`), not scoped to
"local tracks whose title/artist plausibly belongs to the requested
playlist(s)". A user syncing one small playlist out of a much larger local
library (774 local tracks synced against a 9-track playlist here) will
always have hundreds of tagged-but-irrelevant local tracks that can't match
anything in this request — that's expected and not a bug, but the summary
counts them as "never matched by id" anyway and logs ERROR unconditionally
when that count is nonzero. In practice this means **almost every normal
sync_plan/sync_playlists call for a real user will log at ERROR**, not just
the genuine subbox_id-divergence case the log is trying to surface — the
signal is drowned by the near-universal case, defeating the point of using
ERROR (and liable to trip log-based alerting on totally healthy syncs).

**Why not auto-fixed this cycle**: the correct semantics need a design call
(e.g. should the denominator be "tagged locals whose fuzzy title/artist
would plausibly match a track in the requested playlist(s)", or should this
just be an INFO-level count with a separate, narrower signal for genuine
divergence — e.g. only flag a subbox_id that matched via fuzzy fallback for
one track while also existing as a *different* server track's subbox_id).
Picking one is a judgment call, not a scoped bug fix, so this is logged
rather than fixed. **Single-repo (pymix only)** — no subbox-app change
needed to fix this.

**Also note** (not a bug, verified while investigating this): the
`sync_plan()` request that logs this summary is the *second* of two
requests the client made for the same click in this test — the first
`POST /sync/plan` returned `400 Bad Request` before the successful one. Not
yet root-caused; see the corresponding entry in `ux-notes.md` in the client
journal — could be a client retry-after-failure pattern (benign) or a real
bug worth its own follow-up.

## FIXED

<!-- One entry per fix: date, one-line description, commit SHA on this
     branch, how it was re-verified. -->

_(none yet)_

# Continuous UX loop journal (pymix / backend)

This directory is the persistent memory for the autonomous continuous-UX loop
(driven by the `continuous-ux` skill in `subbox-workspace/.claude/skills/`).
Each loop cycle is a fresh context — **this journal is the only thing that
carries state between cycles.** Read it before doing anything else.

pymix has no UI of its own — its "user experience" is what subbox-app users
feel as a result of pymix behavior (slow imports, flaky syncs, confusing
error responses that surface as bad client-side messages, silent partial
failures). This journal exists mainly to:

1. Document verified backend behavior/workflows as they're exercised via the
   client or directly via HTTP, so future cycles (in either repo) don't
   re-derive it from scratch.
2. Log and, conservatively, fix backend bugs that the client-side loop
   traces back to pymix (see `../feishin-qa/docs/qa/bugs.md` for the
   client-observed symptom that led here).
3. Log backend-only correctness issues found by directly exercising the API
   (e.g. via `curl`/httpie against `https://pymix.docker.localhost`), even if
   no client symptom has been noticed yet.

## How to read this directory

- `bugs.md` — correctness bug log (OPEN / FIXED), same conventions as the
  client-side journal.
- `features/*.md` — one file per workflow, written once verified by actually
  driving it (not just reading `docs/workflows.md`/`docs/api.md`).
- `log.md` — one line per cycle.

## Workflow coverage checklist

Grounded in `docs/workflows.md` / `docs/api.md` — check one off (link to its
`features/*.md`) once actually exercised end-to-end, not just read.

- [ ] User creation (`POST /user/create`) — per-user container orchestration
- [ ] Rekordbox import (`POST /rekordbox/import`, background job)
- [ ] Rekordbox export (`POST /rekordbox/export`)
- [ ] Serato import/export (`/serato/import`, `/serato/export`)
- [ ] Watch-dir auto-import (no endpoint — triggered by filesystem)
- [ ] Sync (`/sync/plan`, `/sync`, `/sync/tracks`, `/sync/playlists`)
- [x] Metadata / cues / loops (`/track/metadata/*`) + `/tracks/presence` —
      GET/update round-trip, schema validation, presence: `features/track-metadata-and-presence.md`
- [ ] beets import (`routers/beets_import.py`)
- [ ] Export progress reporting (`routers/export_progress.py`)
- [ ] Track matching (`routers/match_tracks.py`, `routers/sync.py`)
- [ ] Wishlist API (consumed by subbox-app `/wishlist` and by
      `subbox-slskd` — see root `CLAUDE.md`)

**When every row is checked, the loop does not run out of work** — it switches to
self-directed discovery (skill Step 1, tier 5): re-exercise the workflow whose
`features/*.md` is oldest to catch regressions (refresh its verified date), probe
the unhappy edges of a covered path (malformed request bodies, missing/oversized
uploads, a busy/half-migrated container, a job that fails partway, concurrent
requests), and add new rows here for any sub-flow that surfaces. Same conservative
bar — find and fix/log, never add endpoints. Skim the last ~10 `log.md` lines and
pick the least-recently-touched workflow so cycles don't repeat.

## Hard rules (do not relax these)

- **Bug fixes and small UX-adjacent improvements only** (e.g. a clearer error
  message the client surfaces verbatim). No new endpoints, no schema changes,
  no refactors.
- **Conservative fixes only.** Only commit a fix once re-verified against the
  same request/flow. Anything uncertain goes in `bugs.md` as OPEN.
- **One fix commit per repo per cycle**, on the `claude/continuous-ux` branch
  only. A cross-repo fix may commit once here *and* once in `../feishin-qa`.
- **Open a PR per verified fix; never merge.** After committing a verified fix to
  `claude/continuous-ux`, run `../subbox-workspace/qa-runner/open-pr.sh <this
  worktree>` — it cuts a clean branch off `main` (cherry-pick into a throwaway
  worktree), pushes, and opens **one PR per fix** labelled `qa-auto`. Record the
  PR URL in this `bugs.md` `FIXED` entry. **Never merge, never force-push a shared
  branch.** The user merges on GitHub; the next daily run rebases this branch onto
  the updated `main` to pull the merged code in.
- **Every bug gets a GitHub issue, and a closed issue means it's fixed.** When you
  log a bug OPEN in `bugs.md`, file a tracking issue with
  `../subbox-workspace/qa-runner/open-issue.sh <this worktree> "<title>" "<body>"`
  (label `qa-bug`) and record its URL as an `Issue:` line in the entry — never
  re-file one that already has the link. A fix commit/PR carries `Closes #<n>`, so
  merging it closes the issue; the issue's closed state is the signal the bug is
  fixed in `main`, which the loop reconciles back into `bugs.md` each cycle (skill
  Step 1½). A cross-repo bug gets an issue on each affected repo, cross-linked.
- **Never run Alembic migrations, never touch staging/prod DBs or
  containers.** Only the local dev stack (already-running `pymix` +
  `pymix-postgres` under `../traefik/docker-compose.yml`), and never write to
  a running per-user container (`navidrome*`, `beets*`) without confirming
  it's a disposable local test user.
- **subbox_id tagging is sacrosanct** — never touch the ingest path in a way
  that could skip or corrupt `SUBBOX_ID` tagging (see root `CLAUDE.md`).
- **Client-coupled fixes are allowed, but only as a coordinated,
  end-to-end-verified pair — never one-sided.** If a fix here requires a matching
  subbox-app change to resolve the user-facing symptom, you may implement both: the
  server change here, the client change in `../feishin-qa`, one commit per repo on
  each `claude/continuous-ux` branch. Commit **only** after driving the full flow
  with *both* changes live — rebuild the image to `laker93/pymix:qa-local`, swap
  the running container (first confirm it's idle — the user may be testing against
  it), rebuild the Electron client, then reproduce the original symptom and confirm
  it's resolved. Cross-reference both commit SHAs in this `bugs.md` and in
  `../feishin-qa/docs/qa/bugs.md`. If you can't verify both sides this cycle, do
  **not** ship a one-sided fix: log both sides OPEN and stop there.

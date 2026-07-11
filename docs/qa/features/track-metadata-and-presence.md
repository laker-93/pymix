# Track metadata (cues/loops) + presence

Verified live 2026-07-11 against the local dev stack (`pymix.docker.localhost`,
shared `pymix` container on `laker93/pymix:qa-local`), test user `test260526`.
All calls made directly over HTTP as the client would; a mutating round-trip
was driven and then the DB row restored byte-identical (see below).

Endpoints (`pymix/routers/track.py`):

- `POST /tracks/presence` — body `{"subbox_ids": [...]}`, `?username=` or a
  `session_id` cookie. Returns `{"presence": {id: bool}}`.
- `GET /track/metadata/{track_id}?username=` — returns the track's cue/loop
  metadata.
- `POST /track/metadata/update?username=` — writes cue/loop metadata; the
  **`subbox_id` is taken from a cookie**, not the path/body; `cuedata`,
  `source_app`, `change_type` are JSON body fields.
- `DELETE /track` — removes a track from all three tables (map + meta + library).
  Not driven here (destructive; see the delete-track note in feishin-qa).

## Verified behavior

**Presence** (`/tracks/presence`):
- Real id → `true`, unknown/fake id → `false` (mixed batch returns both
  correctly).
- No `username` and no `session_id` → **HTTP 400** `"Must provide a username or
  session_id to identify user"`.
- Guardrail: >1000 ids in one request → HTTP 400 (`_PRESENCE_MAX_IDS`, read from
  source; not driven with a 1000+ batch this cycle).

**Metadata GET** (`/track/metadata/{id}`):
- Track with a `library_table` row → `success:true`, `metadata` = its cuedata.
- Track with no library row (or unknown id) → `success:true` HTTP-wise but
  `{"success": false, "reason": "No metadata found for track_id=…",
  "metadata": null}`.
- No `username`/`session_id` → `{"success": false, "reason": "Must provide
  username or session_id…"}`.

**Metadata update round-trip** (`/track/metadata/update`, driven then reverted):
- POST `{cues:[{index,position,name,color}], loops:[{index,start,end,active}],
  bpm, key}` with a `subbox_id` cookie → `success:true`. Immediately GETting the
  track returned **exactly** the written cues/loops/bpm/key. `cue_schema`
  (track.py:30) allows optional `bpm` (number) and `key` (string) alongside the
  cues/loops arrays.
- Invalid cuedata (an extra/unknown property) → `{"success": false, "reason":
  "Invalid cuedata: Additional properties are not allowed ('bogus' was
  unexpected)"}`. Validation (`jsonschema.validate`) runs **before** any DB
  write, so a bad payload cannot corrupt the stored row.
- `db_controller.update_metadata` overwrites the `library_table` row in place,
  bumps `version`, and appends a row to `meta_history_table` — so the full edit
  history is retained even though `library_table` holds only the latest version.

## Two things to know (not bugs)

1. **Presence and metadata read different tables.** Presence checks
   `subbox_beets_map_table` (the beets/library membership); metadata reads
   `library_table` (cue/loop data, only written by `/track/metadata/update` or
   import). So a track can legitimately be **present but have "No metadata
   found"** — it just has no cue/loop data yet. Don't mistake this for a data
   inconsistency. On `test260526` all 32 `library_table` rows currently hold
   empty `{"cues": [], "loops": []}` (rekordbox import created the rows; the
   source library carried no hot cues/loops).

2. **Metadata endpoints signal errors in the body, not the HTTP status.**
   `GET`/`POST /track/metadata/*` return **HTTP 200 with `success:false`** for
   missing-user, not-found, and invalid-schema cases, whereas `/tracks/presence`
   returns **HTTP 400** for the same missing-user case. This intra-API
   inconsistency is real but not obviously a defect — the client keys off the
   `success` field, and changing the status codes could break that contract.
   Left as an observation, not logged as a bug.

## Test-data hygiene

The update round-trip mutated `test260526`'s library row for subbox_id
`462693b6-4572-4fa8-9739-6e0465956e01` (version → 2, added a `meta_history_table`
row), then **restored it exactly**: `library_table` back to version 1 /
`{"cues": [], "loops": []}` / original `updated_at` (1782641822.511713), and the
extra history row deleted. Post-restore GET confirmed empty cuedata. No net DB
change; no per-user container touched.

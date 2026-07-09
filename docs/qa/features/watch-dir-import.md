# Watch-dir auto-import (pymix)

The path a newly-acquired audio file takes from a user's `watch/` directory
into their private library + per-user Navidrome, with a fresh `SUBBOX_ID`
assigned along the way. This is the **import half** of the wishlist →
download → import journey (the Soulseek/download half is exercised by the
`wishlist-import-dev` skill; see `directives.md`).

Verified 2026-07-09 by driving it live against the local dev stack, test user
`test260526`, on the shared `pymix` container (`laker93/pymix:qa-local`).

## What was driven (import-half shortcut)

Per the `wishlist-import-dev` SKILL.md, you can exercise the import half on its
own by dropping a **real** audio file straight into the user's watch dir and
skipping slskd. On this machine the watch dir is the Docker named volume
`user-updownloads`, mounted into `filebrowser` at `/data/users`, so the file
was placed with:

```
docker cp <file> filebrowser:/data/users/test260526/watch/
```

The scratch file used: a copy of a real MP3 (231 s of real audio), re-tagged to
a unique, obviously-scratch identity — `artist=QA UX Loop`,
`title=Import Probe 2026-07-09`, `album=qa-scratch` — **with its pre-existing
`SUBBOX_ID` TXXX frame stripped first** (via mutagen), so pymix would assign a
fresh one exactly as it would for genuinely new music. (Lesson: the real files
in `~/Downloads/test-watch` are already subbox-processed and carry a
`SUBBOX_ID`; strip it or the import isn't a faithful "new track" test.)

## The pipeline (observed, end to end, ~9 s wall clock)

`pymix/handlers/filebrowser_file_handler.py`:

1. **`poll_watchdir`** (`awatch` on the `user-updownloads` volume root) sees the
   new file under `<user>/watch/`.
2. **Debounce.** `DEBOUNCE_SECONDS = 15` — it waits until every file in the
   watch dir has been stable (unmodified) for 15 s before importing, so
   in-progress downloads aren't ingested half-written. Log line:
   `watch: triggering import for user test260526 (~3.6 MB, debounce complete)`.
   Also guarded by `user_library_size_exceeded` (logs `watch: library size
   exceeded` and skips if over quota).
3. **`trigger_processing` → `stage_for_import`** moves the file from
   `/user-updownloads/<user>/watch` to `/private-staged/<user>/`, then runs
   `beet import --group-albums --set user=<user> --set public=False -q
   /downloads` in the per-user `beets<user>` container.
4. **`_map_subbox_id_beet_id`** finds beets items with an unset `subbox_id`
   (`beet list -f '$id:$path' subbox_id::^$`), and for each one whose path
   resolves under `/music`, mints a UUID, records it in pymix's
   `subbox_beet_map`, and writes it back with `beet modify -y id:<n>
   subbox_id=<uuid>`. That is the `SUBBOX_ID` tag being stamped into the file.
5. `watch import: finished for user <user> (success=True)`.

## Verified outcomes

- File landed at `/music/test260526/QA UX Loop/qa-scratch/00 - QA UX Loop -
  Import Probe 2026-07-09.mp3` — correct `artist/album/NN - artist - title`
  layout (track number defaulted to `00` since none was tagged).
- **`SUBBOX_ID` physically present** in the imported file's ID3
  (`TXXX:SUBBOX_ID = 1e5002e2-9050-4067-8192-b317278d1cf0`), confirmed by
  copying the file back out and reading it with mutagen — not just the beets
  DB claiming it.
- Private-music file count went 83 → 84; watch dir was emptied (the watcher
  **moves**, not copies).
- **Navidrome scanned it in** with no manual trigger: the track is queryable in
  `navidrome.db` `media_file` as `Import Probe 2026-07-09 | QA UX Loop |
  qa-scratch`.

## Notes / caveats seen (not import bugs)

- On every watch import, `_map_subbox_id_beet_id` also re-touches 5 **pre-existing
  orphaned** beets entries (paths under `/downloads/…`, no `subbox_id`) and logs
  `Could not resolve path for beet_id=…, skipping` for each. Pre-existing data
  hygiene, not caused by the import — logged OPEN in `bugs.md`
  (`orphaned-downloads-beets-entries`).
- A single non-fatal `stderr: subsonic: Error: Expecting value: line 1 column 1
  (char 0)` is emitted during `beet modify` — the beets `subsonic` plugin's
  auto-notify getting a non-JSON reply. Import still succeeds; cosmetic log
  noise. Not investigated further this cycle.

## Left in place for the rest of the journey

The `QA UX Loop / Import Probe 2026-07-09` track
(`subbox_id=1e5002e2-9050-4067-8192-b317278d1cf0`) is **deliberately left in
`test260526`'s library** so a later cycle can drive sub-step 4 (add it to a
playlist in the client) and sub-step 5 (sync-download missing playlist tracks
to the isolated `subbox-dev/music` local dir). Clean it up (delete the file +
its beets entry + purge from Navidrome) only once sub-steps 4–5 are done or the
directive is abandoned. Scratch source copy: `/tmp/qa-import-scratch/`.

# Wishlist Soulseek acquisition → import → available (verified)

The *acquisition* half of the wishlist journey: a `resolved` wishlist row a user
doesn't own yet gets pulled off Soulseek by `download_wishlist.py`, bridged into
the watch dir, ingested (SUBBOX_ID stamped), scanned by Navidrome, and finally
promoted `downloaded → available` by pymix's reconcile loop. Complements
`watch-dir-import.md` (which covers only the import half, driven with a
hand-dropped file). Verified end to end 2026-07-10 against `test260526` on the
local dev stack (shared `pymix` on `laker93/pymix:qa-local`).

## Preconditions that matter

- **slskd must be logged into the Soulseek network** — not just up. Check
  `GET /api/v0/server` → `state: "Connected, LoggedIn"`. This is the flaky
  external dependency; if it's not logged in, the search step finds nothing and
  the journey can't proceed.
- **`/etc/hosts` must map the per-user Navidrome vhost.** The script's owned-check
  hits `navidrome<user>.docker.localhost` via Python `urllib`, whose
  `getaddrinfo` does *not* resolve `*.localhost` on macOS (curl does, which masks
  the gap). `127.0.0.1  navidrometest260526.docker.localhost` was added to
  `/etc/hosts` on 2026-07-09; confirmed resolving this cycle.
- **The `--max-downloads` cap slices `missing[:N]` *before* searching**
  (`download_wishlist.py:~1142`). There is no per-row filter. With rows ordered
  `[Text Chunk, Blood of Aza, Aphex Twin]`, only `--max-downloads 3` reaches the
  Aphex row. A smaller cap silently never searches the later rows.

## The verified flow (Aphex Twin – Xtal)

Seeded row: `wishlist_id=91ce2e1072bd4122b1c2b887e902a01b`, `resolved`,
`linked_subbox_id=None` (correctly not-yet-owned).

1. **Search + download (slskd).** `download_wishlist.py --max-downloads 3`
   (no `--dry-run`) searched all three `wishlist`-status rows. Text Chunk and
   Blood of Aza found no suitable Soulseek file (consistent across dry-run and
   real run — those rows simply aren't well-seeded on the network). Aphex Twin –
   Xtal matched with 82 candidate sources; the script queued and pulled
   `Aphex Twin - Xtal.flac` from peer **Slapper** (a real transfer, slskd
   `Completed, Succeeded`, 37,117,528 B / 12,954,463 samples / 44.1 kHz FLAC).
2. **Row flip → `downloaded` (script).** On transfer success the script's
   `on_downloaded` callback `PATCH`ed the row to `status: downloaded`
   (`linked_subbox_id` still None). This is what stops the next pass re-pulling
   it (later passes only search `wishlist`-status rows).
3. **Bridge into the watch dir (local-dev only).** Host-run slskd writes to
   `~/Downloads/test-watch/…` (a host dir), *not* the `user-updownloads` named
   volume pymix watches. Bridged with
   `docker cp "<file>" filebrowser:/data/users/test260526/watch/Aphex\ Twin\ -\ Xtal.flac`.
   (In a real deployment slskd's download dir *is* the watch dir, so this step
   doesn't exist — it's purely a local-dev wiring gap.)
4. **Import + SUBBOX_ID stamp (pymix watcher).** `poll_watchdir` detected the
   file, waited out the debounce (`~35 MB, debounce complete`), staged it,
   ran `beet import --group-albums`, landed it at
   `/music/Aphex Twin/Selected Ambient Works 85–92/01 - Aphex Twin - Xtal.flac`
   (beet_id 666), then `_map_subbox_id_beet_id` assigned and **physically wrote**
   `SUBBOX_ID=09d4a6f0-bb14-48e3-9104-3cb3b98c0670` (confirmed with `metaflac`
   inside `beetstest260526`). `watch import: finished … (success=True)`.
5. **Navidrome scan.** `startScan.view` took the per-user library 77 → 78
   `media_file`; the track became searchable (`search3` returns
   `Aphex Twin – Xtal`, id `ukf6Az0Y7iu6IHafrVfFl1`).
6. **Reconcile → `available` (pymix).** Once Navidrome had the track, the
   reconcile loop matched it to the seeded row and promoted it: the row is now
   `status: available`, `linked_subbox_id: 09d4a6f0-bb14-48e3-9104-3cb3b98c0670`
   — byte-identical to the SUBBOX_ID pymix stamped in step 4. (An immediate
   forced `POST /wishlist/reconcile` right after import returned `resolved: 0`
   because reconcile ran once *before* Navidrome had scanned — the "failed to
   find match on Xtal or any of its tokens" log at import time. Give Navidrome
   the scan first, then reconcile matches.)

## Timing / gotcha for future cycles

The reconcile loop can fire in the window between import and Navidrome's scan,
log a benign `no matches … failed to find match on <title>`, and return without
promoting. That is **not** a failure — the next reconcile cycle (or a forced one
*after* a Navidrome scan) promotes correctly. Don't chase that log as a bug.

One benign stderr appears during the `beet modify` that writes SUBBOX_ID:
`subsonic: Error: Expecting value: line 1 column 1 (char 0)`. The tag write
still succeeds (verified physically); it's the beets subsonic plugin choking on
a rescan-trigger response, not a tagging failure.

## Resulting state left in place

- Test library `test260526` gained a genuine Aphex Twin – Xtal track
  (`/music/Aphex Twin/Selected Ambient Works 85–92/01 - Aphex Twin - Xtal.flac`,
  beet_id 666, `subbox_id=09d4a6f0-…`). Not marked `qa-scratch` — it's a real,
  fully-tagged track and a realistic terminal state for the journey; left in
  place. A future cycle may remove it (delete the file, `beet remove` id 666,
  let Navidrome purge) if library drift becomes a concern.
- The seeded wishlist row is now `available`/linked — the correct end state, no
  longer scratch to clean up.
- Two `Aphex Twin - Xtal*.flac` copies remain in the host `~/Downloads/test-watch`
  slskd download dir (one pre-existing, one from this run's dedup suffix). Left
  alone — that's the user's slskd download folder, not QA scratch.
</content>
</invoke>

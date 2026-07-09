# Cycle log (pymix)

Append one line per loop cycle, newest at the bottom. Keep it terse — detail
belongs in `bugs.md` / `features/*.md`, not here.

Format: `YYYY-MM-DD HH:MM | <workflow explored> | <outcome>`

Outcome is one of: `verified`, `documented`, `bug-fixed` (see bugs.md),
`logged` (issue found, not fixed), `blocked` (say why).

<!-- entries start below -->
2026-07-09 10:20 | sync_plan/sync_playlists subbox_id fast path (pymix #21) | logged (subbox_id_match_summary logs ERROR on almost every normal sync — see bugs.md); rebuilt+redeployed local pymix from this branch as laker93/pymix:qa-local to verify live
2026-07-09 12:05 | fixed + re-verified subbox_id_match_summary false-positive ERROR (user-directed, not autonomous) | bug-fixed (see bugs.md FIXED); rebuilt+redeployed laker93/pymix:qa-local twice to confirm each iteration live; pytest passed
2026-07-09 20:40 | watch-dir auto-import driven live (import half of phone/Discord wishlist directive, sub-step 3) | verified end to end — poll_watchdir→debounce(15s)→stage→beet import→_map_subbox_id_beet_id stamped fresh SUBBOX_ID (physically confirmed in file), landed in /music, Navidrome scanned it. New features/watch-dir-import.md. Logged OPEN bug orphaned-downloads-beets-entries (5 stale /downloads/ beets rows, no subbox_id, warn every import — pre-existing, needs design call). No fix committed.

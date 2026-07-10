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
2026-07-10 06:45 | wishlist Soulseek acquisition → import → available, full round trip (feishin-qa Soulseek-acquisition directive) | verified end to end — download_wishlist.py pulled Aphex Twin - Xtal off Soulseek (peer "Slapper"), row wishlist→downloaded; bridged into watch dir, poll_watchdir→beet import (beet_id 666)→physically stamped SUBBOX_ID 09d4a6f0-…, Navidrome scan 77→78, reconcile promoted downloaded→available with linked_subbox_id=09d4a6f0-…. New features/wishlist-download-acquisition.md. Noted benign reconcile-before-scan "failed to find match" log (not a bug) + benign beet-modify subsonic stderr. No bug, no fix committed.
2026-07-10 09:30 | yt-dlp cookie-auth path (pymix #21 ytdlp_support.py — feishin-qa PENDING directive) | verified live (venv, no network/prod) — resolve_cookiefile matrix (None→None, missing→None+warn, present→path) + opts wiring: a present cookies file reaches opts["cookiefile"] in both YoutubeMatchService._search and LinkParseService._extract_info, absent→no key (anonymous unchanged). No bug, no code change. New features/ytdlp-cookie-auth.md. Authenticated-download outcome is prod-only (needs real cookies + datacenter IP) — handed to user; directive moved to DONE (local scope).

# yt-dlp cookie auth (pymix #21 `ytdlp_support.py`)

Verified behavior of the yt-dlp cookie-auth path used by wishlist link parsing
and YouTube matching. Driven live against the `pymix-qa` venv on branch
`claude/continuous-ux` — 2026-07-10.

## What it is / why it exists

YouTube (and to a lesser degree SoundCloud/Bandcamp) bot-challenges anonymous
`yt-dlp` metadata/search requests coming from datacenter IPs — which is exactly
what prod is (a cloud droplet) — with a "Sign in to confirm you're not a bot"
gate. To get past it, `yt-dlp` is handed a Netscape-format cookies file via its
`cookiefile` option. One file can hold youtube.com + soundcloud.com +
bandcamp.com cookies at once, so a single mounted secret covers every link
source `LinkParseService` handles.

Two services consume it:

- `pymix/services/link_parse_service.py` — `LinkParseService._extract_info`
  (wishlist link → track metadata).
- `pymix/services/youtube_match_service.py` — `YoutubeMatchService._search`
  (wishlist track → best-effort YouTube match via `ytsearch`).

Both take a `cookies_path` and run it through
`pymix/services/ytdlp_support.py::resolve_cookiefile`. In `containers.py` both
are wired to `config.wishlist.ytdlp_cookies_path`:

- `config.dev.yaml` — the line is **commented out** → `None` → anonymous
  (the pre-#21 behavior).
- `config.prod.yaml` — `ytdlp_cookies_path: /subbox/secrets/ytdlp-cookies.txt`
  (a mounted secret).

## `resolve_cookiefile` contract (verified live)

| input                              | returns | side effect |
|------------------------------------|---------|-------------|
| `None` (unconfigured)              | `None`  | none — anonymous, unchanged pre-#21 behavior |
| configured path, **file missing**  | `None`  | logs `WARNING … configured … but not found; proceeding without cookies` |
| configured path, **file present**  | the path | none |

The missing-file branch is deliberately non-fatal: a mis-mounted/absent secret
degrades to anonymous rather than breaking link parsing outright.

## Opts wiring (verified live this cycle)

Ran the three services through a `YoutubeDL` stub that captures the opts dict:

- **present cookies file** → both `YoutubeMatchService._search` and
  `LinkParseService._extract_info` put `opts["cookiefile"] = <path>`.
- **no cookies file** (`None`) → neither adds a `cookiefile` key (anonymous
  path is byte-for-byte the old behavior; `ignore_no_formats_error` etc.
  unchanged).

So when a real cookies file **is** present, it genuinely reaches yt-dlp — the
wiring is not dead. Verification script (throwaway):
`/tmp/verify_ytdlp_cookies.py` — stubs `YoutubeDL`, no network, no prod. Exercises
the `resolve_cookiefile` matrix + both services' opts in the present/absent cases.

## What is NOT (and cannot be) verified in local dev

Whether YouTube actually **accepts** the cookies and lets an otherwise-blocked
datacenter request through. That requires (a) a real, valid cookies file and
(b) a host that YouTube actually bot-challenges (a datacenter IP — prod), not a
residential/local machine where anonymous requests already succeed. The local
dev stack has neither, so the end-to-end "cookies unblock a 429/bot-gated
request" outcome is a **prod-only, user-supplied-cookies** validation. Handed
back to the user: drop a valid `ytdlp-cookies.txt` at
`/subbox/secrets/ytdlp-cookies.txt` on prod and confirm a previously-gated
wishlist YouTube resolve/match starts succeeding.

## Verdict

No bug. The cookie path is correctly implemented and correctly wired; it
degrades gracefully when unconfigured/mis-mounted and does inject the cookies
file when present. Everything checkable without prod is verified. No code
change.

# Pymix Backend - Wishlist Feature Implementation Guide

## Goal

Implement Wishlist as a first-class Pymix domain.

Pymix owns:

* wishlist storage
* wishlist workflow
* wishlist state transitions
* future Beets integration

Navidrome should remain unaware of Wishlist.

---

# State Machine

```text
wishlist
    ↓
downloaded
    ↓
imported
    ↓
available

wishlist
    ↓
ignored
```

Definitions:

* wishlist = desired track
* downloaded = acquired but not imported
* imported = Beets imported successfully
* available = visible in Navidrome
* ignored = no longer wanted

---

# Database Layer

Modify:

```text
pymix/model/db_tables.py
```

Add:

```python
class WishlistRow(Base):
    __tablename__ = 'wishlist_table'

    id = Column(Integer, primary_key=True, autoincrement=True)

    wishlist_id = Column(String, unique=True, nullable=False)

    user_id = Column(String, nullable=False)

    artist = Column(String, nullable=False)
    title = Column(String, nullable=False)
    album = Column(String)

    status = Column(String, nullable=False)

    youtube_video_id = Column(String)
    youtube_url = Column(String)

    linked_subbox_id = Column(String)

    created_at = Column(Float)
    updated_at = Column(Float)
```

---

# Migration

Create migration:

```text
pymix/migrations/versions/
```

Example:

```text
003_add_wishlist_table.py
```

Create:

```sql
wishlist_table
```

with indexes on:

```text
user_id
status
artist
title
```

---

# Models

Create:

```text
pymix/model/wishlist.py
```

```python
@dataclass
class WishlistItem:
    ...
```

Follow existing model patterns.

---

# Database Controller

Modify:

```text
pymix/controllers/db_controller.py
```

Add:

```python
create_wishlist_item()
get_wishlist_items()
get_wishlist_item()
update_wishlist_item()
delete_wishlist_item()
```

Filter all queries by:

```python
user_id
```

---

# API Router

Create:

```text
pymix/routers/wishlist.py
```

Endpoints:

```http
GET    /wishlist
POST   /wishlist

GET    /wishlist/{id}
PATCH  /wishlist/{id}
DELETE /wishlist/{id}

POST   /wishlist/{id}/match-youtube
```

Follow patterns used in:

```text
track.py
user.py
match_tracks.py
```

---

# Registration

Modify:

```text
pymix/registration.py
```

Register:

```python
wishlist_router
```

with FastAPI.

---

# Request Models

Create:

```text
pymix/model/api/wishlist_requests.py
```

Request:

```python
class CreateWishlistRequest(BaseModel):
    artist: str
    title: str
    album: str | None = None
```

Update:

```python
class UpdateWishlistRequest(BaseModel):
    ...
```

---

# YouTube Matching Service

Create:

```text
pymix/services/youtube_match_service.py
```

Method:

```python
match_track(
    artist: str,
    title: str
)
```

Returns:

```python
youtube_video_id
youtube_url
youtube_title
confidence
```

Initially use:

```text
"{artist} {title}"
```

search query.

Store selected match on WishlistRow.

---

# Future Beets Integration

Create future endpoint:

```http
POST /events/beets-import-complete
```

Payload:

```json
{
    "user_id": "...",
    "artist": "...",
    "title": "...",
    "album": "...",
    "subbox_id": "..."
}
```

---

# Wishlist Auto-Matching

When Beets import event arrives:

Search:

```python
wishlist_items
```

where:

```python
status in (
    "wishlist",
    "downloaded"
)
```

Use fuzzy matching:

```python
rapidfuzz
```

Compare:

```python
artist
title
```

If confidence > 90:

```python
status = "imported"
linked_subbox_id = imported_track_id
```

---

# Future Navidrome Availability Detection

Later create:

```python
update_available_status()
```

which checks:

```python
linked_subbox_id
```

against Navidrome library.

Transition:

```text
imported
    ↓
available
```

---

# Offline Wishlist via Google Sheets

A user can capture wishlist items **offline** in a Google Sheet. Pymix polls each
user's sheet, imports new rows as wishlist items, and (best-effort) writes a status
back into the sheet. This is the offline counterpart to the in-app wishlist CRUD —
both land in the same `wishlist_table`.

The matching client-side flow (copy button + URL modal) lives in
`../feishin/.github/wishlist.md`.

## Access model: service account + share-back (V1)

There is **one** Google **service account** for the whole deployment (its key lives
at `google_sheets.credentials_path`, e.g.
`/subbox/secrets/google-sheets-service-account.json`). Pymix authenticates as that
service account for **all** users' sheets — it is not per-user.

The user owns their copy of the template sheet. For pymix to touch it, **the user
shares their copy with the service-account email as Editor**. The service-account
email is surfaced in the app (it is `credentials.service_account_email`, the same
one printed by `dev_sandbox/create_wishlist_sheet_template.py`).

End-to-end flow:

```text
1. User clicks "Connect offline wishlist" in the app.
2. App opens the template's Google "Make a copy" URL
   (https://docs.google.com/spreadsheets/d/<TEMPLATE_ID>/copy).
   -> Google creates a copy in the USER's Drive (user-owned, private).
3. App shows a modal that REQUIRES the user to paste their copy's URL.
4. App parses the sheet id from the URL and calls PATCH /wishlist/sheet.
   -> pymix stores it on UserRow.wishlist_sheet_id.
5. The sheet_sync_loop background task (every google_sheets.poll_interval_s,
   default 300s) reads every such user's sheet, imports new rows, and writes
   "Added" / an error string back into the Status + Added columns.
```

## What already exists

| Piece | Location |
|---|---|
| Service-account Sheets wrapper (`read_rows`, `write_status`) | `pymix/services/google_sheets_service.py` |
| Row → wishlist-item sync, status write-back | `pymix/services/sheet_sync_service.py` |
| Background poll loop | `pymix/handlers/sheet_sync_handler.py` (`sheet_sync_loop`), started in `registration.py` `lifespan` |
| `wishlist_sheet_id` column | `UserRow` in `pymix/model/db_tables.py` |
| `PATCH /wishlist/sheet` (`SetWishlistSheetRequest`) | `pymix/routers/wishlist.py` |
| `update_user_wishlist_sheet_id`, `get_users_with_wishlist_sheet` | `pymix/controllers/db_controller.py` |
| Config (`credentials_path`, `poll_interval_s`) | `pymix/config/config.{dev,prod}.yaml` under `google_sheets` |
| Template generator (headers, formatting, protected ranges, instructions) | `dev_sandbox/create_wishlist_sheet_template.py` |

The sheet's six columns are `Raw Note, Artist, Title, YouTube URL, Status, Added`.
Per row, `_sync_row` imports the first of: YouTube URL → `status="wishlist"` (+ video
id); Artist+Title → `status="wishlist"`; Raw Note → `status="inbox"` (needs-info).

## Write access is optional — and must degrade safely

Sharing the copy as **Editor** gives the service account read **and** write, so it
can stamp `Added` back. If the user shares **Viewer-only** (or forgets the write
grant), reads still work but `write_status` fails — caught and logged in
`SheetSyncService._write_status`, so a single poll won't crash. If the user shares
**nothing**, even `read_rows` fails — caught in `sync_user`. Either way one user's
misconfiguration never breaks the loop for others. **Surface the service-account
email and the "share as Editor" step prominently** so write-back normally works.

### Dedup is content-based, not row-position-based

`create_wishlist_item` does **no** dedup itself; it always inserts a fresh
`wishlist_id`. Two guards in `SheetSyncService.sync_user` stop the same row being
imported twice:

1. **Status check (cheap, primary path)** — `if row["status"]: continue`. Once a row
   has been imported and `write_status` successfully wrote `Added` back, it's
   skipped on every later poll without touching the DB.
2. **Content-signature match (fallback)** — for rows with no Status (new row, or
   write-back never landed because the user only shared the sheet as Viewer),
   `sync_user` fetches the user's existing wishlist items once per poll and builds a
   signature per item: `youtube_video_id` if present, else
   `(artist.lower(), title.lower())`, else `raw_note.lower()`. The same signature is
   computed for the sheet row (`_row_signature`); if it already exists in the DB, the
   row is treated as already-imported — `_sync_row` is skipped (no duplicate insert)
   but a best-effort `write_status(..., "Added")` is still attempted so the row can
   take the cheap Status path next time write access is fixed.

This was originally a per-user high-water-mark row-index cursor
(`UserRow.wishlist_sheet_synced_through_row`), but that breaks if the user deletes
or reorders rows in their copy of the sheet — a row's *position* isn't a stable
identity, only its *content* is. The cursor column and its migration
(`005_add_wishlist_sheet_cursor.py`) were removed in
`006_drop_wishlist_sheet_cursor.py`.

Known limitation: the existing-item signature is computed from the DB's *current*
artist/title, not what was originally typed into the sheet. If a user edits an
already-imported item's artist/title in the app, a sheet row with the *original*
text no longer matches it and would re-import as a duplicate — this only protects
against literal duplicate content, not edits made after import.

## Future: one-click via OAuth (`drive.file`) — not V1

The share-back step (copy → share with a service-account email → paste URL) is the
main UX wart. A future V2 can remove all three steps with per-user OAuth:

* User authorizes Subbox once (scope `https://www.googleapis.com/auth/drive.file`,
  which is **non-sensitive** — no Google CASA restricted-scope review; an
  unverified app still works but shows the "unverified app" screen and is capped at
  100 users until verified).
* Subbox calls Drive `files.copy` on the public template into the **user's** Drive
  and retains read/write because the app created that file — no manual share, no URL
  paste, sheet stays user-owned.
* Per-user refresh tokens stored server-side (extend `user_token_table` or add a
  dedicated table; encrypt at rest; handle refresh + revocation).
* `GoogleSheetsService` would swap its single service-account credential for the
  per-user OAuth credential; the `read_rows` / `write_status` / sync layer is
  otherwise unchanged.

Defer until offline-wishlist adoption justifies the OAuth + token-management
overhead. Tracked here so the service-account model isn't mistaken for the end state.

---

# Security

All Wishlist operations must:

* require authenticated user
* filter by user_id
* never expose another user's wishlist items

Wishlist is a multi-tenant resource.

Apply same ownership validation patterns used elsewhere in Pymix.

---

# Acceptance Criteria

* Wishlist table exists
* CRUD API implemented
* User-scoped access enforced
* YouTube matching endpoint implemented
* Status updates supported
* Database migration created
* FastAPI router registered
* Existing functionality unaffected
* Foundation exists for future Beets event automation

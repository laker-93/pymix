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

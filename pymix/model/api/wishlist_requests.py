from typing import Optional

from pydantic import BaseModel, model_validator


class CreateWishlistRequest(BaseModel):
    artist: str = ""
    title: str = ""
    album: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    bandcamp_url: Optional[str] = None
    soundcloud_url: Optional[str] = None

    @model_validator(mode="after")
    def require_some_identifying_info(self) -> "CreateWishlistRequest":
        has_url = bool(self.youtube_url or self.bandcamp_url or self.soundcloud_url)
        if not has_url and not self.artist.strip() and not self.title.strip():
            raise ValueError("artist, title, or a URL is required")
        return self

    @property
    def initial_status(self) -> str:
        """Items missing a URL and either artist or title need more info before they're a usable wishlist entry."""
        has_url = bool(self.youtube_url or self.bandcamp_url or self.soundcloud_url)
        if has_url or (self.artist.strip() and self.title.strip()):
            return "wishlist"
        return "inbox"


class CreateWishlistBulkRequest(BaseModel):
    items: list[CreateWishlistRequest]


class SetWishlistSheetRequest(BaseModel):
    sheet_id: str


class ParseLinkRequest(BaseModel):
    url: str


class MatchMetadataRequest(BaseModel):
    """Metadata lookup. Either a raw ``query`` (free text, e.g. an inbox raw_note) or an
    ``artist``/``title`` pair the user has typed. The server routes the pair through a
    fielded MusicBrainz search (artist as a constraint) rather than flattening it into
    one string — see MusicBrainzMatchService.match_fields."""

    query: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None

    @model_validator(mode="after")
    def require_some_text(self) -> "MatchMetadataRequest":
        if not (self.query or self.artist or self.title):
            raise ValueError("query, artist, or title is required")
        return self


class UpdateWishlistRequest(BaseModel):
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    status: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    bandcamp_url: Optional[str] = None
    soundcloud_url: Optional[str] = None
    linked_subbox_id: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {"status": "downloaded"}
        }
    }

from typing import Optional

from pydantic import BaseModel


class CreateWishlistRequest(BaseModel):
    artist: str
    title: str
    album: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None


class SetWishlistSheetRequest(BaseModel):
    sheet_id: str


class UpdateWishlistRequest(BaseModel):
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    status: Optional[str] = None
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    linked_subbox_id: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {"status": "downloaded"}
        }
    }

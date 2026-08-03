from typing import Optional

from pydantic import BaseModel, EmailStr, Field, model_validator

from pymix.model.invite_request import DJ_SOFTWARE_OPTIONS, DjSoftware


class CreateInviteRequest(BaseModel):
    """A prospective beta tester asking for an invite token.

    Unauthenticated — the caller has no account yet — so every field is bounded to keep
    the row (and the request body) small; see ``routers/invite_request.py`` for the
    per-IP cap that goes with it.
    """

    email: EmailStr = Field(max_length=254)
    dj_software: str = Field(max_length=32)
    dj_software_other: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def check_dj_software(self) -> "CreateInviteRequest":
        if self.dj_software not in DJ_SOFTWARE_OPTIONS:
            raise ValueError(f"dj_software must be one of {', '.join(DJ_SOFTWARE_OPTIONS)}")

        # The free-text field only means anything alongside 'other'; drop it otherwise so
        # a client that leaves a stale value in the field can't mislabel a known package.
        if self.dj_software != DjSoftware.OTHER.value:
            self.dj_software_other = None
        elif self.dj_software_other is not None and not self.dj_software_other.strip():
            self.dj_software_other = None

        return self

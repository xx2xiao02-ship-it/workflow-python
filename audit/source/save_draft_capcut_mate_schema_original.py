from pydantic import BaseModel, Field


class SaveDraftRequest(BaseModel):
    draft_url: str = Field(default="", description="草稿URL")


class SaveDraftResponse(BaseModel):
    draft_url: str = Field(default="", description="草稿URL")

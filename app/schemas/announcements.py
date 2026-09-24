from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models import AnnouncementCategory


class AnnouncementDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    category: AnnouncementCategory
    is_pinned: bool = False


class AnnouncementDraftUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1)
    category: AnnouncementCategory | None = None
    is_pinned: bool | None = None


class AnnouncementSubmitRequest(BaseModel):
    publish_type: Literal["immediate", "scheduled"] = "immediate"
    publish_at: str | None = Field(default=None, description="ISO 8601 时间；定时发布时必填且必须晚于当前时间")


class AnnouncementReviewRequest(BaseModel):
    passed: bool
    opinion: str | None = Field(default=None, max_length=1000)


class AnnouncementWithdrawRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AnnouncementRescheduleRequest(BaseModel):
    publish_at: str = Field(description="新的 ISO 8601 发布时间，必须晚于当前时间")

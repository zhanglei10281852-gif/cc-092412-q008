from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AnnouncementDraftRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    category: Literal["通知", "公告", "政策", "公示"]
    is_pinned: bool = False


class AnnouncementReviseRequest(AnnouncementDraftRequest):
    pass


class AnnouncementSubmitRequest(BaseModel):
    scheduled_for: datetime | None = None

    @field_validator("scheduled_for")
    @classmethod
    def tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("定时发布时间必须带时区")
        return value


class AnnouncementReviewRequest(BaseModel):
    approved: bool
    opinion: str = Field(default="", max_length=1000)


class AnnouncementWithdrawRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.announcement import (
    AnnouncementDraftRequest,
    AnnouncementReviewRequest,
    AnnouncementReviseRequest,
    AnnouncementSubmitRequest,
    AnnouncementWithdrawRequest,
)
from app.services.announcements import AnnouncementService

# 公众阅读接口：只暴露已发布公告的生效版本与历史版本
public_router = APIRouter(prefix="/announcements", tags=["公告公开"])

# 管理接口：草稿、送审、审阅、更正、撤回、归档全流程
admin_router = APIRouter(prefix="/api/announcements", tags=["公告管理"])

CATEGORIES = ("通知", "公告", "政策", "公示")


@public_router.get("")
def public_list(
    category: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict:
    if category is not None and category not in CATEGORIES:
        category = None
    offset = (page - 1) * size
    result = AnnouncementService(get_connection()).list_public(
        category=category, limit=size, offset=offset
    )
    result["page"] = page
    result["size"] = size
    result.pop("limit", None)
    result.pop("offset", None)
    return result


@public_router.get("/{announcement_id}")
def public_detail(
    announcement_id: int,
    version_no: int | None = Query(default=None, ge=1),
) -> dict:
    # 默认返回生效版本；指定 version_no 时返回当时版本，并标明是否存在后续更正
    return AnnouncementService(get_connection()).public_version(announcement_id, version_no)


@admin_router.post("", status_code=201)
def create_draft(data: AnnouncementDraftRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).create_draft(principal, data.model_dump())


@admin_router.get("")
def list_internal(
    status: list[str] | None = Query(default=None),
    category: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    offset = (page - 1) * size
    result = AnnouncementService(get_connection()).list_internal(
        principal, statuses=status, category=category, limit=size, offset=offset
    )
    result["page"] = page
    result["size"] = size
    result.pop("limit", None)
    result.pop("offset", None)
    return result


@admin_router.get("/{announcement_id}")
def internal_detail(announcement_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return AnnouncementService(get_connection()).detail(principal, announcement_id)


@admin_router.put("/{announcement_id}/revise")
def revise(announcement_id: int, data: AnnouncementReviseRequest,
           principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).revise(principal, announcement_id, data.model_dump())


@admin_router.post("/{announcement_id}/submit")
def submit(announcement_id: int, data: AnnouncementSubmitRequest,
           principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).submit(principal, announcement_id, data.scheduled_for)


@admin_router.post("/{announcement_id}/review")
def review(announcement_id: int, data: AnnouncementReviewRequest,
           principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).review(
            principal, announcement_id, data.approved, data.opinion
        )


@admin_router.post("/{announcement_id}/correct")
def correct(announcement_id: int, data: AnnouncementDraftRequest,
            principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).correct(principal, announcement_id, data.model_dump())


@admin_router.post("/{announcement_id}/withdraw")
def withdraw(announcement_id: int, data: AnnouncementWithdrawRequest,
             principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).withdraw(principal, announcement_id, data.reason)


@admin_router.post("/{announcement_id}/archive")
def archive(announcement_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).archive(principal, announcement_id)


@admin_router.get("/{announcement_id}/events")
def events(announcement_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return {"data": AnnouncementService(get_connection()).events(principal, announcement_id)}

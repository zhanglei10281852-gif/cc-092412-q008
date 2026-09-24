from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.announcements import (
    AnnouncementDraftRequest,
    AnnouncementDraftUpdateRequest,
    AnnouncementRescheduleRequest,
    AnnouncementReviewRequest,
    AnnouncementSubmitRequest,
    AnnouncementWithdrawRequest,
)
from app.services.announcement_scheduler import AnnouncementPublishScheduler
from app.services.announcements import AnnouncementService

router = APIRouter(tags=["公告管理"])


# ====================================================================== 公众阅读接口（无需登录）


@router.get("/announcements", tags=["公告公开"])
def public_list_announcements(
    category: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """公开列表：只返回已发布公告，置顶与内容均以生效版本为准。"""
    service = AnnouncementService(get_connection())
    result = service.public_list(category=category, limit=size, offset=(page - 1) * size)
    return {"total": result["total"], "page": page, "size": size, "data": result["data"]}


@router.get("/announcements/{announcement_id}", tags=["公告公开"])
def public_get_announcement(announcement_id: int):
    """阅读接口返回生效版本；撤回后继续展示当时版本并标明撤回原因。"""
    return AnnouncementService(get_connection()).public_detail(announcement_id)


@router.get("/announcements/{announcement_id}/versions/{version_no}", tags=["公告公开"])
def public_get_announcement_version(announcement_id: int, version_no: int):
    """旧链接永远返回当时发布的版本内容，并标明后续是否已有更正版本。"""
    return AnnouncementService(get_connection()).public_version(announcement_id, version_no)


# ====================================================================== 管理接口（需要登录与权限）


management = APIRouter(prefix="/api/announcements", tags=["公告管理"])


@management.post("", status_code=201)
def create_draft(data: AnnouncementDraftRequest, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).create_draft(principal, data.model_dump())


@management.get("")
def list_announcements(
    status: Optional[str] = None,
    category: Optional[str] = None,
    review_status: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(current_principal),
) -> dict:
    result = AnnouncementService(get_connection()).list_management(
        principal, status=status, category=category, review_status=review_status,
        limit=size, offset=(page - 1) * size
    )
    return {"total": result["total"], "page": page, "size": size, "data": result["data"]}


@management.get("/{announcement_id}")
def get_announcement(announcement_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return AnnouncementService(get_connection()).detail(principal, announcement_id)


@management.patch("/{announcement_id}")
def revise_draft(
    announcement_id: int,
    data: AnnouncementDraftUpdateRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).revise_draft(
            principal, announcement_id, data.model_dump(exclude_unset=True)
        )


@management.post("/{announcement_id}/corrections", status_code=201)
def create_correction(
    announcement_id: int,
    data: AnnouncementDraftRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).create_correction(principal, announcement_id, data.model_dump())


@management.post("/{announcement_id}/submit")
def submit_announcement(
    announcement_id: int,
    data: AnnouncementSubmitRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).submit(
            principal, announcement_id, data.publish_type, data.publish_at
        )


@management.post("/{announcement_id}/review")
def review_announcement(
    announcement_id: int,
    data: AnnouncementReviewRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).review(
            principal, announcement_id, data.passed, data.opinion
        )


@management.post("/{announcement_id}/withdraw")
def withdraw_announcement(
    announcement_id: int,
    data: AnnouncementWithdrawRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).withdraw(principal, announcement_id, data.reason)


@management.post("/{announcement_id}/archive")
def archive_announcement(announcement_id: int, principal: Principal = Depends(current_principal)) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).archive(principal, announcement_id)


@management.post("/{announcement_id}/reschedule")
def reschedule_announcement(
    announcement_id: int,
    data: AnnouncementRescheduleRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection).reschedule(principal, announcement_id, data.publish_at)


@management.post("/run-due")
def run_due_publishes(principal: Principal = Depends(current_principal)) -> dict:
    """立即执行所有到期的定时发布任务（运维/测试入口）。"""
    principal.require("jobs.run")
    processed = AnnouncementPublishScheduler().run_once()
    return {"processed": processed}

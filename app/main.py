from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import audit, auth, departments_admin, maintenance, metrics, roles, system, users, workflow
from app.core.errors import DomainError
from app.database import close_connection, get_connection, init_db
from app.routers import affairs, announcements, departments, petitions, residents
from app.services.scheduler import AnnouncementScheduler


def _scheduler_enabled() -> bool:
    return os.getenv("TOWNSHIP_SCHEDULER_ENABLED", "1").strip() not in {"0", "false", "False", ""}


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    scheduler: AnnouncementScheduler | None = None
    if _scheduler_enabled():
        # 启动即扫描数据库中到点的发布任务，进程重启后仍能准确补发且仅发布一次
        from app.core.config import Settings
        scheduler = AnnouncementScheduler(lease_seconds=Settings.load().job_lease_seconds)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        close_connection()


app = FastAPI(title="乡镇政务协同服务", version="2.1.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    # 越权拒绝携带的审计上下文：业务事务已随异常回滚，这里独立写入留痕
    denied = exc.context.get("audit_denied") if exc.context else None
    if denied is not None:
        try:
            from app.services.audit import AuditContext, AuditService
            AuditService(get_connection()).record(
                AuditContext(denied["actor_user_id"], denied["actor_name"]),
                action=denied["action"],
                resource_type="announcement",
                resource_id=denied["resource_id"],
                outcome="denied",
                metadata={"reason": denied["reason"], "path": str(request.url.path)},
            )
        except Exception:  # noqa: BLE001 - 审计失败不能掩盖原始业务错误
            logging.getLogger("app.audit").exception("越权审计留痕写入失败")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "context": {k: v for k, v in exc.context.items() if k != "audit_denied"}}},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(departments_admin.router)
app.include_router(workflow.router)
app.include_router(metrics.router)
app.include_router(maintenance.router)
app.include_router(residents.router)
app.include_router(affairs.router)
app.include_router(announcements.public_router)
app.include_router(announcements.admin_router)
app.include_router(departments.router)
app.include_router(petitions.router)


@app.get("/")
def root() -> dict:
    return {"service": "乡镇政务协同服务", "version": "2.1.0"}

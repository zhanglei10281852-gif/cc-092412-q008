from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditContext, AuditService

PUBLISH_JOB_TYPE = "announcement.publish"

# 公告工作流状态
DRAFT = "draft"
PENDING_REVIEW = "pending_review"
APPROVED = "approved"
PUBLISHED = "published"
WITHDRAWN = "withdrawn"
ARCHIVED = "archived"

# 版本审阅状态
V_DRAFT = "draft"
V_SUBMITTED = "submitted"
V_APPROVED = "approved"
V_REJECTED = "rejected"

MANAGE_PERMISSIONS = ("announcements.write", "announcements.review", "announcements.audit")


class AnnouncementService:
    """公告草稿、送审、审阅、发布、撤回、归档的领域服务。

    所有写操作都要求外层使用 BEGIN IMMEDIATE 事务，配合条件 UPDATE 保证
    定时回调、撤回与发布并发时结果确定。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 内部辅助

    def _now_storage(self) -> str:
        return to_storage(self.clock.now())

    def _require_any_permission(self, principal: Principal) -> None:
        if not any(principal.can(code) for code in MANAGE_PERMISSIONS):
            raise PermissionDeniedError("缺少公告管理权限")

    def _get_workflow(self, announcement_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM announcement_workflow WHERE announcement_id=?",
            (announcement_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("公告不存在")
        return dict(row)

    def _get_version(self, announcement_id: int, version_no: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM announcement_versions WHERE announcement_id=? AND version_no=?",
            (announcement_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("公告版本不存在")
        return dict(row)

    def _latest_version_no(self, announcement_id: int) -> int:
        row = self.connection.execute(
            "SELECT MAX(version_no) AS m FROM announcement_versions WHERE announcement_id=?",
            (announcement_id,),
        ).fetchone()
        return int(row["m"] or 0)

    def _has_inflight_version(self, announcement_id: int) -> bool:
        """存在送审中、或已批准但尚未生效（含定时等待）的版本。"""
        row = self.connection.execute(
            """
            SELECT v.id FROM announcement_versions v
            LEFT JOIN background_jobs j ON j.id=v.publish_job_id
            WHERE v.announcement_id=?
              AND v.published_at IS NULL
              AND (v.review_status='submitted'
                   OR (v.review_status='approved'
                       AND (v.publish_type='immediate'
                            OR (j.id IS NOT NULL AND j.status IN ('pending','running')))))
            LIMIT 1
            """,
            (announcement_id,),
        ).fetchone()
        return row is not None

    def _add_event(
        self,
        announcement_id: int,
        event: str,
        principal: Principal | None,
        *,
        version_no: int | None = None,
        detail: str | None = None,
        created_at: str | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO announcement_events(announcement_id,version_no,event,actor_user_id,actor_name,detail,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                announcement_id,
                version_no,
                event,
                principal.user_id if principal else None,
                principal.display_name if principal else "system",
                detail,
                created_at or self._now_storage(),
            ),
        )

    def _cancel_scheduled_jobs(self, announcement_id: int, *, version_no: int | None = None) -> int:
        """取消尚未结束的定时发布任务，返回取消数量。"""
        sql = (
            "UPDATE background_jobs SET status='cancelled',updated_at=? "
            "WHERE job_type=? AND status IN ('pending','running') AND id IN ("
            "SELECT publish_job_id FROM announcement_versions "
            "WHERE announcement_id=? AND published_at IS NULL AND publish_job_id IS NOT NULL"
        )
        params: list[Any] = [self._now_storage(), PUBLISH_JOB_TYPE, announcement_id]
        if version_no is not None:
            sql += " AND version_no=?"
            params.append(version_no)
        sql += ")"
        cursor = self.connection.execute(sql, tuple(params))
        return cursor.rowcount

    def _reset_inflight_versions(self, announcement_id: int) -> int:
        """撤回/归档时把尚未生效的在途版本打回草稿：送审中或已批准待发均终止。

        已取消的定时任务关联一并清空，避免撤回后审阅通过把公告“复活”。
        """
        cursor = self.connection.execute(
            "UPDATE announcement_versions SET review_status='draft',publish_type='immediate',"
            "publish_at=NULL,publish_job_id=NULL,submitted_at=NULL "
            "WHERE announcement_id=? AND published_at IS NULL AND review_status IN ('submitted','approved')",
            (announcement_id,),
        )
        return cursor.rowcount

    def _publish_version(self, announcement_id: int, version_no: int, *, published_at: str) -> bool:
        """把指定版本置为生效版本。条件 UPDATE 保证重复回调只生效一次。"""
        cursor = self.connection.execute(
            "UPDATE announcement_versions SET published_at=? "
            "WHERE announcement_id=? AND version_no=? AND review_status='approved' AND published_at IS NULL",
            (published_at, announcement_id, version_no),
        )
        if cursor.rowcount == 0:
            return False
        self.connection.execute(
            "UPDATE announcement_workflow SET status='published',effective_version_no=?,"
            "published_at=COALESCE(published_at,?),withdraw_reason=NULL,withdrawn_at=NULL,"
            "withdrawn_by_user_id=NULL,updated_at=? WHERE announcement_id=?",
            (version_no, published_at, published_at, announcement_id),
        )
        return True

    def _enqueue_publish_job(self, announcement_id: int, version: dict[str, Any], available_at: str) -> int:
        now = self._now_storage()
        key = f"announcement.publish:v{version['id']}:{secrets.token_hex(6)}"
        payload = {
            "announcement_id": announcement_id,
            "version_id": version["id"],
            "version_no": version["version_no"],
        }
        cursor = self.connection.execute(
            "INSERT INTO background_jobs(job_type,deduplication_key,payload_json,status,available_at,created_at,updated_at) "
            "VALUES(?,?,?,'pending',?,?,?)",
            (PUBLISH_JOB_TYPE, key, json.dumps(payload, ensure_ascii=False, sort_keys=True), available_at, now, now),
        )
        return int(cursor.lastrowid)

    def _parse_publish_at(self, publish_at: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(publish_at)
        except ValueError as exc:
            raise ValidationError("发布时间必须是 ISO 8601 格式") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    # ------------------------------------------------------------------ 草稿

    def create_draft(self, principal: Principal, data: dict) -> dict:
        principal.require("announcements.write")
        now = self._now_storage()
        cursor = self.connection.execute(
            "INSERT INTO announcements(title,content,category,publisher,is_pinned,created_at) VALUES(?,?,?,?,?,?)",
            (data["title"], data["content"], data["category"].value, principal.display_name,
             1 if data["is_pinned"] else 0, now),
        )
        announcement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO announcement_versions(announcement_id,version_no,title,content,category,is_pinned,"
            "author_user_id,author_name,review_status,publish_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,'immediate',?)",
            (announcement_id, 1, data["title"], data["content"], data["category"].value,
             1 if data["is_pinned"] else 0, principal.user_id, principal.display_name, V_DRAFT, now),
        )
        self.connection.execute(
            "INSERT INTO announcement_workflow(announcement_id,status,current_version_no,updated_at) VALUES(?,?,?,?)",
            (announcement_id, DRAFT, 1, now),
        )
        self._add_event(announcement_id, "created", principal, version_no=1)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="announcement.draft.create",
            resource_type="announcement",
            resource_id=announcement_id,
            after={"version_no": 1, "title": data["title"]},
        )
        return self.detail(principal, announcement_id)

    def revise_draft(self, principal: Principal, announcement_id: int, changes: dict) -> dict:
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        if workflow["status"] == ARCHIVED:
            raise ConflictError("公告已归档，不能再修改")
        version = self._get_version(announcement_id, workflow["current_version_no"])
        if version["review_status"] not in (V_DRAFT, V_REJECTED):
            raise ConflictError("当前版本已送审或已生效，不能直接修改；已公开内容请新建更正版本")
        fields = {key: value for key, value in changes.items() if value is not None}
        if not fields:
            raise ValidationError("没有需要更新的内容")
        title = fields.get("title", version["title"])
        content = fields.get("content", version["content"])
        category = fields["category"].value if "category" in fields else version["category"]
        is_pinned = fields.get("is_pinned", bool(version["is_pinned"]))
        self.connection.execute(
            "UPDATE announcement_versions SET title=?,content=?,category=?,is_pinned=?,review_status='draft',"
            "reviewer_user_id=NULL,reviewer_name=NULL,review_opinion=NULL,reviewed_at=NULL WHERE id=?",
            (title, content, category, 1 if is_pinned else 0, version["id"]),
        )
        now = self._now_storage()
        if workflow["effective_version_no"] is None:
            self.connection.execute(
                "UPDATE announcement_workflow SET status=?,updated_at=? WHERE announcement_id=?",
                (DRAFT, now, announcement_id),
            )
        self._add_event(announcement_id, "revised", principal, version_no=version["version_no"], detail="修改草稿")
        return self.detail(principal, announcement_id)

    def create_correction(self, principal: Principal, announcement_id: int, data: dict) -> dict:
        """已公开内容的修改必须产生新版本。"""
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        if workflow["status"] not in (PUBLISHED, WITHDRAWN):
            raise ConflictError("只有已发布或已撤回的公告才能新建更正版本")
        if self._has_inflight_version(announcement_id):
            raise ConflictError("已有送审中或等待定时发布的版本，请先处理后再新建版本")
        new_no = self._latest_version_no(announcement_id) + 1
        now = self._now_storage()
        self.connection.execute(
            "INSERT INTO announcement_versions(announcement_id,version_no,title,content,category,is_pinned,"
            "author_user_id,author_name,review_status,publish_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,'immediate',?)",
            (announcement_id, new_no, data["title"], data["content"], data["category"].value,
             1 if data["is_pinned"] else 0, principal.user_id, principal.display_name, V_DRAFT, now),
        )
        self.connection.execute(
            "UPDATE announcement_workflow SET current_version_no=?,updated_at=? WHERE announcement_id=?",
            (new_no, now, announcement_id),
        )
        self._add_event(announcement_id, "correction_created", principal, version_no=new_no, detail="新建更正版本")
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="announcement.correction.create",
            resource_type="announcement",
            resource_id=announcement_id,
            after={"version_no": new_no},
        )
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 送审与审阅

    def submit(self, principal: Principal, announcement_id: int, publish_type: str, publish_at: str | None) -> dict:
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        if workflow["status"] == ARCHIVED:
            raise ConflictError("公告已归档，不能再送审")
        version = self._get_version(announcement_id, workflow["current_version_no"])
        if version["review_status"] not in (V_DRAFT, V_REJECTED):
            raise ConflictError("只有草稿或被驳回的版本可以送审")
        scheduled_at: str | None = None
        if publish_type == "scheduled":
            if not publish_at:
                raise ValidationError("定时发布必须指定发布时间")
            target = self._parse_publish_at(publish_at)
            if target <= self.clock.now():
                raise ValidationError("发布时间必须晚于当前时间；过期计划不会被接受")
            scheduled_at = to_storage(target)
        now = self._now_storage()
        self.connection.execute(
            "UPDATE announcement_versions SET review_status='submitted',publish_type=?,publish_at=?,"
            "submitted_at=? WHERE id=?",
            ("scheduled" if scheduled_at else "immediate", scheduled_at, now, version["id"]),
        )
        if workflow["effective_version_no"] is None:
            self.connection.execute(
                "UPDATE announcement_workflow SET status=?,updated_at=? WHERE announcement_id=?",
                (PENDING_REVIEW, now, announcement_id),
            )
        else:
            self.connection.execute(
                "UPDATE announcement_workflow SET updated_at=? WHERE announcement_id=?",
                (now, announcement_id),
            )
        self._add_event(
            announcement_id, "submitted", principal, version_no=version["version_no"],
            detail=f"送审，发布方式：{'定时 ' + scheduled_at if scheduled_at else '立即发布'}",
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="announcement.submit",
            resource_type="announcement",
            resource_id=announcement_id,
            metadata={"version_no": version["version_no"], "publish_type": publish_type},
        )
        return self.detail(principal, announcement_id)

    def review(self, principal: Principal, announcement_id: int, passed: bool, opinion: str | None) -> dict:
        principal.require("announcements.review")
        workflow = self._get_workflow(announcement_id)
        version = self._get_version(announcement_id, workflow["current_version_no"])
        if version["review_status"] != V_SUBMITTED:
            raise ConflictError("当前版本不在待审阅状态")
        # 职责分离：撰写人与审阅人不能是同一账号
        if version["author_user_id"] is not None and version["author_user_id"] == principal.user_id:
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="announcement.review",
                resource_type="announcement",
                resource_id=announcement_id,
                outcome="denied",
                metadata={"version_no": version["version_no"], "reason": "author_is_reviewer"},
            )
            raise PermissionDeniedError("撰写人与审阅人不能是同一账号")
        now = self._now_storage()
        if not passed:
            self.connection.execute(
                "UPDATE announcement_versions SET review_status='rejected',reviewer_user_id=?,reviewer_name=?,"
                "review_opinion=?,reviewed_at=?,publish_type='immediate',publish_at=NULL,publish_job_id=NULL WHERE id=?",
                (principal.user_id, principal.display_name, opinion, now, version["id"]),
            )
            if workflow["effective_version_no"] is None:
                self.connection.execute(
                    "UPDATE announcement_workflow SET status=?,updated_at=? WHERE announcement_id=?",
                    (DRAFT, now, announcement_id),
                )
            else:
                self.connection.execute(
                    "UPDATE announcement_workflow SET updated_at=? WHERE announcement_id=?",
                    (now, announcement_id),
                )
            self._add_event(
                announcement_id, "rejected", principal, version_no=version["version_no"],
                detail=opinion or "审阅驳回",
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="announcement.review",
                resource_type="announcement",
                resource_id=announcement_id,
                metadata={"version_no": version["version_no"], "decision": "rejected"},
            )
            return self.detail(principal, announcement_id)

        self.connection.execute(
            "UPDATE announcement_versions SET review_status='approved',reviewer_user_id=?,reviewer_name=?,"
            "review_opinion=?,reviewed_at=? WHERE id=?",
            (principal.user_id, principal.display_name, opinion, now, version["id"]),
        )
        due = not version["publish_at"] or from_storage(version["publish_at"]) <= self.clock.now()
        if version["publish_type"] == "immediate" or due:
            self._publish_version(announcement_id, version["version_no"], published_at=now)
            reason = "审阅通过立即发布" if version["publish_type"] == "immediate" else "审阅通过时已到计划时间，立即发布"
            self._add_event(
                announcement_id, "approved_published", principal,
                version_no=version["version_no"], detail=reason,
            )
        else:
            job_id = self._enqueue_publish_job(announcement_id, version, version["publish_at"])
            self.connection.execute(
                "UPDATE announcement_versions SET publish_job_id=? WHERE id=?",
                (job_id, version["id"]),
            )
            if workflow["effective_version_no"] is None:
                self.connection.execute(
                    "UPDATE announcement_workflow SET status=?,updated_at=? WHERE announcement_id=?",
                    (APPROVED, now, announcement_id),
                )
            else:
                self.connection.execute(
                    "UPDATE announcement_workflow SET updated_at=? WHERE announcement_id=?",
                    (now, announcement_id),
                )
            self._add_event(
                announcement_id, "approved_scheduled", principal, version_no=version["version_no"],
                detail=f"审阅通过，计划于 {version['publish_at']} 定时发布（任务 {job_id}）",
            )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="announcement.review",
            resource_type="announcement",
            resource_id=announcement_id,
            metadata={"version_no": version["version_no"], "decision": "approved"},
        )
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 撤回、归档、改期

    def withdraw(self, principal: Principal, announcement_id: int, reason: str) -> dict:
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        if workflow["status"] not in (PUBLISHED, APPROVED):
            raise ConflictError("当前状态不允许撤回：仅已发布或已批准等待定时发布的公告可以撤回")
        cancelled = self._cancel_scheduled_jobs(announcement_id)
        reset_versions = self._reset_inflight_versions(announcement_id)
        now = self._now_storage()
        # 条件 UPDATE：与定时发布回调竞争时，只有一方能把状态推进一步，结果确定
        cursor = self.connection.execute(
            "UPDATE announcement_workflow SET status=?,withdraw_reason=?,withdrawn_at=?,"
            "withdrawn_by_user_id=?,updated_at=? WHERE announcement_id=? AND status IN (?,?)",
            (WITHDRAWN, reason, now, principal.user_id, now, announcement_id, PUBLISHED, APPROVED),
        )
        if cursor.rowcount == 0:
            raise ConflictError("公告状态已变化，撤回未生效")
        detail = f"撤回：{reason}"
        if cancelled:
            detail += f"；同时取消 {cancelled} 个待执行的定时发布任务"
        if reset_versions:
            detail += f"；{reset_versions} 个在途版本（送审中/待发布）已退回草稿"
        self._add_event(announcement_id, "withdrawn", principal,
                        version_no=workflow["current_version_no"], detail=detail)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="announcement.withdraw",
            resource_type="announcement",
            resource_id=announcement_id,
            metadata={"reason": reason, "cancelled_jobs": cancelled},
        )
        return self.detail(principal, announcement_id)

    def archive(self, principal: Principal, announcement_id: int) -> dict:
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        if workflow["status"] not in (DRAFT, WITHDRAWN):
            raise ConflictError("只有草稿或已撤回的公告可以归档")
        cancelled = self._cancel_scheduled_jobs(announcement_id)
        reset_versions = self._reset_inflight_versions(announcement_id)
        now = self._now_storage()
        self.connection.execute(
            "UPDATE announcement_workflow SET status=?,archived_at=?,updated_at=? WHERE announcement_id=?",
            (ARCHIVED, now, now, announcement_id),
        )
        detail = "归档"
        if cancelled:
            detail += f"；取消 {cancelled} 个定时任务"
        if reset_versions:
            detail += f"；{reset_versions} 个在途版本退回草稿"
        self._add_event(
            announcement_id, "archived", principal, version_no=workflow["current_version_no"], detail=detail
        )
        return self.detail(principal, announcement_id)

    def reschedule(self, principal: Principal, announcement_id: int, publish_at: str) -> dict:
        principal.require("announcements.write")
        workflow = self._get_workflow(announcement_id)
        version = self._get_version(announcement_id, workflow["current_version_no"])
        if version["publish_job_id"] is None:
            raise ConflictError("当前版本没有可调整的待执行定时发布计划")
        job = self.connection.execute(
            "SELECT * FROM background_jobs WHERE id=?", (version["publish_job_id"],)
        ).fetchone()
        if (
            version["review_status"] != V_APPROVED
            or version["published_at"] is not None
            or job is None
            or job["status"] not in ("pending", "running")
        ):
            raise ConflictError("当前版本没有可调整的待执行定时发布计划")
        target = self._parse_publish_at(publish_at)
        if target <= self.clock.now():
            raise ValidationError("新的发布时间必须晚于当前时间；已过期请重新送审")
        new_at = to_storage(target)
        now = self._now_storage()
        self.connection.execute(
            "UPDATE background_jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('pending','running')",
            (now, job["id"]),
        )
        new_job_id = self._enqueue_publish_job(announcement_id, version, new_at)
        self.connection.execute(
            "UPDATE announcement_versions SET publish_at=?,publish_job_id=? WHERE id=?",
            (new_at, new_job_id, version["id"]),
        )
        self.connection.execute(
            "UPDATE announcement_workflow SET updated_at=? WHERE announcement_id=?",
            (now, announcement_id),
        )
        self._add_event(
            announcement_id, "rescheduled", principal, version_no=version["version_no"],
            detail=f"发布时间调整为 {new_at}（新任务 {new_job_id}，旧任务 {job['id']} 取消）",
        )
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 定时发布执行

    def run_due_publish(self, worker: str, *, lease_seconds: int = 60) -> dict | None:
        """领取并执行一个到期的公告发布任务。调用方必须在外层即时事务中调用。

        - 任务持久化在 background_jobs 中，进程重启后扫描到期任务即可补发；
        - 条件 UPDATE 使版本只生效一次、任务只完成一次；
        - 租约过期的 running 任务可被重新领取，执行逻辑幂等（重复回调安全）；
        - 公告在到期前被撤回/归档时跳过发布并给出确定结果。
        """
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        stale = to_storage(now_dt - timedelta(seconds=lease_seconds))
        self.connection.execute(
            "UPDATE background_jobs SET status='pending',locked_at=NULL,locked_by=NULL,updated_at=? "
            "WHERE status='running' AND locked_at<? AND job_type=?",
            (now, stale, PUBLISH_JOB_TYPE),
        )
        job_row = self.connection.execute(
            "SELECT * FROM background_jobs WHERE job_type=? AND status='pending' AND available_at<=? "
            "ORDER BY available_at,id LIMIT 1",
            (PUBLISH_JOB_TYPE, now),
        ).fetchone()
        if job_row is None:
            return None
        job = dict(job_row)
        claimed = self.connection.execute(
            "UPDATE background_jobs SET status='running',attempts=attempts+1,locked_at=?,locked_by=?,updated_at=? "
            "WHERE id=? AND status='pending'",
            (now, worker, now, job["id"]),
        )
        if claimed.rowcount != 1:
            return None

        payload = json.loads(job["payload_json"])
        announcement_id = int(payload["announcement_id"])
        version_no = int(payload["version_no"])
        workflow = self._get_workflow(announcement_id)
        version = self._get_version(announcement_id, version_no)

        if version["published_at"] is not None:
            outcome = "already_published"
        elif workflow["status"] in (WITHDRAWN, ARCHIVED):
            outcome = "skipped_withdrawn"
            self._add_event(
                announcement_id, "publish_skipped", None, version_no=version_no,
                detail=f"计划发布时公告状态为 {workflow['status']}，本次发布不生效",
                created_at=now,
            )
        else:
            became_effective = self._publish_version(announcement_id, version_no, published_at=now)
            outcome = "published" if became_effective else "already_published"
            if became_effective:
                self._add_event(
                    announcement_id, "scheduled_published", None, version_no=version_no,
                    detail=f"定时任务 {job['id']} 到期发布生效", created_at=now,
                )

        self.connection.execute(
            "UPDATE background_jobs SET status='completed',result_json=?,locked_at=NULL,locked_by=NULL,updated_at=? "
            "WHERE id=? AND status='running' AND locked_by=?",
            (json.dumps({"outcome": outcome, "announcement_id": announcement_id, "version_no": version_no},
                        ensure_ascii=False, sort_keys=True), now, job["id"], worker),
        )
        return {
            "job_id": job["id"],
            "announcement_id": announcement_id,
            "version_no": version_no,
            "outcome": outcome,
        }

    # ------------------------------------------------------------------ 管理查询

    def detail(self, principal: Principal | None, announcement_id: int) -> dict[str, Any]:
        if principal is not None:
            self._require_any_permission(principal)
        announcement = self.connection.execute(
            "SELECT * FROM announcements WHERE id=?", (announcement_id,)
        ).fetchone()
        if announcement is None:
            raise NotFoundError("公告不存在")
        workflow = self._get_workflow(announcement_id)
        versions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM announcement_versions WHERE announcement_id=? ORDER BY version_no",
                (announcement_id,),
            ).fetchall()
        ]
        for item in versions:
            item["is_pinned"] = bool(item["is_pinned"])
        events = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM announcement_events WHERE announcement_id=? ORDER BY id",
                (announcement_id,),
            ).fetchall()
        ]
        current = next((item for item in versions if item["version_no"] == workflow["current_version_no"]), None)
        effective = next((item for item in versions if item["version_no"] == workflow["effective_version_no"]), None)
        return {
            "id": announcement_id,
            "title": announcement["title"],
            "status": workflow["status"],
            "current_version_no": workflow["current_version_no"],
            "effective_version_no": workflow["effective_version_no"],
            "published_at": workflow["published_at"],
            "withdraw_reason": workflow["withdraw_reason"],
            "withdrawn_at": workflow["withdrawn_at"],
            "archived_at": workflow["archived_at"],
            "updated_at": workflow["updated_at"],
            "current_version": current,
            "effective_version": effective,
            "versions": versions,
            "events": events,
        }

    def list_management(
        self, principal: Principal, *, status: str | None, category: str | None,
        review_status: str | None, limit: int, offset: int
    ) -> dict:
        self._require_any_permission(principal)
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("w.status=?")
            params.append(status)
        if category:
            conditions.append("cv.category=?")
            params.append(category)
        if review_status:
            conditions.append("cv.review_status=?")
            params.append(review_status)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM announcement_workflow w "
            "JOIN announcement_versions cv ON cv.announcement_id=w.announcement_id AND cv.version_no=w.current_version_no"
            + where,
            tuple(params),
        ).fetchone()[0])
        params.extend([limit, offset])
        rows = self.connection.execute(
            "SELECT w.announcement_id AS id,w.status,w.current_version_no,w.effective_version_no,"
            "w.published_at,w.withdraw_reason,w.withdrawn_at,w.archived_at,w.updated_at,"
            "cv.title,cv.content,cv.category,cv.is_pinned,cv.review_status,cv.publish_type,cv.publish_at AS scheduled_publish_at,"
            "ev.title AS effective_title,ev.is_pinned AS effective_is_pinned "
            "FROM announcement_workflow w "
            "JOIN announcement_versions cv ON cv.announcement_id=w.announcement_id AND cv.version_no=w.current_version_no "
            "LEFT JOIN announcement_versions ev ON ev.announcement_id=w.announcement_id AND ev.version_no=w.effective_version_no"
            + where + " ORDER BY w.announcement_id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["is_pinned"] = bool(item["is_pinned"])
            item["effective_is_pinned"] = (
                bool(item["effective_is_pinned"]) if item["effective_is_pinned"] is not None else None
            )
            data.append(item)
        return {"total": total, "data": data}

    # ------------------------------------------------------------------ 公众阅读

    def public_list(self, *, category: str | None, limit: int, offset: int) -> dict:
        """只列出已发布公告，内容、置顶、时间全部取生效版本。"""
        conditions = ["w.status='published'"]
        params: list[Any] = []
        if category:
            conditions.append("v.category=?")
            params.append(category)
        where = " WHERE " + " AND ".join(conditions)
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM announcement_workflow w "
            "JOIN announcement_versions v ON v.announcement_id=w.announcement_id AND v.version_no=w.effective_version_no"
            + where,
            tuple(params),
        ).fetchone()[0])
        params.extend([limit, offset])
        rows = self.connection.execute(
            "SELECT w.announcement_id AS id,v.version_no,v.title,v.content,v.category,v.is_pinned,"
            "v.published_at,w.published_at AS first_published_at,"
            "(SELECT COUNT(*) FROM announcement_versions x "
            "WHERE x.announcement_id=w.announcement_id AND x.published_at IS NOT NULL) AS published_versions "
            "FROM announcement_workflow w "
            "JOIN announcement_versions v ON v.announcement_id=w.announcement_id AND v.version_no=w.effective_version_no"
            + where + " ORDER BY v.is_pinned DESC,v.published_at DESC,w.announcement_id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["is_pinned"] = bool(item["is_pinned"])
            item["has_correction"] = item["published_versions"] > 1
            data.append(item)
        return {"total": total, "data": data}

    def public_detail(self, announcement_id: int) -> dict[str, Any]:
        """阅读接口以生效版本为准；撤回后展示生效版本快照与撤回原因。"""
        workflow = self._get_workflow(announcement_id)
        if workflow["effective_version_no"] is None or workflow["status"] == ARCHIVED:
            raise NotFoundError("公告不存在或尚未发布")
        version = self._get_version(announcement_id, workflow["effective_version_no"])
        published_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM announcement_versions WHERE announcement_id=? AND published_at IS NOT NULL",
            (announcement_id,),
        ).fetchone()[0])
        return {
            "id": announcement_id,
            "status": workflow["status"],
            "version_no": version["version_no"],
            "title": version["title"],
            "content": version["content"],
            "category": version["category"],
            "is_pinned": bool(version["is_pinned"]),
            "published_at": version["published_at"],
            "withdraw_reason": workflow["withdraw_reason"],
            "withdrawn_at": workflow["withdrawn_at"],
            "published_versions": published_count,
        }

    def public_version(self, announcement_id: int, version_no: int) -> dict[str, Any]:
        """旧版本链接永远返回当时内容，并标明是否已被后续更正取代。"""
        workflow = self._get_workflow(announcement_id)
        version = self._get_version(announcement_id, version_no)
        if version["published_at"] is None:
            raise NotFoundError("该版本不存在或从未发布")
        later_row = self.connection.execute(
            "SELECT MIN(version_no) AS m FROM announcement_versions "
            "WHERE announcement_id=? AND version_no>? AND published_at IS NOT NULL",
            (announcement_id, version_no),
        ).fetchone()
        return {
            "id": announcement_id,
            "version_no": version_no,
            "title": version["title"],
            "content": version["content"],
            "category": version["category"],
            "is_pinned": bool(version["is_pinned"]),
            "published_at": version["published_at"],
            "is_effective": workflow["effective_version_no"] == version_no,
            "superseded_by": later_row["m"],
            "announcement_status": workflow["status"],
            "withdraw_reason": workflow["withdraw_reason"] if workflow["status"] == WITHDRAWN else None,
        }

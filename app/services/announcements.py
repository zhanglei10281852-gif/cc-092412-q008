from __future__ import annotations

import json
import sqlite3

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditContext, AuditService
from app.services.jobs import JobService

PUBLISH_JOB_TYPE = "announcement.publish"

# 各状态下是否允许拟稿人继续编辑版本内容
EDITABLE_STATUSES = {"draft", "rejected"}
# 公众可见的版本状态
VISIBLE_VERSION_STATUSES = {"published", "superseded", "withdrawn", "archived"}


class AnnouncementService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.jobs = JobService(connection, self.clock)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 内部辅助

    def _header(self, announcement_id: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM announcements WHERE id=?", (announcement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("公告不存在")
        return dict(row)

    def _version(self, announcement_id: int, version_no: int) -> dict:
        row = self.connection.execute(
            "SELECT * FROM announcement_versions WHERE announcement_id=? AND version_no=?",
            (announcement_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("公告版本不存在")
        return dict(row)

    def _current_version(self, header: dict) -> dict:
        return self._version(header["id"], header["current_version_no"])

    def _event(self, announcement_id: int, event: str, principal: Principal | None,
               version_no: int | None, detail: dict | None = None) -> None:
        self.connection.execute(
            "INSERT INTO announcement_events(announcement_id,version_no,event,actor_user_id,actor_name,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                announcement_id,
                version_no,
                event,
                principal.user_id if principal else None,
                principal.display_name if principal else "系统",
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                to_storage(self.clock.now()),
            ),
        )

    def _audit(self, principal: Principal, action: str, announcement_id: int, *,
               before: dict | None = None, after: dict | None = None, metadata: dict | None = None) -> None:
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=action,
            resource_type="announcement",
            resource_id=announcement_id,
            before=before,
            after=after,
            metadata=metadata,
        )

    def _denied(self, principal: Principal, action: str, announcement_id: int,
                reason: str, message: str) -> PermissionDeniedError:
        """构造越权异常；审计留痕由全局异常处理器在业务事务回滚后独立写入。"""
        return PermissionDeniedError(
            message,
            context={
                "audit_denied": {
                    "actor_user_id": principal.user_id,
                    "actor_name": principal.display_name,
                    "action": action,
                    "resource_id": announcement_id,
                    "reason": reason,
                }
            },
        )

    # ------------------------------------------------------------------ 拟稿与修订

    def create_draft(self, principal: Principal, data: dict) -> dict:
        principal.require("announcements.write")
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO announcements(status,current_version_no,effective_version_no,created_at,updated_at) "
            "VALUES('draft',1,NULL,?,?)",
            (now, now),
        )
        announcement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO announcement_versions(announcement_id,version_no,title,content,category,is_pinned,"
            "status,author_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,'draft',?,?,?)",
            (announcement_id, 1, data["title"], data["content"], data["category"],
             1 if data["is_pinned"] else 0, principal.user_id, now, now),
        )
        self._event(announcement_id, "draft_created", principal, 1)
        self._audit(principal, "announcement.draft.create", announcement_id,
                    after={"version_no": 1, "title": data["title"]})
        return self.detail(principal, announcement_id)

    def revise(self, principal: Principal, announcement_id: int, data: dict) -> dict:
        principal.require("announcements.write")
        header = self._header(announcement_id)
        current = self._current_version(header)
        correction_in_progress = (
            header["status"] == "published"
            and header["effective_version_no"] is not None
            and current["version_no"] != header["effective_version_no"]
        )
        if header["status"] not in EDITABLE_STATUSES and not correction_in_progress:
            raise ConflictError(f"当前状态（{header['status']}）不能修订公告")
        if current["status"] not in EDITABLE_STATUSES:
            raise ConflictError("当前版本正在送审，不能修订")
        if current["author_user_id"] != principal.user_id and "*" not in principal.permissions:
            raise PermissionDeniedError("只能修订本人拟写的公告")
        now = to_storage(self.clock.now())
        new_no = header["current_version_no"] + 1
        self.connection.execute(
            "INSERT INTO announcement_versions(announcement_id,version_no,title,content,category,is_pinned,"
            "status,author_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,'draft',?,?,?)",
            (announcement_id, new_no, data["title"], data["content"], data["category"],
             1 if data["is_pinned"] else 0, principal.user_id, now, now),
        )
        # 驳回意见只针对被驳回的旧稿，新版本从头流转；更正期间主状态保持 published
        self.connection.execute(
            "UPDATE announcements SET status=CASE WHEN effective_version_no IS NULL THEN 'draft' ELSE status END,"
            "current_version_no=?,updated_at=? WHERE id=?",
            (new_no, now, announcement_id),
        )
        self._event(announcement_id, "revised", principal, new_no,
                    {"from_version_no": current["version_no"],
                     "effective_version_no": header["effective_version_no"]})
        self._audit(principal, "announcement.revise", announcement_id,
                    before={"status": header["status"], "version_no": current["version_no"]},
                    after={"status": "published" if correction_in_progress else "draft",
                           "version_no": new_no})
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 送审与审阅

    def submit(self, principal: Principal, announcement_id: int, scheduled_for: object | None) -> dict:
        principal.require("announcements.write")
        header = self._header(announcement_id)
        version = self._current_version(header)
        # 首次流转（无生效版本）时主状态为 draft/rejected；更正流程中主状态仍为 published
        if version["status"] not in EDITABLE_STATUSES:
            raise ConflictError("只有草稿或被驳回的版本可以送审")
        if header["status"] not in EDITABLE_STATUSES | {"published"}:
            raise ConflictError("当前公告状态不能送审")
        if version["author_user_id"] != principal.user_id and "*" not in principal.permissions:
            raise PermissionDeniedError("只能提交本人拟写的公告")
        now_dt = self.clock.now()
        now = to_storage(now_dt)
        scheduled_text = to_storage(scheduled_for) if scheduled_for is not None else None
        if scheduled_text is not None and scheduled_text <= now:
            raise ValidationError("定时发布时间必须晚于当前时间")
        job_id = None
        if scheduled_text is not None:
            job_id = self._schedule_publish_job(announcement_id, version["version_no"], scheduled_for)
        # 送审期间版本一律为 submitted；是否定时只记录在 scheduled_for 上，审阅通过后才允许发布
        target = "submitted"
        self.connection.execute(
            "UPDATE announcement_versions SET status=?,submitted_at=?,scheduled_for=?,"
            "publish_job_id=?,reviewer_user_id=NULL,reviewed_at=NULL,review_opinion=NULL,updated_at=? "
            "WHERE id=?",
            (target, now, scheduled_text, job_id, now, version["id"]),
        )
        if header["effective_version_no"] is None:
            # 首次发布：主状态进入待审阅，公众尚不可见
            self.connection.execute(
                "UPDATE announcements SET status=?,updated_at=? WHERE id=?",
                (target, now, announcement_id),
            )
        # 更正流程：主状态保持 published，旧生效版本继续对公众可见，直到新版本发布
        self._event(announcement_id, "submitted", principal, version["version_no"],
                    {"scheduled_for": scheduled_text, "has_effective": header["effective_version_no"] is not None})
        self._audit(principal, "announcement.submit", announcement_id,
                    before={"status": header["status"]},
                    after={"status": target, "scheduled_for": scheduled_text})
        return self.detail(principal, announcement_id)

    def review(self, principal: Principal, announcement_id: int, approved: bool, opinion: str) -> dict:
        principal.require("announcements.review")
        header = self._header(announcement_id)
        version = self._current_version(header)
        in_correction = header["effective_version_no"] is not None
        if version["status"] not in {"submitted", "scheduled"}:
            raise ConflictError("该版本不在待审阅状态")
        if not in_correction and header["status"] not in {"submitted", "scheduled"}:
            raise ConflictError("只有待审阅或待发布的公告可以审阅")
        # 撰写人与审阅人不能是同一账号
        if version["author_user_id"] == principal.user_id:
            raise self._denied(principal, "announcement.review", announcement_id,
                              "reviewer_is_author", "撰写人与审阅人不能是同一账号")
        now = to_storage(self.clock.now())
        opinion = opinion.strip()
        if approved:
            due_now = not version["scheduled_for"] or version["scheduled_for"] <= now
            if not due_now:
                # 审阅通过且发布时刻未到：版本进入待发布，等待后台任务
                job_id = self._ensure_pending_job(version)
                self.connection.execute(
                    "UPDATE announcement_versions SET status='scheduled',reviewer_user_id=?,reviewed_at=?,"
                    "review_opinion=?,publish_job_id=?,updated_at=? WHERE id=?",
                    (principal.user_id, now, opinion, job_id, now, version["id"]),
                )
                if not in_correction:
                    self.connection.execute(
                        "UPDATE announcements SET status='scheduled',updated_at=? WHERE id=?",
                        (now, announcement_id),
                    )
                # 更正待发布期间主状态保持 published，旧版本继续公开
                target_status = "scheduled" if not in_correction else "published"
            else:
                # 立即发布（无定时或计划时间已过）；更正通过时旧生效版本置为 superseded
                if in_correction:
                    self.connection.execute(
                        "UPDATE announcement_versions SET status='superseded',updated_at=? "
                        "WHERE announcement_id=? AND version_no=? AND status='published'",
                        (now, announcement_id, header["effective_version_no"]),
                    )
                self.connection.execute(
                    "UPDATE announcement_versions SET status='published',reviewer_user_id=?,reviewed_at=?,"
                    "review_opinion=?,published_at=COALESCE(published_at,?),updated_at=? WHERE id=?",
                    (principal.user_id, now, opinion, now, now, version["id"]),
                )
                self.connection.execute(
                    "UPDATE announcements SET status='published',effective_version_no=?,updated_at=? WHERE id=?",
                    (version["version_no"], now, announcement_id),
                )
                # 若计划时间已过且数据库中仍有待执行任务，条件更新会幂等处理，此处无需额外取消
                target_status = "published"
            self._event(announcement_id, "approved", principal, version["version_no"],
                        {"scheduled_for": version["scheduled_for"], "in_correction": in_correction})
        else:
            self.connection.execute(
                "UPDATE announcement_versions SET status='rejected',reviewer_user_id=?,reviewed_at=?,"
                "review_opinion=?,scheduled_for=NULL,publish_job_id=NULL,updated_at=? WHERE id=?",
                (principal.user_id, now, opinion, now, version["id"]),
            )
            if version["publish_job_id"] is not None:
                self._cancel_publish_job(int(version["publish_job_id"]))
            if in_correction:
                # 更正流程被驳回：旧生效版本继续公开，主状态保持 published
                target_status = "published"
            else:
                self.connection.execute(
                    "UPDATE announcements SET status='rejected',updated_at=? WHERE id=?",
                    (now, announcement_id),
                )
                target_status = "rejected"
            self._event(announcement_id, "rejected", principal, version["version_no"],
                        {"opinion": opinion, "kept_effective": header["effective_version_no"]})
        self._audit(
            principal,
            "announcement.review" if approved else "announcement.reject",
            announcement_id,
            before={"status": header["status"]},
            after={"status": target_status},
            metadata={"opinion": opinion, "author_user_id": version["author_user_id"]},
        )
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 定时发布回调

    def _schedule_publish_job(self, announcement_id: int, version_no: int, scheduled_for: object) -> int:
        """每次送审生成独立任务（键含送审时刻与随机后缀）；驳回即取消旧任务，重复回调由条件更新幂等兜底。"""
        import secrets
        stamp = self.clock.now().strftime("%Y%m%d%H%M%S%f")
        key = f"announcement-publish:{announcement_id}:v{version_no}:{stamp}:{secrets.token_hex(4)}"
        job = self.jobs.enqueue(
            PUBLISH_JOB_TYPE,
            key,
            {"announcement_id": announcement_id, "version_no": version_no},
            run_at=scheduled_for,
        )
        if job["status"] in {"cancelled", "failed", "completed"}:
            raise ConflictError("该版本的发布计划已终结，请修订后重新送审")
        return int(job["id"])

    def _cancel_publish_job(self, job_id: int) -> None:
        self.connection.execute(
            "UPDATE background_jobs SET status='cancelled',updated_at=? "
            "WHERE id=? AND status IN ('pending','running')",
            (to_storage(self.clock.now()), job_id),
        )

    def _ensure_pending_job(self, version: dict) -> int:
        """审阅通过时确认仍有可执行的发布任务；送审任务若已失效则按版本重新入队。"""
        if version["publish_job_id"] is not None:
            job = self.connection.execute(
                "SELECT status FROM background_jobs WHERE id=?", (int(version["publish_job_id"]),)
            ).fetchone()
            if job is not None and dict(job)["status"] in {"pending", "running"}:
                return int(version["publish_job_id"])
        scheduled_for = version["scheduled_for"]
        return self._schedule_publish_job(
            version["announcement_id"], version["version_no"], from_storage(scheduled_for)
        )

    def publish_due(self) -> int:
        """发布所有已到时间且审阅通过的版本。幂等：条件更新保证每个版本最多生效一次。"""
        now = to_storage(self.clock.now())
        rows = self.connection.execute(
            "SELECT v.announcement_id AS announcement_id, v.version_no AS version_no "
            "FROM announcement_versions v JOIN announcements a ON a.id=v.announcement_id "
            "WHERE v.status='scheduled' AND v.version_no=a.current_version_no "
            "AND a.status IN ('scheduled','published') AND v.scheduled_for IS NOT NULL AND v.scheduled_for<=?",
            (now,),
        ).fetchall()
        published = 0
        for row in rows:
            published += self._publish_one(int(row["announcement_id"]), int(row["version_no"]))
        return published

    def _publish_one(self, announcement_id: int, version_no: int) -> int:
        """条件更新保证恰好发布一次：版本仍为 scheduled 时才生效。"""
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE announcement_versions SET status='published',published_at=COALESCE(published_at,?),updated_at=? "
            "WHERE announcement_id=? AND version_no=? AND status='scheduled'",
            (now, now, announcement_id, version_no),
        )
        if cursor.rowcount != 1:
            # 版本已驳回、已撤回或已被更新版本取代：重复回调不做任何事
            return 0
        # 更正生效：上一版公开内容转为被取代状态，但记录仍可按版本号读取
        self.connection.execute(
            "UPDATE announcement_versions SET status='superseded',updated_at=? "
            "WHERE announcement_id=? AND status='published' AND version_no<>?",
            (now, announcement_id, version_no),
        )
        header = self.connection.execute(
            "UPDATE announcements SET status='published',effective_version_no=?,updated_at=? "
            "WHERE id=? AND status IN ('scheduled','published')",
            (version_no, now, announcement_id),
        )
        if header.rowcount != 1:
            raise ConflictError("公告主状态不允许发布，发布中止")
        self._event(announcement_id, "scheduled_publish", None, version_no)
        self.audit.record(
            AuditContext(None, "系统定时任务"),
            action="announcement.scheduled_publish",
            resource_type="announcement",
            resource_id=announcement_id,
            after={"status": "published", "version_no": version_no},
        )
        return 1

    def dispatch_job(self, job: dict) -> bool:
        """供后台任务执行器调用。返回 True 表示任务已处理（无论是否真正发布），False 表示类型不归本服务管。"""
        if job["job_type"] != PUBLISH_JOB_TYPE:
            return False
        payload = json.loads(job["payload_json"])
        announcement_id = int(payload["announcement_id"])
        version_no = int(payload.get("version_no", 0))
        header_row = self.connection.execute(
            "SELECT status,current_version_no FROM announcements WHERE id=?", (announcement_id,)
        ).fetchone()
        if header_row is None:
            return True  # 公告已删除，任务确认完成，避免反复重试
        header = dict(header_row)
        version_row = self.connection.execute(
            "SELECT status FROM announcement_versions WHERE announcement_id=? AND version_no=?",
            (announcement_id, version_no),
        ).fetchone()
        if version_row is None:
            return True
        if dict(version_row)["status"] != "scheduled":
            return True  # 重复回调：版本已发布或已终止，幂等确认成功
        if header["status"] not in {"scheduled", "published"} or header["current_version_no"] != version_no:
            # 公告已被撤回或归档：确定性终止残留的待发布版本，避免发布与撤回竞争产生悬挂状态
            self.connection.execute(
                "UPDATE announcement_versions SET status='rejected',scheduled_for=NULL,publish_job_id=NULL,updated_at=? "
                "WHERE announcement_id=? AND version_no=? AND status='scheduled'",
                (to_storage(self.clock.now()), announcement_id, version_no),
            )
            self._event(announcement_id, "scheduled_publish_aborted", None, version_no,
                        {"header_status": header["status"]})
            return True
        self._publish_one(announcement_id, version_no)
        return True

    # ------------------------------------------------------------------ 撤回与归档

    def withdraw(self, principal: Principal, announcement_id: int, reason: str) -> dict:
        principal.require("announcements.publish")
        header = self._header(announcement_id)
        if header["status"] != "published":
            raise ConflictError("只有已发布的公告可以撤回")
        if header["effective_version_no"] is None:
            raise ConflictError("公告缺少生效版本，不能撤回")
        now = to_storage(self.clock.now())
        version = self._version(announcement_id, header["effective_version_no"])
        # 若存在在途更正版本（草稿/待审阅/待发布），撤回将其一并终止并取消定时任务
        current = self._current_version(header)
        if current["version_no"] != version["version_no"] and current["status"] in {
            "draft", "submitted", "scheduled", "rejected",
        }:
            if current["publish_job_id"] is not None:
                self._cancel_publish_job(int(current["publish_job_id"]))
            self.connection.execute(
                "UPDATE announcement_versions SET status='rejected',scheduled_for=NULL,publish_job_id=NULL,updated_at=? "
                "WHERE id=? AND status IN ('draft','submitted','scheduled')",
                (now, current["id"]),
            )
        cursor = self.connection.execute(
            "UPDATE announcement_versions SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,"
            "withdrawn_reason=?,updated_at=? WHERE id=? AND status='published'",
            (now, principal.user_id, reason.strip(), now, version["id"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("生效版本已不是发布状态，撤回未执行")
        header_cursor = self.connection.execute(
            "UPDATE announcements SET status='withdrawn',updated_at=? WHERE id=? AND status='published'",
            (now, announcement_id),
        )
        if header_cursor.rowcount != 1:
            raise ConflictError("公告已被其他操作改变状态，撤回未执行")
        self._event(announcement_id, "withdrawn", principal, version["version_no"],
                    {"reason": reason.strip(), "aborted_version_no": current["version_no"]
                     if current["version_no"] != version["version_no"] else None})
        self._audit(principal, "announcement.withdraw", announcement_id,
                    before={"status": "published", "version_no": version["version_no"]},
                    after={"status": "withdrawn"}, metadata={"reason": reason.strip()})
        return self.detail(principal, announcement_id)

    def archive(self, principal: Principal, announcement_id: int) -> dict:
        principal.require("announcements.publish")
        header = self._header(announcement_id)
        if header["status"] not in {"withdrawn", "published"}:
            raise ConflictError("只有已发布或已撤回的公告可以归档")
        now = to_storage(self.clock.now())
        # 归档时终止可能存在的在途更正版本及其定时任务
        current = self._current_version(header)
        if current["version_no"] != header["effective_version_no"] and current["status"] in {
            "draft", "submitted", "scheduled", "rejected",
        }:
            if current["publish_job_id"] is not None:
                self._cancel_publish_job(int(current["publish_job_id"]))
            self.connection.execute(
                "UPDATE announcement_versions SET status='archived',scheduled_for=NULL,publish_job_id=NULL,"
                "archived_at=COALESCE(archived_at,?),updated_at=? WHERE id=?",
                (now, now, current["id"]),
            )
        self.connection.execute(
            "UPDATE announcement_versions SET status='archived',archived_at=COALESCE(archived_at,?),updated_at=? "
            "WHERE announcement_id=? AND status IN ('published','withdrawn','superseded')",
            (now, now, announcement_id),
        )
        self.connection.execute(
            "UPDATE announcements SET status='archived',updated_at=? WHERE id=? AND status IN ('published','withdrawn')",
            (now, announcement_id),
        )
        self._event(announcement_id, "archived", principal, header["effective_version_no"])
        self._audit(principal, "announcement.archive", announcement_id,
                    before={"status": header["status"]}, after={"status": "archived"})
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 更正（已公开内容的修订）

    def correct(self, principal: Principal, announcement_id: int, data: dict) -> dict:
        """发起更正：旧生效版本保持公开，新版本以草稿重新走送审；新版本通过前旧链接照旧返回。"""
        principal.require("announcements.write")
        header = self._header(announcement_id)
        if header["status"] != "published" or header["effective_version_no"] is None:
            raise ConflictError("只有已发布的公告可以发起更正")
        current = self._current_version(header)
        if current["version_no"] != header["effective_version_no"] or current["status"] != "published":
            raise ConflictError("该公告已有正在流转的更正版本，请在当前草稿上修订")
        if current["author_user_id"] != principal.user_id and "*" not in principal.permissions:
            raise PermissionDeniedError("只能更正本人拟写的公告")
        now = to_storage(self.clock.now())
        new_no = header["current_version_no"] + 1
        self.connection.execute(
            "INSERT INTO announcement_versions(announcement_id,version_no,title,content,category,is_pinned,"
            "status,author_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,'draft',?,?,?)",
            (announcement_id, new_no, data["title"], data["content"], data["category"],
             1 if data["is_pinned"] else 0, principal.user_id, now, now),
        )
        # 主状态保持 published：公众仍读到旧生效版本，直到更正版本审阅通过并发布
        self.connection.execute(
            "UPDATE announcements SET current_version_no=?,updated_at=? WHERE id=? AND status='published'",
            (new_no, now, announcement_id),
        )
        self._event(announcement_id, "correction_started", principal, new_no,
                    {"effective_version_no": header["effective_version_no"]})
        self._audit(principal, "announcement.correction.start", announcement_id,
                    before={"current_version_no": current["version_no"]},
                    after={"status": "published", "draft_version_no": new_no})
        return self.detail(principal, announcement_id)

    # ------------------------------------------------------------------ 查询

    def _can_view_internal(self, principal: Principal | None, header: dict, version: dict) -> bool:
        if principal is None:
            return False
        if "*" in principal.permissions or principal.can("announcements.review"):
            return True
        if principal.can("announcements.write") and version["author_user_id"] == principal.user_id:
            return True
        return False

    def detail(self, principal: Principal | None, announcement_id: int) -> dict:
        header = self._header(announcement_id)
        version = self._current_version(header)
        if not self._can_view_internal(principal, header, version):
            # 非内部视角只暴露已发布公告（返回当前生效版本）
            if header["status"] != "published" or header["effective_version_no"] is None:
                raise NotFoundError("公告不存在")
            version = self._version(announcement_id, header["effective_version_no"])
        effective = None
        if header["effective_version_no"] is not None:
            effective = self._version(announcement_id, header["effective_version_no"])
        versions = self.connection.execute(
            "SELECT * FROM announcement_versions WHERE announcement_id=? ORDER BY version_no",
            (announcement_id,),
        ).fetchall()
        visible_versions = []
        for row in versions:
            item = dict(row)
            is_current = item["version_no"] == header["current_version_no"]
            internal = self._can_view_internal(principal, header, item)
            # 公众可见版本、当前版本、以及被驳回的稿件（作者需要看到驳回意见）
            if item["status"] in VISIBLE_VERSION_STATUSES or is_current or (internal and item["status"] == "rejected"):
                visible_versions.append(self._version_view(item, principal, header))
        return {
            "id": announcement_id,
            "status": header["status"],
            "current_version_no": header["current_version_no"],
            "effective_version_no": header["effective_version_no"],
            "created_at": header["created_at"],
            "updated_at": header["updated_at"],
            "version": self._version_view(version, principal, header),
            "effective_version": self._version_view(effective, principal, header) if effective else None,
            "versions": visible_versions,
        }

    def _version_view(self, version: dict | None, principal: Principal | None, header: dict) -> dict | None:
        if version is None:
            return None
        view = {
            "version_no": version["version_no"],
            "title": version["title"],
            "content": version["content"],
            "category": version["category"],
            "is_pinned": bool(version["is_pinned"]),
            "status": version["status"],
            "created_at": version["created_at"],
            "published_at": version["published_at"],
        }
        if version["status"] == "superseded":
            view["correction_notice"] = "该版本已被后续更正取代，最新内容请查看当前生效版本"
        if version["status"] == "withdrawn":
            view["withdrawn_reason"] = version["withdrawn_reason"]
            view["withdrawn_at"] = version["withdrawn_at"]
        if self._can_view_internal(principal, header, version):
            view.update({
                "author_user_id": version["author_user_id"],
                "submitted_at": version["submitted_at"],
                "reviewer_user_id": version["reviewer_user_id"],
                "reviewed_at": version["reviewed_at"],
                "review_opinion": version["review_opinion"],
                "scheduled_for": version["scheduled_for"],
                "archived_at": version["archived_at"],
            })
        return view

    def events(self, principal: Principal, announcement_id: int) -> list[dict]:
        header = self._header(announcement_id)
        current = self._current_version(header)
        if not (
            principal.can("announcements.review")
            or principal.can("announcements.publish")
            or (principal.can("announcements.write") and current["author_user_id"] == principal.user_id)
        ):
            raise PermissionDeniedError("无权查看该公告的流转记录")
        rows = self.connection.execute(
            "SELECT * FROM announcement_events WHERE announcement_id=? ORDER BY id",
            (announcement_id,),
        ).fetchall()
        return [
            {
                "version_no": row["version_no"],
                "event": row["event"],
                "actor_user_id": row["actor_user_id"],
                "actor_name": row["actor_name"],
                "detail": json.loads(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_internal(self, principal: Principal, *, statuses: list[str] | None,
                      category: str | None, limit: int, offset: int) -> dict:
        if not (principal.can("announcements.write") or principal.can("announcements.review")
                or principal.can("announcements.publish")):
            raise PermissionDeniedError("缺少公告管理权限")
        conditions = []
        params: list = []
        # 仅有拟稿权限的账号只能看到自己拟写的公告；审阅/发布权限可见全部
        if not (principal.can("announcements.review") or principal.can("announcements.publish")):
            conditions.append("v.author_user_id=?")
            params.append(principal.user_id)
        if statuses:
            allowed = {"draft", "submitted", "rejected", "scheduled", "published", "withdrawn", "archived"}
            filtered = [item for item in statuses if item in allowed]
            if filtered:
                conditions.append("a.status IN ({})".format(",".join("?" for _ in filtered)))
                params.extend(filtered)
        if category:
            conditions.append("v.category=?")
            params.append(category)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        total = int(self.connection.execute(
            f"SELECT COUNT(*) FROM announcements a JOIN announcement_versions v "
            f"ON v.announcement_id=a.id AND v.version_no=a.current_version_no{where}",
            tuple(params),
        ).fetchone()[0])
        rows = self.connection.execute(
            f"SELECT a.*,v.title,v.content,v.category,v.is_pinned,v.scheduled_for,v.author_user_id "
            f"FROM announcements a JOIN announcement_versions v "
            f"ON v.announcement_id=a.id AND v.version_no=a.current_version_no{where} "
            f"ORDER BY a.updated_at DESC,a.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "data": [self._list_row(dict(row)) for row in rows]}

    def list_public(self, *, category: str | None, limit: int, offset: int) -> dict:
        """公开列表一律以生效版本（effective_version_no）的内容与置顶顺序为准。"""
        conditions = ["a.status='published'"]
        params: list = []
        if category:
            conditions.append("e.category=?")
            params.append(category)
        where = " WHERE " + " AND ".join(conditions)
        total = int(self.connection.execute(
            "SELECT COUNT(*) FROM announcements a "
            "JOIN announcement_versions e ON e.announcement_id=a.id AND e.version_no=a.effective_version_no"
            + where,
            tuple(params),
        ).fetchone()[0])
        rows = self.connection.execute(
            "SELECT a.id,a.effective_version_no AS version_no,a.updated_at,"
            "e.title,e.content,e.category,e.is_pinned,e.published_at "
            "FROM announcements a JOIN announcement_versions e "
            "ON e.announcement_id=a.id AND e.version_no=a.effective_version_no"
            + where +
            " ORDER BY e.is_pinned DESC,e.published_at DESC,a.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "data": [self._list_row(dict(row), public_view=True) for row in rows]}

    @staticmethod
    def _list_row(row: dict, *, public_view: bool = False) -> dict:
        view = {
            "id": row["id"],
            "version_no": row.get("version_no") or row.get("effective_version_no"),
            "title": row["title"],
            "category": row["category"],
            "is_pinned": bool(row["is_pinned"]),
            "published_at": row.get("published_at"),
        }
        if not public_view:
            view["status"] = row["status"]
            view["scheduled_for"] = row.get("scheduled_for")
            view["author_user_id"] = row.get("author_user_id")
        return view

    def public_version(self, announcement_id: int, version_no: int | None = None) -> dict:
        """公众阅读接口。默认返回生效版本；指定版本号时返回当时版本并标明更正情况。"""
        header = self._header(announcement_id)
        if header["status"] not in {"published", "withdrawn", "archived"} or header["effective_version_no"] is None:
            raise NotFoundError("公告不存在")
        target_no = version_no if version_no is not None else header["effective_version_no"]
        version = self._version(announcement_id, target_no)
        if version["status"] not in VISIBLE_VERSION_STATUSES:
            raise NotFoundError("该版本不存在或尚未公开")
        view = self._version_view(version, None, header)
        assert view is not None
        view["id"] = announcement_id
        view["effective_version_no"] = header["effective_version_no"]
        view["has_later_correction"] = target_no < header["effective_version_no"]
        if version["status"] == "withdrawn":
            view["notice"] = "该公告已撤回"
        elif version["status"] == "archived":
            view["notice"] = "该公告已归档"
        # 公告级状态优先：撤回原因与归档提示以生效版本为准，阅读任何历史版本都一致展示
        if header["status"] == "withdrawn":
            effective = self._version(announcement_id, header["effective_version_no"])
            view["notice"] = "该公告已撤回"
            view["withdrawn_reason"] = effective["withdrawn_reason"]
            view["withdrawn_at"] = effective["withdrawn_at"]
        elif header["status"] == "archived":
            view["notice"] = "该公告已归档"
        return view

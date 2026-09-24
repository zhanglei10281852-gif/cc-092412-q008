from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, transaction
from app.services.announcements import AnnouncementService


DRAFT = {"title": "秋收补贴政策", "content": "第一版正文", "category": "政策", "is_pinned": True}


@pytest.fixture()
def accounts(client, admin):
    # 经办员（拟稿）与审阅员（审阅/撤回/归档）必须是不同账号
    client.post("/api/users", headers=admin["headers"],
                json={"username": "clerk.ann", "password": "Clerk!23456", "display_name": "撰稿员", "role_codes": ["clerk"]})
    client.post("/api/users", headers=admin["headers"],
                json={"username": "reviewer.ann", "password": "Review!23456", "display_name": "审阅员", "role_codes": ["reviewer"]})
    clerk_token = client.post("/api/auth/login", json={"username": "clerk.ann", "password": "Clerk!23456", "client_label": "t"}).json()["token"]
    reviewer_token = client.post("/api/auth/login", json={"username": "reviewer.ann", "password": "Review!23456", "client_label": "t"}).json()["token"]
    return {
        "clerk": {"Authorization": f"Bearer {clerk_token}"},
        "reviewer": {"Authorization": f"Bearer {reviewer_token}"},
        "admin": admin["headers"],
    }


def create_and_publish(client, headers_clerk, headers_reviewer, draft=DRAFT):
    created = client.post("/api/announcements", headers=headers_clerk, json=draft)
    assert created.status_code == 201, created.text
    announcement_id = created.json()["id"]
    assert client.post(f"/api/announcements/{announcement_id}/submit", headers=headers_clerk, json={}).status_code == 200
    review = client.post(f"/api/announcements/{announcement_id}/review", headers=headers_reviewer,
                         json={"approved": True, "opinion": "同意发布"})
    assert review.status_code == 200, review.text
    return announcement_id


# ---------------------------------------------------------------- 草稿到发布的完整流转

def test_draft_submit_approve_flow(client, accounts):
    created = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT)
    announcement_id = created.json()["id"]
    # 草稿在公开接口不可见
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    # 无权限账号不能送审/审阅
    assert client.post(f"/api/announcements/{announcement_id}/submit",
                       headers=accounts["reviewer"], json={}).status_code == 403
    assert client.post(f"/api/announcements/{announcement_id}/submit",
                       headers=accounts["clerk"], json={}).status_code == 200
    # 通过后即时公开
    review = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                         json={"approved": True, "opinion": "同意"})
    assert review.status_code == 200
    public = client.get(f"/announcements/{announcement_id}")
    assert public.status_code == 200
    body = public.json()
    assert body["title"] == "秋收补贴政策"
    assert body["version_no"] == 1
    assert body["effective_version_no"] == 1


def test_reject_returns_to_draft_and_keeps_opinion(client, accounts):
    created = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT)
    announcement_id = created.json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    review = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                         json={"approved": False, "opinion": "标题有错字"})
    assert review.status_code == 200
    assert review.json()["status"] == "rejected"
    # 驳回后公众仍不可见
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    # 作者能看到驳回意见
    detail = client.get(f"/api/announcements/{announcement_id}", headers=accounts["clerk"]).json()
    assert detail["version"]["review_opinion"] == "标题有错字"
    # 修改后产生新版本，可以重新送审
    revised = client.put(f"/api/announcements/{announcement_id}/revise", headers=accounts["clerk"],
                         json={**DRAFT, "title": "秋收补贴政策（修订）"})
    assert revised.status_code == 200
    assert revised.json()["current_version_no"] == 2
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    approved = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                           json={"approved": True, "opinion": "通过"})
    assert approved.status_code == 200
    assert client.get(f"/announcements/{announcement_id}").json()["version_no"] == 2


# ---------------------------------------------------------------- 职责分离

def test_author_cannot_review_own_announcement(client, accounts):
    created = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT)
    announcement_id = created.json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    # 撰稿人账号没有审阅权限
    forbidden = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["clerk"],
                            json={"approved": True})
    assert forbidden.status_code == 403
    # 即使管理员（拥有全部权限）是作者本人也不能自审
    admin_draft = client.post("/api/announcements", headers=accounts["admin"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{admin_draft}/submit", headers=accounts["admin"], json={})
    self_review = client.post(f"/api/announcements/{admin_draft}/review", headers=accounts["admin"],
                              json={"approved": True})
    assert self_review.status_code == 403
    # 越权尝试被审计留痕
    audit_rows = client.get("/api/audit?action=announcement.review&outcome=denied", headers=accounts["admin"]).json()
    assert audit_rows["total"] >= 1


def test_reviewer_cannot_author(client, accounts):
    forbidden = client.post("/api/announcements", headers=accounts["reviewer"], json=DRAFT)
    assert forbidden.status_code == 403


# ---------------------------------------------------------------- 定时发布

def test_scheduled_publish_persists_and_fires_once(client, accounts):
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    created = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()
    announcement_id = created["id"]
    submit = client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                         json={"scheduled_for": future})
    assert submit.status_code == 200
    approved = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                           json={"approved": True})
    assert approved.json()["status"] == "scheduled"
    # 未到时间不公开
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    # 模拟进程重启后扫描：没有到点任务
    assert _run_due() == 0
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    # 把计划时间改到过去，模拟重启后补发到点任务
    with transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE announcement_versions SET scheduled_for=? WHERE announcement_id=?",
            ("2026-01-01T00:00:00+00:00", announcement_id),
        )
        connection.execute(
            "UPDATE background_jobs SET available_at=? WHERE job_type='announcement.publish'",
            ("2026-01-01T00:00:00+00:00",),
        )
    assert _run_due() == 1
    body = client.get(f"/announcements/{announcement_id}").json()
    assert body["version_no"] == 1
    # 重复执行回调/扫描，只能发布一次
    assert _run_due() == 0
    with transaction(immediate=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions WHERE announcement_id=? AND status='published'",
            (announcement_id,),
        ).fetchone()[0]
    assert count == 1


def test_overdue_schedule_approved_late_publishes_immediately(client, accounts):
    # 送审一个很近的未来时间，审阅时已过：应立即发布
    near = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    created = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()
    announcement_id = created["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": near})
    import time
    time.sleep(1.2)
    review = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                         json={"approved": True})
    assert review.json()["status"] == "published"
    assert client.get(f"/announcements/{announcement_id}").status_code == 200


def test_scheduled_past_time_rejected_at_submit(client, accounts):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    response = client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                           json={"scheduled_for": past})
    assert response.status_code == 422


def test_reject_cancels_scheduled_job(client, accounts):
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": False, "opinion": "不准发"})
    # 到点扫描不会把驳回稿发布出来
    with transaction(immediate=True) as connection:
        connection.execute("UPDATE background_jobs SET available_at='2026-01-01T00:00:00+00:00'")
    assert _run_due() == 0
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    with transaction(immediate=True) as connection:
        statuses = [r[0] for r in connection.execute(
            "SELECT status FROM background_jobs WHERE job_type='announcement.publish'")]
    assert statuses == ["cancelled"]


# ---------------------------------------------------------------- 撤回与归档

def test_withdraw_records_reason_and_removes_from_public_list(client, accounts):
    announcement_id = create_and_publish(client, accounts["clerk"], accounts["reviewer"])
    # 撰稿人无权撤回
    assert client.post(f"/api/announcements/{announcement_id}/withdraw", headers=accounts["clerk"],
                       json={"reason": "内容有误"}).status_code == 403
    withdrawn = client.post(f"/api/announcements/{announcement_id}/withdraw", headers=accounts["reviewer"],
                            json={"reason": "政策依据调整"})
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"
    # 公开列表不再返回
    assert client.get("/announcements").json()["data"] == []
    # 旧链接仍可访问，但标明已撤回及撤回原因（以生效版本为准）
    legacy = client.get(f"/announcements/{announcement_id}")
    assert legacy.status_code == 200
    assert legacy.json()["notice"] == "该公告已撤回"
    assert legacy.json()["withdrawn_reason"] == "政策依据调整"
    # 再次撤回得到确定的冲突结果
    again = client.post(f"/api/announcements/{announcement_id}/withdraw", headers=accounts["reviewer"],
                        json={"reason": "重复撤回"})
    assert again.status_code == 409
    # 归档
    archived = client.post(f"/api/announcements/{announcement_id}/archive", headers=accounts["reviewer"])
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"


def test_concurrent_withdraw_and_publish_has_single_outcome(client, accounts):
    from app.core.security import Principal
    from app.services.scheduler import JobDispatcher

    reviewer = client_principal(accounts["reviewer"])

    # 交错一：定时发布先提交，随后撤回 -> 撤回得到确定冲突，公告保持已发布
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": True})
    # 待发布状态不能撤回（必须先公开发布）
    early = client.post(f"/api/announcements/{announcement_id}/withdraw", headers=accounts["reviewer"],
                        json={"reason": "提前撤回"})
    assert early.status_code == 409
    with transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE announcement_versions SET scheduled_for='2026-01-01T00:00:00+00:00' WHERE announcement_id=?",
            (announcement_id,),
        )
        service = AnnouncementService(connection, FrozenClock(datetime(2026, 9, 24, tzinfo=UTC)))
        assert service.publish_due() == 1
        # 发布完成后，迟到的重复回调不再产生第二次发布
        job = connection.execute(
            "SELECT * FROM background_jobs WHERE job_type='announcement.publish'"
        ).fetchone()
    with transaction(immediate=True) as connection:
        assert AnnouncementService(connection).dispatch_job(dict(job)) is True
        published_count = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions WHERE announcement_id=? AND status='published'",
            (announcement_id,),
        ).fetchone()[0]
    assert published_count == 1

    # 交错二：更正版本定时发布与撤回竞争
    # v1 已公开，v2 更正稿审阅通过后等待定时发布
    second = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{second}/submit", headers=accounts["clerk"], json={})
    client.post(f"/api/announcements/{second}/review", headers=accounts["reviewer"], json={"approved": True})
    client.post(f"/api/announcements/{second}/correct", headers=accounts["clerk"],
                json={**DRAFT, "content": "更正版"})
    client.post(f"/api/announcements/{second}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{second}/review", headers=accounts["reviewer"], json={"approved": True})
    assert client.get(f"/api/announcements/{second}", headers=accounts["reviewer"]).json()["status"] == "published"

    # 顺序 2a：撤回先提交 -> v1 撤回、v2 终止；迟到的发布回调不得复活公告
    withdrawn = client.post(f"/api/announcements/{second}/withdraw", headers=accounts["reviewer"],
                            json={"reason": "政策废止"})
    assert withdrawn.status_code == 200
    # 模拟撤回提交瞬间已被领取、稍后才执行到的发布回调（真实竞争窗口）
    fake_job = {"job_type": "announcement.publish",
                "payload_json": f'{{"announcement_id": {second}, "version_no": 2}}'}
    with transaction(immediate=True) as connection:
        assert AnnouncementService(connection).dispatch_job(fake_job) is True
    with transaction(immediate=True) as connection:
        state = connection.execute("SELECT status FROM announcements WHERE id=?", (second,)).fetchone()[0]
        v2_state = connection.execute(
            "SELECT status FROM announcement_versions WHERE announcement_id=? AND version_no=2", (second,)
        ).fetchone()[0]
    assert state == "withdrawn"
    assert v2_state == "rejected"
    listed = [item["id"] for item in client.get("/announcements").json()["data"]]
    assert second not in listed
    assert client.get(f"/announcements/{second}").json()["notice"] == "该公告已撤回"

    # 顺序 2b：发布回调先提交 -> v2 生效，撤回作用于新生效版本，同样得到唯一确定结果
    third = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{third}/submit", headers=accounts["clerk"], json={})
    client.post(f"/api/announcements/{third}/review", headers=accounts["reviewer"], json={"approved": True})
    client.post(f"/api/announcements/{third}/correct", headers=accounts["clerk"],
                json={**DRAFT, "content": "更正版"})
    client.post(f"/api/announcements/{third}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{third}/review", headers=accounts["reviewer"], json={"approved": True})
    with transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE announcement_versions SET scheduled_for='2026-01-01T00:00:00+00:00' "
            "WHERE announcement_id=? AND version_no=2", (third,)
        )
        fired = AnnouncementService(connection, FrozenClock(datetime(2026, 9, 24, tzinfo=UTC))).publish_due()
    assert fired == 1
    assert client.get(f"/announcements/{third}").json()["content"] == "更正版"
    final_withdraw = client.post(f"/api/announcements/{third}/withdraw", headers=accounts["reviewer"],
                                 json={"reason": "废止新版"})
    assert final_withdraw.status_code == 200
    assert final_withdraw.json()["status"] == "withdrawn"
    legacy = client.get(f"/announcements/{third}").json()
    assert legacy["withdrawn_reason"] == "废止新版"
    listed_final = [item["id"] for item in client.get("/announcements").json()["data"]]
    assert third not in listed_final


# ---------------------------------------------------------------- 更正与历史版本

def test_correction_creates_new_version_and_keeps_old_link(client, accounts):
    announcement_id = create_and_publish(client, accounts["clerk"], accounts["reviewer"])
    # 更正期间旧版本仍正常公开
    corrected = client.post(f"/api/announcements/{announcement_id}/correct", headers=accounts["clerk"],
                            json={**DRAFT, "content": "第二版更正正文", "is_pinned": False})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["current_version_no"] == 2
    # 公众读到的仍是第 1 版（生效版本），置顶也仍以旧版为准
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["version_no"] == 1
    assert public["is_pinned"] is True
    # 更正稿送审通过（即时发布）
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": True, "opinion": "更正通过"})
    # 默认阅读接口（公开）返回新生效版本
    latest = client.get(f"/announcements/{announcement_id}").json()
    assert latest["version_no"] == 2
    assert latest["content"] == "第二版更正正文"
    assert latest["is_pinned"] is False
    assert latest["has_later_correction"] is False
    # 旧链接（显式版本号）继续返回当时版本，并标明已有后续更正
    old = client.get(f"/announcements/{announcement_id}", params={"version_no": 1}).json()
    assert old["content"] == "第一版正文"
    assert old["is_pinned"] is True
    assert old["has_later_correction"] is True
    assert "更正" in old["correction_notice"]
    # 置顶顺序以新生效版本为准：不再置顶
    rows = client.get("/announcements").json()["data"]
    assert rows[0]["is_pinned"] is False


def test_correction_rejected_keeps_old_version_effective(client, accounts):
    announcement_id = create_and_publish(client, accounts["clerk"], accounts["reviewer"])
    client.post(f"/api/announcements/{announcement_id}/correct", headers=accounts["clerk"],
                json={**DRAFT, "content": "错误的更正"})
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    rejected = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                           json={"approved": False, "opinion": "更正依据不足"})
    # 主状态仍为已发布，公众继续读到旧版
    assert rejected.json()["status"] == "published"
    body = client.get(f"/announcements/{announcement_id}").json()
    assert body["version_no"] == 1
    assert body["content"] == "第一版正文"


def test_withdraw_aborts_in_flight_correction(client, accounts):
    announcement_id = create_and_publish(client, accounts["clerk"], accounts["reviewer"])
    client.post(f"/api/announcements/{announcement_id}/correct", headers=accounts["clerk"],
                json={**DRAFT, "content": "更正稿"})
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"], json={})
    # 更正稿在审阅中时撤回：旧版撤回，更正稿终止
    withdrawn = client.post(f"/api/announcements/{announcement_id}/withdraw", headers=accounts["reviewer"],
                            json={"reason": "政策废止"})
    assert withdrawn.status_code == 200
    versions = {v["version_no"]: v["status"] for v in withdrawn.json()["versions"]}
    assert versions[1] == "withdrawn"
    assert versions[2] == "rejected"
    # 公开列表移除，但旧链接标明已撤回及原因
    assert client.get("/announcements").json()["data"] == []
    legacy = client.get(f"/announcements/{announcement_id}")
    assert legacy.status_code == 200
    assert legacy.json()["notice"] == "该公告已撤回"
    assert legacy.json()["withdrawn_reason"] == "政策废止"


# ---------------------------------------------------------------- 其他

def test_events_are_recorded(client, accounts):
    announcement_id = create_and_publish(client, accounts["clerk"], accounts["reviewer"])
    events = client.get(f"/api/announcements/{announcement_id}/events", headers=accounts["reviewer"])
    assert events.status_code == 200
    names = [item["event"] for item in events.json()["data"]]
    assert names == ["draft_created", "submitted", "approved"]


def test_public_list_filters_category_and_hides_drafts(client, accounts):
    first = create_and_publish(client, accounts["clerk"], accounts["reviewer"], DRAFT)
    second_draft = {**DRAFT, "title": "停水通知", "category": "通知", "is_pinned": False}
    create_and_publish(client, accounts["clerk"], accounts["reviewer"], second_draft)
    # 再建一个草稿，不应出现
    client.post("/api/announcements", headers=accounts["clerk"], json={**DRAFT, "title": "未发布草稿"})
    policy_only = client.get("/announcements", params={"category": "政策"}).json()
    assert [item["id"] for item in policy_only["data"]] == [first]
    assert client.get("/announcements").json()["total"] == 2


def test_clerk_internal_list_only_owns_drafts(client, admin, accounts):
    # 撰稿员的草稿
    own = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    # 管理员的草稿（撰稿员不应看到）
    other = client.post("/api/announcements", headers=admin["headers"], json={**DRAFT, "title": "他人草稿"}).json()["id"]
    rows = client.get("/api/announcements", headers=accounts["clerk"]).json()
    ids = {item["id"] for item in rows["data"]}
    assert own in ids
    assert other not in ids
    # 直接访问他人草稿得到 404
    assert client.get(f"/api/announcements/{other}", headers=accounts["clerk"]).status_code == 404
    # 审阅员可以看到全部
    review_rows = client.get("/api/announcements", headers=accounts["reviewer"]).json()
    assert {own, other} <= {item["id"] for item in review_rows["data"]}


# ---------------------------------------------------------------- 调度器与重启语义

def test_scheduler_thread_publishes_due_job_once(client, accounts):
    """真实后台线程轮询：到点自动发布；重复轮询不会二次发布。"""
    from app.services.scheduler import AnnouncementScheduler
    future = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": True})
    scheduler = AnnouncementScheduler(poll_interval_seconds=0.05, lease_seconds=30)
    scheduler.start()
    try:
        deadline = datetime.now(UTC) + timedelta(seconds=8)
        while datetime.now(UTC) < deadline:
            if client.get(f"/announcements/{announcement_id}").status_code == 200:
                break
            __import__("time").sleep(0.1)
        body = client.get(f"/announcements/{announcement_id}")
        assert body.status_code == 200
        assert body.json()["version_no"] == 1
        # 再等待两个轮询周期，确认没有重复发布

    finally:
        scheduler.stop()
    with transaction(immediate=True) as connection:
        published = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions WHERE announcement_id=? AND status='published'",
            (announcement_id,),
        ).fetchone()[0]
        completed = connection.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE job_type='announcement.publish' AND status='completed'"
        ).fetchone()[0]
    assert published == 1
    assert completed == 1


def test_due_job_survives_restart_and_claims_once(client, accounts):
    """任务持久化在 SQLite：模拟进程重启（全新服务实例）后仍能领取并发布，且只执行一次。"""
    from app.services.scheduler import AnnouncementScheduler
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": True})
    with transaction(immediate=True) as connection:
        connection.execute("UPDATE background_jobs SET available_at='2026-01-01T00:00:00+00:00'")
    # 重启后的新调度器实例：领取 -> 发布 -> 完成
    scheduler = AnnouncementScheduler(poll_interval_seconds=0.05)
    assert scheduler.run_due_once() == 1
    assert client.get(f"/announcements/{announcement_id}").json()["version_no"] == 1
    # 再次"重启扫描"没有可执行任务
    assert AnnouncementScheduler().run_due_once() == 0


def test_resubmit_after_rejection_with_schedule_uses_fresh_job(client, accounts):
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    announcement_id = client.post("/api/announcements", headers=accounts["clerk"], json=DRAFT).json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                json={"scheduled_for": future})
    client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                json={"approved": False, "opinion": "修改"})
    client.put(f"/api/announcements/{announcement_id}/revise", headers=accounts["clerk"], json=DRAFT)
    # 重新定时送审：旧任务已取消，新任务正常创建
    again = client.post(f"/api/announcements/{announcement_id}/submit", headers=accounts["clerk"],
                        json={"scheduled_for": future})
    assert again.status_code == 200
    approved = client.post(f"/api/announcements/{announcement_id}/review", headers=accounts["reviewer"],
                           json={"approved": True})
    assert approved.json()["status"] == "scheduled"
    with transaction(immediate=True) as connection:
        statuses = [r[0] for r in connection.execute(
            "SELECT status FROM background_jobs WHERE job_type='announcement.publish' ORDER BY id")]
    assert statuses[0] == "cancelled"
    assert statuses[-1] == "pending"


# ---------------------------------------------------------------- 辅助

def _run_due() -> int:
    """在独立事务中同步执行所有到点的公告发布任务。"""
    from app.services.scheduler import AnnouncementScheduler
    with transaction(immediate=True) as connection:
        return AnnouncementService(connection, FrozenClock(datetime(2026, 9, 24, tzinfo=UTC))).publish_due()


def client_principal(headers: dict):
    token = headers["Authorization"].removeprefix("Bearer ")
    from app.services.auth import AuthService
    return AuthService(get_connection()).principal(token)

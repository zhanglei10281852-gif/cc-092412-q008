from __future__ import annotations

import threading

from app.database import get_connection, transaction
from app.services.announcement_scheduler import AnnouncementPublishScheduler
from app.services.announcements import AnnouncementService


def _make_user(client, admin, username, permissions):
    role_code = f"role.{username}"
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": role_code, "name": username, "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Clerk!23456", "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Clerk!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _publish_immediate(client, author_h, reviewer_h, draft):
    created = client.post("/api/announcements", headers=author_h, json=draft)
    assert created.status_code == 201, created.text
    announcement_id = created.json()["id"]
    submitted = client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    assert submitted.status_code == 200, submitted.text
    reviewed = client.post(
        f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True}
    )
    assert reviewed.status_code == 200, reviewed.text
    return announcement_id


def test_draft_review_reject_revise_then_publish(client, admin):
    author_h = _make_user(client, admin, "author.one", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.one", ["announcements.review"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "政策初稿", "content": "有错字", "category": "政策", "is_pinned": False},
    )
    announcement_id = created.json()["id"]

    # 草稿与送审中都不对公众可见
    assert client.get("/announcements").json()["data"] == []
    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    assert client.get(f"/announcements/{announcement_id}").status_code == 404

    # 驳回
    rejected = client.post(
        f"/api/announcements/{announcement_id}/review", headers=reviewer_h,
        json={"passed": False, "opinion": "存在错字"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "draft"
    assert rejected.json()["current_version"]["review_status"] == "rejected"

    # 撰写人修改后重新送审、通过
    revised = client.patch(
        f"/api/announcements/{announcement_id}", headers=author_h, json={"content": "已订正"}
    )
    assert revised.status_code == 200
    assert revised.json()["current_version"]["review_status"] == "draft"
    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    approved = client.post(f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True})
    assert approved.json()["status"] == "published"
    assert approved.json()["effective_version_no"] == 1

    detail = client.get(f"/announcements/{announcement_id}")
    assert detail.status_code == 200
    assert detail.json()["content"] == "已订正"


def test_author_cannot_review_own_announcement(client, admin):
    author_h = _make_user(client, admin, "author.two", ["announcements.write", "announcements.review"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "自审公告", "content": "正文", "category": "公告"},
    )
    announcement_id = created.json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    # 即使同一账号同时拥有两种权限，也不能审批自己撰写的版本
    denied = client.post(
        f"/api/announcements/{announcement_id}/review", headers=author_h, json={"passed": True}
    )
    assert denied.status_code == 403
    # 被拒后版本仍处于待审阅
    body = client.get(f"/api/announcements/{announcement_id}", headers=author_h).json()
    assert body["current_version"]["review_status"] == "submitted"


def test_writer_without_review_permission_cannot_approve(client, admin):
    author_h = _make_user(client, admin, "author.three", ["announcements.write"])
    other_h = _make_user(client, admin, "author.four", ["announcements.write"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "越权审批", "content": "正文", "category": "通知"},
    )
    announcement_id = created.json()["id"]
    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    denied = client.post(
        f"/api/announcements/{announcement_id}/review", headers=other_h, json={"passed": True}
    )
    assert denied.status_code == 403


def test_published_content_change_creates_version_and_old_link_kept(client, admin):
    author_h = _make_user(client, admin, "author.five", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.five", ["announcements.review"])

    announcement_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "补贴通知", "content": "第一版内容", "category": "通知", "is_pinned": True},
    )

    # 已发布版本不能直接修改
    bad = client.patch(
        f"/api/announcements/{announcement_id}", headers=author_h, json={"content": "直接覆盖"}
    )
    assert bad.status_code == 409

    # 必须新建更正版本，重新走审阅
    correction = client.post(
        f"/api/announcements/{announcement_id}/corrections", headers=author_h,
        json={"title": "补贴通知（更正）", "content": "第二版内容", "category": "通知", "is_pinned": False},
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["current_version_no"] == 2
    # v2 审阅期间，公众读到的仍是 v1，且仍置顶
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["version_no"] == 1
    assert public["content"] == "第一版内容"

    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})
    client.post(f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True})

    # 阅读接口现在以生效版本 v2 为准（内容与置顶均切换）
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["version_no"] == 2
    assert public["content"] == "第二版内容"
    assert public["is_pinned"] is False

    # 旧链接继续返回当时版本，并标明后续更正
    v1 = client.get(f"/announcements/{announcement_id}/versions/1").json()
    assert v1["content"] == "第一版内容"
    assert v1["is_effective"] is False
    assert v1["superseded_by"] == 2
    v2 = client.get(f"/announcements/{announcement_id}/versions/2").json()
    assert v2["is_effective"] is True
    assert v2["superseded_by"] is None

    # 未发布版本不能通过版本链接访问
    draft_next = client.post(
        f"/api/announcements/{announcement_id}/corrections", headers=author_h,
        json={"title": "x", "content": "v3", "category": "通知"},
    )
    assert draft_next.status_code == 201
    assert client.get(f"/announcements/{announcement_id}/versions/3").status_code == 404


def test_pinning_order_follows_effective_version(client, admin):
    author_h = _make_user(client, admin, "author.six", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.six", ["announcements.review"])

    pinned_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "置顶公告", "content": "A", "category": "公告", "is_pinned": True},
    )
    normal_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "普通公告", "content": "B", "category": "公告", "is_pinned": False},
    )
    rows = client.get("/announcements").json()["data"]
    assert [row["id"] for row in rows] == [pinned_id, normal_id]

    # 置顶公告发布取消置顶的更正版本后，排序立即让位
    client.post(
        f"/api/announcements/{pinned_id}/corrections", headers=author_h,
        json={"title": "置顶公告", "content": "A2", "category": "公告", "is_pinned": False},
    )
    client.post(f"/api/announcements/{pinned_id}/submit", headers=author_h, json={})
    client.post(f"/api/announcements/{pinned_id}/review", headers=reviewer_h, json={"passed": True})
    rows = client.get("/announcements").json()["data"]
    assert [row["id"] for row in rows] == [normal_id, pinned_id]
    assert all(row["is_pinned"] is False for row in rows)


def test_withdraw_shows_reason_and_then_archive(client, admin):
    author_h = _make_user(client, admin, "author.seven", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.seven", ["announcements.review"])

    announcement_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "将撤回", "content": "正文", "category": "公示"},
    )
    withdrawn = client.post(
        f"/api/announcements/{announcement_id}/withdraw", headers=author_h,
        json={"reason": "政策依据调整"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"

    # 撤回后不在公开列表，但阅读接口仍返回生效版本快照并标明撤回原因
    assert client.get("/announcements").json()["data"] == []
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["status"] == "withdrawn"
    assert public["withdraw_reason"] == "政策依据调整"
    assert public["content"] == "正文"
    old = client.get(f"/announcements/{announcement_id}/versions/1").json()
    assert old["announcement_status"] == "withdrawn"
    assert old["withdraw_reason"] == "政策依据调整"

    archived = client.post(f"/api/announcements/{announcement_id}/archive", headers=author_h)
    assert archived.status_code == 200
    assert client.get(f"/announcements/{announcement_id}").status_code == 404
    # 归档不抹掉历史版本留痕
    assert client.get(f"/announcements/{announcement_id}/versions/1").status_code == 200


def test_scheduled_publish_rejects_past_time(client, admin):
    author_h = _make_user(client, admin, "author.eight", ["announcements.write"])
    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "定时", "content": "正文", "category": "通知"},
    )
    announcement_id = created.json()["id"]
    stale = client.post(
        f"/api/announcements/{announcement_id}/submit", headers=author_h,
        json={"publish_type": "scheduled", "publish_at": "2000-01-01T00:00:00+00:00"},
    )
    assert stale.status_code == 422


def test_scheduled_publish_runs_after_restart_exactly_once(client, admin):
    author_h = _make_user(client, admin, "author.nine", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.nine", ["announcements.review"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "定时发布", "content": "准时见", "category": "通知"},
    )
    announcement_id = created.json()["id"]
    client.post(
        f"/api/announcements/{announcement_id}/submit", headers=author_h,
        json={"publish_type": "scheduled", "publish_at": "2099-01-01T00:00:00+00:00"},
    )
    reviewed = client.post(
        f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True}
    )
    assert reviewed.json()["status"] == "approved"
    assert client.get("/announcements").json()["data"] == []

    # 未到期时调度器空跑
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 0

    # 模拟进程停机跨越计划时间后重启：任务持久化在库中，到期即被扫描执行
    connection = get_connection()
    connection.execute(
        "UPDATE background_jobs SET available_at=datetime('now','-1 minute') "
        "WHERE job_type='announcement.publish'"
    )
    connection.commit()

    processed = client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"]
    assert processed == 1
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["content"] == "准时见"
    assert public["version_no"] == 1

    # 重复回调（任务被异常重置）必须幂等：只发布一次、published_at 不被覆盖
    first_published_at = public["published_at"]
    connection.execute(
        "UPDATE background_jobs SET status='pending',locked_at=NULL,locked_by=NULL,available_at=datetime('now') "
        "WHERE job_type='announcement.publish'"
    )
    connection.commit()
    again = client.post("/api/announcements/run-due", headers=admin["headers"]).json()
    assert again["processed"] == 1
    versions = client.get(
        f"/api/announcements/{announcement_id}", headers=reviewer_h
    ).json()["versions"]
    assert [v for v in versions if v["published_at"] is not None] and len(
        [v for v in versions if v["published_at"] is not None]
    ) == 1
    assert client.get(f"/announcements/{announcement_id}").json()["published_at"] == first_published_at
    # 没有遗留待执行任务
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 0


def test_withdraw_before_schedule_cancels_and_late_callback_is_skipped(client, admin):
    author_h = _make_user(client, admin, "author.ten", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.ten", ["announcements.review"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "先发后撤", "content": "正文", "category": "通知"},
    )
    announcement_id = created.json()["id"]
    client.post(
        f"/api/announcements/{announcement_id}/submit", headers=author_h,
        json={"publish_type": "scheduled", "publish_at": "2099-01-01T00:00:00+00:00"},
    )
    client.post(f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True})

    withdrawn = client.post(
        f"/api/announcements/{announcement_id}/withdraw", headers=author_h, json={"reason": "暂缓"}
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"

    # 旧任务被取消；即使出现过期重复回调，发布也必须跳过，撤回结论不被推翻
    connection = get_connection()
    connection.execute(
        "UPDATE background_jobs SET status='pending',locked_at=NULL,locked_by=NULL,available_at=datetime('now') "
        "WHERE job_type='announcement.publish'"
    )
    connection.commit()
    processed = client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"]
    assert processed == 1
    body = client.get(f"/api/announcements/{announcement_id}", headers=reviewer_h).json()
    assert body["status"] == "withdrawn"
    assert body["effective_version_no"] is None
    detail = client.get(f"/api/announcements/{announcement_id}", headers=author_h).json()
    assert any(event["event"] == "publish_skipped" for event in detail["events"])


def test_concurrent_withdraw_and_publish_has_determined_result(client, admin):
    author_h = _make_user(client, admin, "author.eleven", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.eleven", ["announcements.review"])
    author_user_id = client.get("/api/auth/me", headers=author_h).json()["user_id"]

    announcement_id = None
    for index in range(8):
        created = client.post(
            "/api/announcements", headers=author_h,
            json={"title": f"并发公告{index}", "content": "正文", "category": "通知"},
        )
        aid = created.json()["id"]
        client.post(
            f"/api/announcements/{aid}/submit", headers=author_h,
            json={"publish_type": "scheduled", "publish_at": "2099-01-01T00:00:00+00:00"},
        )
        client.post(f"/api/announcements/{aid}/review", headers=reviewer_h, json={"passed": True})
        connection = get_connection()
        connection.execute(
            "UPDATE background_jobs SET available_at=datetime('now') WHERE job_type='announcement.publish'"
        )
        connection.commit()

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def publish_side() -> None:
            try:
                barrier.wait()
                AnnouncementPublishScheduler().run_once()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def withdraw_side() -> None:
            try:
                barrier.wait()
                with transaction(immediate=True) as connection_tx:
                    from app.core.security import Principal
                    principal = Principal(
                        user_id=author_user_id, username="author.eleven", display_name="author.eleven",
                        department_id=None, permissions=frozenset({"announcements.write"}), session_id=0,
                    )
                    AnnouncementService(connection_tx).withdraw(principal, aid, "并发撤回")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=publish_side)
        t2 = threading.Thread(target=withdraw_side)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        assert not errors, errors

        # 无论谁先提交，最终状态必须自洽：撤回成立；v1 要么按计划生效过（撤回生效版本），
        # 要么未生效（撤回先于发布），两种结果都只能出现一次。
        detail = client.get(f"/api/announcements/{aid}", headers=reviewer_h).json()
        assert detail["status"] == "withdrawn"
        assert detail["withdraw_reason"] == "并发撤回"
        published = [v for v in detail["versions"] if v["published_at"] is not None]
        assert len(published) <= 1
        announcement_id = aid

    assert announcement_id is not None


def test_scheduled_correction_on_published_keeps_v1_until_due(client, admin):
    author_h = _make_user(client, admin, "author.sc", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.sc", ["announcements.review"])

    announcement_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "生效公告", "content": "v1 正文", "category": "政策"},
    )
    client.post(
        f"/api/announcements/{announcement_id}/corrections", headers=author_h,
        json={"title": "生效公告", "content": "v2 正文", "category": "政策"},
    )
    client.post(
        f"/api/announcements/{announcement_id}/submit", headers=author_h,
        json={"publish_type": "scheduled", "publish_at": "2099-06-01T00:00:00+00:00"},
    )
    approved = client.post(
        f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True}
    )
    # v2 定时等待期间，公告整体仍为 published，公众继续读 v1
    assert approved.json()["status"] == "published"
    assert approved.json()["effective_version_no"] == 1
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["version_no"] == 1
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 0

    # 到期后（模拟重启跨越计划时间）切换为 v2，且只切换一次
    connection = get_connection()
    connection.execute(
        "UPDATE background_jobs SET available_at=datetime('now','-1 minute') "
        "WHERE job_type='announcement.publish'"
    )
    connection.commit()
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 1
    public = client.get(f"/announcements/{announcement_id}").json()
    assert public["version_no"] == 2
    assert public["content"] == "v2 正文"
    v1 = client.get(f"/announcements/{announcement_id}/versions/1").json()
    assert v1["is_effective"] is False
    assert v1["superseded_by"] == 2


def test_reschedule_requires_future_time_and_uses_new_job(client, admin):
    author_h = _make_user(client, admin, "author.rs", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.rs", ["announcements.review"])

    created = client.post(
        "/api/announcements", headers=author_h,
        json={"title": "改期", "content": "正文", "category": "通知"},
    )
    announcement_id = created.json()["id"]
    client.post(
        f"/api/announcements/{announcement_id}/submit", headers=author_h,
        json={"publish_type": "scheduled", "publish_at": "2099-03-01T00:00:00+00:00"},
    )
    client.post(f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True})

    past = client.post(
        f"/api/announcements/{announcement_id}/reschedule", headers=author_h,
        json={"publish_at": "2000-01-01T00:00:00+00:00"},
    )
    assert past.status_code == 422

    moved = client.post(
        f"/api/announcements/{announcement_id}/reschedule", headers=author_h,
        json={"publish_at": "2099-09-01T00:00:00+00:00"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["current_version"]["publish_at"].startswith("2099-09-01")
    # 旧任务被取消，到期扫描没有可执行任务
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 0
    connection = get_connection()
    jobs = connection.execute(
        "SELECT status FROM background_jobs WHERE job_type='announcement.publish' ORDER BY id"
    ).fetchall()
    assert [row["status"] for row in jobs] == ["cancelled", "pending"]

    # 新任务到期后仍只发布一次
    connection.execute(
        "UPDATE background_jobs SET available_at=datetime('now','-1 minute') WHERE status='pending'"
    )
    connection.commit()
    assert client.post("/api/announcements/run-due", headers=admin["headers"]).json()["processed"] == 1
    assert client.get(f"/announcements/{announcement_id}").json()["version_no"] == 1


def test_withdraw_terminates_inflight_submitted_correction(client, admin):
    author_h = _make_user(client, admin, "author.wi", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.wi", ["announcements.review"])

    announcement_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "在途撤回", "content": "v1", "category": "通知"},
    )
    client.post(
        f"/api/announcements/{announcement_id}/corrections", headers=author_h,
        json={"title": "在途撤回", "content": "v2", "category": "通知"},
    )
    client.post(f"/api/announcements/{announcement_id}/submit", headers=author_h, json={})

    withdrawn = client.post(
        f"/api/announcements/{announcement_id}/withdraw", headers=author_h, json={"reason": "整体撤回"}
    )
    assert withdrawn.status_code == 200
    # v2 已被打回草稿，审阅通过必须被拒绝，公告不会复活
    review = client.post(
        f"/api/announcements/{announcement_id}/review", headers=reviewer_h, json={"passed": True}
    )
    assert review.status_code == 409
    detail = client.get(f"/api/announcements/{announcement_id}", headers=reviewer_h).json()
    assert detail["status"] == "withdrawn"
    assert detail["current_version"]["review_status"] == "draft"


def test_management_list_filters_and_audit_trail(client, admin):
    author_h = _make_user(client, admin, "author.twelve", ["announcements.write"])
    reviewer_h = _make_user(client, admin, "reviewer.twelve", ["announcements.review"])
    announcement_id = _publish_immediate(
        client, author_h, reviewer_h,
        {"title": "留痕公告", "content": "正文", "category": "公告"},
    )
    pending = client.get(
        "/api/announcements", headers=reviewer_h, params={"review_status": "submitted"}
    ).json()
    assert pending["total"] == 0

    detail = client.get(f"/api/announcements/{announcement_id}", headers=reviewer_h).json()
    events = [event["event"] for event in detail["events"]]
    assert events == ["created", "submitted", "approved_published"]

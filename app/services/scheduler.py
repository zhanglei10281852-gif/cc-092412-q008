from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from app.core.clock import SystemClock
from app.database import close_connection, transaction
from app.services.announcements import PUBLISH_JOB_TYPE
from app.services.jobs import JobService

logger = logging.getLogger("app.scheduler")


class JobDispatcher:
    """把已领取的后台任务分发给对应领域服务。所有处理器必须幂等。"""

    def __init__(self) -> None:
        from app.services.announcements import AnnouncementService
        self.announcements = AnnouncementService

    def handles(self, job_type: str) -> bool:
        return job_type == PUBLISH_JOB_TYPE

    def dispatch(self, connection, job: dict) -> bool:
        if job["job_type"] == PUBLISH_JOB_TYPE:
            return self.announcements(connection).dispatch_job(job)
        return False


class AnnouncementScheduler:
    """单线程后台执行器。

    设计要点：
    - 任务状态保存在 SQLite，进程重启后启动即领取所有到点任务，补发不遗漏；
    - 每次只处理一个任务并在独立即时事务中提交，配合条件更新保证发布恰好一次；
    - 进程崩溃后租约到期的 running 任务会被重新领取，处理器幂等，不会重复发布；
    - stop() 用于测试与优雅停机。
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 60,
        worker_name: str = "announcement-scheduler",
        dispatcher_factory: Callable[[], JobDispatcher] = JobDispatcher,
        stopped: threading.Event | None = None,
    ) -> None:
        self.poll_interval = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.worker_name = worker_name
        self.dispatcher_factory = dispatcher_factory
        self._stop = stopped or threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name=self.worker_name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_forever(self) -> None:
        # 调度线程使用独立连接（get_connection 按线程隔离）
        dispatcher = self.dispatcher_factory()
        try:
            while not self._stop.is_set():
                worked = self.tick(dispatcher)
                if not worked:
                    self._stop.wait(self.poll_interval)
        finally:
            close_connection()

    def tick(self, dispatcher: JobDispatcher | None = None) -> bool:
        dispatcher = dispatcher or self.dispatcher_factory()
        # 第一步：在独立即时事务中领取任务（崩溃后由租约恢复，不会丢任务）
        try:
            with transaction(immediate=True) as tx:
                job = JobService(tx, SystemClock()).claim(
                    self.worker_name, lease_seconds=self.lease_seconds, job_types=(PUBLISH_JOB_TYPE,)
                )
        except Exception:  # noqa: BLE001
            logger.exception("领取后台任务失败")
            self._stop.wait(self.poll_interval)
            return True
        if job is None:
            return False
        # 第二步：在另一个即时事务中执行；领域操作全部幂等且带状态条件
        try:
            with transaction(immediate=True) as tx:
                handled = dispatcher.dispatch(tx, job)
                if not handled:
                    JobService(tx, SystemClock()).fail(job["id"], self.worker_name, "没有匹配的任务处理器")
                else:
                    JobService(tx, SystemClock()).complete(
                        job["id"], self.worker_name,
                        {"announcement_id": json.loads(job["payload_json"]).get("announcement_id")},
                    )
        except Exception as exc:  # noqa: BLE001 - 单条任务异常不能杀死调度线程
            logger.exception("公告定时任务处理失败，将在退避后重试")
            try:
                with transaction(immediate=True) as tx:
                    JobService(tx, SystemClock()).fail(
                        job["id"], self.worker_name, str(exc)[:500], retry_seconds=max(5, self.lease_seconds)
                    )
            except Exception:  # noqa: BLE001
                logger.exception("任务失败回执写入失败")
        return True

    def run_due_once(self) -> int:
        """同步补发所有到点任务（也供 CLI / 测试调用）。"""
        count = 0
        dispatcher = self.dispatcher_factory()
        while self.tick(dispatcher):
            count += 1
        return count

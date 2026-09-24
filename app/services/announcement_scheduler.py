from __future__ import annotations

import os
import threading

from app.database import close_connection, get_connection, transaction
from app.services.announcements import AnnouncementService


class AnnouncementPublishScheduler:
    """后台轮询线程：执行到期的公告定时发布任务。

    任务状态全部持久化在 SQLite 中，线程本身不保存任何状态，
    因此进程重启后会自动扫描到所有到期未执行的任务并补发；
    是否真正发布由服务层的条件 UPDATE 决定，保证只发布一次。
    """

    def __init__(self, *, poll_seconds: float | None = None, worker: str | None = None) -> None:
        configured = poll_seconds if poll_seconds is not None else float(os.getenv("TOWNSHIP_ANNOUNCEMENT_POLL_SECONDS", "2"))
        self.poll_seconds = max(0.2, configured)
        self.enabled = os.getenv("TOWNSHIP_SCHEDULER_ENABLED", "1").strip() not in ("0", "false", "no")
        self.worker = worker or f"announcement-scheduler:{os.getpid()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        """执行当前所有到期任务，返回本次处理的任务数。"""
        processed = 0
        while True:
            with transaction(immediate=True) as connection:
                result = AnnouncementService(connection).run_due_publish(self.worker)
            if result is None:
                break
            processed += 1
        return processed

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    # 单次轮询失败不能杀死调度线程；下个周期继续重试
                    pass
                self._stop.wait(self.poll_seconds)
        finally:
            close_connection()

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="announcement-publish-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

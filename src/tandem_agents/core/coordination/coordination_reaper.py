from __future__ import annotations

import logging
import threading
from typing import Any

from src.tandem_agents.core.coordination.coordination import CoordinationStore

logger = logging.getLogger("aca.coordination.reaper")


def coordination_worker_stale_interval(cfg) -> int:
    heartbeat = max(1, int(cfg.coordination.heartbeat_interval_seconds or 1))
    return max(1, heartbeat * 3)


def coordination_reaper_interval(cfg) -> int:
    ttl = max(1, int(cfg.coordination.lease_ttl_seconds or 1))
    heartbeat = max(1, int(cfg.coordination.heartbeat_interval_seconds or 1))
    return max(1, min(heartbeat, max(1, ttl // 3)))


def coordination_reaper_tick(cfg) -> list[dict[str, Any]]:
    store = CoordinationStore.from_config(cfg)
    store.ensure_schema()
    expired = store.reap_expired_leases()
    stale_workers = store.reap_stale_workers(stale_after_seconds=coordination_worker_stale_interval(cfg))
    return [*expired, *stale_workers]


class ReaperThreadHandle:
    """Handle returned by start_reaper_thread.

    Use stop() to signal the reaper to exit; the thread is a daemon so it
    will not block process exit even if stop() is never called, but callers
    should always stop() in a finally for clean shutdown and to release the
    SQLite connection promptly.
    """

    def __init__(self, thread: threading.Thread, stop_event: threading.Event) -> None:
        self.thread = thread
        self._stop_event = stop_event

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)


def start_reaper_thread(cfg) -> ReaperThreadHandle:
    """Run the coordination reaper in a daemon background thread.

    Use this from CLI entry points (run_once / run_worker) so that
    long-running CLI processes also reap orphaned leases. The API server
    has its own asyncio-native loop in api/main.py.
    """
    interval = coordination_reaper_interval(cfg)
    stop_event = threading.Event()

    def _loop() -> None:
        logger.info("Starting coordination lease reaper thread (interval=%ss).", interval)
        while not stop_event.is_set():
            try:
                expired = coordination_reaper_tick(cfg)
                if expired:
                    logger.info("Reaped %s expired coordination lease(s).", len(expired))
            except Exception:
                logger.exception("Coordination lease reaper tick failed")
            # Wait with cooperative cancellation
            if stop_event.wait(timeout=interval):
                break
        logger.info("Coordination lease reaper thread stopped.")

    thread = threading.Thread(
        target=_loop,
        name="aca-coordination-reaper",
        daemon=True,
    )
    thread.start()
    return ReaperThreadHandle(thread, stop_event)

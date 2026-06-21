"""Run the Playwright worker outside the Qt GUI process."""
from __future__ import annotations

from multiprocessing.synchronize import Event
from multiprocessing.queues import Queue

from browser.browser_manager import BrowserManager


def run_browser_worker(
    event_queue: Queue,
    stop_event: Event,
    retry_invoice_id: int | None,
    retry_errors_only: bool,
    worker_id: int,
    use_worker_profile: bool,
) -> None:
    """Forward worker events over IPC so Qt never shares its Python thread."""
    manager = BrowserManager(
        retry_invoice_id=retry_invoice_id,
        retry_errors_only=retry_errors_only,
        worker_id=worker_id,
        use_worker_profile=use_worker_profile,
        stop_event=stop_event,
    )
    manager.log_message.connect(lambda message: event_queue.put(("log", message)))
    manager.job_progress_changed.connect(lambda: event_queue.put(("progress", None)))
    manager.state_changed.connect(lambda is_open: event_queue.put(("state", is_open)))
    manager.finished.connect(lambda: event_queue.put(("finished", None)))
    manager.run()

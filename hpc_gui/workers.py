"""Qt thread workers so the UI stays responsive during SSH/SFTP calls.

NOTE on lifetime: PySide6 QRunnables with autoDelete=True (the Qt default) are
destroyed as soon as ``run()`` returns, and cross-thread queued signal
emissions reference the sender QObject at dispatch time. If no Python owner
keeps the Worker alive, the QObject carrying the signals gets freed under
the main thread, producing an access violation (0xC0000005) the next time
Qt tries to deliver an emission.

We mitigate this two ways:

1. ``setAutoDelete(False)`` — Qt doesn't touch us.
2. A module-level ``_LIVE`` set keeps a strong Python reference to every
   worker until its terminal (``finished`` or ``error``) signal has been
   emitted. The emission itself runs on the worker thread, so the reference
   is released only after the queued metacall has been posted.
"""
from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class _WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    log = Signal(str)


_LIVE: set["Worker"] = set()
_LIVE_LOCK = threading.Lock()


class Worker(QRunnable):
    """Runs ``fn(*args, **kwargs)`` on a QThreadPool worker thread.

    If ``fn`` has ``progress`` / ``log`` / ``should_cancel`` parameters, they
    are auto-injected (emitting to the corresponding signals / honoring the
    cancel flag).
    """

    def __init__(self, fn: Callable[..., Any], *args, **kwargs) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _WorkerSignals()
        self._cancelled = False

        import inspect
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        if "progress" in params:
            self.kwargs.setdefault("progress", self.signals.progress.emit)
        if "log" in params:
            self.kwargs.setdefault("log", self.signals.log.emit)
        if "should_cancel" in params:
            self.kwargs.setdefault("should_cancel", lambda: self._cancelled)

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            msg = f"{exc}\n\n{traceback.format_exc()}"
            try:
                self.signals.error.emit(msg)
            finally:
                _release(self)
            return
        try:
            self.signals.finished.emit(result)
        finally:
            _release(self)


def _retain(w: "Worker") -> None:
    with _LIVE_LOCK:
        _LIVE.add(w)


def _release(w: "Worker") -> None:
    with _LIVE_LOCK:
        _LIVE.discard(w)


def pool() -> QThreadPool:
    return QThreadPool.globalInstance()


def run_async(fn: Callable[..., Any], *args,
              on_done: Callable[[Any], None] | None = None,
              on_error: Callable[[str], None] | None = None,
              on_progress: Callable[[object], None] | None = None,
              on_log: Callable[[str], None] | None = None,
              **kwargs) -> Worker:
    """Schedule ``fn`` on a worker thread, wire callbacks, retain until done."""
    w = Worker(fn, *args, **kwargs)
    if on_done is not None:
        w.signals.finished.connect(on_done)
    if on_error is not None:
        w.signals.error.connect(on_error)
    if on_progress is not None:
        w.signals.progress.connect(on_progress)
    if on_log is not None:
        w.signals.log.connect(on_log)
    _retain(w)
    pool().start(w)
    return w

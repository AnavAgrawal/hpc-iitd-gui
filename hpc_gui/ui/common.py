"""Small UI helpers used across tabs."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QLabel, QMessageBox, QPlainTextEdit, QSizePolicy, QWidget,
)


MONO = QFont("Cascadia Mono")
MONO.setStyleHint(QFont.Monospace)
MONO.setPointSize(10)


def monospace(w: QWidget) -> QWidget:
    w.setFont(MONO)
    return w


def plain_log(read_only: bool = True) -> QPlainTextEdit:
    e = QPlainTextEdit()
    e.setReadOnly(read_only)
    e.setFont(MONO)
    e.setLineWrapMode(QPlainTextEdit.NoWrap)
    return e


def error_box(parent, title: str, msg: str) -> None:
    QMessageBox.critical(parent, title, msg)


def info_box(parent, title: str, msg: str) -> None:
    QMessageBox.information(parent, title, msg)


def confirm(parent, title: str, msg: str) -> bool:
    btn = QMessageBox.question(parent, title, msg,
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    return btn == QMessageBox.Yes


def labeled(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lab


def toasts(widget: QWidget):
    """Return the ToastManager attached to the widget's top-level window.
    Falls back to a silent no-op if none is installed (e.g. during tests)."""
    win = widget.window() if widget else None
    mgr = getattr(win, "toasts", None) if win else None
    if mgr is None:
        class _NoOp:
            def info(self, *a, **k): pass
            def success(self, *a, **k): pass
            def warn(self, *a, **k): pass
            def error(self, *a, **k): pass
        return _NoOp()
    return mgr


def set_busy(widget: QWidget, busy: bool) -> None:
    """Flash the global busy spinner (if present) while async work is in flight."""
    win = widget.window() if widget else None
    spinner = getattr(win, "busy", None) if win else None
    if spinner is None:
        return
    # Reference-counted: each call to busy increments, done decrements.
    counter = getattr(win, "_busy_refs", 0)
    counter = counter + (1 if busy else -1)
    if counter < 0:
        counter = 0
    win._busy_refs = counter
    if counter > 0:
        spinner.start()
    else:
        spinner.stop()

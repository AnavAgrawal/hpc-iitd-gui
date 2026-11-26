"""Logs tab: view / tail a remote text file with search and word-wrap."""
from __future__ import annotations

import posixpath

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCharFormat, QColor, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .. import config
from ..client import HpcClient
from ..workers import run_async
from . import style as st
from .common import plain_log, toasts


def _human_size(n: int) -> str:
    f = float(n)
    for u in ["B", "K", "M", "G", "T"]:
        if f < 1024:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} P"


class LogsTab(QWidget):
    def __init__(self, client: HpcClient, settings: config.Settings) -> None:
        super().__init__()
        self.client = client
        self.settings = settings

        # ---------- top row ----------
        self.path_combo = QComboBox(); self.path_combo.setEditable(True)
        for p in settings.recent_log_paths: self.path_combo.addItem(p)
        self.path_combo.setEditText(settings.recent_log_paths[0] if settings.recent_log_paths else "")
        self.path_combo.lineEdit().setPlaceholderText("remote path to a text file")

        self.fetch_btn = QPushButton("Fetch"); self.fetch_btn.setDefault(True)
        self.tail_cb = QCheckBox("Follow"); self.tail_cb.setChecked(True)
        self.tail_cb.setToolTip("Poll the file for new content at the given interval.")
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(2, 60); self.interval_spin.setValue(5)
        self.interval_spin.setSuffix(" s")
        self.bytes_spin = QSpinBox(); self.bytes_spin.setRange(1, 50000); self.bytes_spin.setValue(200)
        self.bytes_spin.setSuffix(" KB")
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("Clear")

        top = QHBoxLayout()
        top.addWidget(QLabel("Path:"))
        top.addWidget(self.path_combo, 1)
        top.addWidget(QLabel("Last"))
        top.addWidget(self.bytes_spin)
        top.addWidget(self.fetch_btn)
        top.addWidget(self.tail_cb)
        top.addWidget(QLabel("every"))
        top.addWidget(self.interval_spin)
        top.addWidget(self.stop_btn)
        top.addWidget(self.clear_btn)

        # ---------- metadata row ----------
        self.meta = QLabel("—")
        self.meta.setStyleSheet(f"color:{st.TEXT_MUTED}; padding: 2px 4px;")

        # ---------- find / options row ----------
        self.find_edit = QLineEdit(); self.find_edit.setPlaceholderText("Find…  (Ctrl+F)")
        self.find_edit.setClearButtonEnabled(True)
        self.find_prev_btn = QPushButton("◀")
        self.find_next_btn = QPushButton("▶")
        for b in (self.find_prev_btn, self.find_next_btn):
            b.setFixedWidth(32); b.setProperty("role", "ghost")
        self.wrap_cb = QCheckBox("Wrap"); self.wrap_cb.setChecked(settings.word_wrap_logs)
        self.end_btn = QPushButton("End"); self.end_btn.setProperty("role", "ghost")
        self.end_btn.setToolTip("Scroll to the end")

        find_row = QHBoxLayout()
        find_row.addWidget(self.find_edit, 1)
        find_row.addWidget(self.find_prev_btn); find_row.addWidget(self.find_next_btn)
        find_row.addSpacing(12)
        find_row.addWidget(self.wrap_cb)
        find_row.addWidget(self.end_btn)

        # ---------- viewer ----------
        self.viewer = plain_log(); self.viewer.setMaximumBlockCount(20000)
        self._apply_wrap(self.wrap_cb.isChecked())

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.meta)
        root.addLayout(find_row)
        root.addWidget(self.viewer, 1)

        self.timer = QTimer(self); self.timer.timeout.connect(self._fetch)

        # Signals
        self.fetch_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.clear_btn.clicked.connect(self.viewer.clear)
        self.tail_cb.toggled.connect(
            lambda on: self._start() if on and self.path_combo.currentText() else None)
        self.interval_spin.valueChanged.connect(
            lambda v: self.timer.start(v * 1000) if self.timer.isActive() else None)
        self.find_edit.returnPressed.connect(self._find_next)
        self.find_next_btn.clicked.connect(self._find_next)
        self.find_prev_btn.clicked.connect(self._find_prev)
        self.wrap_cb.toggled.connect(self._apply_wrap)
        self.end_btn.clicked.connect(self._goto_end)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.find_edit.setFocus)
        QShortcut(QKeySequence("F3"), self, activated=self._find_next)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self._find_prev)
        QShortcut(QKeySequence("Ctrl+End"), self, activated=self._goto_end)

    def open_path(self, remote_path: str) -> None:
        self.path_combo.setEditText(remote_path)
        if self.path_combo.findText(remote_path) < 0:
            self.path_combo.addItem(remote_path)
        self._start()

    def refresh(self) -> None:
        self._fetch()

    def _start(self) -> None:
        p = self.path_combo.currentText().strip()
        if not p or not self.client.connected:
            return
        self.settings.recent_log_paths = config.push_recent(
            self.settings.recent_log_paths, p, limit=15)
        config.save_settings(self.settings)
        self._fetch()
        if self.tail_cb.isChecked():
            self.timer.start(self.interval_spin.value() * 1000)
            self.stop_btn.setEnabled(True)

    def _stop(self) -> None:
        self.timer.stop()
        self.stop_btn.setEnabled(False)

    def _fetch(self) -> None:
        p = self.path_combo.currentText().strip()
        if not p or not self.client.connected:
            return
        max_bytes = self.bytes_spin.value() * 1024

        def task():
            attr = self.client.stat(p)
            size = attr.st_size if attr else 0
            mtime = attr.st_mtime if attr else 0
            text = self.client.read_text(p, max_bytes=max_bytes, tail=True)
            return size, mtime, text

        def done(result):
            size, mtime, text = result
            sb = self.viewer.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 4
            lines = text.count("\n")
            import datetime
            ts = datetime.datetime.fromtimestamp(int(mtime)).strftime("%Y-%m-%d %H:%M:%S") if mtime else "?"
            self.meta.setText(
                f"{p}    ·    size {_human_size(size)}    ·    modified {ts}    ·    {lines:,} lines shown")
            self.viewer.setPlainText(text)
            if at_bottom:
                sb.setValue(sb.maximum())

        def err(msg: str):
            self.viewer.setPlainText(f"[err] {msg.splitlines()[0]}")
            self.meta.setText("(read failed)")

        run_async(task, on_done=done, on_error=err)

    # ---------- find ----------
    def _find(self, direction: int) -> None:
        term = self.find_edit.text()
        if not term:
            return
        flags = QTextCursor.MoveAnchor  # placeholder; we use QPlainTextEdit.find
        from PySide6.QtGui import QTextDocument
        opts = QTextDocument.FindFlags()
        if direction < 0:
            opts |= QTextDocument.FindBackward
        found = self.viewer.find(term, opts)
        if not found:
            # wrap around
            c = self.viewer.textCursor()
            c.movePosition(QTextCursor.End if direction < 0 else QTextCursor.Start)
            self.viewer.setTextCursor(c)
            self.viewer.find(term, opts)

    def _find_next(self) -> None: self._find(+1)
    def _find_prev(self) -> None: self._find(-1)

    def _apply_wrap(self, on: bool) -> None:
        self.viewer.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)
        self.settings.word_wrap_logs = on
        config.save_settings(self.settings)

    def _goto_end(self) -> None:
        sb = self.viewer.verticalScrollBar()
        sb.setValue(sb.maximum())

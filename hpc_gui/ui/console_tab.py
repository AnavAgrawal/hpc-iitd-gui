"""Console tab: run arbitrary commands on the login node with history."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from ..client import HpcClient
from ..workers import run_async
from . import style as st
from .common import MONO, plain_log, toasts


PRESETS = [
    ("qstat -u $USER", "Your active jobs"),
    ("qstat -x -u $USER", "Include finished jobs"),
    ("qstat -Q", "Queue summary"),
    ("amgr ls project", "Your projects"),
    ("df -h ~/scratch | head -n 4", "Scratch disk usage"),
    ("ls -la ~/scratch | head", "List ~/scratch"),
    ("uptime", "Cluster load"),
    ("who | wc -l", "Users on this login node"),
    ("nvidia-smi 2>/dev/null || echo 'no GPU on login node'", "GPU info"),
]


class ConsoleTab(QWidget):
    HISTORY_LIMIT = 200

    def __init__(self, client: HpcClient) -> None:
        super().__init__()
        self.client = client
        self._history: list[str] = []

        warning = QLabel(
            "⚠  Commands run on an HPC login node.  "
            "Do NOT run long jobs here — submit a PBS job instead."
        )
        warning.setStyleSheet(
            f"background:{st.SURFACE_HI}; border-left: 3px solid {st.WARN}; "
            f"padding:8px 12px; color:{st.WARN}; border-radius:6px;")

        # Input row
        self.cmd_combo = QComboBox(); self.cmd_combo.setEditable(True)
        self.cmd_combo.lineEdit().setPlaceholderText("Enter a shell command (e.g. qstat -u $USER)  — Enter to run")
        self.cmd_combo.setFont(MONO)
        self.run_btn = QPushButton("Run"); self.run_btn.setDefault(True)
        self.clear_btn = QPushButton("Clear")
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy entire output")

        top = QHBoxLayout()
        top.addWidget(self.cmd_combo, 1)
        top.addWidget(self.run_btn); top.addWidget(self.clear_btn); top.addWidget(self.copy_btn)

        # Presets (small buttons)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        preset_row.addWidget(QLabel("Presets:"))
        for cmd, desc in PRESETS:
            b = QPushButton(cmd)
            b.setProperty("role", "ghost")
            b.setToolTip(desc)
            b.clicked.connect(lambda _=False, c=cmd: self._run_cmd(c))
            preset_row.addWidget(b)
        preset_row.addStretch(1)

        # History list (sidebar)
        self.history_list = QListWidget()
        self.history_list.setMinimumWidth(220); self.history_list.setMaximumWidth(320)
        self.history_list.setFont(MONO)
        self.history_list.itemDoubleClicked.connect(self._history_dblclick)
        hist_box = QGroupBox("History")
        hb = QVBoxLayout(hist_box)
        hb.setContentsMargins(8, 14, 8, 8)
        hb.addWidget(self.history_list, 1)

        self.output = plain_log(); self.output.setMaximumBlockCount(40000)

        center = QWidget(); cl = QVBoxLayout(center); cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self.output, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(center); split.addWidget(hist_box)
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)

        root = QVBoxLayout(self)
        root.addWidget(warning)
        root.addLayout(top)
        root.addLayout(preset_row)
        root.addWidget(split, 1)

        self.run_btn.clicked.connect(lambda: self._run_cmd(self.cmd_combo.currentText()))
        self.cmd_combo.lineEdit().returnPressed.connect(
            lambda: self._run_cmd(self.cmd_combo.currentText()))
        self.clear_btn.clicked.connect(self.output.clear)
        self.copy_btn.clicked.connect(self._copy_output)

    def _copy_output(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.output.toPlainText())
        toasts(self).info("Console output copied.")

    def _history_dblclick(self, item: QListWidgetItem) -> None:
        txt = item.data(Qt.UserRole) or item.text()
        self._run_cmd(txt)

    def _append(self, text: str, color: str | None = None, bold: bool = False) -> None:
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        if color: fmt.setForeground(QColor(color))
        if bold:
            from PySide6.QtGui import QFont
            f = QFont(MONO); f.setBold(True); fmt.setFont(f)
        cursor.insertText(text, fmt)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def _run_cmd(self, cmd: str) -> None:
        cmd = cmd.strip()
        if not cmd: return
        if not self.client.connected:
            self._append("[err] not connected\n", st.ERR); return
        ts = time.strftime("%H:%M:%S")
        self._append(f"\n[{ts}] $ ", st.TEXT_MUTED)
        self._append(f"{cmd}\n", st.ACCENT, bold=True)
        # Update history
        if cmd in self._history:
            self._history.remove(cmd)
        self._history.insert(0, cmd)
        self._history = self._history[: self.HISTORY_LIMIT]
        self.cmd_combo.blockSignals(True)
        self.cmd_combo.clear()
        self.cmd_combo.addItems(self._history)
        self.cmd_combo.setEditText(cmd)
        self.cmd_combo.blockSignals(False)

        # Update sidebar
        item = QListWidgetItem(cmd)
        item.setData(Qt.UserRole, cmd)
        self.history_list.insertItem(0, item)
        # Cap sidebar size
        while self.history_list.count() > self.HISTORY_LIMIT:
            self.history_list.takeItem(self.history_list.count() - 1)

        t0 = time.perf_counter()
        def task(): return self.client.run(cmd, timeout=60)

        def done(res):
            dt = time.perf_counter() - t0
            if res.stdout:
                self._append(res.stdout)
                if not res.stdout.endswith("\n"): self._append("\n")
            if res.stderr:
                self._append(res.stderr, st.ERR)
                if not res.stderr.endswith("\n"): self._append("\n")
            self._append(
                f"[exit={res.exit_code}  time={dt:.2f}s]\n",
                st.OK if res.ok else st.ERR,
            )

        def err(msg):
            self._append(f"[err] {msg.splitlines()[0]}\n", st.ERR)

        run_async(task, on_done=done, on_error=err)

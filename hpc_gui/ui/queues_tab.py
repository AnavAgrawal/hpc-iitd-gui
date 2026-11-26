"""Queues tab: status of all PBS queues with visual load bars + drill-down."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..client import HpcClient
from ..workers import run_async
from . import style as st
from .common import toasts


HEADERS = ["Queue", "Load", "Running", "Queued", "Held",
           "Total", "Max", "Enabled", "Started", "Type"]


def _stat_card(title: str, value: str, color: str) -> tuple[QFrame, QLabel]:
    f = QFrame()
    f.setStyleSheet(
        f"QFrame{{background:{st.BG_ALT}; border:1px solid {st.BORDER}; "
        f"border-radius:8px; border-left: 3px solid {color};}}")
    v = QVBoxLayout(f); v.setContentsMargins(12, 8, 12, 8); v.setSpacing(0)
    t = QLabel(title); t.setStyleSheet(f"color:{st.TEXT_DIM}; font-size:9pt;")
    val = QLabel(value); val.setStyleSheet(
        f"color:{st.TEXT}; font-size:18pt; font-weight:700;")
    v.addWidget(t); v.addWidget(val)
    return f, val


class LoadBar(QWidget):
    """Compact stacked bar: [running | queued | held] on a capacity track."""

    def __init__(self, running: int, queued: int, held: int,
                 capacity: int, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(18)
        self.setMinimumWidth(120)
        self._r = running; self._q = queued; self._h = held
        self._cap = max(capacity, running + queued + held, 1)
        self.setToolTip(
            f"Running: {running}   Queued: {queued}   Held: {held}\n"
            f"Capacity reference: {self._cap}"
        )

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 2, 0, -2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(st.SURFACE))
        p.drawRoundedRect(rect, 4, 4)
        total = self._cap
        x = rect.x(); y = rect.y(); h = rect.height()
        def seg(count: int, color: str) -> None:
            nonlocal x
            if count <= 0: return
            w = int(rect.width() * count / total)
            if w <= 0: return
            p.setBrush(QColor(color))
            p.drawRect(x, y, w, h)
            x += w
        seg(self._r, st.OK)
        seg(self._q, st.WARN)
        seg(self._h, st.TEXT_DIM)
        pen = QPen(QColor(st.BORDER)); pen.setWidth(1)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 4, 4)


JOB_HEADERS = ["Job ID", "User", "Name", "State", "Nodes", "CPUs",
               "Walltime", "Elapsed"]


STATE_LABEL = {
    "R": "running", "Q": "queued", "H": "held", "F": "finished",
    "E": "exiting", "C": "completed", "S": "suspended",
    "W": "waiting", "T": "transit",
}


class QueuesTab(QWidget):
    def __init__(self, client: HpcClient) -> None:
        super().__init__()
        self.client = client
        self._selected_queue: str = ""

        # ---------- cluster summary ----------
        summary_row = QHBoxLayout(); summary_row.setSpacing(10)
        self.card_total, self._v_total = _stat_card("Total jobs", "—", st.ACCENT)
        self.card_run, self._v_run = _stat_card("Running", "—", st.OK)
        self.card_q, self._v_q = _stat_card("Queued", "—", st.WARN)
        self.card_held, self._v_held = _stat_card("Held", "—", st.TEXT_DIM)
        self.card_queues, self._v_queues = _stat_card("Queues", "—", st.INFO)
        for c in (self.card_total, self.card_run, self.card_q, self.card_held, self.card_queues):
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            summary_row.addWidget(c, 1)

        # ---------- controls ----------
        self.refresh_btn = QPushButton("⟳  Refresh")
        self.refresh_btn.setProperty("role", "ghost")
        self.auto_cb = QCheckBox("Auto-refresh")
        self.auto_cb.setChecked(True)
        self.interval = QSpinBox(); self.interval.setRange(10, 600)
        self.interval.setValue(30); self.interval.setSuffix(" s")

        ctrl = QHBoxLayout()
        ctrl.addWidget(self.refresh_btn)
        ctrl.addWidget(self.auto_cb)
        ctrl.addWidget(QLabel("every"))
        ctrl.addWidget(self.interval)
        ctrl.addStretch(1)

        # ---------- top: queues table ----------
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._on_queue_selected)

        queues_wrap = QWidget(); qv = QVBoxLayout(queues_wrap); qv.setContentsMargins(0, 0, 0, 0)
        qv.addLayout(ctrl); qv.addWidget(self.table, 1)

        # ---------- bottom: jobs in queue ----------
        self.jobs_hdr = QLabel("Select a queue to see its jobs.")
        self.jobs_hdr.setStyleSheet(f"color:{st.TEXT_DIM}; padding:4px 0;")
        self.jobs_filter = QLineEdit()
        self.jobs_filter.setPlaceholderText("Filter jobs in this queue (user / name / id / state)…")
        self.jobs_filter.setClearButtonEnabled(True)
        self.jobs_filter.textChanged.connect(self._repaint_jobs)

        self.jobs_table = QTableWidget(0, len(JOB_HEADERS))
        self.jobs_table.setHorizontalHeaderLabels(JOB_HEADERS)
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.jobs_table.setAlternatingRowColors(True)
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        jhh = self.jobs_table.horizontalHeader()
        jhh.setSectionResizeMode(QHeaderView.ResizeToContents)
        jhh.setSectionResizeMode(2, QHeaderView.Stretch)

        jobs_wrap = QWidget(); jw = QVBoxLayout(jobs_wrap); jw.setContentsMargins(0, 0, 0, 0)
        jobs_hdr_row = QHBoxLayout()
        jobs_hdr_row.addWidget(self.jobs_hdr, 1)
        jobs_hdr_row.addWidget(self.jobs_filter, 1)
        jw.addLayout(jobs_hdr_row)
        jw.addWidget(self.jobs_table, 1)

        split = QSplitter(Qt.Vertical)
        split.addWidget(queues_wrap)
        split.addWidget(jobs_wrap)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

        root = QVBoxLayout(self)
        root.addLayout(summary_row)
        root.addWidget(split, 1)

        self.refresh_btn.clicked.connect(self.refresh)
        self.timer = QTimer(self); self.timer.timeout.connect(self._maybe_refresh)
        self.timer.start(self.interval.value() * 1000)
        self.interval.valueChanged.connect(lambda v: self.timer.start(v * 1000))
        self.auto_cb.toggled.connect(
            lambda on: self.timer.start(self.interval.value() * 1000) if on else self.timer.stop())

        self._queue_jobs: list = []

    # ---------- refresh ----------
    def _maybe_refresh(self) -> None:
        if self.auto_cb.isChecked() and self.client.connected and self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        if not self.client.connected:
            return

        def task():
            qs, raw_q = self.client.list_queues()
            return qs, raw_q

        def done(result):
            queues, _raw = result
            self._render_queues(queues)
            # Re-fetch jobs for currently selected queue too.
            if self._selected_queue:
                self._fetch_queue_jobs(self._selected_queue)

        run_async(task, on_done=done)

    def _render_queues(self, queues) -> None:
        def to_int(s: str) -> int:
            try: return int(s)
            except (ValueError, TypeError): return 0

        total_r = total_q = total_h = 0
        for q in queues:
            total_r += to_int(q.running)
            total_q += to_int(q.queued)
            total_h += to_int(q.held)
        total = total_r + total_q + total_h
        self._v_total.setText(str(total))
        self._v_run.setText(str(total_r))
        self._v_q.setText(str(total_q))
        self._v_held.setText(str(total_h))
        self._v_queues.setText(str(len(queues)))

        # Preserve selection across refresh
        selected_name = self._selected_queue
        self.table.setRowCount(0)
        for q in queues:
            r = self.table.rowCount(); self.table.insertRow(r)
            name_item = QTableWidgetItem(q.name)
            name_item.setData(Qt.UserRole, q.name)
            if q.name == "high":
                name_item.setForeground(QBrush(QColor(st.WARN)))
                name_item.setToolTip("High priority queue — 5× cost.")
            elif q.enabled != "yes":
                name_item.setForeground(QBrush(QColor(st.TEXT_MUTED)))
            self.table.setItem(r, 0, name_item)

            running = to_int(q.running); queued = to_int(q.queued); held = to_int(q.held)
            cap = to_int(q.max) or (running + queued + held) or 1
            bar = LoadBar(running, queued, held, cap)
            self.table.setCellWidget(r, 1, bar)

            cells = [q.running, q.queued, q.held, q.total, q.max,
                     q.enabled, q.started, q.qtype]
            for col, val in enumerate(cells, start=2):
                it = QTableWidgetItem(str(val) if val is not None else "")
                it.setTextAlignment(Qt.AlignCenter)
                if col == 2 and running > 0: it.setForeground(QBrush(QColor(st.OK)))
                elif col == 3 and queued > 0: it.setForeground(QBrush(QColor(st.WARN)))
                elif col == 4 and held > 0: it.setForeground(QBrush(QColor(st.TEXT_DIM)))
                if q.enabled != "yes":
                    it.setForeground(QBrush(QColor(st.TEXT_MUTED)))
                self.table.setItem(r, col, it)
            self.table.setRowHeight(r, 26)

        if selected_name:
            self._select_row_by_name(selected_name)

    def _select_row_by_name(self, name: str) -> None:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.text() == name:
                self.table.blockSignals(True)
                self.table.selectRow(r)
                self.table.blockSignals(False)
                return

    # ---------- drill-down: jobs in selected queue ----------
    def _on_queue_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        it = self.table.item(row, 0)
        if not it:
            return
        name = it.text()
        if name == self._selected_queue:
            return
        self._selected_queue = name
        self.jobs_hdr.setText(f"Jobs in queue  “{name}”  — loading…")
        self._fetch_queue_jobs(name)

    def _fetch_queue_jobs(self, queue: str) -> None:
        def task():
            return self.client.list_queue_jobs(queue)

        def done(result):
            jobs, raw = result
            self._queue_jobs = list(jobs)
            self._repaint_jobs()
            if not jobs:
                self.jobs_hdr.setText(
                    f"Jobs in queue  “{queue}”  — none (queue may be empty or hidden).")
            else:
                mine = sum(1 for j in jobs if j.username.rstrip("*") == self.client.username)
                self.jobs_hdr.setText(
                    f"Jobs in queue  “{queue}”  — {len(jobs)} total"
                    + (f", {mine} yours" if mine else ""))

        def err(msg):
            self._queue_jobs = []
            self.jobs_table.setRowCount(0)
            self.jobs_hdr.setText(
                f"Jobs in queue  “{queue}”  — error: {msg.splitlines()[0]}")
            toasts(self).error(msg.splitlines()[0], title="Queue listing")

        run_async(task, on_done=done, on_error=err)

    def _repaint_jobs(self) -> None:
        f = self.jobs_filter.text().strip().lower()
        my_user = self.client.username
        self.jobs_table.setRowCount(0)
        for j in self._queue_jobs:
            if f:
                hay = " ".join([j.job_id, j.username, j.name, j.state]).lower()
                if f not in hay:
                    continue
            r = self.jobs_table.rowCount(); self.jobs_table.insertRow(r)
            own = (j.username.rstrip("*") == my_user)
            tint = QColor("#12321d") if own else None

            def put(col: int, text: str, align=Qt.AlignLeft | Qt.AlignVCenter,
                    fg: str | None = None) -> None:
                it = QTableWidgetItem(text)
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(QBrush(QColor(fg)))
                if tint is not None:
                    it.setBackground(QBrush(tint))
                self.jobs_table.setItem(r, col, it)

            put(0, j.job_id, Qt.AlignLeft | Qt.AlignVCenter,
                st.ACCENT if own else None)
            put(1, j.username, Qt.AlignLeft | Qt.AlignVCenter,
                st.OK if own else None)
            put(2, j.name)
            state_text = f"{j.state} · {STATE_LABEL.get(j.state, '')}".strip(" ·")
            put(3, state_text, Qt.AlignLeft | Qt.AlignVCenter,
                st.STATE_COLORS.get(j.state))
            put(4, j.nds, Qt.AlignCenter)
            put(5, j.tsk, Qt.AlignCenter)
            put(6, j.walltime, Qt.AlignCenter)
            put(7, j.elapsed, Qt.AlignCenter)

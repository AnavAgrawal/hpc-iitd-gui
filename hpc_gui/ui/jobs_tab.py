"""Jobs tab: live qstat table with quick-filter, progress bars, context menu."""
from __future__ import annotations

import re

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..client import HpcClient
from ..parsers import Job
from ..workers import run_async
from . import style as st
from .common import confirm, error_box, info_box, plain_log, toasts


STATE_LABEL = {
    "R": "running", "Q": "queued", "H": "held", "F": "finished",
    "E": "exiting", "C": "completed", "S": "suspended",
    "W": "waiting", "T": "transit",
}


_HMS_RE = re.compile(r"^\s*(\d+)(?::(\d+))?(?::(\d+))?\s*$")


def _fmt_est_start(raw: str, state: str) -> tuple[str, str]:
    """Format ``estimated.start_time`` (e.g. 'Wed Apr 23 14:30:00 2025') as a
    short relative time. Returns (display, tooltip)."""
    if state == "R":
        return ("running", "Job is currently running.")
    if state in ("F", "C", "E"):
        return ("—", "")
    if not raw:
        return ("—", "PBS has not produced an estimate yet.")
    from datetime import datetime
    # Try common PBS format "Day Mon DD HH:MM:SS YYYY"
    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            when = datetime.strptime(raw, fmt)
            break
        except ValueError:
            when = None
    if when is None:
        return (raw, raw)
    now = datetime.now()
    delta = when - now
    secs = int(delta.total_seconds())
    sign = ""
    if secs < 0:
        sign = "-"; secs = -secs
    days, rem = divmod(secs, 86400)
    hrs, rem = divmod(rem, 3600)
    mins = rem // 60
    if days: rel = f"in {sign}{days}d {hrs}h"
    elif hrs: rel = f"in {sign}{hrs}h {mins}m"
    else: rel = f"in {sign}{mins}m"
    return (rel, f"Estimated start: {when.strftime('%Y-%m-%d %H:%M')}")


def _hm_to_seconds(s: str) -> int:
    """Parse 'HH:MM' or 'HH:MM:SS' or just 'HH' into seconds. Returns 0 on failure."""
    if not s or s == "--":
        return 0
    m = _HMS_RE.match(s)
    if not m:
        return 0
    parts = [int(x) if x else 0 for x in m.groups()]
    # For 2-part 'HH:MM', groups are (HH, MM, None) -> treat as hours/minutes.
    if parts[2] == 0 and s.count(":") == 1:
        h, mm, _ = parts
        return h * 3600 + mm * 60
    h, mm, ss = parts
    return h * 3600 + mm * 60 + ss


class JobsTab(QWidget):
    view_log = Signal(str)
    tail_log = Signal(str)
    job_counts_changed = Signal(dict)  # {running, queued, held, total}

    HEADERS = ["Job ID", "Name", "Queue", "State", "Progress",
               "Nodes", "CPUs", "GPUs", "Walltime", "Elapsed",
               "Est. start", "User"]

    def __init__(self, client: HpcClient) -> None:
        super().__init__()
        self.client = client
        self._jobs: list[Job] = []
        self._extras: dict = {}
        self._filter = ""

        # ---------- control row ----------
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter  (job id / name / queue / state)  — Ctrl+F")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)

        self.hist_cb = QCheckBox("Include finished")
        self.hist_cb.setToolTip("qstat -x: also show recently completed jobs.")
        self.auto_cb = QCheckBox("Auto-refresh")
        self.auto_cb.setChecked(True)
        self.interval_spin = QSpinBox(); self.interval_spin.setRange(5, 300)
        self.interval_spin.setValue(15); self.interval_spin.setSuffix(" s")

        self.refresh_btn = QPushButton("⟳  Refresh")
        self.refresh_btn.setProperty("role", "ghost")
        self.refresh_btn.setShortcut("F5")
        self.refresh_btn.setToolTip("Refresh the job list (F5)")

        self.count_label = QLabel("")
        self.count_label.setStyleSheet(f"color:{st.TEXT_DIM};")

        ctrl = QHBoxLayout()
        ctrl.addWidget(self.filter_edit, 1)
        ctrl.addWidget(self.hist_cb)
        ctrl.addWidget(self.auto_cb)
        ctrl.addWidget(QLabel("every"))
        ctrl.addWidget(self.interval_spin)
        ctrl.addWidget(self.refresh_btn)

        # ---------- table ----------
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)  # Name stretches
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(4, 160)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(lambda *_: self._details())

        # ---------- actions row ----------
        self.details_btn = QPushButton("Details")
        self.details_btn.setToolTip("qstat -f — full job record")
        self.view_out_btn = QPushButton("View .out")
        self.view_err_btn = QPushButton("View .err")
        self.tail_out_btn = QPushButton("Tail .out")
        self.hold_btn = QPushButton("Hold")
        self.release_btn = QPushButton("Release")
        self.est_btn = QPushButton("Est. start")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("role", "danger")

        actions = QHBoxLayout()
        for b in [self.details_btn, self.view_out_btn, self.view_err_btn,
                  self.tail_out_btn, self.est_btn, self.hold_btn,
                  self.release_btn]:
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.count_label)
        actions.addStretch(0)
        actions.addWidget(self.cancel_btn)

        # ---------- details pane ----------
        self.details = plain_log()
        self.details.setPlaceholderText(
            "Select a job and press Details (or double-click) to see full qstat -f output."
        )

        split = QSplitter(Qt.Vertical)
        top = QWidget(); top_l = QVBoxLayout(top); top_l.setContentsMargins(0, 0, 0, 0)
        top_l.addLayout(ctrl); top_l.addWidget(self.table, 1); top_l.addLayout(actions)
        split.addWidget(top); split.addWidget(self.details)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)

        root = QVBoxLayout(self); root.addWidget(split, 1)

        # ---------- timers / signals ----------
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._maybe_refresh)
        self.timer.start(self.interval_spin.value() * 1000)
        self.refresh_btn.clicked.connect(self.refresh)
        self.hist_cb.toggled.connect(self.refresh)
        self.interval_spin.valueChanged.connect(lambda v: self.timer.start(v * 1000))
        self.auto_cb.toggled.connect(
            lambda on: self.timer.start(self.interval_spin.value() * 1000) if on else self.timer.stop())
        self.details_btn.clicked.connect(self._details)
        self.cancel_btn.clicked.connect(lambda: self._simple_action("qdel", self.client.qdel, "cancelled"))
        self.hold_btn.clicked.connect(lambda: self._simple_action("qhold", self.client.qhold, "held"))
        self.release_btn.clicked.connect(lambda: self._simple_action("qrls", self.client.qrls, "released"))
        self.view_out_btn.clicked.connect(lambda: self._open_log(".out"))
        self.view_err_btn.clicked.connect(lambda: self._open_log(".err"))
        self.tail_out_btn.clicked.connect(lambda: self._tail_log(".out"))
        self.est_btn.clicked.connect(self._estimate)

        # Ctrl+F focuses the filter.
        from PySide6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.filter_edit.setFocus)

    # ---------- refresh ----------
    def _maybe_refresh(self) -> None:
        if self.auto_cb.isChecked() and self.client.connected and self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        if not self.client.connected:
            return
        include_history = self.hist_cb.isChecked()

        def task():
            return self.client.list_my_jobs_enriched(include_history=include_history)

        def done(result):
            jobs, _raw, extras = result
            self._jobs = jobs
            self._extras = extras or {}
            self._repaint()
            counts = self._counts(jobs)
            self.job_counts_changed.emit(counts)

        def err(msg):
            self.details.setPlainText(f"[err] {msg.splitlines()[0]}")
            toasts(self).error(msg.splitlines()[0], title="qstat failed")

        run_async(task, on_done=done, on_error=err)

    def _counts(self, jobs: list[Job]) -> dict:
        r = q = h = 0
        for j in jobs:
            if j.state == "R": r += 1
            elif j.state == "Q": q += 1
            elif j.state == "H": h += 1
        return {"running": r, "queued": q, "held": h, "total": len(jobs)}

    # ---------- painting ----------
    def _on_filter_changed(self, txt: str) -> None:
        self._filter = txt.strip().lower()
        self._repaint()

    def _matches_filter(self, j: Job) -> bool:
        if not self._filter:
            return True
        hay = " ".join([j.job_id, j.name, j.queue, j.state, j.username]).lower()
        return self._filter in hay

    def _repaint(self) -> None:
        selected_id = self._selected_job_id()
        self.table.setRowCount(0)
        visible: list[Job] = []
        for j in self._jobs:
            if not self._matches_filter(j):
                continue
            visible.append(j)
            r = self.table.rowCount()
            self.table.insertRow(r)

            id_item = QTableWidgetItem(j.job_id)
            id_item.setData(Qt.UserRole, j.job_id)
            self.table.setItem(r, 0, id_item)
            self.table.setItem(r, 1, QTableWidgetItem(j.name))

            queue_item = QTableWidgetItem(j.queue)
            if j.queue == "high":
                queue_item.setForeground(QBrush(QColor(st.WARN)))
                queue_item.setToolTip("High priority queue — 5× cost.")
            self.table.setItem(r, 2, queue_item)

            state_text = f"{j.state} · {STATE_LABEL.get(j.state, '')}".strip(" ·")
            state_item = QTableWidgetItem(state_text)
            sc = st.STATE_COLORS.get(j.state)
            if sc:
                state_item.setForeground(QBrush(QColor(sc)))
            # Subtle row tint for running & held
            if j.state == "R":
                tint = QColor("#122a19")
            elif j.state == "H":
                tint = QColor("#1a1f25")
            elif j.state == "Q":
                tint = QColor("#2a2516")
            elif j.state in ("F", "C"):
                tint = QColor("#15191e")
            else:
                tint = None
            if tint is not None:
                for c in range(self.table.columnCount()):
                    it = self.table.item(r, c)
                    if it is not None:
                        it.setBackground(QBrush(tint))
            self.table.setItem(r, 3, state_item)

            # Progress column
            elapsed_s = _hm_to_seconds(j.elapsed)
            walltime_s = _hm_to_seconds(j.walltime)
            pct = 0
            if walltime_s > 0 and elapsed_s >= 0:
                pct = max(0, min(100, int(100 * elapsed_s / walltime_s)))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(pct if j.state == "R" else 0 if j.state in ("Q", "H") else 100)
            if j.state == "R":
                bar.setFormat(f"{pct}%")
            elif j.state in ("Q", "H"):
                bar.setFormat("—")
            else:
                bar.setFormat(STATE_LABEL.get(j.state, j.state))
            color = st.OK if j.state == "R" else (
                st.TEXT_MUTED if j.state in ("F", "C") else st.WARN)
            bar.setStyleSheet(f"""
                QProgressBar {{
                    background: {st.BG};
                    border: 1px solid {st.BORDER};
                    border-radius: 4px; height: 14px; text-align: center;
                    color: {st.TEXT_DIM};
                }}
                QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}
            """)
            self.table.setCellWidget(r, 4, bar)

            # Extras (from batched qstat -f)
            ex = self._extras.get(j.job_id, {})
            ncpus = ex.get("ncpus") or j.tsk or ""
            ngpus = ex.get("ngpus")
            nodes = ex.get("nodes") or j.nds or ""
            est_raw = ex.get("est_start") or ""
            est_display, est_tt = _fmt_est_start(est_raw, j.state)

            nodes_item = QTableWidgetItem(str(nodes))
            nodes_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 5, nodes_item)

            cpus_item = QTableWidgetItem(str(ncpus))
            cpus_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 6, cpus_item)

            gpus_text = str(ngpus) if ngpus is not None else "—"
            gpus_item = QTableWidgetItem(gpus_text)
            gpus_item.setTextAlignment(Qt.AlignCenter)
            if ngpus and int(ngpus) > 0:
                gpus_item.setForeground(QBrush(QColor(st.INFO)))
            self.table.setItem(r, 7, gpus_item)

            self.table.setItem(r, 8, QTableWidgetItem(j.walltime))
            self.table.setItem(r, 9, QTableWidgetItem(j.elapsed))

            est_item = QTableWidgetItem(est_display)
            if est_tt:
                est_item.setToolTip(est_tt)
            est_item.setForeground(QBrush(QColor(st.TEXT_DIM)))
            self.table.setItem(r, 10, est_item)

            self.table.setItem(r, 11, QTableWidgetItem(j.username))

        for col in (0, 5, 6, 7, 8, 9, 10, 11):
            self.table.resizeColumnToContents(col)

        counts = self._counts(visible)
        total = counts["total"]
        if self._filter:
            self.count_label.setText(f"showing {total} / {len(self._jobs)}")
        else:
            tail = (f"{counts['running']}R · {counts['queued']}Q · {counts['held']}H"
                    if total else "no jobs")
            self.count_label.setText(tail)

        if selected_id:
            self._select_by_id(selected_id)

    # ---------- selection helpers ----------
    def _selected_job_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _select_by_id(self, jid: str) -> None:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.text() == jid:
                self.table.selectRow(r); return

    def _selected_job(self) -> Job | None:
        jid = self._selected_job_id()
        if not jid: return None
        for j in self._jobs:
            if j.job_id == jid: return j
        return None

    # ---------- context menu ----------
    def _show_context_menu(self, pos) -> None:
        jid = self._selected_job_id()
        if not jid:
            return
        j = self._selected_job()
        menu = QMenu(self)
        act_details = QAction("Show details (qstat -f)", self)
        act_details.triggered.connect(self._details)
        menu.addAction(act_details)
        act_est = QAction("Estimated start time", self)
        act_est.triggered.connect(self._estimate)
        menu.addAction(act_est)
        menu.addSeparator()
        act_viewo = QAction("View stdout (.out)", self)
        act_viewo.triggered.connect(lambda: self._open_log(".out"))
        menu.addAction(act_viewo)
        act_viewe = QAction("View stderr (.err)", self)
        act_viewe.triggered.connect(lambda: self._open_log(".err"))
        menu.addAction(act_viewe)
        act_tail = QAction("Tail stdout (follow)", self)
        act_tail.triggered.connect(lambda: self._tail_log(".out"))
        menu.addAction(act_tail)
        menu.addSeparator()
        act_copy = QAction("Copy job ID", self)
        act_copy.triggered.connect(self._copy_id)
        menu.addAction(act_copy)
        menu.addSeparator()
        if j and j.state == "Q":
            act_hold = QAction("Hold (qhold)", self)
            act_hold.triggered.connect(
                lambda: self._simple_action("qhold", self.client.qhold, "held"))
            menu.addAction(act_hold)
        if j and j.state == "H":
            act_rel = QAction("Release (qrls)", self)
            act_rel.triggered.connect(
                lambda: self._simple_action("qrls", self.client.qrls, "released"))
            menu.addAction(act_rel)
        act_cancel = QAction("Cancel (qdel)", self)
        act_cancel.triggered.connect(
            lambda: self._simple_action("qdel", self.client.qdel, "cancelled"))
        menu.addAction(act_cancel)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_id(self) -> None:
        jid = self._selected_job_id()
        if jid:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(jid)
            toasts(self).info(f"Copied {jid}", title="Clipboard")

    # ---------- actions ----------
    def _details(self) -> None:
        jid = self._selected_job_id()
        if not jid:
            toasts(self).warn("Select a job first."); return
        self.details.setPlainText(f"[..] fetching qstat -f {jid}")
        def task(): return self.client.job_full(jid)
        def done(res): self.details.setPlainText(res.output or "(no output)")
        def err(msg): self.details.setPlainText(f"[err] {msg}")
        run_async(task, on_done=done, on_error=err)

    def _simple_action(self, name: str, fn, verb: str) -> None:
        jid = self._selected_job_id()
        if not jid:
            toasts(self).warn("Select a job first."); return
        if name == "qdel" and not confirm(self, "Cancel job", f"Really cancel {jid}?"):
            return
        def task(): return fn(jid)
        def done(res):
            if res.ok:
                toasts(self).success(f"{jid} {verb}.", title=name)
                self.refresh()
            else:
                toasts(self).error(res.output or f"{name} failed", title=name)
        def err(msg):
            toasts(self).error(msg, title=name)
        run_async(task, on_done=done, on_error=err)

    def _resolve_log_path(self, kind: str) -> str | None:
        jid = self._selected_job_id()
        if not jid:
            toasts(self).warn("Select a job first."); return None
        full = self.client.job_full_parsed(jid)
        key = "Output_Path" if kind == ".out" else "Error_Path"
        raw = full.get(key, "")
        if not raw:
            toasts(self).error(f"{key} not found in qstat -f."); return None
        return raw.split(":", 1)[1] if ":" in raw else raw

    def _open_log(self, kind: str) -> None:
        path = self._resolve_log_path(kind)
        if path: self.view_log.emit(path)

    def _tail_log(self, kind: str) -> None:
        path = self._resolve_log_path(kind)
        if path: self.tail_log.emit(path)

    def _estimate(self) -> None:
        jid = self._selected_job_id()
        if not jid:
            toasts(self).warn("Select a job first."); return
        def task(): return self.client.job_estimate(jid)
        def done(res): self.details.setPlainText(res.output)
        run_async(task, on_done=done)

"""Dashboard: at-a-glance HPC status with cards, live jobs list, cluster viz."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..client import HpcClient
from ..workers import run_async
from . import style as st


class _StatCard(QFrame):
    """A flat card: accent stripe on the left, title + big value + optional sub."""

    def __init__(self, title: str, accent: str, icon: str = "") -> None:
        super().__init__()
        self._accent = accent
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"QFrame{{background:{st.BG_ALT}; border:1px solid {st.BORDER}; "
            f"border-radius:8px; border-left: 3px solid {accent};}}")
        v = QVBoxLayout(self); v.setContentsMargins(14, 10, 14, 10); v.setSpacing(2)

        head = QHBoxLayout(); head.setSpacing(6)
        if icon:
            ic = QLabel(icon); ic.setStyleSheet(f"color:{accent}; font-size:11pt; font-weight:700;")
            head.addWidget(ic)
        t = QLabel(title)
        t.setStyleSheet(f"color:{st.TEXT_DIM}; font-size:10pt; font-weight:500;")
        head.addWidget(t); head.addStretch(1)
        v.addLayout(head)

        self.value = QLabel("—")
        f = QFont(); f.setPointSize(22); f.setBold(True)
        self.value.setFont(f)
        self.value.setStyleSheet(f"color:{st.TEXT};")
        v.addWidget(self.value)

        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color:{st.TEXT_MUTED}; font-size:9pt;")
        v.addWidget(self.sub)

    def set(self, text: str, sub: str = "") -> None:
        self.value.setText(text)
        self.sub.setText(sub)

    def set_accent(self, color: str) -> None:
        self._accent = color
        self.setStyleSheet(
            f"QFrame{{background:{st.BG_ALT}; border:1px solid {st.BORDER}; "
            f"border-radius:8px; border-left: 3px solid {color};}}")


class ClusterLoadWidget(QFrame):
    """Horizontal bar per queue: green (running) / amber (queued) / grey (held)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"QFrame{{background:{st.BG_ALT}; border:1px solid {st.BORDER}; border-radius:8px;}}")
        self._rows: list[tuple[str, int, int, int, int]] = []
        self.setMinimumHeight(160)

    def update_data(self, rows) -> None:
        self._rows = rows
        self.update()

    def sizeHint(self):
        # Grow height with number of queues
        from PySide6.QtCore import QSize
        h = 40 + max(1, len(self._rows)) * 28
        return QSize(400, h)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(st.BG_ALT))
        # Title
        p.setPen(QColor(st.TEXT_DIM))
        f = self.font(); f.setPointSize(10); f.setBold(True); p.setFont(f)
        p.drawText(14, 22, "Cluster load by queue")
        # Legend
        legend_x = self.width() - 260
        legend_y = 18
        self._legend(p, legend_x, legend_y, st.OK, "running")
        self._legend(p, legend_x + 80, legend_y, st.WARN, "queued")
        self._legend(p, legend_x + 160, legend_y, st.TEXT_DIM, "held")

        # Bars
        f2 = self.font(); f2.setBold(False); p.setFont(f2)
        top = 40
        label_w = 100
        bar_x = 14 + label_w
        bar_w = max(20, self.width() - bar_x - 28)
        for i, (name, r, q, h, cap) in enumerate(self._rows):
            y = top + i * 28
            p.setPen(QColor(st.TEXT))
            p.drawText(14, y + 14, name)
            # Track
            track = self.rect().adjusted(bar_x, y + 4, -(self.width() - bar_x - bar_w), -(self.height() - y - 20))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(st.SURFACE))
            from PySide6.QtCore import QRect
            trect = QRect(bar_x, y + 4, bar_w, 16)
            p.drawRoundedRect(trect, 4, 4)
            # Segments
            total = max(cap, r + q + h, 1)
            x = bar_x
            def seg(count, color):
                nonlocal x
                if count <= 0:
                    return
                w = int(bar_w * count / total)
                if w <= 0:
                    return
                p.setBrush(QColor(color))
                p.drawRect(x, y + 4, w, 16)
                x += w
            seg(r, st.OK); seg(q, st.WARN); seg(h, st.TEXT_DIM)
            # Outline
            pen = QPen(QColor(st.BORDER)); pen.setWidth(1); p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(trect, 4, 4)
            # Counts label
            p.setPen(QColor(st.TEXT_DIM))
            p.drawText(bar_x + bar_w + 6, y + 16, f"{r}·{q}·{h}")

        if not self._rows:
            p.setPen(QColor(st.TEXT_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "(no data — connect and refresh)")

    def _legend(self, p: QPainter, x: int, y: int, color: str, label: str) -> None:
        p.setPen(Qt.NoPen); p.setBrush(QColor(color))
        from PySide6.QtCore import QRect
        p.drawRoundedRect(QRect(x, y - 9, 10, 10), 2, 2)
        p.setPen(QColor(st.TEXT_DIM))
        p.drawText(x + 16, y, label)


class DashboardTab(QWidget):
    go_to_jobs = Signal()
    go_to_submit = Signal()
    go_to_files = Signal()
    go_to_logs = Signal()
    balance_refreshed = Signal(object)  # float or None

    def __init__(self, client: HpcClient) -> None:
        super().__init__()
        self.client = client
        self._project: str = ""
        self._user: str = ""

        # ---------- cards ----------
        self.user_card = _StatCard("User", st.INFO, "👤")
        self.project_card = _StatCard("Project", st.INFO, "#")
        self.balance_card = _StatCard("Balance (₹)", st.OK, "₹")
        self.running_card = _StatCard("Running", st.OK, "▶")
        self.queued_card = _StatCard("Queued", st.WARN, "⏳")
        self.finished_card = _StatCard("Finished (24h)", st.TEXT_DIM, "✓")

        grid = QGridLayout()
        grid.setSpacing(10)
        for i, c in enumerate([self.user_card, self.project_card, self.balance_card,
                               self.running_card, self.queued_card, self.finished_card]):
            grid.addWidget(c, i // 3, i % 3)
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # ---------- quick actions ----------
        actions = QGroupBox("Quick actions")
        arow = QHBoxLayout(actions); arow.setSpacing(8)
        self.refresh_btn = QPushButton("⟳ Refresh all"); self.refresh_btn.setProperty("role", "primary")
        self.jobs_btn = QPushButton("View jobs")
        self.submit_btn = QPushButton("Submit a job")
        self.files_btn = QPushButton("Upload files")
        self.logs_btn = QPushButton("Tail a log")
        for b in [self.refresh_btn, self.jobs_btn, self.submit_btn, self.files_btn, self.logs_btn]:
            arow.addWidget(b)
        arow.addStretch(1)

        # ---------- live jobs (compact) ----------
        jobs_box = QGroupBox("Live jobs"); jb = QVBoxLayout(jobs_box); jb.setContentsMargins(12, 14, 12, 10)
        self.jobs_tbl = QTableWidget(0, 5)
        self.jobs_tbl.setHorizontalHeaderLabels(["Job ID", "Name", "Queue", "State", "Progress"])
        self.jobs_tbl.verticalHeader().setVisible(False)
        self.jobs_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.jobs_tbl.setAlternatingRowColors(True)
        self.jobs_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.jobs_tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.jobs_tbl.setColumnWidth(4, 160)
        self.jobs_tbl.itemDoubleClicked.connect(self.go_to_jobs)
        jb.addWidget(self.jobs_tbl, 1)

        # ---------- cluster viz ----------
        self.cluster = ClusterLoadWidget()

        cols = QHBoxLayout(); cols.setSpacing(10)
        cols.addWidget(jobs_box, 3)
        cols.addWidget(self.cluster, 2)

        root = QVBoxLayout(self)
        root.addLayout(grid)
        root.addWidget(actions)
        root.addLayout(cols, 1)

        self.refresh_btn.clicked.connect(self.refresh)
        self.jobs_btn.clicked.connect(self.go_to_jobs)
        self.submit_btn.clicked.connect(self.go_to_submit)
        self.files_btn.clicked.connect(self.go_to_files)
        self.logs_btn.clicked.connect(self.go_to_logs)

    # ---------- public API ----------
    def set_identity(self, user: str, project: str) -> None:
        self._user = user; self._project = project
        self.user_card.set(user or "—")
        self.project_card.set(project or "—")

    def set_balance(self, balance: float | None) -> None:
        if balance is None:
            self.balance_card.set("—")
            self.balance_card.set_accent(st.TEXT_DIM)
        else:
            self.balance_card.set(f"{balance:,.2f}")
            # Warn when balance is low.
            color = st.OK if balance > 2000 else (st.WARN if balance > 200 else st.ERR)
            self.balance_card.set_accent(color)

    def refresh(self) -> None:
        if not self.client.connected:
            return

        self.refresh_btn.setEnabled(False)
        self._set_cards_loading()

        def task():
            jobs, _raw = self.client.list_my_jobs()
            hist_jobs, _ = self.client.list_my_jobs(include_history=True)
            qs, _ = self.client.list_queues()
            balance = self.client.check_balance(self._project) if self._project else None
            return {"jobs": jobs, "history": hist_jobs, "queues": qs, "balance": balance}

        def done(res: dict) -> None:
            self.refresh_btn.setEnabled(True)
            jobs = res["jobs"]
            running = sum(1 for j in jobs if j.state == "R")
            queued = sum(1 for j in jobs if j.state in ("Q", "H"))
            finished = sum(1 for j in res["history"] if j.state in ("F", "C"))
            self.running_card.set(str(running), f"of {len(jobs)}" if jobs else "")
            self.queued_card.set(str(queued))
            self.finished_card.set(str(finished))
            if res["balance"] is not None:
                self.set_balance(res["balance"])
                self.balance_refreshed.emit(res["balance"])
            self._populate_jobs(jobs)
            # Build cluster viz data: (name, running, queued, held, capacity)
            rows = []
            def to_int(s: str) -> int:
                try: return int(s)
                except (ValueError, TypeError): return 0
            for q in res["queues"]:
                r_ = to_int(q.running); q_ = to_int(q.queued); h_ = to_int(q.held)
                cap = to_int(q.max) or (r_ + q_ + h_) or 1
                rows.append((q.name, r_, q_, h_, cap))
            self.cluster.update_data(rows)

        def err(_msg: str) -> None:
            self.refresh_btn.setEnabled(True)
            self._set_cards_placeholder()

        run_async(task, on_done=done, on_error=err)

    # ---------- helpers ----------
    def _set_cards_loading(self) -> None:
        for c in (self.running_card, self.queued_card, self.finished_card):
            c.set("…")

    def _set_cards_placeholder(self) -> None:
        for c in (self.running_card, self.queued_card, self.finished_card):
            c.set("—")

    def _populate_jobs(self, jobs) -> None:
        from .jobs_tab import STATE_LABEL, _hm_to_seconds
        self.jobs_tbl.setRowCount(0)
        # Show active first, then recent
        jobs_sorted = sorted(jobs, key=lambda j: (j.state != "R", j.state != "Q"))
        for j in jobs_sorted[:20]:
            r = self.jobs_tbl.rowCount(); self.jobs_tbl.insertRow(r)
            self.jobs_tbl.setItem(r, 0, QTableWidgetItem(j.job_id))
            self.jobs_tbl.setItem(r, 1, QTableWidgetItem(j.name))
            self.jobs_tbl.setItem(r, 2, QTableWidgetItem(j.queue))
            state_item = QTableWidgetItem(f"{j.state} · {STATE_LABEL.get(j.state, '')}")
            sc = st.STATE_COLORS.get(j.state)
            if sc:
                state_item.setForeground(QColor(sc))
            self.jobs_tbl.setItem(r, 3, state_item)

            elapsed_s = _hm_to_seconds(j.elapsed)
            walltime_s = _hm_to_seconds(j.walltime)
            pct = 0
            if walltime_s > 0:
                pct = max(0, min(100, int(100 * elapsed_s / walltime_s)))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(pct if j.state == "R" else (0 if j.state in ("Q", "H") else 100))
            bar.setFormat(f"{pct}%" if j.state == "R" else STATE_LABEL.get(j.state, j.state))
            color = st.OK if j.state == "R" else (
                st.TEXT_MUTED if j.state in ("F", "C") else st.WARN)
            bar.setStyleSheet(f"""
                QProgressBar {{
                    background: {st.BG}; border: 1px solid {st.BORDER};
                    border-radius: 4px; height: 14px; text-align: center;
                    color: {st.TEXT_DIM};
                }}
                QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}
            """)
            self.jobs_tbl.setCellWidget(r, 4, bar)
        self.jobs_tbl.resizeColumnsToContents()
        self.jobs_tbl.setColumnWidth(4, 160)

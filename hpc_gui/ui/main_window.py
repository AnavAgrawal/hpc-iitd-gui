"""Main window: menu bar, tab bar with icons, rich status bar, shortcuts."""
from __future__ import annotations

import base64
import time

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStatusBar, QStyle,
    QTabWidget, QWidget,
)

from .. import config
from ..client import HpcClient
from . import style
from .common import confirm
from .connection_tab import ConnectionTab
from .console_tab import ConsoleTab
from .dashboard_tab import DashboardTab
from .files_tab import FilesTab
from .jobs_tab import JobsTab
from .logs_tab import LogsTab
from .queues_tab import QueuesTab
from .submit_tab import SubmitTab
from .toast import BusySpinner, ToastManager


# Visible tab labels (with icon glyphs for cleanliness; icons set via QStyle).
TAB_LABELS = ["Connection", "Dashboard", "Jobs", "Queues",
              "Submit", "Files", "Logs", "Console"]
TAB_ICONS = ["SP_ComputerIcon", "SP_FileDialogDetailedView",
             "SP_BrowserReload", "SP_FileDialogListView",
             "SP_ArrowUp", "SP_DirIcon", "SP_FileIcon",
             "SP_CommandLink"]


def _b64_save(data: QByteArray) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def _b64_load(data: str) -> QByteArray:
    if not data:
        return QByteArray()
    try:
        return QByteArray(base64.b64decode(data.encode("ascii")))
    except Exception:
        return QByteArray()


class MainWindow(QMainWindow):
    def __init__(self, client: HpcClient, settings: config.Settings) -> None:
        super().__init__()
        self.client = client
        self.settings = settings

        self.setWindowTitle("HPC-IITD")
        self.resize(1360, 860)

        # Toast manager + busy spinner are installed on the window so every
        # tab can access them via common.toasts(self) / common.set_busy(self).
        self.toasts = ToastManager(self)
        self.busy = BusySpinner(self, size=14)

        # ---------- tabs ----------
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(False)

        self.connection_tab = ConnectionTab(client, settings)
        self.dashboard_tab = DashboardTab(client)
        self.jobs_tab = JobsTab(client)
        self.queues_tab = QueuesTab(client)
        self.submit_tab = SubmitTab(client, settings)
        self.files_tab = FilesTab(client, settings)
        self.logs_tab = LogsTab(client, settings)
        self.console_tab = ConsoleTab(client)

        widgets = [self.connection_tab, self.dashboard_tab, self.jobs_tab,
                   self.queues_tab, self.submit_tab, self.files_tab,
                   self.logs_tab, self.console_tab]
        for w, label, icon_name in zip(widgets, TAB_LABELS, TAB_ICONS):
            icon = self.style().standardIcon(getattr(QStyle, icon_name))
            self.tabs.addTab(w, icon, "  " + label)

        self.setCentralWidget(self.tabs)

        # ---------- menus ----------
        self._build_menus()

        # ---------- status bar ----------
        self._build_status_bar()

        # Disable everything except Connection until connected
        self._set_connected(False)

        # ---------- cross-tab signals ----------
        self.connection_tab.connected_changed.connect(self._on_connected_changed)
        self.connection_tab.balance_updated.connect(self._on_balance_updated)
        self.dashboard_tab.balance_refreshed.connect(self._on_balance_updated)
        self.dashboard_tab.go_to_jobs.connect(lambda: self.tabs.setCurrentWidget(self.jobs_tab))
        self.dashboard_tab.go_to_submit.connect(lambda: self.tabs.setCurrentWidget(self.submit_tab))
        self.dashboard_tab.go_to_files.connect(lambda: self.tabs.setCurrentWidget(self.files_tab))
        self.dashboard_tab.go_to_logs.connect(lambda: self.tabs.setCurrentWidget(self.logs_tab))
        self.jobs_tab.view_log.connect(self._open_log_tab)
        self.jobs_tab.tail_log.connect(self._open_log_tab)
        self.jobs_tab.job_counts_changed.connect(self._update_job_counts)
        self.files_tab.open_log.connect(self._open_log_tab)
        self.files_tab.path_changed.connect(self._update_status_path)
        self.submit_tab.submitted.connect(self._on_submitted)

        # ---------- session tick ----------
        self._session_started: float | None = None
        self._tick = QTimer(self); self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_status)
        self._tick.start()

        # Restore window / splitter / tab state
        self._restore_state()

        # Auto-connect if configured
        if settings.auto_connect and settings.username:
            QTimer.singleShot(400, self.connection_tab._do_connect)

    # ---------- building ----------
    def _build_menus(self) -> None:
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        self.act_connect = QAction("&Connect", self, shortcut="Ctrl+K")
        self.act_connect.triggered.connect(self.connection_tab._do_connect)
        self.act_disconnect = QAction("&Disconnect", self, shortcut="Ctrl+Shift+K")
        self.act_disconnect.triggered.connect(self.connection_tab._do_disconnect)
        file_menu.addAction(self.act_connect)
        file_menu.addAction(self.act_disconnect)
        file_menu.addSeparator()
        act_save = QAction("&Save settings", self, shortcut="Ctrl+S")
        act_save.triggered.connect(self.connection_tab._save_settings)
        file_menu.addAction(act_save)
        file_menu.addSeparator()
        act_quit = QAction("E&xit", self, shortcut="Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = mb.addMenu("&View")
        for i, label in enumerate(TAB_LABELS):
            a = QAction(f"&{i+1}  {label}", self, shortcut=f"Ctrl+{i+1}")
            a.triggered.connect(lambda _=False, idx=i: self.tabs.setCurrentIndex(idx))
            view_menu.addAction(a)
        view_menu.addSeparator()
        act_refresh = QAction("Refresh &current tab", self, shortcut="F5")
        act_refresh.triggered.connect(self._refresh_current)
        view_menu.addAction(act_refresh)
        act_refresh_all = QAction("Refresh &all", self, shortcut="Ctrl+F5")
        act_refresh_all.triggered.connect(self._refresh_all)
        view_menu.addAction(act_refresh_all)

        help_menu = mb.addMenu("&Help")
        act_about = QAction("&About", self); act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)
        act_short = QAction("&Keyboard shortcuts", self, shortcut="F1")
        act_short.triggered.connect(self._show_shortcuts)
        help_menu.addAction(act_short)

    def _build_status_bar(self) -> None:
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        # Left-aligned widgets: connection state + session timer
        left = QWidget(); lh = QHBoxLayout(left)
        lh.setContentsMargins(0, 0, 0, 0); lh.setSpacing(8)
        self.led = QLabel("●")
        self.led.setStyleSheet(f"color: {style.ERR}; font-size: 14pt;")
        lh.addWidget(self.led)
        self.sb_conn = QLabel("disconnected")
        lh.addWidget(self.sb_conn)
        lh.addWidget(self._sep())
        self.sb_session = QLabel("")
        lh.addWidget(self.sb_session)
        self.status.addWidget(left, 1)

        # Right-side items: path • balance • jobs • busy
        self.sb_path = QLabel(""); self.sb_path.setStyleSheet(f"color: {style.TEXT_MUTED};")
        self.status.addPermanentWidget(self.sb_path)
        self.status.addPermanentWidget(self._sep())
        self.sb_balance = QLabel("₹ —")
        self.sb_balance.setToolTip("Project balance (₹).  Click 'Refresh' on the Dashboard to update.")
        self.status.addPermanentWidget(self.sb_balance)
        self.status.addPermanentWidget(self._sep())
        self.sb_jobs = QLabel("jobs: —")
        self.status.addPermanentWidget(self.sb_jobs)
        self.status.addPermanentWidget(self._sep())
        self.sb_refresh = QLabel("")
        self.sb_refresh.setStyleSheet(f"color: {style.TEXT_MUTED};")
        self.status.addPermanentWidget(self.sb_refresh)
        # Busy spinner
        self.status.addPermanentWidget(self.busy)

    def _sep(self) -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.VLine)
        f.setStyleSheet(f"color: {style.BORDER}; max-width: 1px;")
        return f

    # ---------- state persistence ----------
    def _restore_state(self) -> None:
        geom = _b64_load(self.settings.window_geometry)
        if not geom.isEmpty():
            self.restoreGeometry(geom)
        state = _b64_load(self.settings.window_state)
        if not state.isEmpty():
            self.restoreState(state)
        idx = self.settings.last_tab
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)

    def _save_state_to_settings(self) -> None:
        self.settings.window_geometry = _b64_save(self.saveGeometry())
        self.settings.window_state = _b64_save(self.saveState())
        self.settings.last_tab = self.tabs.currentIndex()

    # ---------- cross-tab handlers ----------
    def _open_log_tab(self, remote_path: str) -> None:
        self.logs_tab.open_path(remote_path)
        self.tabs.setCurrentWidget(self.logs_tab)

    def _update_status_path(self, path: str) -> None:
        self.sb_path.setText(path or "")

    def _on_balance_updated(self, balance) -> None:
        self.dashboard_tab.set_balance(balance)
        self.submit_tab.set_balance(balance)
        if balance is None:
            self.sb_balance.setText("₹ —")
        else:
            self.sb_balance.setText(f"₹ {balance:,.2f}")

    def _update_job_counts(self, counts: dict) -> None:
        r = counts.get("running", 0); q = counts.get("queued", 0); h = counts.get("held", 0)
        parts = []
        if r: parts.append(f"<span style='color:{style.OK}'>{r}R</span>")
        if q: parts.append(f"<span style='color:{style.WARN}'>{q}Q</span>")
        if h: parts.append(f"<span style='color:{style.TEXT_DIM}'>{h}H</span>")
        self.sb_jobs.setText("jobs: " + (" ".join(parts) if parts else "0"))
        self.sb_refresh.setText("updated " + time.strftime("%H:%M:%S"))

    def _on_submitted(self, job_id: str) -> None:
        self.toasts.success(f"Job {job_id} submitted.", title="Submitted")
        self.tabs.setCurrentWidget(self.jobs_tab)
        self.jobs_tab.refresh()

    def _refresh_current(self) -> None:
        w = self.tabs.currentWidget()
        for name in ("refresh", "_fetch", "refresh_now"):
            fn = getattr(w, name, None)
            if callable(fn):
                try:
                    fn(); return
                except TypeError:
                    pass

    def _refresh_all(self) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not self.tabs.isTabEnabled(i):
                continue
            fn = getattr(w, "refresh", None)
            if callable(fn):
                try:
                    fn()
                except TypeError:
                    pass

    def _set_connected(self, connected: bool) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is self.connection_tab:
                continue
            self.tabs.setTabEnabled(i, connected)
        self.act_disconnect.setEnabled(connected)
        self.act_connect.setEnabled(not connected)
        if connected:
            self.sb_conn.setText(f"{self.client.username}@{self.client.host}")
            self.led.setStyleSheet(f"color: {style.OK}; font-size: 14pt;")
            self._session_started = time.time()
        else:
            self.sb_conn.setText("disconnected")
            self.led.setStyleSheet(f"color: {style.ERR}; font-size: 14pt;")
            self._session_started = None
            self.sb_session.setText("")
            self.sb_balance.setText("₹ —")
            self.sb_jobs.setText("jobs: —")
            self.sb_refresh.setText("")

    def _on_connected_changed(self, connected: bool) -> None:
        self._set_connected(connected)
        if connected:
            self.toasts.success(
                f"Connected to {self.client.host} as {self.client.username}",
                title="HPC")
            self.dashboard_tab.set_identity(
                self.client.username,
                self.connection_tab.project_box.currentText().strip(),
            )
            self.dashboard_tab.refresh()
            self.jobs_tab.refresh()
            self.queues_tab.refresh()
            self.files_tab._goto(self.settings.remote_workdir or "~/scratch")
            # Restore last tab, or default to dashboard if coming from connection.
            if self.tabs.currentWidget() is self.connection_tab:
                tgt = self.settings.last_tab if self.settings.last_tab else 1
                if 0 <= tgt < self.tabs.count() and self.tabs.isTabEnabled(tgt):
                    self.tabs.setCurrentIndex(tgt)
                else:
                    self.tabs.setCurrentWidget(self.dashboard_tab)
        else:
            self.toasts.info("Disconnected.", title="HPC")
            self.dashboard_tab.set_identity("", "")
            self.dashboard_tab.set_balance(None)
            self.tabs.setCurrentWidget(self.connection_tab)

    def _tick_status(self) -> None:
        if self._session_started is None:
            return
        secs = int(time.time() - self._session_started)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        self.sb_session.setText(f"session {h:d}h {m:02d}m {s:02d}s" if h else f"session {m:d}m {s:02d}s")

    # ---------- about / help ----------
    def _show_about(self) -> None:
        QMessageBox.about(self, "HPC-IITD GUI",
            "<h3>HPC-IITD GUI</h3>"
            "<p>Desktop client for the IITD High Performance Computing cluster.</p>"
            "<p>Built with PySide6 + paramiko. Credentials stored in Windows Credential Manager.</p>"
            "<p style='color:#8b99a8'>Cluster: <code>hpc.iitd.ac.in</code>"
            " · Scheduler: PBS Pro"
            " · Budgets: <code>amgr</code></p>")

    def _show_shortcuts(self) -> None:
        txt = """
        <h3>Keyboard shortcuts</h3>
        <table cellpadding='4'>
        <tr><td><b>Ctrl+K</b></td><td>Connect</td></tr>
        <tr><td><b>Ctrl+Shift+K</b></td><td>Disconnect</td></tr>
        <tr><td><b>Ctrl+S</b></td><td>Save settings</td></tr>
        <tr><td><b>Ctrl+1&hellip;8</b></td><td>Switch to tab</td></tr>
        <tr><td><b>F5</b></td><td>Refresh current tab</td></tr>
        <tr><td><b>Ctrl+F5</b></td><td>Refresh every tab</td></tr>
        <tr><td><b>F1</b></td><td>This cheat sheet</td></tr>
        <tr><td><b>Ctrl+Q</b></td><td>Quit</td></tr>
        </table>
        <p style='color:#8b99a8'>Hint: right-click on jobs and files rows for context menus.</p>
        """
        QMessageBox.information(self, "Shortcuts", txt)

    # ---------- close ----------
    def closeEvent(self, event) -> None:
        # Save state first — always.
        self._save_state_to_settings()
        config.save_settings(self.settings)
        try:
            self.client.close()
        except Exception:
            pass
        return super().closeEvent(event)

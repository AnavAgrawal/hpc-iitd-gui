"""Files tab: SFTP browser with breadcrumbs, drag-drop upload, and batch ops."""
from __future__ import annotations

import datetime as _dt
import os
import posixpath

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMenu, QProgressBar, QPushButton, QSizePolicy,
    QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from .. import config
from ..client import HpcClient
from ..workers import Worker, pool, run_async
from . import style as st
from .common import confirm, error_box, plain_log, toasts


# ---------- helpers ----------
def _human_size(n: int) -> str:
    f = float(n)
    for u in ["B", "K", "M", "G", "T"]:
        if f < 1024:
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return f"{f:.1f}P"


def _human_time(ts: int) -> str:
    if not ts: return ""
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _icon_for(name: str, is_dir: bool) -> str:
    if is_dir:
        return "📁"
    n = name.lower()
    if n.endswith((".pbs", ".sh", ".bash", ".zsh")): return "▶"
    if n.endswith((".py", ".ipynb")): return "🐍"
    if n.endswith((".out", ".err", ".log")): return "📜"
    if n.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")): return "🖼"
    if n.endswith((".tar", ".gz", ".zip", ".bz2", ".xz")): return "🗜"
    if n.endswith((".pdf",)): return "📑"
    if n.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")): return "⚙"
    if n.endswith((".csv", ".tsv", ".parquet")): return "🗂"
    if n.endswith((".txt", ".md")): return "📝"
    return "📄"


class Breadcrumb(QWidget):
    """Clickable breadcrumb: /  home  scratch  project"""

    clicked = Signal(str)  # full path

    def __init__(self) -> None:
        super().__init__()
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(2)
        self._buttons: list[QToolButton] = []

    def set_path(self, full_path: str, home: str) -> None:
        # Tear down previous buttons
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w: w.deleteLater()
        self._buttons.clear()

        parts: list[tuple[str, str]] = []  # (label, full_path)
        if home and (full_path == home or full_path.startswith(home + "/")):
            parts.append(("~", home))
            rest = full_path[len(home):].lstrip("/")
            acc = home
            if rest:
                for seg in rest.split("/"):
                    if not seg: continue
                    acc = acc + "/" + seg
                    parts.append((seg, acc))
        else:
            parts.append(("/", "/"))
            acc = ""
            for seg in full_path.strip("/").split("/"):
                if not seg: continue
                acc = acc + "/" + seg
                parts.append((seg, acc))

        for i, (label, p) in enumerate(parts):
            btn = QToolButton()
            btn.setText(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAutoRaise(True)
            btn.setStyleSheet(
                f"QToolButton{{color:{st.TEXT}; background:transparent; padding:3px 8px; border-radius:4px;}}"
                f"QToolButton:hover{{background:{st.SURFACE_HI};}}")
            btn.clicked.connect(lambda _=False, path=p: self.clicked.emit(path))
            self._lay.addWidget(btn)
            self._buttons.append(btn)
            if i < len(parts) - 1:
                sep = QLabel("/")
                sep.setStyleSheet(f"color:{st.TEXT_MUTED}; padding: 0 2px;")
                self._lay.addWidget(sep)
        self._lay.addStretch(1)


class FilesTab(QWidget):
    open_log = Signal(str)
    path_changed = Signal(str)

    def __init__(self, client: HpcClient, settings: config.Settings) -> None:
        super().__init__()
        self.client = client
        self.settings = settings
        self._current_dir = "~"
        self._current_worker: Worker | None = None

        self.setAcceptDrops(True)

        # ---------- path bar ----------
        self.path_combo = QComboBox(); self.path_combo.setEditable(True)
        self.path_combo.lineEdit().setPlaceholderText("remote path, e.g. ~/scratch/project")
        for p in settings.recent_remote_dirs or ["~", "~/scratch"]:
            self.path_combo.addItem(p)
        self.go_btn = QPushButton("Go")
        self.up_btn = QPushButton("↑ Up")
        self.home_btn = QPushButton("~ Home")
        self.scratch_btn = QPushButton("~/scratch")
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setToolTip("Refresh listing (F5)")

        top = QHBoxLayout()
        top.addWidget(QLabel("Path:"))
        top.addWidget(self.path_combo, 1)
        top.addWidget(self.go_btn)
        top.addWidget(self.up_btn)
        top.addWidget(self.home_btn)
        top.addWidget(self.scratch_btn)
        top.addWidget(self.refresh_btn)

        # ---------- breadcrumb ----------
        self.crumb = Breadcrumb()
        self.crumb.clicked.connect(self._goto)

        # ---------- table ----------
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Modified", "Type"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemDoubleClicked.connect(self._on_double_click)

        # ---------- actions ----------
        self.upload_file_btn = QPushButton("Upload file…")
        self.upload_dir_btn = QPushButton("Upload folder…")
        self.download_btn = QPushButton("Download…")
        self.mkdir_btn = QPushButton("New folder")
        self.rename_btn = QPushButton("Rename")
        self.delete_btn = QPushButton("Delete"); self.delete_btn.setProperty("role", "danger")
        self.view_btn = QPushButton("View")
        self.cancel_btn = QPushButton("Cancel"); self.cancel_btn.setEnabled(False)

        action_row = QHBoxLayout()
        for b in [self.upload_file_btn, self.upload_dir_btn, self.download_btn,
                  self.mkdir_btn, self.rename_btn, self.view_btn, self.delete_btn]:
            action_row.addWidget(b)
        action_row.addStretch(1)
        action_row.addWidget(self.cancel_btn)

        # ---------- progress + log ----------
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setVisible(False)
        self.log = plain_log(); self.log.setMaximumBlockCount(1000); self.log.setMaximumHeight(180)
        log_hdr = QLabel("Transfer log"); log_hdr.setStyleSheet(f"color:{st.TEXT_DIM};")

        hint = QLabel("Drop files here to upload to the current directory.")
        hint.setStyleSheet(f"color:{st.TEXT_MUTED}; padding:4px;")

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addWidget(self.crumb)
        root.addWidget(self.table, 3)
        root.addLayout(action_row)
        root.addWidget(self.progress)
        root.addWidget(hint)
        root.addWidget(log_hdr)
        root.addWidget(self.log, 1)

        # Signals
        self.go_btn.clicked.connect(self._go)
        self.path_combo.lineEdit().returnPressed.connect(self._go)
        self.up_btn.clicked.connect(self._up)
        self.home_btn.clicked.connect(lambda: self._goto("~"))
        self.scratch_btn.clicked.connect(lambda: self._goto("~/scratch"))
        self.refresh_btn.clicked.connect(lambda: self._goto(self._current_dir))
        self.upload_file_btn.clicked.connect(self._upload_file)
        self.upload_dir_btn.clicked.connect(self._upload_folder)
        self.download_btn.clicked.connect(self._download_selected)
        self.mkdir_btn.clicked.connect(self._mkdir)
        self.rename_btn.clicked.connect(self._rename)
        self.delete_btn.clicked.connect(self._delete)
        self.view_btn.clicked.connect(self._view)
        self.cancel_btn.clicked.connect(self._cancel)

    # ---------- navigation ----------
    def refresh(self) -> None:
        self._goto(self._current_dir)

    def _goto(self, path: str) -> None:
        self._current_dir = path
        self.path_combo.lineEdit().setText(path)
        if not self.client.connected:
            return
        def task(): return (self.client.expand(path), self.client.listdir(path))
        def done(data):
            expanded, entries = data
            self._populate(entries)
            self.crumb.set_path(expanded, self.client.home)
            self.path_changed.emit(expanded)
            self.settings.recent_remote_dirs = config.push_recent(
                self.settings.recent_remote_dirs, path)
            config.save_settings(self.settings)
        def err(msg):
            self._log(f"[err] {msg.splitlines()[0]}")
            toasts(self).error(msg.splitlines()[0], title="Listing failed")
        run_async(task, on_done=done, on_error=err)

    def _populate(self, entries) -> None:
        self.table.setRowCount(0)
        for name, size, is_dir, mtime in entries:
            r = self.table.rowCount(); self.table.insertRow(r)
            items = [
                QTableWidgetItem(f"{_icon_for(name, is_dir)}  {name}"),
                QTableWidgetItem("" if is_dir else _human_size(size)),
                QTableWidgetItem(_human_time(int(mtime))),
                QTableWidgetItem("dir" if is_dir else "file"),
            ]
            if is_dir:
                items[0].setData(Qt.UserRole, ("dir", name))
            else:
                items[0].setData(Qt.UserRole, ("file", name))
            for c, it in enumerate(items):
                self.table.setItem(r, c, it)
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(3)

    def _go(self) -> None:
        self._goto(self.path_combo.currentText().strip())

    def _up(self) -> None:
        cur = self._current_dir
        if cur in ("~", "/"):
            return
        parent = posixpath.dirname(self.client.expand(cur)) or "/"
        self._goto(parent)

    def _current_selection(self) -> list[tuple[str, bool]]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            item = self.table.item(r, 0)
            if not item: continue
            meta = item.data(Qt.UserRole)
            if isinstance(meta, tuple):
                out.append((meta[1], meta[0] == "dir"))
        return out

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        r = item.row()
        name_item = self.table.item(r, 0)
        if not name_item: return
        meta = name_item.data(Qt.UserRole)
        if not isinstance(meta, tuple): return
        kind, name = meta
        full = posixpath.join(self.client.expand(self._current_dir), name)
        if kind == "dir":
            self._goto(full)
        else:
            self.open_log.emit(full)

    # ---------- context menu ----------
    def _show_context_menu(self, pos) -> None:
        sel = self._current_selection()
        if not sel:
            return
        menu = QMenu(self)
        if len(sel) == 1 and not sel[0][1]:
            menu.addAction("View", self._view)
        if len(sel) == 1:
            menu.addAction("Rename…", self._rename)
        menu.addAction("Download…", self._download_selected)
        menu.addSeparator()
        menu.addAction("Copy absolute path", self._copy_path)
        menu.addSeparator()
        a_del = menu.addAction("Delete"); a_del.triggered.connect(self._delete)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_path(self) -> None:
        sel = self._current_selection()
        if not sel: return
        paths = [posixpath.join(self.client.expand(self._current_dir), n) for n, _ in sel]
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(paths))
        toasts(self).info(f"Copied {len(paths)} path(s).", title="Clipboard")

    # ---------- drag & drop ----------
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        if not self.client.connected:
            toasts(self).warn("Connect first."); return
        urls = e.mimeData().urls()
        locals_ = [u.toLocalFile() for u in urls if u.toLocalFile()]
        if not locals_:
            return
        files = [p for p in locals_ if os.path.isfile(p)]
        dirs = [p for p in locals_ if os.path.isdir(p)]
        remote_dir = self._current_dir
        if not confirm(self, "Upload",
                       f"Upload {len(files)} file(s) and {len(dirs)} folder(s)\n"
                       f"into {self.client.expand(remote_dir)} ?"):
            return

        def task(progress, log, should_cancel):
            uploaded = skipped = 0
            # Files first
            for lp in files:
                if should_cancel(): break
                name = os.path.basename(lp)
                rp = posixpath.join(self.client.expand(remote_dir), name)
                log(f"[put] {lp} -> {rp}")
                def cb(done, tot, nm=name): progress({"done": done, "total": tot, "name": nm})
                self.client.put_file(lp, rp, progress=cb)
                uploaded += 1
            # Then folders
            for dp in dirs:
                if should_cancel(): break
                target = posixpath.join(self.client.expand(remote_dir),
                                         os.path.basename(dp.rstrip("/\\")))
                def prog(done, total, name):
                    progress({"done": done, "total": total, "name": name})
                d, s = self.client.upload_tree(dp, target, log=log, progress=prog,
                                               should_cancel=should_cancel)
                uploaded += d; skipped += s
            return {"uploaded": uploaded, "skipped": skipped}

        self._run_fs_op("Drop upload", task)

    # ---------- ops ----------
    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _upload_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to upload")
        if not paths: return
        remote_dir = self._current_dir

        def task(progress, log, should_cancel):
            uploaded = 0
            for lp in paths:
                if should_cancel(): break
                name = os.path.basename(lp)
                rp = posixpath.join(self.client.expand(remote_dir), name)
                log(f"[put] {lp} -> {rp}")
                def cb(done, tot, nm=name): progress({"done": done, "total": tot, "name": nm})
                self.client.put_file(lp, rp, progress=cb)
                uploaded += 1
            return {"uploaded": uploaded, "total": len(paths)}

        self._run_fs_op("Upload files", task)

    def _upload_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select folder to upload")
        if not path: return
        remote_dir = posixpath.join(self.client.expand(self._current_dir),
                                    os.path.basename(path.rstrip("/\\")))
        if not confirm(self, "Upload folder",
                       f"Upload\n{path}\n→\n{remote_dir}\n(recursive; skips same-size files)"):
            return
        def task(progress, log, should_cancel):
            def prog(done, total, name):
                progress({"done": done, "total": total, "name": name})
            return self.client.upload_tree(path, remote_dir,
                                           log=log, progress=prog,
                                           should_cancel=should_cancel)
        self._run_fs_op("Upload folder", task)

    def _download_selected(self) -> None:
        sel = self._current_selection()
        if not sel:
            toasts(self).warn("Nothing selected."); return
        target_dir = QFileDialog.getExistingDirectory(self, "Download into…")
        if not target_dir: return
        remote_dir = self.client.expand(self._current_dir)
        def task(progress, log, should_cancel):
            total_done = 0; total_skip = 0
            for name, is_dir in sel:
                if should_cancel(): break
                rp = posixpath.join(remote_dir, name)
                lp = os.path.join(target_dir, name)
                if is_dir:
                    def prog(done, total, nm):
                        progress({"done": done, "total": total, "name": nm})
                    d, s = self.client.download_tree(rp, lp, log=log, progress=prog,
                                                      should_cancel=should_cancel)
                    total_done += d; total_skip += s
                else:
                    log(f"[get] {rp}")
                    self.client.get_file(rp, lp)
                    total_done += 1
            return (total_done, total_skip)
        self._run_fs_op("Download", task)

    def _mkdir(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip(): return
        full = posixpath.join(self.client.expand(self._current_dir), name.strip())
        def task(): self.client.mkdirs(full); return full
        run_async(task,
                  on_done=lambda r: (self._log(f"[ok] created {r}"), self.refresh()),
                  on_error=lambda m: toasts(self).error(m, title="mkdir"))

    def _rename(self) -> None:
        sel = self._current_selection()
        if len(sel) != 1:
            toasts(self).warn("Select exactly one entry to rename."); return
        old_name = sel[0][0]
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old_name)
        if not ok or not new_name.strip() or new_name == old_name: return
        base = self.client.expand(self._current_dir)
        old_full = posixpath.join(base, old_name)
        new_full = posixpath.join(base, new_name.strip())
        def task(): self.client.rename(old_full, new_full); return new_full
        run_async(task,
                  on_done=lambda _: (self.refresh(), toasts(self).success("Renamed.")),
                  on_error=lambda m: toasts(self).error(m, title="rename"))

    def _delete(self) -> None:
        sel = self._current_selection()
        if not sel: return
        msg = "Delete:\n" + "\n".join(f"  {'(dir) ' if d else ''}{n}" for n, d in sel)
        if not confirm(self, "Delete", msg + "\n\nThis cannot be undone."):
            return
        base = self.client.expand(self._current_dir)
        def task(log):
            for name, _ in sel:
                full = posixpath.join(base, name)
                log(f"[rm ] {full}")
                self.client.remove(full)
        run_async(task,
                  on_done=lambda _: (self.refresh(), toasts(self).success(f"Deleted {len(sel)} item(s).")),
                  on_log=self._log,
                  on_error=lambda m: toasts(self).error(m, title="delete"))

    def _view(self) -> None:
        sel = self._current_selection()
        if len(sel) != 1 or sel[0][1]:
            toasts(self).warn("Pick a single file."); return
        full = posixpath.join(self.client.expand(self._current_dir), sel[0][0])
        self.open_log.emit(full)

    # ---------- long-running op helpers ----------
    def _run_fs_op(self, label: str, task) -> None:
        if not self.client.connected:
            toasts(self).error("Connect first."); return
        self._log(f"[..] {label}")
        self.progress.setValue(0); self.progress.setVisible(True)
        self.cancel_btn.setEnabled(True)
        w = Worker(task)
        self._current_worker = w
        w.signals.log.connect(self._log)
        w.signals.progress.connect(self._on_progress)
        w.signals.finished.connect(lambda r: self._op_done(label, r))
        w.signals.error.connect(lambda m: self._op_err(label, m))
        pool().start(w)

    def _on_progress(self, payload) -> None:
        if isinstance(payload, dict):
            done = payload.get("done", 0); total = payload.get("total", 0) or 1
            pct = int(100 * done / total) if total else 0
            self.progress.setValue(pct)
            if "name" in payload:
                self.progress.setFormat(f"%p%  {payload['name']}")
        elif isinstance(payload, (list, tuple)) and len(payload) == 2:
            done, total = payload
            pct = int(100 * done / (total or 1))
            self.progress.setValue(pct)

    def _op_done(self, label: str, result) -> None:
        self._log(f"[ok] {label}: {result}")
        self.cancel_btn.setEnabled(False)
        self.progress.setValue(100)
        self._current_worker = None
        toasts(self).success(str(result), title=label)
        self.refresh()

    def _op_err(self, label: str, msg: str) -> None:
        self._log(f"[err] {label}: {msg.splitlines()[0]}")
        self.cancel_btn.setEnabled(False)
        self._current_worker = None
        toasts(self).error(msg.splitlines()[0], title=label)

    def _cancel(self) -> None:
        if self._current_worker is not None:
            self._current_worker.cancel()
            self._log("[..] cancelling…")

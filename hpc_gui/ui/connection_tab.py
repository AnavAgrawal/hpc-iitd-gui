"""Connection tab: credentials + Connect/Disconnect + saved password."""
from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .. import config
from ..client import HpcClient
from ..workers import run_async
from . import style as st
from .common import error_box, plain_log, toasts


STATUS_STYLES = {
    "disconnected": f"background:{st.ERR}; color:white; padding:4px 14px; border-radius:12px; font-weight:600;",
    "connecting":   f"background:{st.WARN}; color:#1d1300; padding:4px 14px; border-radius:12px; font-weight:600;",
    "connected":    f"background:{st.OK}; color:white; padding:4px 14px; border-radius:12px; font-weight:600;",
}


class ConnectionTab(QWidget):
    connected_changed = Signal(bool)
    balance_updated = Signal(object)   # float or None
    projects_updated = Signal(list)

    def __init__(self, client: HpcClient, settings: config.Settings) -> None:
        super().__init__()
        self.client = client
        self.settings = settings

        # Welcome header
        title = QLabel("Connect to IITD HPC")
        title.setStyleSheet(f"color:{st.TEXT}; font-size:18pt; font-weight:700;")
        sub = QLabel(
            "Enter your Kerberos credentials. Passwords are stored in Windows "
            "Credential Manager via Python <code>keyring</code>."
        )
        sub.setStyleSheet(f"color:{st.TEXT_DIM};")
        sub.setTextFormat(Qt.RichText)

        # Profile selector. Lets the user switch between saved logins (e.g.
        # their own and a friend's) without re-typing every field.
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(220)
        self.save_profile_btn = QPushButton("Save as profile…")
        self.save_profile_btn.setProperty("role", "ghost")
        self.delete_profile_btn = QPushButton("Delete profile")
        self.delete_profile_btn.setProperty("role", "ghost")
        prof_box = QGroupBox("Profile")
        prof_l = QHBoxLayout(prof_box)
        prof_l.setContentsMargins(16, 18, 16, 14)
        prof_l.setSpacing(8)
        prof_l.addWidget(QLabel("Login as:"))
        prof_l.addWidget(self.profile_box, 1)
        prof_l.addWidget(self.save_profile_btn)
        prof_l.addWidget(self.delete_profile_btn)
        # Suppress profile change side-effects while we initialise the combo.
        self._loading_profile = True

        # Fields
        self.host_edit = QLineEdit(settings.host)
        self.user_edit = QLineEdit(settings.username)
        self.user_edit.setPlaceholderText("e.g. mtXXXXXXX")
        self.pass_edit = QLineEdit(); self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("Kerberos password")
        self.show_pw_cb = QCheckBox("Show")
        self.show_pw_cb.toggled.connect(
            lambda on: self.pass_edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        pw_row = QWidget(); pw_l = QHBoxLayout(pw_row); pw_l.setContentsMargins(0, 0, 0, 0)
        pw_l.addWidget(self.pass_edit, 1); pw_l.addWidget(self.show_pw_cb)

        self.project_box = QComboBox(); self.project_box.setEditable(True)
        if settings.project:
            self.project_box.addItem(settings.project)
            self.project_box.setCurrentText(settings.project)
        self.workdir_edit = QLineEdit(settings.remote_workdir or "~/scratch")
        self.email_edit = QLineEdit(settings.email)
        self.email_edit.setPlaceholderText("optional — for PBS mail -M")

        self.save_cb = QCheckBox("Save password in Windows Credential Manager")
        self.save_cb.setChecked(settings.save_password)
        self.auto_cb = QCheckBox("Auto-connect on startup")
        self.auto_cb.setChecked(settings.auto_connect)

        self.connect_btn = QPushButton("Connect"); self.connect_btn.setDefault(True)
        self.connect_btn.setShortcut("Return")
        self.connect_btn.setProperty("role", "primary")
        self.disconnect_btn = QPushButton("Disconnect"); self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.setProperty("role", "danger")
        self.save_btn = QPushButton("Save settings")
        self.save_btn.setProperty("role", "ghost")

        self.status_label = QLabel("disconnected")
        self.status_label.setStyleSheet(STATUS_STYLES["disconnected"])

        # Form layout
        form_box = QGroupBox("Credentials")
        form = QFormLayout(form_box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setContentsMargins(16, 22, 16, 16)
        form.setVerticalSpacing(10)
        form.setHorizontalSpacing(12)
        form.addRow("Host", self.host_edit)
        form.addRow("User ID", self.user_edit)
        form.addRow("Password", pw_row)
        form.addRow("Project", self.project_box)
        form.addRow("Remote working dir", self.workdir_edit)
        form.addRow("Email", self.email_edit)
        form.addRow(self.save_cb)
        form.addRow(self.auto_cb)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.disconnect_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(QLabel("Status:"))
        btn_row.addWidget(self.status_label)

        # Log
        self.log = plain_log()
        self.log.setMaximumBlockCount(2000)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.addWidget(title)
        root.addWidget(sub)
        root.addSpacing(8)
        root.addWidget(prof_box)
        root.addWidget(form_box)
        root.addLayout(btn_row)
        root.addWidget(QLabel("Log:"))
        root.addWidget(self.log, 1)

        # Signals
        self.connect_btn.clicked.connect(self._do_connect)
        self.disconnect_btn.clicked.connect(self._do_disconnect)
        self.save_btn.clicked.connect(self._save_settings)
        self.save_profile_btn.clicked.connect(self._on_save_profile)
        self.delete_profile_btn.clicked.connect(self._on_delete_profile)
        self.profile_box.currentIndexChanged.connect(self._on_profile_changed)

        self._refresh_profile_box()
        self._loading_profile = False

        self._load_saved_password()
        self.user_edit.editingFinished.connect(self._load_saved_password)
        self.host_edit.editingFinished.connect(self._load_saved_password)

    # ---------- helpers ----------
    def _set_status(self, state: str, text: str | None = None) -> None:
        self.status_label.setStyleSheet(STATUS_STYLES.get(state, STATUS_STYLES["disconnected"]))
        self.status_label.setText(text or state)

    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    def _load_saved_password(self) -> None:
        host = self.host_edit.text().strip()
        user = self.user_edit.text().strip()
        if host and user and not self.pass_edit.text():
            pw = config.load_password(host, user)
            if pw:
                self.pass_edit.setText(pw)
                self._append_log(f"[ok] loaded saved password for {user}@{host}")

    def _save_settings(self) -> None:
        self._save_settings_silent()
        if self.save_cb.isChecked() and self.pass_edit.text():
            ok = config.save_password(self.settings.host, self.settings.username,
                                      self.pass_edit.text())
            self._append_log("[ok] credentials saved" if ok else "[warn] keyring save failed")
        elif not self.save_cb.isChecked():
            config.delete_password(self.settings.host, self.settings.username)
            self._append_log("[ok] removed saved password (if any)")
        self._append_log("[ok] settings saved")
        toasts(self).success("Settings saved.")

    def _save_settings_silent(self) -> None:
        self.settings.host = self.host_edit.text().strip()
        self.settings.username = self.user_edit.text().strip()
        self.settings.project = self.project_box.currentText().strip()
        self.settings.remote_workdir = self.workdir_edit.text().strip()
        self.settings.email = self.email_edit.text().strip()
        self.settings.save_password = self.save_cb.isChecked()
        self.settings.auto_connect = self.auto_cb.isChecked()
        config.save_settings(self.settings)

    # ---------- actions ----------
    def _do_connect(self) -> None:
        host = self.host_edit.text().strip()
        user = self.user_edit.text().strip()
        pw = self.pass_edit.text()
        if not (host and user and pw):
            toasts(self).warn("Host, User and Password are required.", title="Missing info")
            return
        self._set_status("connecting")
        self.connect_btn.setEnabled(False)
        self._append_log(f"[..] connecting to {user}@{host}")

        def task() -> dict:
            self.client.connect(host, user, pw)
            amgr = self.client.amgr_login(pw)
            projects: list[str] = []
            balance = None
            if amgr.ok:
                projects = self.client.list_projects()
                proj = self.project_box.currentText().strip()
                if not proj and projects:
                    proj = projects[0]
                if proj:
                    balance = self.client.check_balance(proj)
            return {"amgr_ok": amgr.ok, "amgr_out": amgr.output,
                    "projects": projects, "balance": balance}

        def done(result: dict) -> None:
            self._append_log(f"[ok] connected. home={self.client.home}")
            if result["amgr_ok"]:
                self._append_log(f"[ok] amgr login successful; projects={result['projects']}")
                self.projects_updated.emit(result["projects"])
                if result["projects"]:
                    cur = self.project_box.currentText().strip()
                    self.project_box.clear()
                    self.project_box.addItems(result["projects"])
                    if cur and cur in result["projects"]:
                        self.project_box.setCurrentText(cur)
                if result["balance"] is not None:
                    self._append_log(f"[ok] balance = Rs.{result['balance']}")
                    self.balance_updated.emit(result["balance"])
            else:
                self._append_log(f"[warn] amgr login failed: {result['amgr_out'][:200]}")
            self._set_status("connected", f"connected as {user}")
            self.disconnect_btn.setEnabled(True)
            if self.save_cb.isChecked():
                config.save_password(host, user, pw)
            self._save_settings_silent()
            self.connected_changed.emit(True)

        def err(msg: str) -> None:
            self._append_log(f"[err] {msg.splitlines()[0]}")
            toasts(self).error(msg.splitlines()[0], title="Connection failed")
            self._set_status("disconnected")
            self.connect_btn.setEnabled(True)

        run_async(task, on_done=done, on_error=err)

    def _do_disconnect(self) -> None:
        self.client.close()
        self._set_status("disconnected")
        self._append_log("[ok] disconnected")
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.connected_changed.emit(False)

    # ---------- profiles ----------
    def _refresh_profile_box(self) -> None:
        """Rebuild the profile dropdown from settings.profiles."""
        self._loading_profile = True
        try:
            self.profile_box.clear()
            for p in self.settings.profiles:
                if isinstance(p, dict) and p.get("name"):
                    self.profile_box.addItem(p["name"])
            self.profile_box.addItem("— Custom —")
            active = self.settings.active_profile
            if active and config.find_profile(self.settings.profiles, active):
                self.profile_box.setCurrentText(active)
            else:
                self.profile_box.setCurrentIndex(self.profile_box.count() - 1)
        finally:
            self._loading_profile = False

    def _on_profile_changed(self, _idx: int) -> None:
        if self._loading_profile:
            return
        name = self.profile_box.currentText()
        if not name or name == "— Custom —":
            self.settings.active_profile = ""
            return
        prof = config.find_profile(self.settings.profiles, name)
        if not prof:
            return
        self.host_edit.setText(prof.get("host", ""))
        self.user_edit.setText(prof.get("username", ""))
        self.pass_edit.clear()
        proj = prof.get("project", "")
        self.project_box.setCurrentText(proj)
        self.workdir_edit.setText(prof.get("remote_workdir", ""))
        self.email_edit.setText(prof.get("email", ""))
        self.settings.active_profile = name
        # Pull saved password for this (host, username) from the keyring.
        self._load_saved_password()
        self._append_log(f"[ok] switched to profile '{name}'")

    def _on_save_profile(self) -> None:
        default = self.settings.active_profile or self.user_edit.text().strip() or "Profile"
        name, ok = QInputDialog.getText(
            self, "Save profile", "Profile name:", QLineEdit.Normal, default
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        # Use the values currently in the form, not what's in settings.
        prof = {
            "name": name,
            "host": self.host_edit.text().strip(),
            "username": self.user_edit.text().strip(),
            "project": self.project_box.currentText().strip(),
            "remote_workdir": self.workdir_edit.text().strip(),
            "email": self.email_edit.text().strip(),
        }
        config.upsert_profile(self.settings.profiles, prof)
        self.settings.active_profile = name
        # Persist password to keyring for this profile if user opted in.
        if self.save_cb.isChecked() and self.pass_edit.text():
            config.save_password(prof["host"], prof["username"], self.pass_edit.text())
        self._save_settings_silent()
        self._refresh_profile_box()
        toasts(self).success(f"Profile '{name}' saved.")
        self._append_log(f"[ok] profile '{name}' saved")

    def _on_delete_profile(self) -> None:
        name = self.profile_box.currentText()
        if not name or name == "— Custom —":
            return
        if QMessageBox.question(
            self, "Delete profile",
            f"Remove profile '{name}'?\n\n"
            f"(This does NOT delete the saved password from Windows "
            f"Credential Manager — uncheck 'Save password' and click "
            f"'Save settings' to do that.)",
        ) != QMessageBox.Yes:
            return
        self.settings.profiles = [
            p for p in self.settings.profiles
            if not (isinstance(p, dict) and p.get("name") == name)
        ]
        if self.settings.active_profile == name:
            self.settings.active_profile = ""
        config.save_settings(self.settings)
        self._refresh_profile_box()
        self._append_log(f"[ok] profile '{name}' deleted")

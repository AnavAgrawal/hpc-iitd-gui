"""Submit tab: build a PBS script and qsub it, or submit an existing remote script."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from .. import config
from ..client import HpcClient
from ..workers import run_async
from . import style as st
from .common import MONO, confirm, error_box, info_box, plain_log, toasts


# Rates (₹) from HPC-IITD Usage Charges
CPU_RATE = {"standard": 0.10, "low": 0.10, "high": 0.50, "top": 0.50, "scai_q": 0.10}
GPU_RATE = {"standard": 1.00, "low": 1.00, "high": 5.00, "top": 5.00, "scai_q": 1.00}


@dataclass
class JobForm:
    name: str
    project: str
    queue: str
    nodes: int
    ncpus: int
    ngpus: int
    gpu_type: str
    memory: str
    walltime: str
    email: str
    mail_events: str
    workdir: str
    out_path: str
    err_path: str
    script_body: str

    def render(self) -> str:
        lines: list[str] = ["#!/bin/sh"]
        if self.name: lines.append(f"#PBS -N {self.name}")
        if self.project: lines.append(f"#PBS -P {self.project}")
        if self.queue: lines.append(f"#PBS -q {self.queue}")
        if self.mail_events: lines.append(f"#PBS -m {self.mail_events}")
        if self.email: lines.append(f"#PBS -M {self.email}")
        sel = f"select={self.nodes}:ncpus={self.ncpus}"
        if self.ngpus > 0: sel += f":ngpus={self.ngpus}"
        if self.gpu_type: sel += f":centos={self.gpu_type}"
        if self.memory: sel += f":mem={self.memory}"
        lines.append(f"#PBS -l {sel}")
        lines.append(f"#PBS -l walltime={self.walltime}")
        if self.out_path: lines.append(f"#PBS -o {self.out_path}")
        if self.err_path: lines.append(f"#PBS -e {self.err_path}")
        lines += [
            "",
            'echo "================================"',
            'echo "PBS jobid : $PBS_JOBID"',
            'echo "node list : $(cat $PBS_NODEFILE)"',
            'echo "workdir   : $PBS_O_WORKDIR"',
            'date',
            'echo "================================"',
            "",
        ]
        if self.workdir:
            lines.append(f"cd {self.workdir}")
        lines.append("")
        body = self.script_body.rstrip() if self.script_body.strip() else "# TODO: your command(s) here"
        lines.append(body)
        lines.append("")
        lines.append('echo "==== exit=$? ===="; date')
        return "\n".join(lines) + "\n"


_HMS_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")


def _walltime_hours(w: str) -> float:
    m = _HMS_RE.match(w)
    if not m:
        return 0.0
    h, mm, ss = (int(x) for x in m.groups())
    return h + mm / 60 + ss / 3600


def estimate_cost(form: JobForm) -> float:
    hours = _walltime_hours(form.walltime)
    cpu_rate = CPU_RATE.get(form.queue, 0.10)
    gpu_rate = GPU_RATE.get(form.queue, 1.00)
    total_cpu = form.nodes * form.ncpus
    total_gpu = form.nodes * form.ngpus
    return (total_cpu * cpu_rate + total_gpu * gpu_rate) * hours


# ---------- templates ----------
TEMPLATES: dict[str, dict] = {
    "CPU – quick test (10 min, 1×2 CPU)": dict(
        queue="standard", nodes=1, ncpus=2, ngpus=0, gpu_type="",
        memory="", walltime="00:10:00",
        body="echo 'hello from $HOSTNAME'\npython -u -c 'import sys; print(sys.version)'\n",
    ),
    "CPU – parallel (1×16 CPU, 2h)": dict(
        queue="standard", nodes=1, ncpus=16, ngpus=0, gpu_type="",
        memory="30gb", walltime="02:00:00",
        body="source $HOME/condaBaseSetup && conda activate myenv\n"
             "python -u train.py\n",
    ),
    "GPU – 1×A100 training (24h)": dict(
        queue="standard", nodes=1, ncpus=8, ngpus=1, gpu_type="icelake",
        memory="30gb", walltime="24:00:00",
        body="source $HOME/condaBaseSetup && conda activate myenv\n"
             "nvidia-smi\npython -u train.py\n",
    ),
    "GPU – high priority (1×A100, 4h, 5× cost)": dict(
        queue="high", nodes=1, ncpus=8, ngpus=1, gpu_type="icelake",
        memory="30gb", walltime="04:00:00",
        body="source $HOME/condaBaseSetup && conda activate myenv\n"
             "python -u train.py\n",
    ),
    "GPU – debug / interactive-style (30 min)": dict(
        queue="standard", nodes=1, ncpus=4, ngpus=1, gpu_type="icelake",
        memory="16gb", walltime="00:30:00",
        body="source $HOME/condaBaseSetup && conda activate myenv\n"
             "python -u debug_script.py\n",
    ),
}


WALLTIME_PRESETS = [
    ("10m", "00:10:00"), ("30m", "00:30:00"),
    ("1h", "01:00:00"), ("4h", "04:00:00"),
    ("24h", "24:00:00"), ("48h", "48:00:00"),
    ("168h", "168:00:00"),
]


class RemoteFilePicker(QDialog):
    """Small dialog to browse the remote filesystem and pick a .pbs file."""

    def __init__(self, client: HpcClient, start: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick remote script")
        self.resize(700, 450)
        self.client = client
        self._cur = start or "~/scratch"
        self.selected: str | None = None

        self.path_edit = QLineEdit(self._cur)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._dblclick)
        self.ok_btn = QPushButton("Select"); self.ok_btn.setDefault(True)
        self.cancel_btn = QPushButton("Cancel")
        self.up_btn = QPushButton("Up")
        self.go_btn = QPushButton("Go")

        top = QHBoxLayout()
        top.addWidget(QLabel("Path:")); top.addWidget(self.path_edit, 1)
        top.addWidget(self.go_btn); top.addWidget(self.up_btn)
        bot = QHBoxLayout(); bot.addStretch(1); bot.addWidget(self.cancel_btn); bot.addWidget(self.ok_btn)

        root = QVBoxLayout(self); root.addLayout(top); root.addWidget(self.list, 1); root.addLayout(bot)

        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn.clicked.connect(self._accept_current)
        self.path_edit.returnPressed.connect(self._load)
        self.go_btn.clicked.connect(self._load)
        self.up_btn.clicked.connect(self._up)
        self._load()

    def _load(self) -> None:
        p = self.path_edit.text().strip() or "~"
        self._cur = p
        self.list.clear()
        def task(): return self.client.listdir(p)
        def done(entries):
            for name, size, is_dir, _m in entries:
                prefix = "📁 " if is_dir else "📄 "
                self.list.addItem(prefix + name)
        def err(msg): self.list.addItem(f"[err] {msg.splitlines()[0]}")
        run_async(task, on_done=done, on_error=err)

    def _up(self) -> None:
        p = self.client.expand(self._cur)
        self.path_edit.setText(posixpath.dirname(p) or "/")
        self._load()

    def _dblclick(self, item) -> None:
        txt = item.text()
        if not txt or txt.startswith("["): return
        name = txt.split(" ", 1)[1] if " " in txt else txt
        full = posixpath.join(self.client.expand(self._cur), name)
        if txt.startswith("📁"):
            self.path_edit.setText(full)
            self._load()
        elif txt.startswith("📄"):
            self.selected = full
            self.accept()

    def _accept_current(self) -> None:
        item = self.list.currentItem()
        if not item: return
        txt = item.text()
        if not txt.startswith("📄"): return
        name = txt.split(" ", 1)[1]
        self.selected = posixpath.join(self.client.expand(self._cur), name)
        self.accept()


class SubmitTab(QWidget):
    submitted = Signal(str)

    def __init__(self, client: HpcClient, settings: config.Settings) -> None:
        super().__init__()
        self.client = client
        self.settings = settings

        self._balance: float | None = None

        self.inner = QTabWidget()
        self.inner.addTab(self._build_form_tab(), "Build script")
        self.inner.addTab(self._build_remote_tab(), "Submit existing script")

        root = QVBoxLayout(self); root.addWidget(self.inner)

    def set_balance(self, balance: float | None) -> None:
        self._balance = balance
        self._update_preview()

    # ---------- build form ----------
    def _build_form_tab(self) -> QWidget:
        w = QWidget()

        # Template row
        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.tmpl_combo = QComboBox()
        self.tmpl_combo.addItem("— custom —")
        for k in TEMPLATES.keys():
            self.tmpl_combo.addItem(k)
        self.apply_tmpl_btn = QPushButton("Apply")
        tmpl_row.addWidget(self.tmpl_combo, 1); tmpl_row.addWidget(self.apply_tmpl_btn)

        self.apply_tmpl_btn.clicked.connect(self._apply_template)

        # Form widgets
        grp = QGroupBox("Job parameters")
        form = QFormLayout(grp)
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(12)
        form.setContentsMargins(14, 18, 14, 12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.name_edit = QLineEdit("my_job")
        self.project_edit = QLineEdit(self.settings.project)
        self.queue_combo = QComboBox(); self.queue_combo.addItems(["standard", "high", "scai_q", "low", "top"])
        self.queue_combo.setItemData(1, "5× cost — use sparingly", Qt.ToolTipRole)
        self.nodes_spin = QSpinBox(); self.nodes_spin.setRange(1, 100); self.nodes_spin.setValue(1)
        self.ncpus_spin = QSpinBox(); self.ncpus_spin.setRange(1, 64); self.ncpus_spin.setValue(16)
        self.ngpus_spin = QSpinBox(); self.ngpus_spin.setRange(0, 8); self.ngpus_spin.setValue(1)
        self.gpu_type_combo = QComboBox(); self.gpu_type_combo.addItems(["(none)", "icelake"])
        self.gpu_type_combo.setCurrentText("icelake")
        self.mem_edit = QLineEdit(""); self.mem_edit.setPlaceholderText("e.g. 30gb")
        self.walltime_edit = QLineEdit("01:00:00")
        self.email_edit = QLineEdit(self.settings.email)
        self.mail_events_edit = QLineEdit("bea")
        default_wd = self.settings.remote_workdir or "~/scratch"
        self.workdir_edit = QLineEdit("$PBS_O_WORKDIR")
        self.out_edit = QLineEdit("logs/job.out")
        self.err_edit = QLineEdit("logs/job.err")

        # Walltime presets
        wt_row = QHBoxLayout(); wt_row.setSpacing(4)
        wt_row.addWidget(self.walltime_edit)
        for label, val in WALLTIME_PRESETS:
            b = QPushButton(label); b.setProperty("role", "ghost")
            b.setFixedWidth(46); b.setStyleSheet("padding: 3px 0;")
            b.clicked.connect(lambda _=False, v=val: self._set_walltime(v))
            wt_row.addWidget(b)
        wt_holder = QWidget(); wt_holder.setLayout(wt_row)
        wt_row.setContentsMargins(0, 0, 0, 0)

        form.addRow("Job name", self.name_edit)
        form.addRow("Project", self.project_edit)
        form.addRow("Queue", self.queue_combo)
        form.addRow("Nodes", self.nodes_spin)
        form.addRow("CPUs/node", self.ncpus_spin)
        form.addRow("GPUs/node", self.ngpus_spin)
        form.addRow("GPU arch", self.gpu_type_combo)
        form.addRow("Memory", self.mem_edit)
        form.addRow("Walltime", wt_holder)
        form.addRow("Email", self.email_edit)
        form.addRow("Mail (-m)", self.mail_events_edit)
        form.addRow("cd (in-script)", self.workdir_edit)
        form.addRow("stdout (-o)", self.out_edit)
        form.addRow("stderr (-e)", self.err_edit)

        self.script_body = QPlainTextEdit()
        self.script_body.setFont(MONO)
        self.script_body.setPlaceholderText(
            "# Commands that will run inside the allocated job.\n"
            "# Example:\n"
            "#   source $HOME/condaBaseSetup && conda activate myenv\n"
            "#   python -u train.py --epochs 10\n"
        )
        self.script_body.setPlainText(
            "source $HOME/condaBaseSetup && conda activate myenv\n"
            "python -u -c 'import sys; print(\"hello from\", sys.version)'\n"
        )

        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setFont(MONO)

        # Cost widget
        self.cost_value = QLabel("₹ 0.00")
        self.cost_value.setStyleSheet(f"color:{st.OK}; font-weight:700; font-size:14pt;")
        self.cost_detail = QLabel("")
        self.cost_detail.setStyleSheet(f"color:{st.TEXT_DIM};")

        cost_col = QVBoxLayout(); cost_col.setContentsMargins(0, 0, 0, 0); cost_col.setSpacing(0)
        cost_col.addWidget(self.cost_value); cost_col.addWidget(self.cost_detail)
        cost_holder = QWidget(); cost_holder.setLayout(cost_col)

        self.remote_path_edit = QLineEdit()
        self.remote_path_edit.setPlaceholderText("e.g. ~/scratch/my_job.pbs")
        self.remote_path_edit.setText(posixpath.join(default_wd, "my_job.pbs"))

        self.preview_btn = QPushButton("Preview / Update")
        self.save_only_btn = QPushButton("Save only")
        self.submit_btn = QPushButton("Save && Submit"); self.submit_btn.setDefault(True)

        btn_row = QHBoxLayout()
        btn_row.addWidget(cost_holder); btn_row.addStretch(1)
        btn_row.addWidget(self.preview_btn); btn_row.addWidget(self.save_only_btn); btn_row.addWidget(self.submit_btn)

        script_lbl = QLabel("Script body:")
        preview_lbl = QLabel("Rendered PBS script:")

        split = QSplitter(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(tmpl_row); ll.addWidget(grp); ll.addWidget(script_lbl); ll.addWidget(self.script_body, 1)
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(preview_lbl); rl.addWidget(self.preview, 1)
        rl.addWidget(QLabel("Remote save path (.pbs):"))
        rl.addWidget(self.remote_path_edit)
        split.addWidget(left); split.addWidget(right)
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)

        root = QVBoxLayout(w)
        root.addWidget(split, 1)
        root.addLayout(btn_row)

        # Wire up preview update on every change
        self.preview_btn.clicked.connect(self._update_preview)
        self.submit_btn.clicked.connect(self._save_and_submit)
        self.save_only_btn.clicked.connect(self._save_only)
        for wdg in [self.name_edit, self.project_edit, self.walltime_edit,
                    self.email_edit, self.mail_events_edit, self.workdir_edit,
                    self.out_edit, self.err_edit, self.mem_edit]:
            wdg.editingFinished.connect(self._update_preview)
        for wdg in [self.queue_combo, self.gpu_type_combo]:
            wdg.currentIndexChanged.connect(self._update_preview)
        for wdg in [self.nodes_spin, self.ncpus_spin, self.ngpus_spin]:
            wdg.valueChanged.connect(self._update_preview)
        self.script_body.textChanged.connect(self._update_preview)
        self._update_preview()
        return w

    def _apply_template(self) -> None:
        name = self.tmpl_combo.currentText()
        t = TEMPLATES.get(name)
        if not t:
            return
        self.queue_combo.setCurrentText(t["queue"])
        self.nodes_spin.setValue(t["nodes"])
        self.ncpus_spin.setValue(t["ncpus"])
        self.ngpus_spin.setValue(t["ngpus"])
        self.gpu_type_combo.setCurrentText(t["gpu_type"] or "(none)")
        self.mem_edit.setText(t["memory"])
        self.walltime_edit.setText(t["walltime"])
        self.script_body.setPlainText(t["body"])
        self._update_preview()
        toasts(self).info(f"Applied template: {name}")

    def _set_walltime(self, v: str) -> None:
        self.walltime_edit.setText(v)
        self._update_preview()

    def _collect_form(self) -> JobForm:
        gpu_type = self.gpu_type_combo.currentText()
        if gpu_type == "(none)":
            gpu_type = ""
        return JobForm(
            name=self.name_edit.text().strip(),
            project=self.project_edit.text().strip(),
            queue=self.queue_combo.currentText().strip(),
            nodes=self.nodes_spin.value(),
            ncpus=self.ncpus_spin.value(),
            ngpus=self.ngpus_spin.value(),
            gpu_type=gpu_type,
            memory=self.mem_edit.text().strip(),
            walltime=self.walltime_edit.text().strip(),
            email=self.email_edit.text().strip(),
            mail_events=self.mail_events_edit.text().strip(),
            workdir=self.workdir_edit.text().strip(),
            out_path=self.out_edit.text().strip(),
            err_path=self.err_edit.text().strip(),
            script_body=self.script_body.toPlainText(),
        )

    def _update_preview(self) -> None:
        form = self._collect_form()
        self.preview.setPlainText(form.render())
        cost = estimate_cost(form)
        warn = ""
        if form.queue == "high":
            warn = "  (HIGH · 5× cost)"

        self.cost_value.setText(f"₹ {cost:,.2f}{warn}")
        detail = f"{form.nodes}×{form.ncpus}C"
        if form.ngpus: detail += f" + {form.nodes}×{form.ngpus}GPU"
        detail += f"  ·  {form.walltime}"
        if self._balance is not None:
            ratio = (cost / self._balance) if self._balance > 0 else float("inf")
            if ratio >= 1:
                color = st.ERR; detail += f"   — exceeds balance (₹{self._balance:,.2f})"
            elif ratio > 0.5:
                color = st.WARN; detail += f"   — {ratio*100:.0f}% of balance (₹{self._balance:,.2f})"
            elif ratio > 0.1:
                color = st.WARN; detail += f"   — {ratio*100:.0f}% of balance"
            else:
                color = st.OK; detail += f"   — {ratio*100:.1f}% of balance"
            self.cost_value.setStyleSheet(f"color:{color}; font-weight:700; font-size:14pt;")
        else:
            self.cost_value.setStyleSheet(f"color:{st.OK}; font-weight:700; font-size:14pt;")
        self.cost_detail.setText(detail)

    def _save_only(self) -> None:
        self._do_upload(submit_after=False)

    def _save_and_submit(self) -> None:
        form = self._collect_form()
        cost = estimate_cost(form)
        if form.queue == "high":
            if not confirm(self, "High priority queue",
                           f"Submitting to HIGH queue (5× cost).\n"
                           f"Estimated cost: ₹{cost:,.2f}\n\nContinue?"):
                return
        if self._balance is not None and cost > self._balance:
            if not confirm(self, "Insufficient balance",
                           f"Estimated cost ₹{cost:,.2f} exceeds balance ₹{self._balance:,.2f}.\n\n"
                           "The scheduler will still accept the job but may reject on start. Continue?"):
                return
        self._do_upload(submit_after=True)

    def _do_upload(self, submit_after: bool) -> None:
        if not self.client.connected:
            toasts(self).error("Connect to HPC first."); return
        form = self._collect_form()
        if not _HMS_RE.match(form.walltime):
            error_box(self, "Invalid walltime", "Use hh:mm:ss format.")
            return
        script = form.render()
        remote_path = self.remote_path_edit.text().strip()
        if not remote_path:
            error_box(self, "Missing path", "Enter a remote save path."); return

        def task():
            payload = script.replace("\r\n", "\n")
            import os, tempfile
            fd, local = tempfile.mkstemp(suffix=".pbs"); os.close(fd)
            try:
                with open(local, "w", newline="\n", encoding="utf-8") as f:
                    f.write(payload)
                self.client.put_file(local, remote_path)
            finally:
                try: os.unlink(local)
                except OSError: pass
            if submit_after:
                workdir = posixpath.dirname(self.client.expand(remote_path))
                jid, res = self.client.qsub(remote_path, workdir=workdir)
                return {"saved": True, "job_id": jid, "qsub_out": res.output, "ok": res.ok}
            return {"saved": True, "job_id": None, "qsub_out": "", "ok": True}

        def done(result):
            if not result["ok"]:
                toasts(self).error(result["qsub_out"].splitlines()[0] if result["qsub_out"] else "qsub failed", title="qsub failed")
                return
            if result["job_id"]:
                self.submitted.emit(result["job_id"])
                self.settings.recent_pbs_scripts = config.push_recent(
                    self.settings.recent_pbs_scripts, remote_path)
                config.save_settings(self.settings)
            else:
                toasts(self).success(f"Wrote {remote_path}", title="Saved")

        def err(msg):
            toasts(self).error(msg.splitlines()[0], title="Error")
        run_async(task, on_done=done, on_error=err)

    # ---------- remote-script tab ----------
    def _build_remote_tab(self) -> QWidget:
        w = QWidget()
        self.remote_script_edit = QLineEdit()
        self.remote_script_edit.setPlaceholderText("Remote path to existing .pbs")
        self.remote_workdir2 = QLineEdit()
        self.remote_workdir2.setPlaceholderText("Optional cd (default: script dir)")
        self.extra_flags = QLineEdit()
        self.extra_flags.setPlaceholderText("Extra qsub flags (e.g. -q high)")
        self.remote_preview = QPlainTextEdit()
        self.remote_preview.setFont(MONO); self.remote_preview.setReadOnly(True)
        self.pick_btn = QPushButton("Browse…")
        self.fetch_btn = QPushButton("Fetch remote script")
        self.qsub_btn = QPushButton("qsub"); self.qsub_btn.setDefault(True)
        self.qsub_high_btn = QPushButton("qsub -q high"); self.qsub_high_btn.setProperty("role", "danger")

        form = QFormLayout()
        path_row = QHBoxLayout()
        path_row.addWidget(self.remote_script_edit, 1); path_row.addWidget(self.pick_btn)
        path_holder = QWidget(); path_holder.setLayout(path_row); path_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Script", path_holder)
        form.addRow("Work dir", self.remote_workdir2)
        form.addRow("Extra flags", self.extra_flags)
        row = QHBoxLayout()
        row.addWidget(self.fetch_btn); row.addStretch(1)
        row.addWidget(self.qsub_btn); row.addWidget(self.qsub_high_btn)

        root = QVBoxLayout(w)
        root.addLayout(form); root.addLayout(row)
        root.addWidget(QLabel("Script contents:"))
        root.addWidget(self.remote_preview, 1)

        self.pick_btn.clicked.connect(self._pick_remote)
        self.fetch_btn.clicked.connect(self._remote_preview)
        self.qsub_btn.clicked.connect(lambda: self._submit_remote(""))
        self.qsub_high_btn.clicked.connect(lambda: self._submit_remote("-q high"))
        self.remote_script_edit.returnPressed.connect(self._remote_preview)

        # Populate most-recently-used
        if self.settings.recent_pbs_scripts:
            self.remote_script_edit.setText(self.settings.recent_pbs_scripts[0])
        return w

    def _pick_remote(self) -> None:
        if not self.client.connected:
            toasts(self).warn("Connect first."); return
        start = self.remote_script_edit.text().strip() or (self.settings.remote_workdir or "~/scratch")
        start = posixpath.dirname(start) or "~/scratch"
        dlg = RemoteFilePicker(self.client, start, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected:
            self.remote_script_edit.setText(dlg.selected)
            self._remote_preview()

    def _remote_preview(self) -> None:
        p = self.remote_script_edit.text().strip()
        if not p:
            return
        def task(): return self.client.read_text(p, max_bytes=200000)
        def done(text): self.remote_preview.setPlainText(text)
        def err(msg): self.remote_preview.setPlainText(f"[err] {msg}")
        run_async(task, on_done=done, on_error=err)

    def _submit_remote(self, extra: str) -> None:
        p = self.remote_script_edit.text().strip()
        if not p:
            toasts(self).warn("Enter a script path."); return
        workdir = self.remote_workdir2.text().strip() or None
        combined = (extra + " " + self.extra_flags.text().strip()).strip()
        if "high" in combined and not confirm(
                self, "High queue", "Submit to HIGH priority queue (5× cost)?"):
            return
        def task(): return self.client.qsub(p, workdir=workdir, extra_flags=combined)
        def done(res):
            jid, cmd_res = res
            if cmd_res.ok and jid:
                self.submitted.emit(jid)
                self.settings.recent_pbs_scripts = config.push_recent(
                    self.settings.recent_pbs_scripts, p)
                config.save_settings(self.settings)
            else:
                toasts(self).error((cmd_res.output or "qsub failed").splitlines()[0], title="qsub failed")
        def err(msg):
            toasts(self).error(msg, title="Error")
        run_async(task, on_done=done, on_error=err)

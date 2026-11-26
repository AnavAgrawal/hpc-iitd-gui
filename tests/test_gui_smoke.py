"""GUI smoke test: construct the main window, connect to the live HPC from the
event loop, let it populate every tab, then quit cleanly.

Uses Qt offscreen platform so no display is required. A non-zero exit means
the GUI blew up on something (import, signal wiring, live data, etc.).
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer, QThreadPool
from PySide6.QtWidgets import QApplication

from hpc_gui import config
from hpc_gui.client import HpcClient
from hpc_gui.ui.main_window import MainWindow

HOST = "hpc.iitd.ac.in"
USER = os.environ.get("HPC_USER", "")
PASS = os.environ.get("HPC_PASS", "")


def main() -> int:
    app = QApplication(sys.argv)
    settings = config.Settings(
        host=HOST, username=USER, project=os.environ.get("HPC_PROJECT", ""),
        remote_workdir="~/scratch/hpc_gui_test",
        email=os.environ.get("HPC_EMAIL", ""),
    )
    client = HpcClient()
    win = MainWindow(client, settings)
    win.show()

    # Pre-fill password and trigger connect from the event loop so the worker
    # thread machinery runs end-to-end.
    win.connection_tab.pass_edit.setText(PASS)

    failed: list[str] = []

    def phase1_connect():
        print("[t+0.5s] triggering Connect…")
        win.connection_tab._do_connect()

    def phase2_check_connected():
        connected = client.connected
        print(f"[t+4s] connected={connected}")
        if not connected:
            failed.append("not connected after 4s")
            app.quit(); return

    def phase3_exercise():
        print("[t+7s] tab info + explicit refresh…")
        for i in range(win.tabs.count()):
            print(f"    tab[{i}] = {win.tabs.tabText(i):12s} enabled={win.tabs.isTabEnabled(i)}")
        # Refresh queues explicitly and wait
        win.queues_tab.refresh()
        # Exercise Submit preview
        win.submit_tab._update_preview()
        assert "#PBS -N" in win.submit_tab.preview.toPlainText()
        print("    Submit render preview: OK")
        # Console run
        win.console_tab._run_cmd("echo hello && qstat -Q | head -n 3")
        # Logs open
        win.logs_tab.open_path("~/scratch/hpc_gui_test/logs/test.out")

    def phase4_verify_dashboard():
        dt = win.dashboard_tab
        bal_text = dt.balance_card.value.text()
        user_text = dt.user_card.value.text()
        print(f"[t+12s] dashboard balance='{bal_text}' user='{user_text}'")
        if user_text != USER:
            failed.append(f"dashboard user mismatch: {user_text}")
        if bal_text == "—":
            failed.append(f"dashboard balance not populated")
        # Verify jobs table populated
        rows = win.jobs_tab.table.rowCount()
        print(f"[t+12s] jobs_tab rows={rows}")
        if rows < 1:
            failed.append("jobs table is empty — expected at least the running job")
        # Queues populated
        qrows = win.queues_tab.table.rowCount()
        print(f"[t+12s] queues_tab rows={qrows}")
        if qrows < 5:
            failed.append(f"queues table underpopulated ({qrows})")
        # Console got output
        console_text = win.console_tab.output.toPlainText()
        print(f"[t+12s] console output has {len(console_text)} chars")
        if "hello" not in console_text:
            failed.append("console did not return expected output")

    def finish():
        # Drain all background workers and close SSH before Qt tears down.
        QThreadPool.globalInstance().waitForDone(10_000)
        try:
            client.close()
        except Exception:
            pass
        win.close()
        if failed:
            print("\nSMOKE FAILED:")
            for f in failed:
                print(f"  - {f}")
            app.exit(1)
        else:
            print("\nGUI SMOKE: ALL CHECKS PASSED")
            app.exit(0)

    QTimer.singleShot(500, phase1_connect)
    QTimer.singleShot(5000, phase2_check_connected)
    QTimer.singleShot(8000, phase3_exercise)
    QTimer.singleShot(16000, phase4_verify_dashboard)
    QTimer.singleShot(18000, finish)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

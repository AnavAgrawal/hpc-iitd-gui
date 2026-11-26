"""Long-running GUI stress test: connect, then run many refresh / submit / log
cycles in quick succession so we exercise the worker lifetime fix and can
catch the post-connect crash the user reported.

Bumps garbage collection aggressively so any dangling-sender bug would
reproduce quickly.
"""
from __future__ import annotations

import gc
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
    # Aggressive GC so any unreferenced Worker would be collected immediately.
    gc.set_debug(gc.DEBUG_STATS if "-v" in sys.argv else 0)
    gc.enable()

    settings = config.Settings(
        host=HOST, username=USER, project=os.environ.get("HPC_PROJECT", ""),
        remote_workdir="~/scratch/hpc_gui_test",
        email=os.environ.get("HPC_EMAIL", ""),
        auto_refresh_interval=2,
    )
    client = HpcClient()
    win = MainWindow(client, settings)
    win.show()
    win.connection_tab.pass_edit.setText(PASS)

    # Shorter auto-refresh on Jobs/Queues to pile up workers.
    win.jobs_tab.interval_spin.setValue(5)
    win.queues_tab.interval.setValue(5)

    failed: list[str] = []
    cycle_count = [0]

    def connect():
        print("[.] connect")
        win.connection_tab._do_connect()

    def cycle():
        """Fire refresh on multiple tabs + a console command, back-to-back."""
        cycle_count[0] += 1
        i = cycle_count[0]
        print(f"[cycle {i}] fire refreshes + run command")
        win.dashboard_tab.refresh()
        win.jobs_tab.refresh()
        win.queues_tab.refresh()
        win.files_tab._goto("~/scratch")
        win.console_tab._run_cmd("date +%T && qstat -Q | wc -l")
        win.logs_tab.open_path("~/scratch/hpc_gui_test/logs/test.out")
        # Force collection right after scheduling — this is what would kill
        # workers kept only by the thread pool if we weren't retaining them.
        gc.collect()

    def status():
        from hpc_gui.workers import _LIVE
        print(f"    alive_workers={len(_LIVE)}  "
              f"thread_pool_active={QThreadPool.globalInstance().activeThreadCount()}")

    def done():
        # Drain workers and verify we actually completed cycles successfully.
        print("[.] draining…")
        QThreadPool.globalInstance().waitForDone(15_000)
        status()
        dash_bal = win.dashboard_tab.balance_card.value.text()
        q_rows = win.queues_tab.table.rowCount()
        j_rows = win.jobs_tab.table.rowCount()
        console_lines = win.console_tab.output.toPlainText().count("[exit=")
        print(f"    dashboard balance = {dash_bal}")
        print(f"    queues rows       = {q_rows}")
        print(f"    jobs rows         = {j_rows}")
        print(f"    console completions = {console_lines}")
        if dash_bal == "—":
            failed.append("dashboard balance never populated")
        if q_rows < 5:
            failed.append(f"queues rows underpopulated: {q_rows}")
        if console_lines < cycle_count[0] - 1:
            failed.append(f"console completions={console_lines} < cycles-1={cycle_count[0]-1}")
        try:
            client.close()
        except Exception:
            pass
        win.close()
        if failed:
            print("\nSTRESS FAILED:")
            for f in failed:
                print(f"  - {f}")
            app.exit(1)
        else:
            print(f"\nGUI STRESS PASSED — {cycle_count[0]} cycles, no crash")
            app.exit(0)

    # Timeline
    QTimer.singleShot(500, connect)

    # After connect, fire 15 cycles spaced 800ms apart (~12s of churn).
    for k in range(15):
        QTimer.singleShot(4000 + k * 800, cycle)

    # Periodic status prints
    for t in (5000, 8000, 11000, 14000, 17000, 20000):
        QTimer.singleShot(t, status)

    QTimer.singleShot(22000, done)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

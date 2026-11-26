"""Application entry point."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import config
from .client import HpcClient
from .ui import style
from .ui.main_window import MainWindow


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("HPC-IITD GUI")
    app.setOrganizationName("IITD")

    style.apply(app)

    settings = config.load_settings()
    client = HpcClient()

    win = MainWindow(client, settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

"""Entry point for MISA Auto Tool."""
from __future__ import annotations

import sys
import multiprocessing
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from database.database import Database
from services.self_watcher import run_parent_watcher
from ui.main_window import MainWindow


def main() -> int:
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--watch-parent":
        return run_parent_watcher(int(sys.argv[2]), Path(__file__).resolve())
    app = QApplication(sys.argv)
    app.setApplicationName("MISA Auto Tool")
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    app.setWindowIcon(QIcon(str(resource_root / "icon.png")))
    database = Database()
    database.initialize()
    database.recover_interrupted_jobs()
    window = MainWindow()
    window.show()
    # A Python process started from an IDE/terminal can occasionally leave the
    # newly created native window behind its launcher.  Activate it after Qt
    # has created the window handle so mouse and keyboard input go to MISA.
    QTimer.singleShot(0, lambda: (window.raise_(), window.activateWindow()))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

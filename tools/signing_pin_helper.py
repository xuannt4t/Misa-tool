"""Standalone watcher for native MISA signing-PIN dialogs.

Run this helper before issuing invoices when the signer dialog is displayed by
another Windows process.  Keeping it separate from the Playwright worker makes
the watcher survive browser restarts and lets the user see its live status.
"""
from __future__ import annotations

import sys
from pathlib import Path


# Support both `python tools/signing_pin_helper.py` and a separately bundled
# helper executable beside the main application.
PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.config import DEFAULT_SIGNING_PIN
from database.database import Database
from services.pin_dialog import submit_pin_if_prompted


class SigningPinHelper(QWidget):
    """A small always-on desktop companion for the native signing prompt."""

    def __init__(self) -> None:
        super().__init__()
        self._database = Database()
        self._database.initialize()
        self._watching = True

        self.setWindowTitle("MISA Signing PIN Helper")
        self.setMinimumWidth(390)

        title = QLabel("Trợ lý nhập PIN ký số")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self._status = QLabel("Đang chờ popup ký số…")
        self._status.setWordWrap(True)

        self._pin_input = QLineEdit(
            self._database.get_setting("signing_pin", DEFAULT_SIGNING_PIN) or ""
        )
        self._pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin_input.setPlaceholderText("Nhập mã PIN ký số")
        self._show_pin = QCheckBox("Hiện PIN")
        self._show_pin.toggled.connect(self._toggle_pin_visibility)

        self._toggle_button = QPushButton("Tạm dừng")
        self._toggle_button.clicked.connect(self._toggle_watching)
        self._save_button = QPushButton("Lưu PIN")
        self._save_button.clicked.connect(self._save_pin)

        form = QFormLayout()
        form.addRow("Mã PIN:", self._pin_input)
        form.addRow("", self._show_pin)

        buttons = QHBoxLayout()
        buttons.addWidget(self._save_button)
        buttons.addWidget(self._toggle_button)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                "Mở tool này trước khi phát hành. Nó sẽ liên tục dò popup PIN "
                "của ứng dụng ký số, kể cả khi trình duyệt đang khởi động lại."
            )
        )
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self._status)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._watch_for_pin)
        self._timer.start()

    def _toggle_pin_visibility(self, visible: bool) -> None:
        self._pin_input.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )

    def _save_pin(self) -> None:
        self._database.set_setting("signing_pin", self._pin_input.text())
        self._status.setText("Đã lưu PIN. Đang chờ popup ký số…")

    def _toggle_watching(self) -> None:
        self._watching = not self._watching
        self._toggle_button.setText("Tạm dừng" if self._watching else "Bắt đầu theo dõi")
        self._status.setText(
            "Đang chờ popup ký số…" if self._watching else "Đã tạm dừng theo dõi PIN."
        )

    def _watch_for_pin(self) -> None:
        if not self._watching:
            return
        if not self._pin_input.text():
            self._status.setText("Hãy nhập PIN trước khi bật theo dõi.")
            return
        if submit_pin_if_prompted(self._pin_input.text()):
            self._status.setText("Đã nhập PIN và xác nhận popup ký số.")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MISA Signing PIN Helper")
    window = SigningPinHelper()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

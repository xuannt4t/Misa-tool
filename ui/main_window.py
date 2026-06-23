"""Main desktop window."""
from __future__ import annotations

import logging
import html
import multiprocessing
from queue import Empty
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QLabel, QAbstractItemView, QFileDialog,
    QFrame, QGridLayout, QProgressBar, QScrollArea, QSplitter, QTabBar, QSpinBox, QMessageBox, QFormLayout, QCheckBox, QComboBox, QSizePolicy, QInputDialog,
)

from config.config import APP_NAME, LOG_FILE, ensure_runtime_directories
from config.config import (
    ADJUSTMENT_REASON, ADJUSTMENT_ITEM_NAME, ADJUSTMENT_ITEM_TYPE, ADJUSTMENT_VAT_RATE,
    DEFAULT_RECORD_RUN_LIMIT, DEFAULT_RECORD_RUN_MODE, DEFAULT_SIGNING_PIN, MAX_CONCURRENT_TASKS,
)
from database.database import Database
from services.excel_reader import read_invoices_excel
from services.excel_splitter import split_excel_workbook
from services.self_watcher import AUTO_RESTART_SETTING, auto_restart_enabled, start_self_watcher
from workers.browser_process import run_browser_worker


def configure_logging() -> None:
    ensure_runtime_directories()
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        encoding="utf-8",
    )


class ToggleSwitch(QCheckBox):
    """Large, explicit toggle so the whole control is easy to click."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(36)
        self.setMinimumWidth(230)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            "QCheckBox { background:#e2e8f0; color:#334155; border:1px solid #cbd5e1;"
            " border-radius:7px; padding:7px 12px; font-weight:700; }"
            "QCheckBox:hover { background:#cbd5e1; border-color:#94a3b8; }"
            "QCheckBox:checked { background:#2563eb; color:white; border-color:#1d4ed8; }"
            "QCheckBox:checked:hover { background:#1d4ed8; }"
            "QCheckBox::indicator { width:0; height:0; }"
        )


class JobCard(QFrame):
    clicked = Signal(int)

    def __init__(self, job) -> None:
        super().__init__()
        self.job_id = job["id"]
        self.setObjectName("jobCard")
        self.setFixedHeight(166)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        status_colors = {"queued": "#64748b", "running": "#2563eb", "completed": "#16a34a", "error": "#dc2626"}
        color = status_colors.get(job["status"], "#64748b")
        self.setStyleSheet(
            "QFrame#jobCard { background: #ffffff; border: 1px solid #dbe3ef; border-radius: 10px; }"
            "QFrame#jobCard:hover { border: 1px solid #2563eb; background: #f8fbff; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)
        header = QHBoxLayout()
        title = QLabel(f"H\u00f3a \u0111\u01a1n {job['hoa_don'] or '-'}")
        title.setStyleSheet("font-weight: 700; color: #172554;")
        badge = QLabel(job["status"].upper())
        badge.setStyleSheet(f"background:{color}; color:white; border-radius:8px; padding:3px 8px; font-size:10px; font-weight:700;")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(badge)
        layout.addLayout(header)
        customer = QLabel(job["ten_khach_hang"] or "Chưa có tên khách hàng")
        customer.setStyleSheet("color:#475569;")
        customer.setWordWrap(True)
        layout.addWidget(customer)
        duration = "-" if job["duration_seconds"] is None else f"{job['duration_seconds']:.2f} giây"
        layout.addWidget(QLabel(f"Ngày HĐ: {job['date'] or '-'}  •  ID: {job['invoice_id']}  •  {duration}"))
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(job["progress"])
        progress.setFormat(f"{job['progress']}%")
        progress.setStyleSheet(
            "QProgressBar { border: 0; background:#e2e8f0; border-radius:6px; height:16px; text-align:center; color:white; font-weight:700; }"
            f"QProgressBar::chunk {{ background:{color}; border-radius:6px; }}"
        )
        layout.addWidget(progress)
        step = QLabel(job["current_step"])
        step.setStyleSheet("color:#64748b; font-style:italic;")
        layout.addWidget(step)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.job_id)
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        configure_logging()
        self._started_at = datetime.now()
        logging.info("Ứng dụng được khởi động.")
        self._process_context = multiprocessing.get_context("spawn")
        self._worker_processes: list[dict] = []
        self._active_worker_count = 0
        self._database = Database()
        self._self_watcher_started = False
        self._auto_open_misa_scheduled = False
        self._page = 1
        self._page_size = 50
        self._worker_event_timer = QTimer(self)
        self._worker_event_timer.setInterval(75)
        self._worker_event_timer.timeout.connect(self._poll_worker_events)

        self.setWindowTitle(APP_NAME)
        # The configuration form needs enough room for its editable controls.
        # At the former 720x420 startup size Qt compressed the layout so much
        # that it looked like a static preview on smaller displays.
        self.setMinimumSize(900, 620)
        self.resize(1040, 720)
        # Make editable controls unmistakable. The platform default makes
        # enabled fields look almost identical to disabled fields.
        self.setStyleSheet(
            "QLineEdit, QTextEdit, QSpinBox {"
            " background: #ffffff; color: #172554; border: 1px solid #cbd5e1;"
            " border-radius: 6px; padding: 5px 7px; selection-background-color: #93c5fd;"
            "}"
            "QLineEdit:hover, QTextEdit:hover, QSpinBox:hover { border-color: #60a5fa; }"
            "QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {"
            " border: 2px solid #2563eb; background: #f8fbff;"
            "}"
            "QLineEdit:disabled, QTextEdit:disabled, QSpinBox:disabled {"
            " background: #f1f5f9; color: #94a3b8; border-color: #e2e8f0;"
            "}"
            "QTableWidget { selection-background-color: #2563eb; selection-color: #ffffff; }"
            "QTableWidget:focus { border: 2px solid #2563eb; }"
            "QTableWidget::item:selected { background-color: #2563eb; color: #ffffff; }"
            "QCheckBox { spacing: 9px; padding: 3px; font-weight: 600; }"
            "QCheckBox::indicator { width: 38px; height: 21px; border-radius: 10px; background: #cbd5e1; }"
            "QCheckBox::indicator:checked { background: #2563eb; }"
            "QCheckBox::indicator:hover { background: #94a3b8; }"
            "QCheckBox::indicator:checked:hover { background: #1d4ed8; }"
            "QPushButton { min-height: 20px; }"
            "QPushButton:hover:enabled { background-color: #eff6ff; border-color: #60a5fa; }"
            "QPushButton:pressed:enabled { background-color: #dbeafe; }"
            "QPushButton:disabled { color: #94a3b8; background: #f8fafc; border-color: #e2e8f0; }"
        )
        self.open_button = QPushButton("▶  Mở MISA")
        self.open_button.setStyleSheet(
            "QPushButton { background:#16a34a; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover:enabled { background:#15803d; }"
            "QPushButton:disabled { background:#bbf7d0; color:#166534; }"
        )
        self.close_button = QPushButton("■  Dừng MISA")
        self.close_button.setStyleSheet(
            "QPushButton { background:#dc2626; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover:enabled { background:#b91c1c; }"
            "QPushButton:disabled { background:#fecaca; color:#991b1b; }"
        )
        self.close_button.setEnabled(False)
        self.import_data_button = QPushButton("▤  D\u1eef li\u1ec7u import")
        self.import_excel_button = QPushButton("Nh\u1eadp Excel")
        self.split_excel_button = QPushButton("Chia file Excel")
        self.delete_selected_button = QPushButton("Xóa đã chọn")
        self.delete_selected_button.setStyleSheet(
            "QPushButton { background:#dc2626; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover { background:#b91c1c; }"
            "QPushButton:disabled { background:#fca5a5; }"
        )
        self.delete_all_button = QPushButton("Xóa tất cả")
        self.delete_all_button.setStyleSheet(
            "QPushButton { background:#991b1b; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover { background:#7f1d1d; }"
            "QPushButton:disabled { background:#fecaca; color:#991b1b; }"
        )
        self.reset_status_button = QPushButton("Reset status")
        self.reset_selected_failed_button = QPushButton("Reset lỗi đã chọn")
        self.reset_selected_failed_button.setStyleSheet(
            "QPushButton { background:#ea580c; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover { background:#c2410c; }"
        )
        self.reset_status_button.setStyleSheet(
            "QPushButton { background:#dc2626; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }"
            "QPushButton:hover { background:#b91c1c; }"
        )
        self.jobs_button = QPushButton("◷  Ti\u1ebfn tr\u00ecnh x\u1eed l\u00fd")
        self.config_button = QPushButton("⚙  C\u1ea5u h\u00ecnh")
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("T\u00ecm s\u1ed1 h\u00f3a \u0111\u01a1n, k\u00fd hi\u1ec7u, MST, \u0111\u1ecba ch\u1ec9...")
        self.mst2_filter_input = QComboBox()
        self.mst2_filter_input.addItem("MST2: Tất cả", "all")
        self.mst2_filter_input.addItem("MST2: Có dữ liệu", "with")
        self.mst2_filter_input.addItem("MST2: Trống", "without")
        self.status_filter_input = QComboBox()
        self.status_filter_input.addItem("Trạng thái: Tất cả", None)
        self.status_filter_input.addItem("Chưa thực hiện", 0)
        self.status_filter_input.addItem("Đã thực hiện", 1)
        self.status_filter_input.addItem("Lỗi", 2)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.invoice_table = QTableWidget()
        self.invoice_table.setColumnCount(14)
        self.invoice_table.setHorizontalHeaderLabels([
            "Chọn", "ID", "Th\u00e1ng", "A", "KHMH\u0110", "H\u00f3a \u0111\u01a1n", "Ng\u00e0y", "T\u00ean kh\u00e1ch h\u00e0ng", "\u0110\u1ecba ch\u1ec9",
            "MST 1", "MST 2", "D\u00f2ng Excel", "Tr\u1ea1ng th\u00e1i", "L\u1ed7i",
        ])
        self.invoice_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.invoice_table.setAlternatingRowColors(True)
        self.invoice_table.horizontalHeader().setStretchLastSection(True)
        self._updating_invoice_selection = False
        self.invoice_table.horizontalHeader().sectionClicked.connect(self.toggle_page_invoice_selection)
        self.invoice_table.itemChanged.connect(self.update_page_selection_header)
        self.previous_button = QPushButton("Trang tr\u01b0\u1edbc")
        self.next_button = QPushButton("Trang sau")
        self.page_label = QLabel()
        self.page_size_label = QLabel("Hiển thị:")
        self.page_size_input = QComboBox()
        for page_size in (50, 100, 200, 500):
            self.page_size_input.addItem(f"{page_size} dòng", page_size)

        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.import_data_button)
        buttons.addWidget(self.split_excel_button)
        buttons.addWidget(self.jobs_button)
        buttons.addWidget(self.config_button)
        buttons.addWidget(self.close_button)
        self._nav_buttons = (
            self.import_data_button,
            self.split_excel_button,
            self.jobs_button,
            self.config_button,
        )
        for button in self._nav_buttons:
            button.setCheckable(True)
            button.setStyleSheet(
                "QPushButton { border: 1px solid #dbe3ef; border-radius: 6px; padding: 6px 12px; }"
                "QPushButton:checked { background: #2563eb; color: white; border-color: #2563eb; font-weight: 700; }"
            )
        search_layout = QHBoxLayout()
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.mst2_filter_input)
        search_layout.addWidget(self.status_filter_input)
        search_layout.addWidget(self.import_excel_button)
        search_layout.addWidget(self.delete_selected_button)
        search_layout.addWidget(self.delete_all_button)
        search_layout.addWidget(self.reset_selected_failed_button)
        search_layout.addWidget(self.reset_status_button)
        self.done_invoice_count_label = QLabel()
        self.pending_invoice_count_label = QLabel()
        self.failed_invoice_count_label = QLabel()
        invoice_summary = QHBoxLayout()
        for label, color in (
            (self.done_invoice_count_label, "#16a34a"),
            (self.pending_invoice_count_label, "#f59e0b"),
            (self.failed_invoice_count_label, "#dc2626"),
        ):
            label.setStyleSheet(f"background:{color}; color:white; border-radius:7px; padding:5px 12px; font-weight:700;")
            invoice_summary.addWidget(label)
        invoice_summary.addStretch()
        pagination = QHBoxLayout()
        pagination.addWidget(self.previous_button)
        pagination.addWidget(self.page_label)
        pagination.addWidget(self.next_button)
        pagination.addStretch()
        pagination.addWidget(self.page_size_label)
        pagination.addWidget(self.page_size_input)
        self.data_panel = QWidget()
        data_layout = QVBoxLayout()
        data_layout.setContentsMargins(0, 0, 0, 0)
        data_layout.addLayout(search_layout)
        data_layout.addLayout(invoice_summary)
        data_layout.addWidget(self.invoice_table)
        data_layout.addLayout(pagination)
        self.data_panel.setLayout(data_layout)
        self.data_panel.hide()

        self.split_source_input = QLineEdit()
        self.split_source_input.setReadOnly(True)
        self.split_source_input.setPlaceholderText("Chưa chọn file Excel nguồn")
        self.split_source_button = QPushButton("Chọn file")
        self.split_output_input = QLineEdit()
        self.split_output_input.setReadOnly(True)
        self.split_output_input.setPlaceholderText("Chưa chọn thư mục xuất")
        self.split_output_button = QPushButton("Chọn thư mục")
        self.split_rows_input = QSpinBox()
        self.split_rows_input.setRange(1, 1_000_000)
        self.split_rows_input.setValue(1000)
        self.split_run_button = QPushButton("Chia file")
        self.split_run_button.setStyleSheet(
            "QPushButton { background:#2563eb; color:white; border:0; border-radius:7px; padding:8px 18px; font-weight:700; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        self.split_result_label = QLabel(
            "Các sheet được gộp theo thứ tự trong file nguồn, rồi chia theo tổng số dòng mỗi file."
        )
        self.split_result_label.setWordWrap(True)
        self.split_result_label.setStyleSheet("color:#64748b;")
        self.split_panel = QWidget()
        split_form = QFormLayout(self.split_panel)
        split_form.setContentsMargins(28, 20, 28, 20)
        split_source_row = QWidget()
        split_source_layout = QHBoxLayout(split_source_row)
        split_source_layout.setContentsMargins(0, 0, 0, 0)
        split_source_layout.addWidget(self.split_source_input)
        split_source_layout.addWidget(self.split_source_button)
        split_output_row = QWidget()
        split_output_layout = QHBoxLayout(split_output_row)
        split_output_layout.setContentsMargins(0, 0, 0, 0)
        split_output_layout.addWidget(self.split_output_input)
        split_output_layout.addWidget(self.split_output_button)
        split_button_row = QWidget()
        split_button_layout = QHBoxLayout(split_button_row)
        split_button_layout.setContentsMargins(0, 0, 0, 0)
        split_button_layout.addStretch()
        split_button_layout.addWidget(self.split_run_button)
        split_button_layout.addStretch()
        split_form.addRow("File Excel nguồn:", split_source_row)
        split_form.addRow("Số dòng mỗi file:", self.split_rows_input)
        split_form.addRow("Thư mục xuất:", split_output_row)
        split_form.addRow("", split_button_row)
        split_form.addRow("Ghi chú:", self.split_result_label)
        self.split_panel.hide()

        self.jobs_page = 1
        self.jobs_page_size = 12
        self._selected_job_id: int | None = None
        self._job_filter = "running"
        self.worker_table = QTableWidget()
        self.worker_table.setColumnCount(4)
        self.worker_table.setHorizontalHeaderLabels(["Worker", "PID", "Trạng thái", "Thao tác"])
        self.worker_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.worker_table.setFixedHeight(118)
        self.worker_table.horizontalHeader().setStretchLastSection(True)
        self.job_tabs = QTabBar()
        self.job_tabs.addTab("Đang xử lý")
        self.job_tabs.addTab("Đã hoàn thành")
        self.job_tabs.addTab("Lỗi")
        self.job_tabs.setStyleSheet(
            "QTabBar::tab { background:#f1f5f9; color:#475569; padding:8px 28px; border:1px solid #dbe3ef; }"
            "QTabBar::tab:selected { background:#2563eb; color:white; font-weight:700; border-color:#2563eb; }"
            "QTabBar::tab:hover { background:#dbeafe; }"
        )
        self.running_count_label = QLabel()
        self.completed_count_label = QLabel()
        self.error_count_label = QLabel()
        self.summary_bar = QWidget()
        self.summary_bar.setFixedHeight(46)
        summary_layout = QHBoxLayout(self.summary_bar)
        summary_layout.setContentsMargins(8, 6, 8, 6)
        summary_layout.setSpacing(8)
        for label, color in (
            (self.running_count_label, "#2563eb"),
            (self.completed_count_label, "#16a34a"),
            (self.error_count_label, "#dc2626"),
        ):
            label.setFixedHeight(30)
            label.setMinimumWidth(145)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            label.setStyleSheet(f"background:{color}; color:white; border-radius:8px; padding:4px 12px; font-weight:700;")
            summary_layout.addWidget(label)
        summary_layout.addStretch()
        self.jobs_grid = QGridLayout()
        self.jobs_grid.setSpacing(10)
        self.jobs_container = QWidget()
        self.jobs_container.setLayout(self.jobs_grid)
        jobs_scroll = QScrollArea()
        jobs_scroll.setWidgetResizable(True)
        jobs_scroll.setWidget(self.jobs_container)
        self.job_log_view = QTextEdit()
        self.job_log_view.setReadOnly(True)
        self.job_log_view.setPlaceholderText("Ch\u1ecdn m\u1ed9t job \u0111\u1ec3 xem log ri\u00eang.")
        self.retry_job_button = QPushButton("Chạy lại tất cả lỗi")
        self.retry_job_button.setEnabled(False)
        self.retry_job_button.setStyleSheet("QPushButton { background:#dc2626; color:white; border:0; border-radius:6px; padding:6px 14px; font-weight:700; }")
        job_detail = QWidget()
        detail_layout = QVBoxLayout(job_detail)
        detail_header = QHBoxLayout()
        detail_header.addWidget(QLabel("Nh\u1eadt k\u00fd job \u0111\u00e3 ch\u1ecdn"))
        detail_header.addStretch()
        detail_header.addWidget(self.retry_job_button)
        detail_layout.addLayout(detail_header)
        detail_layout.addWidget(self.job_log_view)
        jobs_splitter = QSplitter()
        jobs_splitter.addWidget(jobs_scroll)
        jobs_splitter.addWidget(job_detail)
        jobs_splitter.setSizes([560, 300])
        self.jobs_previous_button = QPushButton("Trang tr\u01b0\u1edbc")
        self.jobs_next_button = QPushButton("Trang sau")
        self.jobs_page_label = QLabel()
        jobs_pagination = QHBoxLayout()
        jobs_pagination.addWidget(self.jobs_previous_button)
        jobs_pagination.addWidget(self.jobs_page_label)
        jobs_pagination.addWidget(self.jobs_next_button)
        jobs_pagination.addStretch()
        self.jobs_panel = QWidget()
        jobs_layout = QVBoxLayout(self.jobs_panel)
        jobs_layout.setContentsMargins(0, 0, 0, 0)
        jobs_layout.addWidget(QLabel("Worker đang chạy"))
        jobs_layout.addWidget(self.worker_table)
        jobs_layout.addWidget(self.job_tabs)
        jobs_layout.addWidget(self.summary_bar)
        jobs_layout.addWidget(jobs_splitter)
        jobs_layout.addLayout(jobs_pagination)
        jobs_layout.setStretch(2, 1)
        self.jobs_panel.hide()
        self.config_panel = QWidget()
        config_form = QFormLayout(self.config_panel)
        config_form.setContentsMargins(28, 20, 28, 20)
        self.concurrent_tasks_input = QSpinBox()
        self.concurrent_tasks_input.setRange(1, 20)
        self.run_all_toggle = ToggleSwitch("Chạy tất cả bản ghi chờ")
        self.run_all_toggle.setToolTip("Bật để xử lý toàn bộ bản ghi chờ; tắt để nhập số lượng cần chạy.")
        self.record_limit_input = QSpinBox()
        self.record_limit_input.setRange(1, 1_000_000)
        self.record_limit_label = QLabel("Số bản ghi:")
        run_mode_widget = QWidget()
        run_mode_layout = QHBoxLayout(run_mode_widget)
        run_mode_layout.setContentsMargins(0, 0, 0, 0)
        run_mode_layout.addWidget(self.run_all_toggle)
        run_mode_layout.addWidget(self.record_limit_label)
        run_mode_layout.addWidget(self.record_limit_input)
        run_mode_layout.addStretch()
        self.reason_input = QTextEdit()
        self.reason_input.setFixedHeight(70)
        self.item_name_input = QTextEdit()
        self.item_name_input.setFixedHeight(90)
        self.item_type_input = QLineEdit()
        self.vat_rate_input = QLineEdit()
        self.signing_pin_input = QLineEdit()
        self.signing_pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.auto_restart_apps_toggle = ToggleSwitch("Tự chạy lại app khi bị đóng")
        self.auto_restart_apps_toggle.setToolTip(
            "Áp dụng cho MISA Auto Tool và Trợ lý ký số. Tắt trước khi đóng nếu không muốn app tự mở lại."
        )
        self.auto_open_misa_toggle = ToggleSwitch("Tự mở MISA sau 30 giây")
        self.auto_open_misa_toggle.setToolTip(
            "Khi mở MISA Auto Tool, tự khởi động quy trình Mở MISA sau 30 giây."
        )
        self.signing_pin_input.setPlaceholderText("Nhập mã PIN ký số")
        self.pin_visibility_button = QPushButton("👁")
        self.pin_visibility_button.setCheckable(True)
        self.pin_visibility_button.setFixedSize(34, 34)
        self.pin_visibility_button.setToolTip("Hiển thị mã PIN ký số")
        self.pin_visibility_button.setAccessibleName("Hiển thị mã PIN ký số")
        self.pin_visibility_button.toggled.connect(self._toggle_signing_pin_visibility)
        pin_input_widget = QWidget()
        pin_input_layout = QHBoxLayout(pin_input_widget)
        pin_input_layout.setContentsMargins(0, 0, 0, 0)
        pin_input_layout.setSpacing(6)
        pin_input_layout.addWidget(self.signing_pin_input)
        pin_input_layout.addWidget(self.pin_visibility_button)
        for editable in (
            self.concurrent_tasks_input,
            self.record_limit_input,
            self.reason_input,
            self.item_name_input,
            self.item_type_input,
            self.vat_rate_input,
            self.signing_pin_input,
        ):
            editable.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            editable.setMinimumHeight(34)
            editable.setCursor(Qt.CursorShape.IBeamCursor)
        self.save_config_button = QPushButton("L\u01b0u c\u1ea5u h\u00ecnh")
        self.save_config_button.setFixedWidth(220)
        self.save_config_button.setStyleSheet(
            "QPushButton { background:#2563eb; color:white; border:0; border-radius:7px; padding:8px 18px; font-weight:700; }"
            "QPushButton:hover { background:#1d4ed8; }"
        )
        save_row = QWidget()
        save_layout = QHBoxLayout(save_row)
        save_layout.setContentsMargins(0, 0, 0, 0)
        save_layout.addStretch()
        save_layout.addWidget(self.save_config_button)
        save_layout.addStretch()
        config_form.addRow("S\u1ed1 t\u00e1c v\u1ee5 ch\u1ea1y \u0111\u1ed3ng th\u1eddi:", self.concurrent_tasks_input)
        config_form.addRow("Phạm vi chạy:", run_mode_widget)
        config_form.addRow("L\u00fd do \u0111i\u1ec1u ch\u1ec9nh:", self.reason_input)
        config_form.addRow("N\u1ed9i dung t\u00ean h\u00e0ng h\u00f3a/d\u1ecbch v\u1ee5:", self.item_name_input)
        config_form.addRow("T\u00ednh ch\u1ea5t HHDV:", self.item_type_input)
        config_form.addRow("Thu\u1ebf GTGT:", self.vat_rate_input)
        config_form.addRow("Mã PIN ký số:", pin_input_widget)
        config_form.addRow("Tự khởi động lại:", self.auto_restart_apps_toggle)
        config_form.addRow("Tự chạy:", self.auto_open_misa_toggle)
        config_form.addRow("", save_row)
        self.config_help = QLabel("Tham số dùng được: {ten_khach_hang}, {mst2}, {hoa_don}, {date}")
        self.config_help.setStyleSheet("color:#64748b;")
        config_form.addRow("Ghi ch\u00fa:", self.config_help)
        config_form.takeRow(self.config_help)
        config_form.insertRow(4, "Ghi chú:", self.config_help)
        self.config_panel.hide()
        self.content_panel = QWidget()
        content_layout = QVBoxLayout(self.content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.data_panel)
        content_layout.addWidget(self.split_panel)
        content_layout.addWidget(self.jobs_panel)
        content_layout.addWidget(self.config_panel)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.workspace_splitter.addWidget(self.content_panel)
        self.workspace_splitter.addWidget(self.log_view)
        self.workspace_splitter.setSizes([580, 260])
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 1)
        layout = QVBoxLayout()
        layout.addLayout(buttons)
        layout.addWidget(self.workspace_splitter)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.open_button.clicked.connect(self.open_misa)
        self.close_button.clicked.connect(self.close_browser)
        self.import_data_button.clicked.connect(self.show_import_data)
        self.split_excel_button.clicked.connect(self.show_split_excel)
        self.split_source_button.clicked.connect(self.choose_split_source_file)
        self.split_output_button.clicked.connect(self.choose_split_output_directory)
        self.split_run_button.clicked.connect(self.split_excel_file)
        self.import_excel_button.clicked.connect(self.import_excel)
        self.delete_selected_button.clicked.connect(self.delete_selected_invoices)
        self.delete_all_button.clicked.connect(self.delete_all_invoices)
        self.reset_selected_failed_button.clicked.connect(self.reset_selected_failed_invoices)
        self.reset_status_button.clicked.connect(self.reset_all_statuses)
        self.jobs_button.clicked.connect(self.show_jobs)
        self.config_button.clicked.connect(self.show_config)
        self.save_config_button.clicked.connect(self.save_config)
        self.run_all_toggle.toggled.connect(self.update_run_limit_enabled)
        self.search_input.textChanged.connect(self.on_search_changed)
        self.mst2_filter_input.currentIndexChanged.connect(self.on_invoice_filter_changed)
        self.status_filter_input.currentIndexChanged.connect(self.on_invoice_filter_changed)
        self.page_size_input.currentIndexChanged.connect(self.on_page_size_changed)
        self.previous_button.clicked.connect(self.previous_page)
        self.next_button.clicked.connect(self.next_page)
        self.jobs_previous_button.clicked.connect(self.previous_jobs_page)
        self.jobs_next_button.clicked.connect(self.next_jobs_page)
        self.job_tabs.currentChanged.connect(self.on_job_tab_changed)
        self.retry_job_button.clicked.connect(self.retry_all_failed_jobs)
        self.load_config()
        self._ensure_self_watcher()
        self._schedule_auto_open_misa()

    def open_misa(
        self,
        _checked: bool = False,
        retry_invoice_id: int | None = None,
        retry_errors_only: bool = False,
    ) -> None:
        if any(handle["process"].is_alive() for handle in self._worker_processes):
            return
        self.job_tabs.setCurrentIndex(0)
        self.show_jobs()
        worker_count = 1 if retry_invoice_id is not None else int(
            self._database.get_setting("max_concurrent_tasks", str(MAX_CONCURRENT_TASKS)) or 1
        )
        if retry_invoice_id is None:
            run_mode = self._database.get_setting("record_run_mode", DEFAULT_RECORD_RUN_MODE) or DEFAULT_RECORD_RUN_MODE
            run_limit = int(
                self._database.get_setting("record_run_limit", str(DEFAULT_RECORD_RUN_LIMIT))
                or DEFAULT_RECORD_RUN_LIMIT
            )
            self._database.begin_invoice_run(run_mode, run_limit)
        self._worker_processes = []
        self._active_worker_count = worker_count
        for worker_id in range(1, worker_count + 1):
            event_queue = self._process_context.Queue()
            stop_event = self._process_context.Event()
            process = self._process_context.Process(
                target=run_browser_worker,
                args=(event_queue, stop_event, retry_invoice_id, retry_errors_only, worker_id, worker_count > 1),
                name=f"MISA-browser-worker-{worker_id}",
            )
            self._worker_processes.append({
                "worker_id": worker_id,
                "process": process,
                "queue": event_queue,
                "stop_event": stop_event,
                "finished": False,
                "status": "Đang khởi động",
            })
        self.open_button.setEnabled(False)
        self._worker_event_timer.start()
        for handle in self._worker_processes:
            handle["process"].start()
            handle["status"] = "Đang chạy"
        self.refresh_worker_table()
        self.open_button.setText("●  Đang chạy MISA")

    def close_browser(self) -> None:
        if self._worker_processes:
            self.append_log("Đang đóng trình duyệt...")
            self.close_button.setEnabled(False)
            for handle in self._worker_processes:
                handle["stop_event"].set()
                handle["status"] = "Đang dừng"
            self.refresh_worker_table()

    def stop_worker(self, handle: dict) -> None:
        """Request one worker to stop; its active invoice is finalized as an error."""
        if handle["finished"] or not handle["process"].is_alive():
            return
        handle["stop_event"].set()
        handle["status"] = "Đang dừng"
        self.append_log(
            f"Đã yêu cầu dừng Worker {handle['worker_id']}; hóa đơn đang chạy sẽ được đánh dấu lỗi."
        )
        self.refresh_worker_table()

    def refresh_worker_table(self) -> None:
        self.worker_table.setRowCount(len(self._worker_processes))
        for row, handle in enumerate(self._worker_processes):
            process = handle["process"]
            self.worker_table.setItem(row, 0, QTableWidgetItem(str(handle["worker_id"])))
            self.worker_table.setItem(row, 1, QTableWidgetItem(str(process.pid or "-")))
            status = "Đã dừng" if handle["finished"] else handle.get("status", "Đang chạy")
            self.worker_table.setItem(row, 2, QTableWidgetItem(status))
            button = QPushButton("Dừng worker")
            button.setEnabled(process.is_alive() and not handle["finished"])
            button.clicked.connect(lambda _checked=False, current=handle: self.stop_worker(current))
            self.worker_table.setCellWidget(row, 3, button)

    def on_browser_state_changed(self, is_open: bool) -> None:
        self.close_button.setEnabled(is_open or self._active_worker_count > 0)

    def _poll_worker_events(self) -> None:
        for handle in tuple(self._worker_processes):
            while True:
                try:
                    event_type, payload = handle["queue"].get_nowait()
                except Empty:
                    break
                if event_type == "log":
                    self.append_log(payload)
                elif event_type == "progress":
                    self.refresh_running_jobs()
                elif event_type == "state":
                    self.on_browser_state_changed(payload)
                elif event_type == "finished":
                    self._on_worker_process_finished(handle)
            if not handle["process"].is_alive() and not handle["finished"]:
                self._on_worker_process_finished(handle)
        self.refresh_worker_table()

    def _on_worker_process_finished(self, handle: dict) -> None:
        if handle["finished"]:
            return
        handle["finished"] = True
        handle["status"] = "Đã dừng"
        process = handle["process"]
        process.join(timeout=0.1)
        self._active_worker_count -= 1
        if self._active_worker_count <= 0:
            self._active_worker_count = 0
            self._worker_event_timer.stop()
            self._worker_processes = []
            self.open_button.setText("▶  Mở MISA")
            self.open_button.setEnabled(True)
            self.close_button.setEnabled(False)

    def append_log(self, message: str) -> None:
        color = self._log_color(message)
        entry = f"[{datetime.now():%H:%M:%S}] {message}"
        self.log_view.append(f'<span style="color:{color};">{html.escape(entry)}</span>')

    @staticmethod
    def _log_color(message: str, level: str = "") -> str:
        lowered = message.lower()
        if level.upper() == "ERROR" or "lỗi" in lowered or "không thể" in lowered or "error" in lowered:
            return "#d32f2f"
        if "không có dữ liệu status = 0 phù hợp" in lowered or "không còn bản ghi phù hợp" in lowered:
            return "#d97706"
        if level.upper() == "SUCCESS" or "thành công" in lowered or "hoàn tất" in lowered or "đã " in lowered:
            return "#188038"
        return "#1976d2"

    def show_import_data(self) -> None:
        self._set_active_navigation(self.import_data_button)
        self.jobs_panel.hide()
        self.config_panel.hide()
        self.split_panel.hide()
        self.data_panel.show()
        self.load_invoices()

    def show_split_excel(self) -> None:
        self._set_active_navigation(self.split_excel_button)
        self.data_panel.hide()
        self.jobs_panel.hide()
        self.config_panel.hide()
        self.split_panel.show()

    def show_jobs(self) -> None:
        self._set_active_navigation(self.jobs_button)
        self.data_panel.hide()
        self.config_panel.hide()
        self.split_panel.hide()
        self.jobs_panel.show()
        self.load_jobs()

    def show_config(self) -> None:
        self._set_active_navigation(self.config_button)
        self.data_panel.hide()
        self.jobs_panel.hide()
        self.split_panel.hide()
        self.load_config()
        self.config_panel.show()

    def load_config(self) -> None:
        self.concurrent_tasks_input.setValue(int(self._database.get_setting("max_concurrent_tasks", str(MAX_CONCURRENT_TASKS)) or 1))
        self.reason_input.setPlainText(self._database.get_setting("adjustment_reason", ADJUSTMENT_REASON) or ADJUSTMENT_REASON)
        self.item_name_input.setPlainText(self._database.get_setting("adjustment_item_name", ADJUSTMENT_ITEM_NAME) or ADJUSTMENT_ITEM_NAME)
        self.item_type_input.setText(self._database.get_setting("adjustment_item_type", ADJUSTMENT_ITEM_TYPE) or ADJUSTMENT_ITEM_TYPE)
        self.vat_rate_input.setText(self._database.get_setting("adjustment_vat_rate", ADJUSTMENT_VAT_RATE) or ADJUSTMENT_VAT_RATE)
        self.signing_pin_input.setText(
            self._database.get_setting("signing_pin", DEFAULT_SIGNING_PIN) or ""
        )
        self.auto_restart_apps_toggle.setChecked(auto_restart_enabled(self._database))
        self.auto_open_misa_toggle.setChecked(
            self._database.get_setting("auto_open_misa_on_start", "0") == "1"
        )
        run_mode = self._database.get_setting("record_run_mode", DEFAULT_RECORD_RUN_MODE) or DEFAULT_RECORD_RUN_MODE
        self.run_all_toggle.setChecked(run_mode == "all")
        self.record_limit_input.setValue(int(self._database.get_setting("record_run_limit", str(DEFAULT_RECORD_RUN_LIMIT)) or DEFAULT_RECORD_RUN_LIMIT))
        self.update_run_limit_enabled()

    def _toggle_signing_pin_visibility(self, visible: bool) -> None:
        self.signing_pin_input.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.pin_visibility_button.setToolTip(
            "Ẩn mã PIN ký số" if visible else "Hiển thị mã PIN ký số"
        )

    def _ensure_self_watcher(self) -> None:
        if self._self_watcher_started or not auto_restart_enabled(self._database):
            return
        start_self_watcher(Path(__file__).resolve().parents[1] / "main.py")
        self._self_watcher_started = True

    def _schedule_auto_open_misa(self) -> None:
        if self._auto_open_misa_scheduled or self._database.get_setting(
            "auto_open_misa_on_start", "0"
        ) != "1":
            return
        self._auto_open_misa_scheduled = True
        self.append_log("Sẽ tự mở MISA sau 30 giây.")
        QTimer.singleShot(30_000, self._open_misa_if_enabled)

    def _open_misa_if_enabled(self) -> None:
        self._auto_open_misa_scheduled = False
        if self._database.get_setting("auto_open_misa_on_start", "0") == "1":
            self.append_log("Đang tự mở MISA theo cấu hình.")
            self.open_misa()

    def save_config(self) -> None:
        confirmation = QMessageBox.question(
            self,
            "Xác nhận lưu cấu hình",
            "Bạn có chắc muốn lưu cấu hình này? Cấu hình mới sẽ áp dụng cho lần chạy MISA tiếp theo.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        self._database.set_setting("max_concurrent_tasks", str(self.concurrent_tasks_input.value()))
        self._database.set_setting("adjustment_reason", self.reason_input.toPlainText().strip())
        self._database.set_setting("adjustment_item_name", self.item_name_input.toPlainText().strip())
        self._database.set_setting("adjustment_item_type", self.item_type_input.text().strip())
        self._database.set_setting("adjustment_vat_rate", self.vat_rate_input.text().strip())
        self._database.set_setting("signing_pin", self.signing_pin_input.text())
        self._database.set_setting(
            AUTO_RESTART_SETTING, "1" if self.auto_restart_apps_toggle.isChecked() else "0"
        )
        self._database.set_setting(
            "auto_open_misa_on_start", "1" if self.auto_open_misa_toggle.isChecked() else "0"
        )
        self._database.set_setting("record_run_mode", "all" if self.run_all_toggle.isChecked() else "custom")
        self._database.set_setting("record_run_limit", str(self.record_limit_input.value()))
        self._ensure_self_watcher()
        self._schedule_auto_open_misa()
        QMessageBox.information(
            self,
            "Đã lưu cấu hình",
            "Cấu hình đã được lưu thành công và sẽ áp dụng khi chạy MISA lần tiếp theo.",
        )

    def update_run_limit_enabled(self) -> None:
        show_limit = not self.run_all_toggle.isChecked()
        self.record_limit_label.setVisible(show_limit)
        self.record_limit_input.setVisible(show_limit)

    def _set_active_navigation(self, active_button: QPushButton) -> None:
        for button in self._nav_buttons:
            button.setChecked(button is active_button)

    def on_job_tab_changed(self, index: int) -> None:
        self._job_filter = ("running", "completed", "error")[index]
        self.jobs_page = 1
        self._selected_job_id = None
        self.retry_job_button.setEnabled(False)
        self.job_log_view.clear()
        self.load_jobs()

    def previous_jobs_page(self) -> None:
        if self.jobs_page > 1:
            self.jobs_page -= 1
            self.load_jobs()

    def next_jobs_page(self) -> None:
        self.jobs_page += 1
        self.load_jobs()

    def load_jobs(self) -> None:
        counts = self._database.get_job_counts()
        self.running_count_label.setText(f"Đang xử lý: {counts['running']}")
        self.completed_count_label.setText(f"Đã hoàn thành: {counts['completed']}")
        self.error_count_label.setText(f"Lỗi: {counts['error']}")
        jobs, total = self._database.get_processing_jobs(
            self._job_filter, self.jobs_page_size, (self.jobs_page - 1) * self.jobs_page_size
        )
        if not jobs and self.jobs_page > 1:
            self.jobs_page -= 1
            return self.load_jobs()
        while self.jobs_grid.count():
            item = self.jobs_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, job in enumerate(jobs):
            card = JobCard(job)
            card.clicked.connect(self.show_job_logs)
            self.jobs_grid.addWidget(card, index // 2, index % 2)
        self.jobs_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        total_pages = max(1, (total + self.jobs_page_size - 1) // self.jobs_page_size)
        self.jobs_page_label.setText(f"Trang {self.jobs_page}/{total_pages} - {total} job")
        self.jobs_previous_button.setEnabled(self.jobs_page > 1)
        self.jobs_next_button.setEnabled(self.jobs_page < total_pages)
        self.retry_job_button.setEnabled(self._job_filter == "error" and total > 0)

    def show_job_logs(self, job_id: int) -> None:
        self._selected_job_id = job_id
        logs = self._database.get_job_logs(job_id)
        if not logs:
            self.job_log_view.setPlainText("Job này chưa có log. Khi worker chạy, log riêng sẽ xuất hiện ở đây.")
            return
        lines = []
        for entry in logs:
            text = f"[{entry['created_at']}] {entry['level']}: {entry['message']}"
            color = self._log_color(entry["message"], entry["level"])
            lines.append(f'<span style="color:{color};">{html.escape(text)}</span>')
        self.job_log_view.setHtml("<br>".join(lines))

    def retry_all_failed_jobs(self) -> None:
        confirmation = QMessageBox.question(
            self,
            "Chạy lại tất cả job lỗi",
            "Bạn có muốn chạy lại toàn bộ job lỗi? Lần chạy này bỏ qua giới hạn số bản ghi cấu hình.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        count = self._database.retry_all_failed_jobs()
        if count == 0:
            self.append_log(
                "Không có job lỗi hợp lệ để chạy lại (các mã hóa đơn còn lại đang bị trùng)."
            )
            return
        self.append_log(f"Đã đưa {count} job lỗi về hàng đợi để chạy lại, bỏ qua quota.")
        self.open_misa(retry_errors_only=True)

    def refresh_running_jobs(self) -> None:
        if self.jobs_panel.isVisible():
            self.load_jobs()
            if self._selected_job_id is not None:
                self.show_job_logs(self._selected_job_id)

    def import_excel(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Ch\u1ecdn file Excel", "", "Excel Files (*.xlsx *.xls)"
        )
        if not file_path:
            return
        try:
            invoices = read_invoices_excel(file_path)
            imported = self._database.insert_invoices(invoices)
            self.append_log(f"\u0110\u00e3 nh\u1eadp {imported} d\u00f2ng d\u1eef li\u1ec7u t\u1eeb Excel.")
            self._page = 1
            self.show_import_data()
        except Exception as exc:
            logging.exception("Excel import failed")
            self.append_log(f"L\u1ed7i nh\u1eadp Excel: {exc}")

    def choose_split_source_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file Excel cần chia", "", "Excel Files (*.xlsx *.xls)"
        )
        if file_path:
            self.split_source_input.setText(file_path)
            if not self.split_output_input.text():
                self.split_output_input.setText(str(Path(file_path).parent))

    def choose_split_output_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục lưu các file đã chia", self.split_output_input.text()
        )
        if directory:
            self.split_output_input.setText(directory)

    def split_excel_file(self) -> None:
        source = self.split_source_input.text().strip()
        output_directory = self.split_output_input.text().strip()
        if not source:
            QMessageBox.information(self, "Chưa chọn file", "Hãy chọn file Excel cần chia.")
            return
        if not output_directory:
            QMessageBox.information(self, "Chưa chọn thư mục", "Hãy chọn thư mục lưu file kết quả.")
            return
        try:
            target, output_files, total_rows = split_excel_workbook(
                source, output_directory, self.split_rows_input.value()
            )
            message = (
                f"Đã chia {total_rows} dòng thành {len(output_files)} file.\n"
                f"Thư mục kết quả: {target}"
            )
            self.split_result_label.setText(message)
            self.append_log(message.replace("\n", " - "))
            QMessageBox.information(self, "Đã chia file Excel", message)
        except Exception as exc:
            logging.exception("Excel split failed")
            self.append_log(f"Lỗi chia file Excel: {exc}")
            QMessageBox.critical(self, "Không thể chia file Excel", str(exc))

    def reset_all_statuses(self) -> None:
        confirmation = QMessageBox.question(
            self,
            "Reset status",
            "Bạn có chắc muốn đưa status của toàn bộ bản ghi về 0 và xóa toàn bộ lịch sử job/log không?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        count = self._database.reset_all_invoice_statuses()
        QMessageBox.information(self, "Đã reset status", f"Đã đưa status của {count} bản ghi về 0.")
        if self.data_panel.isVisible():
            self.load_invoices()

    def reset_selected_failed_invoices(self) -> None:
        if any(handle["process"].is_alive() for handle in self._worker_processes):
            QMessageBox.warning(
                self,
                "Không thể reset khi đang chạy",
                "Hãy dừng MISA trước khi reset các bản ghi lỗi.",
            )
            return

        selected_ids = self._selected_invoice_ids_on_page()
        if not selected_ids:
            QMessageBox.information(
                self,
                "Chưa chọn bản ghi",
                "Hãy tick các bản ghi lỗi cần reset, hoặc bấm tiêu đề cột Chọn để tick cả trang.",
            )
            return

        confirmation = QMessageBox.question(
            self,
            "Reset bản ghi lỗi đã chọn",
            f"Đưa các bản ghi lỗi đã chọn về trạng thái chưa thực hiện? ({len(selected_ids)} dòng được chọn)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        reset_count = self._database.reset_failed_invoices_by_ids(selected_ids)
        if reset_count == 0:
            QMessageBox.information(
                self, "Không có bản ghi lỗi", "Các dòng đã chọn không còn ở trạng thái lỗi."
            )
            return
        self.append_log(f"Đã reset {reset_count} bản ghi lỗi đã chọn về trạng thái chưa thực hiện.")
        self.load_invoices()

    def _selected_invoice_ids_on_page(self) -> list[int]:
        selected_ids: list[int] = []
        for row_index in range(self.invoice_table.rowCount()):
            item = self.invoice_table.item(row_index, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                invoice_id = item.data(Qt.ItemDataRole.UserRole)
                if invoice_id is not None:
                    selected_ids.append(int(invoice_id))
        return selected_ids

    def toggle_page_invoice_selection(self, section: int) -> None:
        """Toggle checkboxes only for the currently visible invoice page."""
        if section != 0 or self.invoice_table.rowCount() == 0:
            return
        select_all = any(
            self.invoice_table.item(row, 0).checkState() != Qt.CheckState.Checked
            for row in range(self.invoice_table.rowCount())
            if self.invoice_table.item(row, 0) is not None
        )
        self._updating_invoice_selection = True
        try:
            for row in range(self.invoice_table.rowCount()):
                item = self.invoice_table.item(row, 0)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked if select_all else Qt.CheckState.Unchecked
                    )
        finally:
            self._updating_invoice_selection = False
        self.update_page_selection_header()

    def update_page_selection_header(self, _item=None) -> None:
        if self._updating_invoice_selection:
            return
        total = self.invoice_table.rowCount()
        all_selected = total > 0 and all(
            self.invoice_table.item(row, 0) is not None
            and self.invoice_table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(total)
        )
        header_item = self.invoice_table.horizontalHeaderItem(0)
        if header_item is not None:
            header_item.setText("☑ Chọn" if all_selected else "☐ Chọn")

    def delete_selected_invoices(self) -> None:
        if any(handle["process"].is_alive() for handle in self._worker_processes):
            QMessageBox.warning(
                self,
                "Không thể xóa khi đang chạy",
                "Hãy dừng MISA trước khi xóa bản ghi để tránh worker đang xử lý dữ liệu đã bị xóa.",
            )
            return

        selected_ids: list[int] = []
        for row_index in range(self.invoice_table.rowCount()):
            item = self.invoice_table.item(row_index, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                invoice_id = item.data(Qt.ItemDataRole.UserRole)
                if invoice_id is not None:
                    selected_ids.append(int(invoice_id))

        if not selected_ids:
            QMessageBox.information(self, "Chưa chọn bản ghi", "Hãy tick checkbox ở các bản ghi cần xóa.")
            return

        confirmation = QMessageBox.question(
            self,
            "Xóa bản ghi đã chọn",
            f"Xóa {len(selected_ids)} bản ghi đã chọn? Job và log liên quan cũng sẽ bị xóa.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        deleted = self._database.delete_invoices_by_ids(selected_ids)
        self.append_log(f"Đã xóa {deleted} bản ghi đã chọn.")
        self._page = 1
        self.load_invoices()

    def delete_all_invoices(self) -> None:
        if any(handle["process"].is_alive() for handle in self._worker_processes):
            QMessageBox.warning(
                self,
                "Không thể xóa khi đang chạy",
                "Hãy dừng MISA trước khi xóa toàn bộ dữ liệu.",
            )
            return

        confirmation = QMessageBox.question(
            self,
            "Xóa toàn bộ dữ liệu",
            "Thao tác này xóa toàn bộ bản ghi import, job và log liên quan. Bạn có muốn tiếp tục?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        typed, accepted = QInputDialog.getText(
            self,
            "Xác nhận xóa tất cả",
            "Gõ yes để xác nhận xóa toàn bộ dữ liệu:",
        )
        if not accepted:
            return
        if typed.strip().casefold() != "yes":
            QMessageBox.warning(self, "Chưa xóa dữ liệu", "Bạn chưa nhập đúng chữ yes.")
            return

        self._database.clear_invoices()
        self.append_log("Đã xóa toàn bộ bản ghi import, job và log.")
        self._page = 1
        self.load_invoices()
        QMessageBox.information(self, "Đã xóa tất cả", "Đã xóa toàn bộ dữ liệu import.")

    def on_search_changed(self) -> None:
        self._page = 1
        self.load_invoices()

    def on_invoice_filter_changed(self) -> None:
        self._page = 1
        self.load_invoices()

    def on_page_size_changed(self) -> None:
        self._page_size = int(self.page_size_input.currentData())
        self._page = 1
        self.load_invoices()

    def previous_page(self) -> None:
        if self._page > 1:
            self._page -= 1
            self.load_invoices()

    def next_page(self) -> None:
        self._page += 1
        self.load_invoices()

    def load_invoices(self) -> None:
        counts = self._database.get_invoice_status_counts()
        self.done_invoice_count_label.setText(f"Đã thực hiện: {counts[1]}")
        self.pending_invoice_count_label.setText(f"Chưa thực hiện: {counts[0]}")
        self.failed_invoice_count_label.setText(f"Lỗi: {counts[2]}")
        search = self.search_input.text() if hasattr(self, "search_input") else ""
        rows, total = self._database.get_invoices(
            search,
            self._page_size,
            (self._page - 1) * self._page_size,
            self.mst2_filter_input.currentData(),
            self.status_filter_input.currentData(),
        )
        if not rows and self._page > 1:
            self._page -= 1
            return self.load_invoices()
        self._updating_invoice_selection = True
        self.invoice_table.setRowCount(len(rows))
        keys = ("id", "thang", "a", "khmhd", "hoa_don", "date", "ten_khach_hang", "dia_chi", "mst1", "mst2", "row", "status", "error")
        for row_index, row in enumerate(rows):
            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            checkbox_item.setCheckState(Qt.CheckState.Unchecked)
            checkbox_item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.invoice_table.setItem(row_index, 0, checkbox_item)
            for column_index, key in enumerate(keys, start=1):
                item = QTableWidgetItem("" if row[key] is None else str(row[key]))
                if row["status"] == 1:
                    item.setBackground(QColor("#dcfce7"))
                    item.setForeground(QColor("#166534"))
                elif row["status"] == 2:
                    item.setBackground(QColor("#fee2e2"))
                    item.setForeground(QColor("#991b1b"))
                self.invoice_table.setItem(row_index, column_index, item)
        self._updating_invoice_selection = False
        self.update_page_selection_header()
        # Always start at the first column after loading/filtering. Otherwise a
        # previously scrolled viewport can crop the leading digits of KHMHĐ and
        # invoice numbers, making valid values look as if zeroes were missing.
        self.invoice_table.horizontalScrollBar().setValue(
            self.invoice_table.horizontalScrollBar().minimum()
        )
        total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        self.page_label.setText(f"Trang {self._page}/{total_pages} - {total} d\u00f2ng")
        self.previous_button.setEnabled(self._page > 1)
        self.next_button.setEnabled(self._page < total_pages)

    def closeEvent(self, event) -> None:
        for handle in self._worker_processes:
            handle["stop_event"].set()
        for handle in self._worker_processes:
            process = handle["process"]
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
        event.accept()

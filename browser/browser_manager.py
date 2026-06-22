"""Playwright persistent-browser worker executed in a child process."""
from __future__ import annotations

import logging
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Error, Playwright, sync_playwright
from PySide6.QtCore import QObject, Signal, Slot

from config.config import (
    ADJUSTMENT_FROM_DATE,
    ADJUSTMENT_INVOICE_NUMBER,
    ADJUSTMENT_ITEM_NAME,
    ADJUSTMENT_FORM_MAX_RETRIES,
    ADJUSTMENT_REASON,
    ADJUSTMENT_TO_DATE,
    ADJUSTMENT_VAT_RATE,
    DEFAULT_RECORD_RUN_LIMIT,
    DEFAULT_RECORD_RUN_MODE,
    DEFAULT_SIGNING_PIN,
    MAX_CONCURRENT_TASKS,
    INVOICE_ADJUSTMENT_URL,
    PROFILE_DIR,
    ROOT_DIR,
    TARGET_WORKING_YEAR,
    USE_WINDOWS_CHROME_PROFILE,
    WINDOWS_CHROME_USER_DATA_DIR,
    DATABASE_PATH,
    ensure_runtime_directories,
)
from database.database import Database
from services.pin_dialog import submit_pin_if_prompted


class SkipInvoiceError(Exception):
    """A known MISA validation case: finish this job as error without retrying it."""


class BrowserManager(QObject):
    log_message = Signal(str)
    job_progress_changed = Signal()
    state_changed = Signal(bool)
    finished = Signal()

    def __init__(
        self,
        retry_invoice_id: int | None = None,
        retry_errors_only: bool = False,
        worker_id: int = 1,
        use_worker_profile: bool = False,
        stop_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._retry_invoice_id = retry_invoice_id
        self._retry_errors_only = retry_errors_only
        self._worker_id = worker_id
        self._use_worker_profile = use_worker_profile
        self._stop_requested = stop_event or threading.Event()
        self._context = None
        self._playwright = None
        self._browser_restart_count = 0
        self._search_results: list[dict] = []
        self._database = Database()
        self._active_invoice = None
        self._active_job_id: int | None = None
        self._job_started_at: float | None = None
        self._job_failed = False
        self._job_error: str | None = None
        self._runtime_settings: dict[str, str] = {}

    def request_close(self) -> None:
        """Safe to call from the GUI thread; the worker performs actual cleanup."""
        self._stop_requested.set()

    @Slot()
    def run(self) -> None:
        self._stop_requested.clear()
        try:
            ensure_runtime_directories()
            self._emit("Đang mở trình duyệt...")
            with sync_playwright() as playwright:
                self._playwright = playwright
                self._context = self._launch_context(playwright)
                page = self._context.pages[0] if self._context.pages else self._context.new_page()
                page.goto(INVOICE_ADJUSTMENT_URL, wait_until="commit", timeout=20_000)
                self._emit("Đang chờ bạn đăng nhập MISA...")
                self.state_changed.emit(True)

                authenticated_page = self._wait_for_login()
                if authenticated_page is not None and not self._stop_requested.is_set():
                    page = authenticated_page
                    self._emit("Đăng nhập thành công.")
                    self._process_pending_invoices(page)

                while not self._stop_requested.wait(0.5):
                    if page.is_closed():
                        self._emit("Cửa sổ trình duyệt đã được đóng.")
                        break
        except Exception as exc:
            self._job_failed = True
            self._job_error = str(exc)
            logging.exception("Browser worker failed")
            self._emit(f"Lỗi trình duyệt: {exc}")
        finally:
            if self._stop_requested.is_set() and self._active_job_id is not None:
                self._job_failed = True
                self._job_error = "Người dùng đã dừng worker khi job đang chạy."
            self._reset_current_demo_job()
            # ``sync_playwright()`` owns the browser context and closes it as
            # the ``with`` block exits.  Calling close here happens after its
            # event loop is already stopped and only produces a false ERROR.
            self._context = None
            self._playwright = None
            self.state_changed.emit(False)
            self._emit("Trình duyệt đã đóng.")
            self.finished.emit()

    def _launch_context(self, playwright: Playwright):
        try:
            if self._use_worker_profile:
                user_data_dir = ROOT_DIR / "profile" / f"worker-{self._worker_id}"
                user_data_dir.mkdir(parents=True, exist_ok=True)
                profile_name = "Default"
                self._emit(f"Worker {self._worker_id} đang dùng profile riêng.")
            elif USE_WINDOWS_CHROME_PROFILE:
                user_data_dir, profile_name = self._chrome_profile()
                self._close_existing_chrome()
            else:
                user_data_dir, profile_name = PROFILE_DIR, "Default"
            context = playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                channel="chrome",
                headless=False,
                args=[f"--profile-directory={profile_name}"],
                timeout=20_000,
            )
            if self._use_worker_profile:
                self._emit(f"Worker {self._worker_id} đã mở Chrome profile riêng.")
            elif USE_WINDOWS_CHROME_PROFILE:
                self._emit(f"Đang dùng profile Google Chrome hiện tại ({profile_name}).")
            else:
                self._emit("Đang dùng profile riêng của tool (profile/default).")
            return context
        except Error as exc:
            logging.warning("Could not launch Chrome with the selected profile: %s", exc)
            if USE_WINDOWS_CHROME_PROFILE and not self._use_worker_profile:
                self._emit(
                    "Không thể điều khiển profile Chrome hiện tại. "
                    "Đang chuyển sang profile riêng của tool."
                )
            else:
                self._emit("Không tìm thấy Google Chrome. Đang dùng Chromium của Playwright.")
            fallback_profile = (
                ROOT_DIR / "profile" / f"worker-{self._worker_id}"
                if self._use_worker_profile else PROFILE_DIR
            )
            return playwright.chromium.launch_persistent_context(
                str(fallback_profile), headless=False, timeout=20_000
            )

    def _chrome_profile(self) -> tuple[Path, str]:
        """Return the last Chrome profile used by this Windows user."""
        if not WINDOWS_CHROME_USER_DATA_DIR.exists():
            self._emit("Không tìm thấy profile Chrome Windows. Đang dùng profile của tool.")
            return PROFILE_DIR, "Default"

        profile_name = "Default"
        local_state = WINDOWS_CHROME_USER_DATA_DIR / "Local State"
        try:
            state = json.loads(local_state.read_text(encoding="utf-8"))
            profile_name = state.get("profile", {}).get("last_used", "Default")
        except (OSError, json.JSONDecodeError):
            logging.warning("Could not determine the last Chrome profile; using Default.")
        return WINDOWS_CHROME_USER_DATA_DIR, profile_name

    def _close_existing_chrome(self) -> None:
        """Release Chrome's profile lock before Playwright launches it.

        The user explicitly approved closing all open Chrome windows when the
        tool is started, so the current signed-in Chrome profile can be reused.
        """
        self._emit("Đang đóng các cửa sổ Chrome để dùng phiên đăng nhập hiện tại...")
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Chrome needs a moment to release its user-data lock.
            self._stop_requested.wait(1)
        except OSError as exc:
            logging.warning("Unable to close existing Chrome windows: %s", exc)

    def _wait_for_login(self):
        """Return the authenticated MISA tab, including tabs MISA opens itself."""
        while not self._stop_requested.is_set():
            try:
                for page in self._context.pages:
                    if page.is_closed():
                        continue
                    if self._is_authenticated_misa_page(page):
                        self._emit(f"Đã phát hiện phiên MISA đăng nhập tại: {page.url}")
                        return page
            except Error:
                return None
            self._stop_requested.wait(0.5)
        return None

    def _process_pending_invoices(self, page) -> None:
        """Process pending invoices sequentially until quota, data, or user stop."""
        while not self._stop_requested.is_set():
            if not self._load_first_pending_invoice():
                self._emit("Không còn bản ghi phù hợp để chạy.")
                return
            try:
                self._emit("Đang chuyển tới trang điều chỉnh hóa đơn...")
                page = self._navigate_to_adjustment_page(page)
                self._ensure_working_year(page)
                self._open_adjust_invoice_dialog(page)
                self._search_adjustment_invoice(page)
                self._select_first_adjustment_invoice(page)
                if not self._fill_adjustment_form_with_retry(page):
                    self._reset_current_demo_job()
                    continue
                self._emit("Đang lưu và phát hành hóa đơn điều chỉnh...")
                self._save_adjustment_invoice(page)
                self._emit("Đã lưu và phát hành hóa đơn điều chỉnh thành công.")
            except (Error, SkipInvoiceError) as exc:
                self._job_failed = True
                self._job_error = str(exc)
                logging.warning("Adjustment invoice processing failed: %s", exc)
                self._emit(f"Lỗi xử lý hóa đơn điều chỉnh: {exc}")
            finally:
                self._reset_current_demo_job()
            if self._retry_invoice_id is not None:
                return
            self._stop_requested.wait(0.8)

    def _navigate_to_adjustment_page(self, page):
        """Navigate resiliently when MISA is slow to finish loading auxiliary assets."""
        last_error: Error | None = None
        for attempt in range(1, 4):
            try:
                page.goto(INVOICE_ADJUSTMENT_URL, wait_until="commit", timeout=20_000)
                page.locator(
                    "button[data-command='CreateAdjustInvoice']:visible"
                ).last.wait_for(state="visible", timeout=15_000)
                if attempt > 1:
                    self._emit("Da khoi phuc trinh duyet va vao lai trang dieu chinh.")
                return page
            except Error as exc:
                last_error = exc
                if self._browser_restart_count >= 2 or attempt == 3:
                    break
                page = self._restart_browser_for_current_job(attempt + 1)
        raise Error(
            "Khong the vao trang dieu chinh sau khi khoi dong lai trinh duyet: "
            f"{last_error}"
        )

        # Legacy navigation fallback below is intentionally unreachable.
        last_error: Error | None = None
        for attempt in range(1, 4):
            try:
                # ``commit`` only waits for MISA to start returning the page;
                # waiting for domcontentloaded is unnecessarily fragile here.
                page.goto(INVOICE_ADJUSTMENT_URL, wait_until="commit", timeout=20_000)
                page.locator(
                    "button[data-command='CreateAdjustInvoice']:visible"
                ).last.wait_for(state="visible", timeout=15_000)
                if attempt > 1:
                    self._emit("Đã vào lại trang điều chỉnh hóa đơn.")
                return
            except Error as exc:
                last_error = exc
                if attempt == 3:
                    break
                delay = 2 * attempt
                self._emit(
                    f"Trang điều chỉnh phản hồi chậm; thử lại {attempt + 1}/3 sau {delay} giây..."
                )
                if self._stop_requested.wait(delay):
                    raise Error("Đã dừng khi đang chuyển tới trang điều chỉnh hóa đơn.")
        raise Error(
            "Không thể vào trang điều chỉnh hóa đơn sau 3 lần thử: "
            f"{last_error}"
        )

    def _restart_browser_for_current_job(self, next_attempt: int):
        """Close and relaunch this worker's isolated Chrome profile, keeping its job claim."""
        if self._playwright is None:
            raise Error("Khong the khoi dong lai trinh duyet vi Playwright da dung.")
        self._browser_restart_count += 1
        self._emit(
            f"Trang phan hoi cham; dong Chrome va khoi dong lai ({next_attempt}/3)..."
        )
        self.state_changed.emit(False)
        if self._context is not None:
            try:
                self._context.close()
            except Error:
                # A timed-out page can already have closed its transport.
                pass
        if self._stop_requested.wait(1):
            raise Error("Da dung khi dang khoi dong lai trinh duyet.")
        self._context = self._launch_context(self._playwright)
        self.state_changed.emit(True)
        return self._context.pages[0] if self._context.pages else self._context.new_page()

    def _save_adjustment_invoice(self, page) -> None:
        # The invalid-MST dialog can be returned asynchronously, after the
        # remainder of the form has already been filled.  Check again at the
        # final gate before trying to save.
        self._update_customer_address_if_prompted(page, timeout=750)
        self._skip_if_buyer_tax_code_is_invalid(page, timeout=750)
        self._continue_save_invoice_if_prompted(page, timeout=750)
        save_button = page.locator("button[data-command='SaveAndPublish']:visible").last
        save_button.wait_for(state="visible", timeout=15_000)
        self._skip_if_buyer_tax_code_is_invalid(page, timeout=750)
        with page.expect_response(
            lambda response: "/v3/invoicewithcodedata/save" in response.url
            and response.request.method == "POST",
            timeout=30_000,
        ) as response_info:
            save_button.click()
            # This warning is raised by MISA *after* the initial Save click,
            # not when the tax code is entered.  Confirm it before awaiting
            # the save response, otherwise the modal blocks the request.
            self._continue_save_invoice_if_prompted(page, timeout=3_000)
            self._publish_invoice_without_sending_to_customer(page)
        response = response_info.value
        if not response.ok:
            raise Error(f"MISA trả về HTTP {response.status} khi lưu và phát hành hóa đơn.")
        payload = response.json()
        if not payload.get("success"):
            raise Error(f"MISA không xác nhận lưu và phát hành thành công: {payload}")
        page.wait_for_timeout(800)

    def _publish_invoice_without_sending_to_customer(self, page) -> None:
        """Uncheck customer delivery in MISA's publish screen, then publish."""
        publish_button = page.locator(
            "input#btn-publish-invoice[data-command='Public']:visible"
        ).last
        try:
            publish_button.wait_for(state="visible", timeout=10_000)
        except Error:
            # Some MISA configurations publish directly without this screen.
            return

        result = page.evaluate(
            """labelText => {
                const label = [...document.querySelectorAll('.sendTemplateName')]
                    .find(element => element.textContent.includes(labelText));
                if (!label) return { found: false, checked: false };
                let scope = label;
                let checkbox = null;
                for (let depth = 0; scope && depth < 4 && !checkbox; depth++, scope = scope.parentElement) {
                    checkbox = scope.querySelector("input[type='checkbox']");
                }
                if (!checkbox) return { found: false, checked: false };
                if (checkbox.checked) checkbox.click();
                return { found: true, checked: checkbox.checked };
            }""",
            "G\u1eedi h\u00f3a \u0111\u01a1n cho kh\u00e1ch h\u00e0ng",
        )
        if not result["found"] or result["checked"]:
            raise Error("Không thể bỏ chọn gửi hóa đơn cho khách hàng trước khi phát hành.")

        self._emit("Đã bỏ chọn gửi hóa đơn cho khách hàng.")
        publish_button.click()
        self._confirm_publish_warning_if_prompted(page)
        self._wait_for_publish_or_skip_kyso_unavailable(
            page, publish_button, self._runtime_settings.get("signing_pin", "")
        )
        self._emit("Đã bấm Phát hành hóa đơn.")

    def _wait_for_publish_or_skip_kyso_unavailable(self, page, publish_button, pin: str) -> None:
        """Wait for publish completion but fail fast if the KYSO bridge is unavailable."""
        deadline = time.monotonic() + 30
        pin_submitted = False
        while time.monotonic() < deadline:
            self._skip_if_misa_kyso_unavailable(page, timeout=250)
            if not pin_submitted and submit_pin_if_prompted(pin):
                pin_submitted = True
                self._emit("Đã gửi mã PIN cho ứng dụng ký số.")
            try:
                if not publish_button.is_visible():
                    return
            except Error:
                # MISA can replace the publish dialog after a successful request.
                return
            page.wait_for_timeout(250)
        raise Error("MISA did not complete invoice publishing within 30 seconds.")

    def _skip_if_misa_kyso_unavailable(self, page, timeout: int = 250) -> None:
        """Close the KYSO-unavailable popup and mark this invoice as failed."""
        dialog = page.locator("div[role='dialog']").filter(
            has_text="Công cụ MISA KYSO chưa hoạt động"
        ).last
        try:
            dialog.wait_for(state="visible", timeout=timeout)
        except Error:
            return

        close_button = dialog.locator(
            "input[data-command='Cancel'][value='Đóng']:visible"
        ).last
        if close_button.count() == 0:
            close_button = dialog.locator("button").filter(has_text="Đóng").last
        close_button.wait_for(state="visible", timeout=3_000)
        close_button.click()
        try:
            dialog.wait_for(state="hidden", timeout=5_000)
        except Error:
            pass
        raise SkipInvoiceError(
            "Công cụ MISA KYSO chưa hoạt động; đã bỏ qua bản ghi và đánh dấu lỗi."
        )

    def _confirm_publish_warning_if_prompted(self, page) -> None:
        """Accept MISA's explicit warning about an incomplete item name during publish."""
        dialog = page.locator("div[role='dialog']").filter(
            has_text="chưa nhập đầy đủ thông tin hàng hóa, dịch vụ"
        ).last
        try:
            dialog.wait_for(state="visible", timeout=10_000)
        except Error:
            return

        yes_button = dialog.locator("button").filter(has_text="Có").last
        yes_button.wait_for(state="visible", timeout=5_000)
        yes_button.click()
        dialog.wait_for(state="hidden", timeout=15_000)
        self._emit("Đã xác nhận Có cho cảnh báo phát hành hóa đơn.")

    def _load_first_pending_invoice(self) -> bool:
        """Load only the oldest pending row for the current test run."""
        self._database.initialize()
        if self._retry_invoice_id is not None:
            row = self._database.get_invoice(self._retry_invoice_id)
        else:
            run_mode = "all" if self._retry_errors_only else (
                self._database.get_setting("record_run_mode", DEFAULT_RECORD_RUN_MODE) or DEFAULT_RECORD_RUN_MODE
            )
            run_limit = int(
                self._database.get_setting("record_run_limit", str(DEFAULT_RECORD_RUN_LIMIT))
                or DEFAULT_RECORD_RUN_LIMIT
            )
            row = self._database.claim_next_invoice(
                run_mode, run_limit, retry_errors_only=self._retry_errors_only
            )
        if row is None:
            pending_count = self._database.count_pending_invoices()
            self._emit(
                f"Không có dữ liệu status = 0 phù hợp trong {DATABASE_PATH} "
                f"(tổng bản ghi có MST2: {pending_count})."
            )
            return False
        try:
            invoice_date = datetime.strptime(row["date"].strip(), "%d/%m/%Y")
        except (TypeError, ValueError):
            self._emit(f"Ngày hóa đơn không hợp lệ ở dòng ID {row['id']}: {row['date']}")
            return False

        self._active_invoice = dict(row)
        self._browser_restart_count = 0
        self._runtime_settings = {
            "adjustment_reason": self._database.get_setting("adjustment_reason", ADJUSTMENT_REASON) or ADJUSTMENT_REASON,
            "adjustment_item_name": self._database.get_setting("adjustment_item_name", ADJUSTMENT_ITEM_NAME) or ADJUSTMENT_ITEM_NAME,
            "adjustment_vat_rate": self._database.get_setting("adjustment_vat_rate", ADJUSTMENT_VAT_RATE) or ADJUSTMENT_VAT_RATE,
            "max_concurrent_tasks": self._database.get_setting("max_concurrent_tasks", str(MAX_CONCURRENT_TASKS)) or "1",
            "record_run_mode": self._database.get_setting("record_run_mode", DEFAULT_RECORD_RUN_MODE) or DEFAULT_RECORD_RUN_MODE,
            "record_run_limit": self._database.get_setting("record_run_limit", str(DEFAULT_RECORD_RUN_LIMIT)) or str(DEFAULT_RECORD_RUN_LIMIT),
            "signing_pin": self._database.get_setting("signing_pin", DEFAULT_SIGNING_PIN) or "",
        }
        self._active_job_id = self._database.start_demo_job(row["id"])
        self._job_started_at = time.monotonic()
        self._job_failed = False
        self._job_error = None
        self._active_invoice["from_date"] = invoice_date.strftime("%d/%m/%Y")
        self._active_invoice["to_date"] = (invoice_date + timedelta(days=1)).strftime("%d/%m/%Y")
        self._emit(
            f"Đang chạy thử dữ liệu ID {row['id']}, hóa đơn {row['hoa_don']}, "
            f"ngày {self._active_invoice['from_date']} - {self._active_invoice['to_date']}."
        )
        return True

    def _reset_current_demo_job(self) -> None:
        if self._active_invoice is not None and self._active_job_id is not None:
            duration = round(time.monotonic() - (self._job_started_at or time.monotonic()), 2)
            job_status = "error" if self._job_failed else "completed"
            self._database.add_job_log(
                self._active_job_id,
                "ERROR" if self._job_failed else "SUCCESS",
                f"{'Lỗi sau' if self._job_failed else 'Hoàn tất'} {duration:.2f} giây.",
            )
            self._database.reset_demo_job(
                self._active_invoice["id"], duration, job_status, self._job_error
            )
            self._active_job_id = None
            self._job_started_at = None
            self.job_progress_changed.emit()

    @staticmethod
    def _is_authenticated_misa_page(page) -> bool:
        """Detect a logged-in MISA view by route or a signed-in shell element."""
        url = page.url.lower()
        if "app3.meinvoice.vn" in url and "/v3/" in url and "login" not in url:
            return True
        try:
            return page.locator(".year-of-work-value").first.is_visible(timeout=500)
        except Error:
            return False

    def _ensure_working_year(self, page) -> None:
        """Set MISA's active working year when the adjustment page is loaded."""
        if "/v3/xu-ly-hd/dieu-chinh" not in page.url.lower():
            self._emit("Chưa vào đúng trang điều chỉnh hóa đơn; bỏ qua kiểm tra năm làm việc.")
            return

        try:
            year_value = page.locator(".year-of-work-value").first
            current_year = year_value.inner_text(timeout=10_000).strip()
            if current_year == TARGET_WORKING_YEAR:
                self._emit(f"Năm làm việc đang là {TARGET_WORKING_YEAR}.")
                return

            self._emit(
                f"Năm làm việc hiện tại là {current_year}. "
                f"Đang chuyển sang năm {TARGET_WORKING_YEAR}..."
            )
            year_value.click()
            year_option = page.locator(
                f".dropdown-content [data-year='{TARGET_WORKING_YEAR}']"
            ).first
            with page.expect_response(
                lambda response: "/systemconfig/update" in response.url
                and response.request.method == "POST",
                timeout=15_000,
            ) as response_info:
                year_option.click()

            response = response_info.value
            if not response.ok:
                raise Error(f"MISA trả về HTTP {response.status} khi cập nhật năm làm việc.")

            page.locator(".year-of-work-value").filter(
                has_text=TARGET_WORKING_YEAR
            ).wait_for(state="visible", timeout=15_000)
            self._emit(f"Đã chuyển năm làm việc sang {TARGET_WORKING_YEAR}.")
        except Error as exc:
            logging.warning("Could not verify or update MISA working year: %s", exc)
            self._emit(f"Không thể kiểm tra hoặc đổi năm làm việc: {exc}")

    def _open_adjust_invoice_dialog(self, page) -> None:
        """Open MISA's invoice-selection dialog; no invoice is selected yet."""
        try:
            self._emit("\u0110ang m\u1edf c\u1eeda s\u1ed5 ch\u1ecdn h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh...")
            for attempt in range(1, 4):
                button = page.locator("button[data-command='CreateAdjustInvoice']:visible").last
                button.wait_for(state="visible", timeout=5_000)
                if attempt == 1:
                    button.click(force=True)
                else:
                    # MISA can ignore a pointer click while its toolbar refreshes.
                    button.evaluate("element => element.click()")
                try:
                    page.wait_for_function(
                        """() => {
                            const visible = element => element && element.offsetParent !== null
                                && getComputedStyle(element).display !== 'none'
                                && getComputedStyle(element).visibility !== 'hidden';
                            return [...document.querySelectorAll("[role='dialog'], .ui-dialog")].some(visible)
                                || visible(document.querySelector("#grdSearchInvoiceAdjust"));
                        }""",
                        timeout=5_000,
                    )
                    self._emit("\u0110\u00e3 m\u1edf c\u1eeda s\u1ed5 ch\u1ecdn h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh.")
                    return
                except Error:
                    if attempt == 3:
                        raise
                    self._emit(f"Ch\u01b0a m\u1edf \u0111\u01b0\u1ee3c c\u1eeda s\u1ed5 ch\u1ecdn h\u00f3a \u0111\u01a1n; th\u1eed l\u1ea1i {attempt + 1}/3...")
                    self._stop_requested.wait(0.5)

            button = page.locator("button[data-command='CreateAdjustInvoice']:visible").last
            button.wait_for(state="visible", timeout=15_000)
            self._emit("Đang mở cửa sổ chọn hóa đơn điều chỉnh...")
            button.click(force=True)
            page.wait_for_function(
                """() => {
                    const visible = element => element && element.offsetParent !== null
                        && getComputedStyle(element).display !== 'none'
                        && getComputedStyle(element).visibility !== 'hidden';
                    return [...document.querySelectorAll("[role='dialog'], .ui-dialog")].some(visible)
                        || visible(document.querySelector("#grdSearchInvoiceAdjust"));
                }""",
                timeout=15_000,
            )
            self._emit("Cửa sổ chọn hóa đơn điều chỉnh đã mở.")
        except Error as exc:
            logging.warning("Could not open the adjustment invoice dialog: %s", exc)
            self._emit(f"Không thể mở cửa sổ chọn hóa đơn điều chỉnh: {exc}")
            raise

    def _search_adjustment_invoice(self, page) -> None:
        """Set the adjustment-dialog date range and search an invoice number."""
        try:
            from_date = self._active_invoice["from_date"]
            to_date = self._active_invoice["to_date"]
            invoice_number = self._active_invoice["hoa_don"]
            date_button = page.locator("button[data-popupid='#adjusListPicker-popup']")
            date_button.wait_for(state="visible", timeout=15_000)
            self._emit(
                f"\u0110ang đặt khoảng ngày hóa đơn: {from_date} - {to_date}..."
            )
            date_button.click()

            date_popup = page.locator("#adjusListPicker-popup")
            date_popup.wait_for(state="visible", timeout=10_000)
            from_date = date_popup.locator("#datepicker-adjust-from-search")
            to_date = date_popup.locator("#datepicker-adjust-to-search")
            self._set_datepicker_value(from_date, self._active_invoice["from_date"])
            self._set_datepicker_value(to_date, self._active_invoice["to_date"])
            date_popup.locator("button[data-command='Filter']").click()
            date_popup.wait_for(state="hidden", timeout=10_000)
            self._emit("Đã cập nhật khoảng ngày hóa đơn.")

            invoice_input = page.locator(
                "input[placeholder*='Nh\u1eadp s\u1ed1 h\u00f3a \u0111\u01a1n']:visible"
            ).last
            invoice_input.fill(invoice_number)
            self._emit(f"Đang tìm số hóa đơn {invoice_number}...")
            search_button = page.get_by_role("button", name="T\u00ecm kiếm", exact=True).last
            with page.expect_response(
                lambda response: "/v3/invoice/search/list/v5" in response.url
                and response.request.method == "GET",
                timeout=30_000,
            ) as response_info:
                search_button.click()

            response = response_info.value
            if not response.ok:
                raise Error(f"MISA returned HTTP {response.status} while searching invoices.")
            self._emit("Đã nhận kết quả tìm kiếm hóa đơn.")
        except Error as exc:
            logging.warning("Could not search adjustment invoice: %s", exc)
            self._emit(f"Không thể thiết lập điều kiện tìm kiếm hóa đơn: {exc}")
            raise

    def _search_adjustment_invoice_api(self, page) -> None:
        """Query MISA's invoice API with the authenticated browser session."""
        try:
            self._emit(
                f"\u0110ang t\u00ecm h\u00f3a \u0111\u01a1n t\u1eeb {ADJUSTMENT_FROM_DATE} \u0111\u1ebfn {ADJUSTMENT_TO_DATE}..."
            )
            query = urlencode(
                {
                    "draw": 2,
                    "fromDate": json.dumps(self._api_date(ADJUSTMENT_FROM_DATE, False)),
                    "toDate": json.dumps(self._api_date(ADJUSTMENT_TO_DATE, True)),
                    "searchKey": ADJUSTMENT_INVOICE_NUMBER,
                    "listFieldString": json.dumps(["InvNo"]),
                    "module": "AdjustmentEinvoiceWithCode",
                    "isLoadCloud": "false",
                    "invMethod": 1,
                    "sort": "InvTemplateNoAndSeries asc",
                    "filter": "[]",
                    "start": 0,
                    "length": 2000,
                    "pagingType": 0,
                    "_": int(time.time() * 1000),
                }
            )
            result = page.evaluate(
                """async (query) => {
                    const token = document.querySelector("[name='__requestverificationtoken']")?.value;
                    const headers = {
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "X-Requested-With": "XMLHttpRequest"
                    };
                    if (token) headers.__requestverificationtoken = token;
                    const response = await fetch(`/v3/invoice/search/list/v5?${query}`, {
                        credentials: "same-origin", headers
                    });
                    return { status: response.status, body: await response.text() };
                }""",
                query,
            )
            if result["status"] < 200 or result["status"] >= 300:
                raise Error(f"MISA returned HTTP {result['status']} while searching invoices.")
            payload = json.loads(result["body"])
            self._search_results = json.loads(payload.get("data") or "[]")
            self._emit(f"\u0110\u00e3 nh\u1eadn {len(self._search_results)} h\u00f3a \u0111\u01a1n t\u1eeb API t\u00ecm ki\u1ebfm.")
        except (Error, ValueError) as exc:
            logging.warning("Could not search adjustment invoice using API: %s", exc)
            self._emit(f"Kh\u00f4ng th\u1ec3 t\u00ecm ki\u1ebfm h\u00f3a \u0111\u01a1n qua API: {exc}")

    def _select_first_adjustment_invoice(self, page) -> None:
        """Select the first displayed source invoice and proceed to its adjustment form."""
        try:
            first_row = page.locator("#grdSearchInvoiceAdjust tbody tr").first
            first_row.wait_for(state="visible", timeout=20_000)
            self._emit("\u0110ang ch\u1ecdn h\u00f3a \u0111\u01a1n \u0111\u1ea7u ti\u00ean trong danh s\u00e1ch...")
            first_row.click()
            first_row.wait_for(state="visible", timeout=5_000)

            confirm_button = page.locator("button[data-command='Agree']")
            confirm_button.wait_for(state="visible", timeout=10_000)
            self._emit("\u0110ang x\u00e1c nh\u1eadn h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh...")
            confirm_button.click()
            self._confirm_existing_adjustment_dialog(page)
            page.get_by_text("L\u1eadp h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh", exact=True).wait_for(
                state="visible", timeout=30_000
            )
            self._emit("\u0110\u00e3 m\u1edf m\u00e0n h\u00ecnh l\u1eadp h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh.")
        except Error as exc:
            logging.warning("Could not select the first adjustment invoice: %s", exc)
            self._emit(f"Kh\u00f4ng th\u1ec3 ch\u1ecdn h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh: {exc}")
            raise

    def _confirm_existing_adjustment_dialog(self, page) -> None:
        """Accept MISA's explicit confirmation when an adjustment already exists."""
        required_text = "Hóa đơn này đã được lập hóa đơn điều chỉnh"
        try:
            page.wait_for_function(
                """requiredText => {
                    const visible = element => {
                        const style = window.getComputedStyle(element);
                        return style.display !== 'none' && style.visibility !== 'hidden' && element.offsetParent !== null;
                    };
                    const dialog = [...document.querySelectorAll('[role="dialog"], .ui-dialog, .m-dialog')]
                        .filter(visible)
                        .find(element => element.innerText.includes(requiredText));
                    if (!dialog) return false;
                    const yesButton = [...dialog.querySelectorAll('button')]
                        .find(button => button.textContent.trim() === 'Có' && visible(button));
                    if (!yesButton) return false;
                    yesButton.click();
                    return true;
                }""",
                arg=required_text,
                timeout=1_500,
            )
        except Error:
            # No dialog is normal for invoices that have never been adjusted.
            body_text = page.locator("body").inner_text(timeout=2_000)
            if required_text in body_text:
                raise Error("Đã thấy popup hóa đơn điều chỉnh nhưng không thể bấm nút Có.")
            return
        self._emit("Hóa đơn đã có điều chỉnh; đang xác nhận tiếp tục lập hóa đơn.")
        page.wait_for_function(
            """requiredText => ![...document.querySelectorAll('[role="dialog"], .ui-dialog, .m-dialog')]
                .some(element => element.innerText.includes(requiredText) && element.offsetParent !== null)""",
            arg=required_text,
            timeout=10_000,
        )
        self._emit("Đã xác nhận tiếp tục lập hóa đơn điều chỉnh.")

    def _fill_adjustment_form_with_retry(self, page) -> bool:
        for attempt in range(1, ADJUSTMENT_FORM_MAX_RETRIES + 1):
            try:
                if self._fill_adjustment_form(page):
                    return True
            except SkipInvoiceError as exc:
                self._job_failed = True
                self._job_error = str(exc)
                self._emit(str(exc))
                return False
            if attempt < ADJUSTMENT_FORM_MAX_RETRIES:
                self._emit(
                    f"Th\u1eed l\u1ea1i thao t\u00e1c \u0111i\u1ec1u ch\u1ec9nh l\u1ea7n {attempt + 1}/{ADJUSTMENT_FORM_MAX_RETRIES}..."
                )
                self._stop_requested.wait(1.5)
        self._emit(
            f"Kh\u00f4ng th\u1ec3 ho\u00e0n t\u1ea5t thao t\u00e1c \u0111i\u1ec1u ch\u1ec9nh sau {ADJUSTMENT_FORM_MAX_RETRIES} l\u1ea7n th\u1eed."
        )
        self._job_failed = True
        self._job_error = "Không thể hoàn tất thao tác điều chỉnh sau số lần retry đã cấu hình."
        return False

    def _fill_adjustment_form(self, page) -> bool:
        """Fill buyer tax code, requested reason, first item description, and VAT rate."""
        try:
            self._select_adjustment_invoice_series(page)
            self._fill_buyer_tax_code(page)
            reason = page.locator("textarea#reason-changes:visible").first
            reason.wait_for(state="attached", timeout=30_000)
            reason.scroll_into_view_if_needed(timeout=10_000)
            reason.fill(self._format_adjustment_text(self._runtime_settings["adjustment_reason"]))
            reason.evaluate(
                """element => {
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                    element.dispatchEvent(new Event('blur', { bubbles: true }));
                }"""
            )
            self._emit("\u0110\u00e3 nh\u1eadp l\u00fd do \u0111i\u1ec1u ch\u1ec9nh.")

            first_item_cell = page.locator(
                "#grdSAOrderViewDetail tbody tr:first-child td:nth-child(3)"
            )
            first_item_cell.wait_for(state="visible", timeout=10_000)
            first_item_cell.click()
            item_input = first_item_cell.locator("input, textarea").first
            item_input.wait_for(state="visible", timeout=10_000)
            item_input.fill(self._build_adjustment_item_name())
            item_input.press("Enter")
            self._emit("\u0110\u00e3 nh\u1eadp t\u00ean h\u00e0ng h\u00f3a/d\u1ecbch v\u1ee5.")

            vat_label = page.get_by_text("Thu\u1ebf GTGT:", exact=True)
            vat_input = vat_label.locator(
                "xpath=following::input[@name='VATRate' and not(@type='hidden')][1]"
            )
            vat_input.wait_for(state="visible", timeout=10_000)
            vat_input.scroll_into_view_if_needed(timeout=10_000)
            vat_input.click()
            vat_input.clear()
            vat_input.fill(self._runtime_settings["adjustment_vat_rate"])
            vat_input.evaluate(
                """element => {
                    element.dispatchEvent(new Event('input', { bubbles: true }));
                    element.dispatchEvent(new Event('change', { bubbles: true }));
                    element.dispatchEvent(new Event('blur', { bubbles: true }));
                }"""
            )
            page.get_by_text("Thu\u1ebf GTGT:", exact=True).click()
            self._emit("\u0110\u00e3 nh\u1eadp Thu\u1ebf GTGT 5%.")
            self._write_adjustment_reason(page)
            return True
        except Error as exc:
            logging.warning("Could not fill the adjustment form: %s", exc)
            self._emit(f"Kh\u00f4ng th\u1ec3 nh\u1eadp th\u00f4ng tin h\u00f3a \u0111\u01a1n \u0111i\u1ec1u ch\u1ec9nh: {exc}")
            return False

    def _fill_buyer_tax_code(self, page) -> None:
        """Enter MST2 in MISA's buyer tax-code autocomplete and commit its value."""
        tax_code = (self._active_invoice["mst2"] or "").strip()
        if not tax_code:
            raise Error("Bản ghi không có MST2 để nhập vào thông tin người mua.")

        # MISA sometimes renders the autocomplete input beneath a loading layer;
        # Playwright's ``:visible`` selector then never resolves even though the
        # real #taxCode element is already present.  Address the concrete input
        # from its DOM contract and dispatch the same events as a normal edit.
        # MISA's current adjustment form uses ``select-taxCode``; older
        # versions used ``taxCode``.  Both keep the AccountObjectTaxCode name.
        selector = (
            "input#select-taxCode[name='AccountObjectTaxCode'], "
            "input#taxCode[name='AccountObjectTaxCode'], "
            "input#select-taxCode, input#taxCode"
        )
        tax_frame = self._find_frame_with_tax_code(page, selector)
        result = tax_frame.evaluate(
            """({ selector, value }) => {
                const candidates = [...document.querySelectorAll(selector)];
                const input = candidates.find(element => {
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden';
                }) || candidates.at(-1);
                if (!input) return { found: false, value: '' };

                input.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(input, value);
                input.dispatchEvent(new InputEvent('input', {
                    bubbles: true, inputType: 'insertText', data: value,
                }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                input.blur();
                return { found: true, value: input.value.trim() };
            }""",
            {"selector": selector, "value": tax_code},
        )
        if not result["found"] or result["value"] != tax_code:
            raise Error("Không thể ghi MST vào trường MST/CCCD chủ hộ của MISA.")
        self._emit(f"Đã nhập MST/CCCD chủ hộ: {tax_code}.")
        self._update_customer_address_if_prompted(page, timeout=750)
        self._skip_if_buyer_tax_code_is_invalid(page, timeout=750)
        self._continue_save_invoice_if_prompted(page, timeout=750)

    def _continue_save_invoice_if_prompted(self, page, timeout: int = 750) -> None:
        """Continue every MISA warning dialog that explicitly permits saving."""
        continue_button = page.locator(
            "div[role='dialog'] button.btn.btn-primary"
        ).filter(has_text="Ti\u1ebfp t\u1ee5c l\u01b0u h\u00f3a \u0111\u01a1n").last
        try:
            continue_button.wait_for(state="visible", timeout=timeout)
        except Error:
            return

        dialog = continue_button.locator("xpath=ancestor::div[@role='dialog'][1]")
        continue_button.click()
        dialog.wait_for(state="hidden", timeout=15_000)
        self._emit("\u0110\u00e3 x\u00e1c nh\u1eadn ti\u1ebfp t\u1ee5c l\u01b0u h\u00f3a \u0111\u01a1n theo popup MISA.")

    def _update_customer_address_if_prompted(self, page, timeout: int = 750) -> None:
        """Accept either MISA customer-address refresh dialog after selecting its first option."""
        dialog = page.locator("div[role='dialog']").filter(
            has_text="Cập nhật địa chỉ khách hàng"
        ).last
        try:
            dialog.wait_for(state="visible", timeout=timeout)
        except Error:
            # This prompt appears only when MISA detects a customer-address change.
            return

        # The detailed variant contains a customer-unit table and requires an
        # explicit radio choice before the update can be submitted.
        address_option = dialog.locator(
            "#grdUpdateAddressMisaData input[type='radio'][data-field='Select'][name='select']"
        ).first
        if address_option.count() > 0:
            address_option.check(force=True)
            if not address_option.is_checked():
                raise Error("Không thể chọn địa chỉ khách hàng mới nhất trong popup MISA.")
            self._emit("Đã chọn địa chỉ khách hàng mới nhất trong popup MISA.")

        update_button = dialog.locator(
            "button#btnAgree[data-command='Agree']:visible"
        ).last
        if update_button.count() == 0:
            update_button = dialog.get_by_role("button", name="Cập nhật ngay", exact=True)
        update_button.wait_for(state="visible", timeout=5_000)
        update_button.click()
        dialog.wait_for(state="hidden", timeout=15_000)
        self._emit("Đã cập nhật địa chỉ khách hàng theo popup của MISA.")

    def _skip_if_buyer_tax_code_is_invalid(self, page, timeout: int = 750) -> None:
        """Close MISA's invalid-tax-code dialog and skip this invoice without retries."""
        dialog = page.locator("div[role='dialog']").filter(
            has_text="MST/CCCD chủ hộ không hợp lệ"
        ).last
        try:
            dialog.wait_for(state="visible", timeout=timeout)
        except Error:
            return

        close_button = dialog.locator("button.btn.blue").filter(has_text="Đóng").last
        close_button.wait_for(state="visible", timeout=5_000)
        close_button.click()
        dialog.wait_for(state="hidden", timeout=10_000)
        raise SkipInvoiceError(
            "MST/CCCD chủ hộ không hợp lệ; đã bỏ qua bản ghi và đánh dấu lỗi."
        )

    def _continue_after_tax_status_warning_if_prompted(self, page, timeout: int = 750) -> None:
        """Continue when MISA warns that the buyer's tax code is no longer active."""
        dialog = page.locator("div[role='dialog']").filter(
            has_text="NNT ngừng hoạt động và đã hoàn thành thủ tục chấm dứt hiệu lực MST"
        ).last
        try:
            dialog.wait_for(state="visible", timeout=timeout)
        except Error:
            return

        continue_button = dialog.locator("button.btn.btn-primary").filter(
            has_text="Tiếp tục lưu hóa đơn"
        ).last
        continue_button.wait_for(state="visible", timeout=5_000)
        continue_button.click()
        dialog.wait_for(state="hidden", timeout=15_000)
        self._emit("Đã xác nhận tiếp tục lưu hóa đơn sau cảnh báo trạng thái MST.")

    def _find_frame_with_tax_code(self, page, selector: str):
        """Find MISA's buyer-tax input whether it is rendered in the page or a frame."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for frame in page.frames:
                try:
                    if frame.locator(selector).count() > 0:
                        return frame
                except Error:
                    # MISA can replace an embedded frame while the form loads.
                    continue
            if self._stop_requested.wait(0.5):
                raise Error("Đã dừng trước khi tìm thấy trường MST/CCCD chủ hộ.")
        frame_urls = ", ".join(frame.url for frame in page.frames)
        raise Error(
            "Không tìm thấy trường MST/CCCD chủ hộ (select-taxCode/taxCode) sau 30 giây. "
            f"Frames đang có: {frame_urls}"
        )

    def _select_adjustment_invoice_series(self, page) -> None:
        series_input = page.locator("input#select-invSeries")
        series_input.wait_for(state="visible", timeout=15_000)
        self._emit("Đang chọn ký hiệu hóa đơn đầu tiên...")
        series_input.click()
        dropdown = page.locator(".autocomplete-dropdown:visible")
        dropdown.wait_for(state="visible", timeout=10_000)
        option = dropdown.locator("tbody tr").first
        if option.count() == 0:
            option = dropdown.locator("li, [data-value]").first
        option.wait_for(state="visible", timeout=10_000)
        option.click()
        page.wait_for_function(
            """() => Boolean(document.querySelector('#select-invSeries')?.value.trim())""",
            timeout=10_000,
        )
        self._emit(f"Đã chọn ký hiệu hóa đơn {series_input.input_value().strip()}.")

    def _write_adjustment_reason(self, page) -> None:
        """Write and verify the visible adjustment-reason textarea."""
        reason = page.locator("textarea#reason-changes:visible").first
        reason.wait_for(state="visible", timeout=10_000)
        reason.scroll_into_view_if_needed(timeout=10_000)
        reason.click()
        expected_reason = self._format_adjustment_text(self._runtime_settings["adjustment_reason"])
        reason.fill(expected_reason)
        reason.press("Tab")
        page.wait_for_function(
            """expected => {
                const element = document.querySelector('textarea#reason-changes:not([style*="display: none"])');
                return element && element.value.trim() === expected;
            }""",
            arg=expected_reason,
            timeout=10_000,
        )
        self._emit("\u0110\u00e3 x\u00e1c nh\u1eadn l\u00fd do \u0111i\u1ec1u ch\u1ec9nh.")

    def _build_adjustment_item_name(self) -> str:
        """Build the item description from the current imported data row."""
        return self._format_adjustment_text(self._runtime_settings["adjustment_item_name"])
        customer_name = (self._active_invoice["ten_khach_hang"] or "").strip()
        tax_code = (self._active_invoice["mst2"] or "").strip()
        if not customer_name or not tax_code:
            raise Error("Dữ liệu thử nghiệm thiếu tên khách hàng hoặc mst2.")
        return (
            f"Bổ sung thêm mã số thuế của {customer_name} là: {tax_code} "
            f"của hóa đơn {self._active_invoice['hoa_don']} "
            f"xuất ngày {self._active_invoice['date']}"
        )

    def _format_adjustment_text(self, template: str) -> str:
        values = {
            "ten_khach_hang": (self._active_invoice["ten_khach_hang"] or "").strip(),
            "mst2": (self._active_invoice["mst2"] or "").strip(),
            "hoa_don": self._active_invoice["hoa_don"],
            "date": self._active_invoice["date"],
        }
        try:
            return template.format(**values)
        except KeyError as exc:
            raise Error(f"Tham số cấu hình không hợp lệ: {exc}.") from exc

    @staticmethod
    def _api_date(value: str, end_of_day: bool) -> str:
        day, month, year = value.split("/")
        suffix = "23:59:59.000Z" if end_of_day else "00:00:00.000Z"
        return f"{year}-{month}-{day}T{suffix}"

    @staticmethod
    def _set_datepicker_value(locator, value: str) -> None:
        """Update MISA's custom datepicker and notify its JavaScript handlers."""
        locator.evaluate(
            """(element, value) => {
                const jquery = window.jQuery;
                if (jquery && jquery.fn.datepicker) {
                    jquery(element).datepicker('setDate', value);
                } else {
                    element.value = value;
                }
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                element.dispatchEvent(new Event('blur', { bubbles: true }));
            }"""
            ,
            value,
        )
        if locator.input_value() != value:
            raise Error(f"Datepicker did not accept date {value}.")

    def _emit(self, message: str) -> None:
        logging.info(message)
        if self._active_job_id is not None:
            level = "ERROR" if "Lỗi" in message or "Không thể" in message else "INFO"
            self._database.add_job_log(self._active_job_id, level, message)
            progress, step = self._progress_for_message(message)
            if progress is not None:
                self._database.update_job_progress(self._active_job_id, progress, step)
            self.job_progress_changed.emit()
        self.log_message.emit(message)

    @staticmethod
    def _progress_for_message(message: str) -> tuple[int | None, str]:
        milestones = (
            ("Đăng nhập thành công", 15, "Đã đăng nhập MISA"),
            ("Đang chuyển tới trang điều chỉnh", 25, "Đang mở trang điều chỉnh"),
            ("Năm làm việc", 35, "Đã kiểm tra năm làm việc"),
            ("Cửa sổ chọn hóa đơn điều chỉnh đã mở", 45, "Đã mở chọn hóa đơn"),
            ("Đã cập nhật khoảng ngày", 55, "Đã cập nhật khoảng ngày"),
            ("Đã nhận kết quả tìm kiếm hóa đơn", 65, "Đã tìm thấy kết quả"),
            ("Đang chọn hóa đơn đầu tiên", 75, "Đang chọn hóa đơn"),
            ("Đã mở màn hình lập hóa đơn điều chỉnh", 85, "Đã mở form điều chỉnh"),
            ("Đã nhập MST/CCCD chủ hộ", 88, "Đã nhập MST/CCCD"),
            ("Đã nhập lý do điều chỉnh", 90, "Đã nhập lý do điều chỉnh"),
            ("Đã nhập tên hàng hóa", 92, "Đã nhập tên hàng hóa"),
            ("Đã nhập Thuế GTGT", 94, "Đã nhập thuế GTGT"),
            ("Đã xác nhận lý do điều chỉnh", 96, "Đã xác nhận lý do điều chỉnh"),
            ("Đang lưu và phát hành hóa đơn điều chỉnh", 98, "Đang gửi yêu cầu phát hành"),
            ("Đã lưu và phát hành hóa đơn điều chỉnh thành công", 100, "Đã phát hành hóa đơn điều chỉnh"),
            ("Trang điều chỉnh hóa đơn MISA đã sẵn sàng", 100, "Hoàn tất"),
        )
        for needle, progress, step in milestones:
            if needle in message:
                return progress, step
        return None, ""

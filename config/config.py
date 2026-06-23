"""Central paths and browser configuration."""
from __future__ import annotations

import sys
from pathlib import Path


APP_NAME = "MISA Auto Tool"
INVOICE_ADJUSTMENT_URL = "https://app3.meinvoice.vn/v3/xu-ly-hd/dieu-chinh"
TARGET_WORKING_YEAR = "2026"
ADJUSTMENT_FROM_DATE = "01/01/2026"
ADJUSTMENT_TO_DATE = "01/02/2026"
ADJUSTMENT_INVOICE_NUMBER = "01"
ADJUSTMENT_REASON = "Bổ sung mã số thuế của người mua trên hóa đơn"
ADJUSTMENT_ITEM_NAME = "Bổ sung thêm mã số thuế"
ADJUSTMENT_ITEM_TYPE = "Ghi chú/diễn giải"
ADJUSTMENT_VAT_RATE = "KHAC"
MAX_CONCURRENT_TASKS = 1
DEFAULT_RECORD_RUN_MODE = "custom"
DEFAULT_RECORD_RUN_LIMIT = 100
DEFAULT_SIGNING_PIN = "12345678"
ADJUSTMENT_FORM_MAX_RETRIES = 4


def application_root() -> Path:
    """Return the folder containing the executable, or the source project root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT_DIR = application_root()
PROFILE_DIR = ROOT_DIR / "profile" / "default"
# Temporary mode: reuse the signed-in Windows Chrome profile. Change to False
# to return to the original isolated Playwright profile at profile/default.
USE_WINDOWS_CHROME_PROFILE = False
# The user's ordinary Chrome profile. It is used when Chrome is installed so
# existing MISA login sessions remain available to the tool.
WINDOWS_CHROME_USER_DATA_DIR = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
LOG_DIR = ROOT_DIR / "logs"
DATA_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATA_DIR / "app.db"
LOG_FILE = LOG_DIR / "app.log"


def ensure_runtime_directories() -> None:
    for directory in (PROFILE_DIR, LOG_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)

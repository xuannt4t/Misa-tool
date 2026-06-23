"""Windows automation for the native signing PIN dialog."""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes


# Different signer versions use either of these titles. Unicode escapes keep
# the matching stable when the application is bundled on Windows.
PIN_DIALOG_TITLES = (
    "X\u00e1c nh\u1eadn PIN",
    "Nh\u1eadp m\u00e3 PIN",
)
VK_CONTROL = 0x11
VK_A = 0x41
VK_BACK = 0x08
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WAIT_OBJECT_0 = 0
PIN_SUBMISSION_MUTEX = r"Local\MisaAutoToolSigningPin"
_pin_submission_lock = threading.Lock()
_last_submitted_dialog: int | None = None
_last_submission_error: str | None = None


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]


def _find_pin_dialog() -> int | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    matches: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            window_title = title.value.casefold()
            if any(candidate.casefold() in window_title for candidate in PIN_DIALOG_TITLES):
                matches.append(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return matches[-1] if matches else None


def _focus_pin_control(hwnd: int) -> int | None:
    """Return the first enabled text-input control in the native signer dialog."""
    user32 = ctypes.windll.user32
    edit_controls: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(child: int, _lparam: int) -> bool:
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, class_name, len(class_name))
        control_class = class_name.value.casefold()
        # Native Windows Edit controls use ``Edit``.  Several signer clients
        # are built with Windows Forms and expose their PIN field as
        # ``WindowsForms10.EDIT...`` instead, so exact matching skips it.
        if (
            ("edit" in control_class or "textbox" in control_class)
            and user32.IsWindowVisible(child)
            and user32.IsWindowEnabled(child)
        ):
            edit_controls.append(child)
        return True

    user32.EnumChildWindows(hwnd, callback_type(visit), 0)
    if edit_controls:
        return edit_controls[0]
    return None


def _activate_pin_dialog(hwnd: int) -> int | None:
    """Focus the visible PIN input region and verify it owns keyboard focus.

    Native signer dialogs do not always accept a programmatic ``WM_SETTEXT``.
    Clicking the centre of the discovered Edit control mirrors the user action
    and, while the input queues are attached, lets us verify that the control
    actually received focus before any PIN keystrokes are sent.
    """
    user32 = ctypes.windll.user32
    foreground_thread = user32.GetWindowThreadProcessId(hwnd, None)
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        pin_control = _focus_pin_control(hwnd)
        if not pin_control:
            return None

        rect = wintypes.RECT()
        if not user32.GetWindowRect(pin_control, ctypes.byref(rect)):
            return None
        center_x = (rect.left + rect.right) // 2
        center_y = (rect.top + rect.bottom) // 2
        user32.SetCursorPos(center_x, center_y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        user32.SetFocus(pin_control)

        # GetFocus reads the attached signer input queue here, rather than the
        # listener thread's normal queue.
        if user32.GetForegroundWindow() != hwnd:
            return None
        return pin_control if user32.GetFocus() == pin_control else None
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _press_virtual_key(key: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(key, 0, 0, 0)
    user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


def _send_text(text: str) -> None:
    user32 = ctypes.windll.user32
    events: list[INPUT] = []
    for character in text:
        events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, ord(character), KEYEVENTF_UNICODE, 0, None))))
        events.append(INPUT(1, INPUTUNION(ki=KEYBDINPUT(0, ord(character), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None))))
    if events:
        array_type = INPUT * len(events)
        user32.SendInput(len(events), array_type(*events), ctypes.sizeof(INPUT))


def _control_text_length(hwnd: int) -> int:
    """Read a native Edit value without exposing the PIN in logs or UI."""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return 0
    value = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, value, len(value))
    return len(value.value)


def get_last_pin_submission_error() -> str | None:
    """Return the most recent detected PIN-popup failure without exposing PIN data."""
    return _last_submission_error


def submit_pin_if_prompted(pin: str) -> bool:
    """Fill the focused PIN control and submit the native signer dialog.

    Returns ``True`` only when an ``Xác nhận PIN`` window was found and input
    was sent.  PIN content is deliberately never returned or logged.
    """
    if not pin or os.name != "nt":
        return False
    global _last_submitted_dialog, _last_submission_error
    # Several browser workers can be running at once.  Use both a Python lock
    # and a named Windows mutex so exactly one worker types into a shared PIN
    # dialog; the others keep listening and can take the next prompt.
    if not _pin_submission_lock.acquire(blocking=False):
        return False
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, PIN_SUBMISSION_MUTEX)
    if not mutex or ctypes.windll.kernel32.WaitForSingleObject(mutex, 0) != WAIT_OBJECT_0:
        _pin_submission_lock.release()
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
        return False
    try:
        hwnd = _find_pin_dialog()
        if not hwnd:
            _last_submitted_dialog = None
            _last_submission_error = None
            return False
        if hwnd == _last_submitted_dialog:
            return False

        user32 = ctypes.windll.user32
        pin_control = _activate_pin_dialog(hwnd)
        if not pin_control:
            _last_submission_error = (
                "Đã thấy popup PIN nhưng không tìm được hoặc không focus được ô nhập. "
                "Hãy mở Trợ lý và ứng dụng ký số cùng quyền (không chạy một bên Administrator)."
            )
            return False
        # Focus can be stolen while Windows finishes showing the dialog.  Do a
        # final focus-and-foreground check immediately before sending keys.
        time.sleep(0.2)
        pin_control = _activate_pin_dialog(hwnd)
        if not pin_control:
            _last_submission_error = (
                "Popup PIN đã mất focus trước khi nhập. Hãy mở Trợ lý và ứng dụng ký số cùng quyền."
            )
            return False
        # Type into the verified focused region first.  Some signer versions
        # ignore WM_SETTEXT even though it reports success.
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        _press_virtual_key(VK_A)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        _press_virtual_key(VK_BACK)
        _send_text(pin)
        time.sleep(0.15)
        if _control_text_length(pin_control) != len(pin):
            # Retain a direct-value fallback for signer controls that do not
            # consume injected keyboard input.
            if not _activate_pin_dialog(hwnd):
                _last_submission_error = "Không thể focus lại ô PIN để nhập mã."
                return False
            user32.SetWindowTextW(pin_control, pin)
            time.sleep(0.1)
        if _control_text_length(pin_control) != len(pin):
            _last_submission_error = "Ứng dụng ký số không nhận dữ liệu nhập tự động."
            return False
        _last_submitted_dialog = hwnd
        _last_submission_error = None
        _press_virtual_key(VK_RETURN)
        return True
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)
        _pin_submission_lock.release()

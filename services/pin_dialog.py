"""Windows automation for the native signing PIN dialog."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes


PIN_DIALOG_TITLE = "Xác nhận PIN"
VK_CONTROL = 0x11
VK_A = 0x41
VK_BACK = 0x08
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


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
            if PIN_DIALOG_TITLE.casefold() in title.value.casefold():
                matches.append(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return matches[-1] if matches else None


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


def submit_pin_if_prompted(pin: str) -> bool:
    """Fill the focused PIN control and submit the native signer dialog.

    Returns ``True`` only when an ``Xác nhận PIN`` window was found and input
    was sent.  PIN content is deliberately never returned or logged.
    """
    if not pin or os.name != "nt":
        return False
    hwnd = _find_pin_dialog()
    if not hwnd:
        return False

    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    # The signer dialog focuses the PIN field by default.  Clear it first so
    # an old partial value cannot be combined with the configured PIN.
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    _press_virtual_key(VK_A)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    _press_virtual_key(VK_BACK)
    _send_text(pin)
    _press_virtual_key(VK_RETURN)
    return True

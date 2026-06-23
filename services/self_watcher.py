"""Restart an application after it exits when the user enabled that setting."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from database.database import Database


AUTO_RESTART_SETTING = "auto_restart_apps"
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF


def auto_restart_enabled(database: Database) -> bool:
    return database.get_setting(AUTO_RESTART_SETTING, "0") == "1"


def restart_command(entrypoint: Path) -> list[str]:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(entrypoint))
    return command


def start_self_watcher(entrypoint: Path) -> None:
    """Start a hidden child process that waits for this process to exit."""
    command = restart_command(entrypoint)
    command.extend(("--watch-parent", str(os.getpid())))
    subprocess.Popen(
        command,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


def run_parent_watcher(parent_pid: int, entrypoint: Path) -> int:
    """Wait for one parent process, then restart it if the setting remains enabled."""
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if handle:
            kernel32.WaitForSingleObject(handle, INFINITE)
            kernel32.CloseHandle(handle)
    else:
        while True:
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.5)

    database = Database()
    database.initialize()
    if auto_restart_enabled(database):
        subprocess.Popen(
            restart_command(entrypoint),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
    return 0

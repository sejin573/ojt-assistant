"""OJT 미니 창을 안정적으로 시작하기 위한 Windows 실행 보조 모듈.

로컬 서버의 준비 상태를 확인해 필요하면 백그라운드로 기동하고, 실행 중인 브라우저와 현재
마우스가 위치한 모니터를 조사해 미니 창의 초기 좌표를 계산한다. 실제 UI와 API 처리는
``mini_app.py``와 ``server.py``가 담당한다.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MINI_URL = "http://127.0.0.1:8765/?compact=1"
WINDOW_TITLE = "OJT 미니 도우미"
BROWSER_NAMES = ("chrome.exe", "msedge.exe", "brave.exe", "whale.exe")


class ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", Rect),
        ("rcWork", Rect),
        ("dwFlags", wintypes.DWORD),
    ]


def server_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=0.8):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def ensure_server() -> bool:
    if server_ready():
        return True
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else Path(sys.executable)
    try:
        subprocess.Popen(
            [str(executable), str(ROOT / "server.py"), "--background"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    for _ in range(30):
        if server_ready():
            return True
        time.sleep(0.2)
    return False


def running_browser_paths() -> list[Path]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    found: list[Path] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.casefold() in BROWSER_NAMES:
                process = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        buffer = ctypes.create_unicode_buffer(2048)
                        size = wintypes.DWORD(len(buffer))
                        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                            path = Path(buffer.value)
                            if path.is_file() and path not in found:
                                found.append(path)
                    finally:
                        kernel32.CloseHandle(process)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def find_browser() -> Path | None:
    local = Path(os.environ.get("LOCALAPPDATA", "C:/missing"))
    program_files = Path(os.environ.get("PROGRAMFILES", "C:/missing"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/missing"))
    candidates = [
        local / "Google/Chrome/Application/chrome.exe",
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        local / "Microsoft/Edge/Application/msedge.exe",
        local / "BraveSoftware/Brave-Browser/Application/brave.exe",
        program_files / "Naver/Naver Whale/Application/whale.exe",
    ]
    candidates.extend(running_browser_paths())
    for browser_name in BROWSER_NAMES:
        for path in candidates:
            if path.name.casefold() == browser_name and path.is_file():
                return path
    return None


def target_geometry() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    point = Point()
    user32.GetCursorPos(ctypes.byref(point))
    monitor = user32.MonitorFromPoint(point, 2)
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        work = info.rcWork
    else:
        work = Rect(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    width = min(420, max(360, work.right - work.left - 30))
    height = min(780, max(560, work.bottom - work.top - 30))
    return work.right - width - 14, work.top + 14, width, height


def pin_mini_window(x: int, y: int, width: int, height: int) -> bool:
    user32 = ctypes.windll.user32
    found = wintypes.HWND()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(window: int, _lparam: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(window):
            return True
        length = user32.GetWindowTextLengthW(window)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, title, len(title))
        if WINDOW_TITLE in title.value:
            found = window
            return False
        return True

    for _ in range(50):
        user32.EnumWindows(callback, 0)
        if found:
            user32.ShowWindow(found, 9)
            user32.SetWindowPos(found, ctypes.c_void_p(-1), x, y, width, height, 0x0040)
            return True
        time.sleep(0.2)
    return False


def main() -> None:
    if "--server-only" in sys.argv:
        raise SystemExit(0 if ensure_server() else 1)
    browser = find_browser()
    x, y, width, height = target_geometry()
    if browser:
        command = [
            str(browser),
            f"--app={MINI_URL}",
            f"--window-size={width},{height}",
            f"--window-position={x},{y}",
            "--no-first-run",
            "--disable-session-crashed-bubble",
        ]
    else:
        command = []
    if "--print-command" in sys.argv:
        print(json.dumps({"browser": str(browser or ""), "command": command, "geometry": [x, y, width, height]}, ensure_ascii=False))
        return
    if not ensure_server():
        ctypes.windll.user32.MessageBoxW(0, "OJT 도우미 서버를 시작하지 못했습니다.", WINDOW_TITLE, 0x10)
        return
    if not browser:
        webbrowser.open(MINI_URL)
        return
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pin_mini_window(x, y, width, height)


if __name__ == "__main__":
    main()

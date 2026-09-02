from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import mimetypes
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import uiautomation as ui_auto
    from pynput import mouse
except ImportError:
    ui_auto = None
    mouse = None


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
MAX_BODY_BYTES = 128 * 1024
OLLAMA_URL = "http://127.0.0.1:11434"
# OpenAI 폴백은 선택 사항이다. 계정에서 실제로 쓸 수 있는 모델명으로 바꿔야 한다.
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_OLLAMA_MODELS = Path("F:/OllamaModels")
DATA_DIR = ROOT / "data"
ACTIVITY_DB = DATA_DIR / "activity.db"
ACTIVITY_POLL_SECONDS = 15
IDLE_LIMIT_SECONDS = 5 * 60
RETENTION_DAYS = 60
MAX_WINDOW_EVIDENCE = 40
MAX_CLICK_EVIDENCE = 60
EVIDENCE_TEXT_LIMIT = 200
ALLOWED_STATIC_FILES = {"index.html", "app.js", "styles.css", "favicon.ico"}

# 업무로 보기 어려운 프로그램. 개발 업무도 OJT에 넣으려면 code.exe 줄을 지운다.
IGNORED_PROCESSES = {
    "code.exe",
    "codex.exe",
    "cmd.exe",
    "lockapp.exe",
    "searchhost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "systemsettings.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "ollama.exe",
}
IGNORED_TITLE_KEYWORDS = (
    "ojt 작성 도우미", "ojt 미니 도우미", "ojt-assistant", "폴더 옵션",
    "시스템 트레이 오버플로", "작업 표시줄", "알림 센터",
)
BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "whale.exe", "iexplore.exe"}
# 작업표시줄·트레이·시작메뉴 클릭은 앞에 떠 있던 다른 프로그램의 업무로 잘못 기록된다.
SHELL_CLICK_PROCESSES = {
    "explorer.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "textinputhost.exe",
    "systemsettings.exe",
}
# 메신저는 창 제목이 대화 상대 이름이라 업무 후보에 제목을 쓰지 않는다.
MESSENGER_PROCESSES = {
    "kakaotalk.exe",
    "yjmessenger.exe",
    "daoumessenger 4.0.exe",
    "slack.exe",
    "discord.exe",
    "telegram.exe",
    "line.exe",
}
PROCESS_LABELS = {
    "acorn.exe": "Acorn",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "whale.exe": "웨일",
    "firefox.exe": "Firefox",
    "kakaotalk.exe": "카카오톡",
    "yjmessenger.exe": "사내 메신저",
    "daoumessenger 4.0.exe": "다우 메신저",
    "excel.exe": "Excel",
    "winword.exe": "Word",
    "powerpnt.exe": "PowerPoint",
    "explorer.exe": "파일 탐색기",
    "chatgpt.exe": "ChatGPT",
    "notepad.exe": "메모장",
}
IGNORED_TITLES = {"새 탭", "new tab", "파일 탐색기", "file explorer"}
# 업무 내용이 아닌 껍데기 조작. 이름이 정확히 같을 때만 제외한다.
SHELL_CONTROL_NAMES = {
    "시작",
    "검색",
    "작업 보기",
    "알림",
    "start",
    "search",
    "task view",
    "notifications",
}
# 이름이 없고 업무 의미도 없는 자동화 ID
MEANINGLESS_AUTOMATION_IDS = {"btn_bkgrnd", "backgroundbutton", "btn_background"}
# 트레이·작업표시줄에서 온 기록을 가리키는 표현
SHELL_CONTROL_KEYWORDS = (
    "숨겨진 아이콘 표시",
    "시스템 트레이",
    "작업 표시줄",
    "알림 센터",
    "show hidden icons",
    "notification chevron",
    "taskbar",
    "system tray",
)

monitor_lock = threading.Lock()
monitor_stop = threading.Event()
monitor_enabled = True
interaction_stop = threading.Event()
interaction_queue: queue.Queue[tuple[int, int, datetime] | None] = queue.Queue(maxsize=200)
interaction_listener: Any = None

INTERACTIVE_CONTROL_TYPES = {
    "ButtonControl",
    "CheckBoxControl",
    "ComboBoxControl",
    "CustomControl",
    "DataItemControl",
    "GroupControl",
    "HeaderItemControl",
    "HyperlinkControl",
    "ImageControl",
    "ListItemControl",
    "MenuItemControl",
    "RadioButtonControl",
    "SplitButtonControl",
    "TabItemControl",
    "TreeItemControl",
}


def load_local_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b-instruct").strip() or "qwen3:4b-instruct"


class LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def protect_text(value: str) -> str:
    if not value or os.name != "nt":
        return value
    raw = value.encode("utf-8")
    input_buffer = ctypes.create_string_buffer(raw)
    input_blob = DataBlob(
        len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "OJT activity title",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise OSError("Windows 데이터 보호에 실패했습니다.")
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def unprotect_text(value: str) -> str:
    if not value.startswith("dpapi:") or os.name != "nt":
        return value
    encrypted = base64.b64decode(value[6:])
    input_buffer = ctypes.create_string_buffer(encrypted)
    input_blob = DataBlob(
        len(encrypted), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise OSError("Windows 데이터 복호화에 실패했습니다.")
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def initialize_activity_db() -> None:
    global monitor_enabled
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                activity_date TEXT NOT NULL,
                process_name TEXT NOT NULL,
                window_title TEXT NOT NULL,
                seconds INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_date ON activity_samples(activity_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS interaction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                activity_date TEXT NOT NULL,
                process_name TEXT NOT NULL,
                window_title TEXT NOT NULL,
                control_name TEXT NOT NULL,
                control_type TEXT NOT NULL,
                automation_id TEXT NOT NULL,
                parent_context TEXT NOT NULL,
                action TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_interaction_date ON interaction_events(activity_date)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS monitor_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ojt_entries (
                id TEXT PRIMARY KEY,
                entry_date TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_entry_date ON ojt_entries(entry_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_cache (
                activity_date TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT value FROM monitor_config WHERE key = 'enabled'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO monitor_config(key, value) VALUES('enabled', '1')"
            )
            monitor_enabled = True
        else:
            monitor_enabled = row[0] == "1"
        plaintext_rows = connection.execute(
            "SELECT id, window_title FROM activity_samples WHERE window_title NOT LIKE 'dpapi:%'"
        ).fetchall()
        for row_id, window_title in plaintext_rows:
            try:
                protected = protect_text(window_title)
            except OSError:
                continue
            connection.execute(
                "UPDATE activity_samples SET window_title = ? WHERE id = ?",
                (protected, row_id),
            )
    purge_expired_activity()


def purge_expired_activity() -> None:
    """모니터링 원본은 보존 기간이 지나면 지운다. 작성한 OJT 기록은 남는다."""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).date().isoformat()
    try:
        with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
            connection.execute("DELETE FROM activity_samples WHERE activity_date < ?", (cutoff,))
            connection.execute("DELETE FROM interaction_events WHERE activity_date < ?", (cutoff,))
            connection.execute("DELETE FROM topic_cache WHERE activity_date < ?", (cutoff,))
    except sqlite3.Error:
        return


def is_monitor_enabled() -> bool:
    with monitor_lock:
        return monitor_enabled


def set_monitor_enabled(enabled: bool) -> None:
    global monitor_enabled
    with monitor_lock:
        monitor_enabled = enabled
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        connection.execute(
            "INSERT INTO monitor_config(key, value) VALUES('enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if enabled else "0",),
        )


def get_idle_seconds() -> float:
    if os.name != "nt":
        return 0
    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.windll.user32
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LastInputInfo)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    current_tick = ctypes.windll.kernel32.GetTickCount()
    return ((current_tick - info.dwTime) & 0xFFFFFFFF) / 1000


def process_name_from_pid(process_id: int) -> str:
    if os.name != "nt" or not process_id:
        return ""
    kernel32 = ctypes.windll.kernel32
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
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, int(process_id))
    if not handle:
        return ""
    try:
        path_buffer = ctypes.create_unicode_buffer(1024)
        path_size = wintypes.DWORD(len(path_buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, path_buffer, ctypes.byref(path_size)):
            return Path(path_buffer.value).name
    finally:
        kernel32.CloseHandle(handle)
    return ""


def get_active_window_info() -> tuple[str, str] | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    window = user32.GetForegroundWindow()
    if not window:
        return None
    length = user32.GetWindowTextLengthW(window)
    if length <= 0:
        return None
    title_buffer = ctypes.create_unicode_buffer(min(length + 1, 512))
    user32.GetWindowTextW(window, title_buffer, len(title_buffer))
    title = " ".join(title_buffer.value.split()).strip()
    if not title:
        return None

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
    process_name = process_name_from_pid(process_id.value) or f"pid-{process_id.value}"

    lowered = title.casefold()
    private_markers = ("시크릿", "incognito", "inprivate", "private browsing")
    if any(marker in lowered for marker in private_markers):
        return None
    if "ojt 작성 도우미" in lowered or "ojt 미니 도우미" in lowered:
        return None
    return process_name[:120], title[:500]


def record_activity_sample(process_name: str, window_title: str) -> None:
    now = datetime.now()
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        last = connection.execute(
            """
            SELECT id, recorded_at, process_name, window_title
            FROM activity_samples
            WHERE activity_date = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (now.date().isoformat(),),
        ).fetchone()
        can_extend = False
        last_title = ""
        if last:
            try:
                last_title = unprotect_text(last[3])
            except (OSError, ValueError):
                last_title = ""
        if last and last[2] == process_name and last_title == window_title:
            try:
                elapsed = (now - datetime.fromisoformat(last[1])).total_seconds()
                can_extend = elapsed <= ACTIVITY_POLL_SECONDS * 2.5
            except ValueError:
                can_extend = False
        if can_extend:
            connection.execute(
                "UPDATE activity_samples SET recorded_at = ?, seconds = seconds + ? WHERE id = ?",
                (now.isoformat(timespec="seconds"), ACTIVITY_POLL_SECONDS, last[0]),
            )
        else:
            protected_title = protect_text(window_title)
            connection.execute(
                """
                INSERT INTO activity_samples(
                    recorded_at, activity_date, process_name, window_title, seconds
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now.isoformat(timespec="seconds"),
                    now.date().isoformat(),
                    process_name,
                    protected_title,
                    ACTIVITY_POLL_SECONDS,
                ),
            )


def activity_monitor_loop() -> None:
    last_purge_date = datetime.now().date()
    while not monitor_stop.wait(ACTIVITY_POLL_SECONDS):
        today = datetime.now().date()
        if today != last_purge_date:
            last_purge_date = today
            purge_expired_activity()
        if not is_monitor_enabled() or get_idle_seconds() >= IDLE_LIMIT_SECONDS:
            continue
        activity = get_active_window_info()
        if not activity:
            continue
        try:
            record_activity_sample(*activity)
        except (sqlite3.Error, OSError):
            continue


def start_activity_monitor() -> None:
    monitor_stop.clear()
    thread = threading.Thread(
        target=activity_monitor_loop,
        name="ojt-activity-monitor",
        daemon=True,
    )
    thread.start()


def normalize_ui_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def get_named_parent_context(control: Any) -> str:
    parts = []
    current = control
    for _ in range(4):
        try:
            current = current.GetParentControl()
        except Exception:
            break
        if not current:
            break
        try:
            name = normalize_ui_text(current.Name, 160)
            control_type = normalize_ui_text(current.ControlTypeName, 80)
        except Exception:
            continue
        if name and control_type not in {"WindowControl", "PaneControl"} and name not in parts:
            parts.append(name)
        if len(parts) >= 3:
            break
    return " > ".join(reversed(parts))[:400]


def record_interaction_event(
    occurred_at: datetime,
    process_name: str,
    window_title: str,
    control_name: str,
    control_type: str,
    automation_id: str,
    parent_context: str,
) -> None:
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        connection.execute(
            """
            INSERT INTO interaction_events(
                occurred_at, activity_date, process_name, window_title,
                control_name, control_type, automation_id, parent_context, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'click')
            """,
            (
                occurred_at.isoformat(timespec="milliseconds"),
                occurred_at.date().isoformat(),
                process_name[:120],
                protect_text(window_title[:500]),
                protect_text(control_name[:300]),
                control_type[:80],
                protect_text(automation_id[:200]),
                protect_text(parent_context[:400]),
            ),
        )


def interaction_worker_loop() -> None:
    if ui_auto is None:
        return
    last_signature: tuple[str, str, str, str] | None = None
    last_recorded_at = 0.0
    try:
        initializer = ui_auto.UIAutomationInitializerInThread()
        initializer.__enter__()
    except Exception:
        initializer = None
    try:
        while not interaction_stop.is_set():
            try:
                event = interaction_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            x, y, occurred_at = event
            if not is_monitor_enabled():
                continue
            active_window = get_active_window_info()
            if not active_window:
                continue
            process_name, window_title = active_window
            try:
                control = ui_auto.ControlFromPoint(x, y)
                if not control:
                    continue
                control_type = normalize_ui_text(control.ControlTypeName, 80)
                control_name = normalize_ui_text(control.Name, 300)
                automation_id = normalize_ui_text(control.AutomationId, 200)
                control_process = process_name_from_pid(getattr(control, "ProcessId", 0))
            except Exception:
                continue
            if control_type not in INTERACTIVE_CONTROL_TYPES:
                continue
            if control_process and control_process.casefold() != process_name.casefold():
                # 앞에 떠 있는 창이 아니라 작업표시줄·트레이를 누른 경우다.
                if control_process.casefold() in SHELL_CLICK_PROCESSES:
                    continue
                process_name = control_process
            parent_context = get_named_parent_context(control)
            if not control_name:
                control_name = parent_context.split(" > ")[-1] if parent_context else ""
            if not control_name and not automation_id:
                continue
            signature = (process_name, control_type, control_name, parent_context)
            recorded_at = time.monotonic()
            if signature == last_signature and recorded_at - last_recorded_at < 1.5:
                continue
            try:
                record_interaction_event(
                    occurred_at,
                    process_name,
                    window_title,
                    control_name,
                    control_type,
                    automation_id,
                    parent_context,
                )
            except (sqlite3.Error, OSError):
                continue
            last_signature = signature
            last_recorded_at = recorded_at
    finally:
        if initializer is not None:
            try:
                initializer.__exit__(None, None, None)
            except Exception:
                pass


def on_mouse_click(x: int, y: int, button: Any, pressed: bool) -> None:
    if mouse is None or not pressed or button != mouse.Button.left or not is_monitor_enabled():
        return
    try:
        interaction_queue.put_nowait((int(x), int(y), datetime.now()))
    except queue.Full:
        return


def start_interaction_monitor() -> bool:
    global interaction_listener
    if ui_auto is None or mouse is None:
        return False
    interaction_stop.clear()
    worker = threading.Thread(
        target=interaction_worker_loop,
        name="ojt-interaction-worker",
        daemon=True,
    )
    worker.start()
    try:
        interaction_listener = mouse.Listener(on_click=on_mouse_click)
        interaction_listener.start()
    except Exception:
        interaction_stop.set()
        interaction_listener = None
        return False
    return True


def stop_interaction_monitor() -> None:
    global interaction_listener
    interaction_stop.set()
    try:
        interaction_queue.put_nowait(None)
    except queue.Full:
        pass
    if interaction_listener is not None:
        try:
            interaction_listener.stop()
        except Exception:
            pass
    interaction_listener = None


def get_activity_status(activity_date: str | None = None) -> dict[str, Any]:
    target_date = activity_date or datetime.now().date().isoformat()
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(seconds), 0) FROM activity_samples WHERE activity_date = ?",
            (target_date,),
        ).fetchone()
        interaction_row = connection.execute(
            "SELECT COUNT(*) FROM interaction_events WHERE activity_date = ?",
            (target_date,),
        ).fetchone()
    return {
        "enabled": is_monitor_enabled(),
        "date": target_date,
        "sampleCount": int(row[0]),
        "trackedSeconds": int(row[1]),
        "interactionCount": int(interaction_row[0]),
        "interactionMonitoring": bool(
            interaction_listener is not None and getattr(interaction_listener, "running", False)
        ),
        "pollSeconds": ACTIVITY_POLL_SECONDS,
        "idleLimitSeconds": IDLE_LIMIT_SECONDS,
        "storage": str(ACTIVITY_DB),
    }


def is_ignored_window(process_name: str, window_title: str, total_seconds: int) -> bool:
    process_lower = process_name.casefold()
    title_lower = window_title.casefold()
    if process_lower in IGNORED_PROCESSES:
        return True
    if any(keyword in title_lower for keyword in IGNORED_TITLE_KEYWORDS):
        return True
    if title_lower in IGNORED_TITLES:
        return True
    if process_lower == "explorer.exe" and total_seconds < 120:
        return True
    return False


def get_activity_rows(activity_date: str) -> list[dict[str, Any]]:
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT process_name, window_title, seconds
            FROM activity_samples
            WHERE activity_date = ?
            """,
            (activity_date,),
        ).fetchall()

    grouped: dict[tuple[str, str], int] = {}
    for process_name, protected_title, seconds in rows:
        try:
            window_title = unprotect_text(protected_title)
        except (OSError, ValueError):
            continue
        key = (process_name, window_title)
        grouped[key] = grouped.get(key, 0) + int(seconds)

    # 제외 대상을 먼저 걸러낸 뒤 정렬해야 짧게 사용한 업무 화면이 잘리지 않는다.
    kept = [
        (process_name, window_title, total_seconds)
        for (process_name, window_title), total_seconds in grouped.items()
        if not is_ignored_window(process_name, window_title, total_seconds)
    ]
    kept.sort(key=lambda item: item[2], reverse=True)

    result = []
    for process_name, window_title, total_seconds in kept[:MAX_WINDOW_EVIDENCE]:
        result.append(
            {
                "sourceId": len(result) + 1,
                "kind": "window",
                "process": process_name,
                "title": window_title[:EVIDENCE_TEXT_LIMIT],
                "seconds": int(total_seconds),
                "clickCount": 0,
            }
        )
    return result


def is_shell_control(control_name: str, parent_context: str, window_title: str = "") -> bool:
    """작업표시줄·트레이·시작메뉴 조작은 업무 기록으로 보지 않는다."""
    name = control_name.casefold().strip()
    if name in SHELL_CONTROL_NAMES:
        return True
    haystack = f"{name} {parent_context.casefold()} {window_title.casefold()}"
    return any(keyword in haystack for keyword in SHELL_CONTROL_KEYWORDS)


def get_interaction_rows(activity_date: str) -> list[dict[str, Any]]:
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        rows = connection.execute(
            """
            SELECT process_name, window_title, control_name, control_type,
                   automation_id, parent_context, occurred_at
            FROM interaction_events
            WHERE activity_date = ?
            ORDER BY occurred_at
            """,
            (activity_date,),
        ).fetchall()

    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for (
        process_name,
        protected_window,
        protected_name,
        control_type,
        protected_id,
        protected_parent,
        occurred_at,
    ) in rows:
        if process_name.casefold() in IGNORED_PROCESSES:
            continue
        try:
            window_title = unprotect_text(protected_window)
            control_name = unprotect_text(protected_name)
            automation_id = unprotect_text(protected_id)
            parent_context = unprotect_text(protected_parent)
        except (OSError, ValueError):
            continue
        if any(keyword in window_title.casefold() for keyword in IGNORED_TITLE_KEYWORDS):
            continue
        if is_shell_control(control_name, parent_context, window_title):
            continue
        if not control_name and automation_id.casefold() in MEANINGLESS_AUTOMATION_IDS:
            # 이름 없는 배경 요소라 어떤 업무인지 알려주지 못한다.
            continue
        key = (process_name, window_title, control_name, control_type, parent_context)
        if key not in grouped:
            grouped[key] = {
                "kind": "click",
                "process": process_name,
                "windowTitle": window_title,
                "controlName": control_name,
                "controlType": control_type,
                "automationId": automation_id,
                "parentContext": parent_context,
                "seconds": 0,
                "clickCount": 0,
                "firstOccurredAt": occurred_at,
                "lastOccurredAt": occurred_at,
            }
        grouped[key]["clickCount"] += 1
        grouped[key]["lastOccurredAt"] = occurred_at

    ordered = sorted(
        grouped.values(),
        key=lambda row: (row["clickCount"], row["lastOccurredAt"]),
        reverse=True,
    )[:MAX_CLICK_EVIDENCE]
    # 근거를 시간 순서대로 보여줘야 같은 업무 흐름끼리 묶이기 쉽다.
    ordered.sort(key=lambda row: row["firstOccurredAt"])
    for row in ordered:
        detail = [f"화면: {row['windowTitle'][:120]}"]
        if row["parentContext"]:
            detail.append(f"상위 메뉴: {row['parentContext'][:120]}")
        control_label = row["controlName"] or row["automationId"] or row["controlType"]
        detail.append(f"{row['controlType']}: {control_label[:120]}")
        row["title"] = " | ".join(detail)[:EVIDENCE_TEXT_LIMIT * 2]
        row.pop("firstOccurredAt", None)
        row.pop("lastOccurredAt", None)
    return ordered


def get_activity_evidence(activity_date: str) -> list[dict[str, Any]]:
    rows = get_activity_rows(activity_date) + get_interaction_rows(activity_date)
    for source_id, row in enumerate(rows, start=1):
        row["sourceId"] = source_id
    return rows


def read_config(key: str, default: str = "") -> str:
    try:
        with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
            row = connection.execute(
                "SELECT value FROM monitor_config WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def write_config(key: str, value: str) -> None:
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        connection.execute(
            "INSERT INTO monitor_config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def load_saved_entries() -> list[dict[str, Any]]:
    """작성한 OJT 기록은 브라우저가 아니라 로컬 DB에 보관한다."""
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        rows = connection.execute(
            "SELECT id, payload, created_at, updated_at FROM ojt_entries "
            "ORDER BY entry_date DESC, updated_at DESC"
        ).fetchall()
    entries = []
    for entry_id, payload, created_at, updated_at in rows:
        try:
            entry = json.loads(unprotect_text(payload))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        entry["id"] = entry_id
        entry["createdAt"] = created_at
        entry["updatedAt"] = updated_at
        entries.append(entry)
    return entries


def save_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry_date = str(entry.get("date", "")).strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", entry_date):
        raise ValueError("날짜 형식이 올바르지 않습니다.")
    if not str(entry.get("performedTasks", "")).strip():
        raise ValueError("수행한 업무가 비어 있습니다.")
    entry_id = str(entry.get("id", "")).strip() or f"{entry_date}-{int(time.time() * 1000)}"
    now = datetime.now().isoformat(timespec="seconds")
    stored = {key: value for key, value in entry.items() if key not in {"createdAt", "updatedAt"}}
    stored["id"] = entry_id
    payload = protect_text(json.dumps(stored, ensure_ascii=False))
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        row = connection.execute(
            "SELECT created_at FROM ojt_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        created_at = row[0] if row else now
        connection.execute(
            "INSERT INTO ojt_entries(id, entry_date, payload, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "entry_date = excluded.entry_date, payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (entry_id, entry_date, payload, created_at, now),
        )
    stored["createdAt"] = created_at
    stored["updatedAt"] = now
    return stored


def delete_saved_entry(entry_id: str) -> None:
    with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
        connection.execute("DELETE FROM ojt_entries WHERE id = ?", (entry_id,))


def evidence_fingerprint(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            f"{row.get('kind')}|{row.get('process')}|{row.get('title')}|"
            f"{row.get('seconds')}|{row.get('clickCount')}".encode("utf-8")
        )
    return digest.hexdigest()


def read_topic_cache(activity_date: str) -> tuple[list[dict[str, Any]], str] | None:
    """같은 날짜를 다시 정리하면 저장된 결과를 먼저 보여준다."""
    try:
        with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
            row = connection.execute(
                "SELECT payload, created_at FROM topic_cache WHERE activity_date = ?",
                (activity_date,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        topics = json.loads(unprotect_text(row[0]))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(topics, list) or not topics:
        return None
    return topics, str(row[1])


def write_topic_cache(activity_date: str, fingerprint: str, topics: list[dict[str, Any]]) -> None:
    try:
        payload = protect_text(json.dumps(topics, ensure_ascii=False))
        with sqlite3.connect(ACTIVITY_DB, timeout=10) as connection:
            connection.execute(
                "INSERT INTO topic_cache(activity_date, fingerprint, payload, created_at) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(activity_date) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, payload = excluded.payload, "
                "created_at = excluded.created_at",
                (activity_date, fingerprint, payload, datetime.now().isoformat(timespec="seconds")),
            )
    except (sqlite3.Error, OSError):
        return


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def selected_topic_facts(data: dict[str, Any]) -> str:
    topics = data.get("selectedTopics")
    if not isinstance(topics, list) or not topics:
        return "(선택한 업무 없음)"
    lines = []
    for item in topics[:20]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title", "")).split()).strip()
        description = " ".join(str(item.get("description", "")).split()).strip()
        if not title:
            continue
        lines.append(f"- {title}" + (f" :: {description}" if description else ""))
    return chr(10).join(lines) or "(선택한 업무 없음)"


def build_prompt(data: dict[str, Any]) -> str:
    systems = ", ".join(data.get("systems") or []) or "미지정"
    competencies = ", ".join(data.get("competencyCandidates") or []) or "미지정"
    return f"""<ojt_context>
작성 날짜: {data.get('date', '')}
OJT 주차/단계: {data.get('stage', '')}
관련 시스템 선택: {systems}
연결 후보 역량: {competencies}
</ojt_context>

<selected_work>
{selected_topic_facts(data)}
</selected_work>

<user_facts>
수행 업무 메모(이 한 칸에 어려웠던 점이 포함될 수 있음):
{data.get('workMemo', '')}
</user_facts>

<output_format>
performedTasks 는 아래 형식을 그대로 따른다.

1. 업무 이름
• 그 업무에서 실제로 한 일
• 확인하거나 파악한 내용
2. 다음 업무 이름
• 그 업무에서 실제로 한 일

reflection 은 "• " 로 시작하는 줄만 2~3개 쓴다.
</output_format>

<example>
performedTasks 예시:
1. QR/바코드 영수증 고도화 기능 테스트 및 오류 확인
• 쿠폰번호 입력 방식에 따른 바코드 출력 및 승인취소 기능 테스트 진행
• 쿠폰번호를 한글로 입력할 경우 바코드가 정상 출력되지 않는 현상 확인
• 바코드 사용 시 입력값 형식에 따른 기능 제한 사항 정리
2. 관련 부서 테스트 결과 공유
• 테스트 결과와 확인된 기능 제한 사항을 마케팅커뮤니케이션실과 운영1팀에 이메일 공유

reflection 예시:
• 동일한 기능이라도 입력 데이터 형식에 따라 예상하지 못한 오류가 발생할 수 있어 입력 조건을 기준으로 테스트해야 함을 확인
• 확인한 기술적인 내용을 관련 부서가 이해할 수 있게 전달하는 과정도 중요함을 경험
</example>

<writing_rules>
- 한국어 회사 OJT 일지 초안으로 정리한다.
- 선택한 업무 하나가 소제목 하나가 된다. 합치거나 빠뜨리지 않는다.
- <selected_work> 에 있는 업무는 전부 소제목으로 등장해야 한다.
- 추가 메모가 있으면 마지막 소제목으로 따로 넣는다.
- 각 소제목 아래 • 항목은 1~3개로, 선택한 업무 설명과 메모에 있는 내용만 풀어 쓴다.
- 건수, 용량, 기간처럼 메모에 있는 수치는 반드시 그대로 살린다.
- 확인한 결과나 상태(있음/없음, 정상/오류)를 지어내지 않는다. 메모에 없으면 쓰지 않는다.
- 사용자가 적지 않은 업무, 시스템 기능, 교육 내용, 성과를 만들어내지 않는다.
- "N회 클릭", "몇 번 눌렀다" 같은 조작 묘사는 쓰지 않고 업무 행위로 바꿔 쓴다.
- 수행 업무 메모에 어려움, 오류, 장애, 실패, 부족, 추가 숙지 필요가 명시되어 있으면 이슈로 분리한다.
- 수행 업무 메모에 이슈가 명시되어 있지 않으면 이슈는 정확히 "특이사항 없음"으로 작성한다.
- Acorn의 "별도 관리자 계정 발급"과 회사 계정의 "권한 부여"를 구분한다.
- 사용자가 직접 언급하지 않았다면 수발주 기능을 추가하지 않는다.
- 본사·가맹점 데이터 흐름에 관한 표현도 사용자 메모 범위를 넘지 않는다.
- reflection 은 그날 한 일에서 실제로 얻은 것만 쓰고, 과장하지 않는다.
- 시스템과 역량은 입력 사실에서 명확히 연결되는 것만 반환한다.
</writing_rules>"""


# AI가 사용자 메모에 없는 내용을 덧붙일 때 걷어내는 규칙.
UNSUPPORTED_LINE_TERMS = ("수발주",)
# (원본에 있어야 인정되는 근거, 지울 표현, 대체할 표현)
GUARDED_PHRASES = (
    ("사용을 시작", "권한을 부여받아 사용을 시작함", "권한을 부여받음"),
    ("접근을 완료", "권한을 부여받아 접근을 완료함", "권한을 부여받음"),
    ("사용 가능성을 검토", "업무 권한을 부여받아 사용 가능성을 검토함", "업무 권한을 부여받음"),
)


MATCH_STRIP = re.compile(r"[^가-힣A-Za-z0-9]")


def match_key(text: str) -> str:
    return MATCH_STRIP.sub("", str(text or "")).casefold()


def merge_missing_topics(performed: str, data: dict[str, Any]) -> str:
    """모델이 빠뜨린 업무를 뒤에 붙이고 번호를 다시 매긴다.

    일지에 올릴 기록이라 선택한 업무가 조용히 사라지면 안 된다.
    """
    topics = data.get("selectedTopics")
    if not isinstance(topics, list) or not topics:
        return performed
    sections = [part.strip() for part in re.split(r"\n(?=\s*\d+\.\s)", performed.strip()) if part.strip()]
    bodies = [re.sub(r"^\s*\d+\.\s*", "", part).strip() for part in sections]
    covered = match_key(performed)

    def is_covered(title: str) -> bool:
        key = match_key(title)
        if key and key in covered:
            return True
        tokens = [word for word in str(title).split() if len(word) >= 2]
        if not tokens:
            return False
        hits = sum(1 for word in tokens if match_key(word) in covered)
        return hits * 2 >= len(tokens)

    for item in topics[:20]:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title", "")).split()).strip()
        if not title or is_covered(title):
            continue
        description = " ".join(str(item.get("description", "")).split()).strip()
        bodies.append(title + (f"\n• {description}" if description else ""))

    manual_memo = str(data.get("manualMemo", "")).strip()
    if manual_memo and not is_covered(manual_memo[:20]):
        lines = [line.strip(" -•\t") for line in manual_memo.replace("\r", "").split("\n")]
        bullets = chr(10).join(f"• {line}" for line in lines if line)
        bodies.append("추가 메모\n" + bullets)

    return "\n\n".join(f"{index}. {body}" for index, body in enumerate(bodies, start=1))


def has_selected_topics(data: dict[str, Any]) -> bool:
    topics = data.get("selectedTopics")
    if not isinstance(topics, list):
        return False
    return any(isinstance(item, dict) and str(item.get("title", "")).strip() for item in topics)


def sanitize_entry(parsed: Any, data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise RuntimeError("AI 작성 결과의 형식을 확인할 수 없습니다.")

    source_text = " ".join(
        str(data.get(key, ""))
        for key in ("workMemo", "issueMemo", "reflectionMemo")
    )

    def clean_text(value: Any) -> str:
        text = str(value or "").strip()
        for term in UNSUPPORTED_LINE_TERMS:
            if term not in source_text and term in text:
                text = "\n".join(line for line in text.splitlines() if term not in line).strip()
        for evidence, phrase, replacement in GUARDED_PHRASES:
            if evidence not in source_text:
                text = text.replace(phrase, replacement)
        return text

    def memo_as_bullets(value: Any) -> str:
        lines = [line.strip(" -•\t") for line in str(value or "").replace("\r", "").split("\n")]
        return "\n".join(f"• {line}" for line in lines if line)

    def extract_issue_lines(value: Any) -> list[str]:
        pattern = re.compile(
            r"어려|오류|에러|문제|장애|실패|지연|부족|헷갈|숙지.*필요|"
            r"추가.*확인.*필요|권한.*없|접속.*안|되지 않|불가"
        )
        sentences = re.split(r"\n+|(?<=[.!?])\s+", str(value or "").replace("\r", ""))
        return [sentence.strip(" -•\t") for sentence in sentences if sentence.strip() and pattern.search(sentence)]

    def assemble_from_topics() -> str:
        """AI 본문을 못 쓸 때만 쓰는 최소 형태."""
        topic_sections = []
        for item in selected_topics[:20]:
            if not isinstance(item, dict):
                continue
            title = " ".join(str(item.get("title", "")).split()).strip()
            description = " ".join(str(item.get("description", "")).split()).strip()
            if not title:
                continue
            section = f"{len(topic_sections) + 1}. {title}"
            if description:
                section += f"\n• {description}"
            topic_sections.append(section)
        manual_memo = str(data.get("manualMemo", "")).strip()
        if manual_memo:
            topic_sections.append(f"{len(topic_sections) + 1}. 추가 메모\n{memo_as_bullets(manual_memo)}")
        return "\n\n".join(topic_sections)

    selected_topics = data.get("selectedTopics") or []
    performed = clean_text(parsed.get("performedTasks"))
    # 번호 소제목과 • 항목이 갖춰졌을 때만 AI 본문을 쓴다. 형식이 무너지면
    # 선택한 업무를 그대로 옮긴 최소 형태로 되돌린다.
    if not (performed.lstrip().startswith("1.") and "•" in performed):
        if isinstance(selected_topics, list) and selected_topics:
            performed = assemble_from_topics()
    else:
        performed = merge_missing_topics(performed, data)
    if not performed:
        raise RuntimeError("AI 작성 결과에서 수행 업무를 찾지 못했습니다.")

    issue_memo = str(data.get("issueMemo", "")).strip()
    competencies = [str(item) for item in (data.get("competencyCandidates") or []) if str(item).strip()]
    systems = [str(item) for item in (data.get("systems") or []) if str(item).strip()]
    issue_lines = extract_issue_lines(data.get("workMemo", ""))
    issues = memo_as_bullets(issue_memo or "\n".join(issue_lines)) if issue_memo or issue_lines else "특이사항 없음"
    reflection_memo = str(data.get("reflectionMemo", "")).strip()
    if reflection_memo:
        reflection = memo_as_bullets(reflection_memo)
    else:
        ai_reflection = clean_text(parsed.get("reflection"))
        if ai_reflection and "•" in ai_reflection:
            reflection = ai_reflection
        else:
            focus = competencies[0] if competencies else "금일 수행 업무"
            reflection = (
                f"• 금일 수행한 업무를 통해 {focus} 관련 기본 업무 흐름을 확인함\n"
                "• 관련 세부 기준은 실제 업무를 수행하며 추가로 숙지할 예정"
            )

    return {
        "performedTasks": performed,
        "issues": issues or "특이사항 없음",
        "reflection": reflection,
        "competencies": competencies[:4],
        "systems": systems,
    }


def request_openai(data: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AI_NOT_CONFIGURED")

    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
    schema = {
        "type": "object",
        "properties": {
            "performedTasks": {"type": "string"},
            "issues": {"type": "string"},
            "reflection": {"type": "string"},
            "competencies": {"type": "array", "items": {"type": "string"}},
            "systems": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["performedTasks", "issues", "reflection", "competencies", "systems"],
        "additionalProperties": False,
    }
    payload = {
        "model": model,
        "store": False,
        "input": [
            {
                "role": "developer",
                "content": "입력된 사실만 사용해 간결한 데일리 OJT 일지를 작성한다.",
            },
            {"role": "user", "content": build_prompt(data)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ojt_entry",
                "strict": True,
                "schema": schema,
            },
            "verbosity": "low",
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI 요청 실패 ({exc.code}): {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("OpenAI 서버에 연결할 수 없습니다.") from exc

    output_text = result.get("output_text")
    if not output_text:
        for item in result.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    output_text = content["text"]
                    break
            if output_text:
                break
    if not output_text:
        raise RuntimeError("AI 응답에서 작성 결과를 찾지 못했습니다.")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI 작성 결과의 형식을 확인할 수 없습니다.") from exc
    return {"entry": sanitize_entry(parsed, data), "model": model, "provider": "OpenAI"}


def get_ollama_models(timeout: float = 1.5) -> list[str]:
    request = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [str(item.get("name", "")) for item in result.get("models", []) if item.get("name")]


def ollama_api_reachable(timeout: float = 0.8) -> bool:
    request = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def ensure_ollama_running() -> None:
    if ollama_api_reachable():
        return
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return
    ollama_exe = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
    if not ollama_exe.is_file():
        return

    child_env = os.environ.copy()
    if not child_env.get("OLLAMA_MODELS") and DEFAULT_OLLAMA_MODELS.is_dir():
        child_env["OLLAMA_MODELS"] = str(DEFAULT_OLLAMA_MODELS)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [str(ollama_exe), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            creationflags=creation_flags,
        )
    except OSError:
        return
    for _ in range(12):
        if ollama_api_reachable():
            return
        time.sleep(0.25)


def ollama_ready() -> bool:
    models = get_ollama_models()
    desired_base = OLLAMA_MODEL.split(":", 1)[0]
    return any(name == OLLAMA_MODEL or name.split(":", 1)[0] == desired_base for name in models)


def request_ollama(data: dict[str, Any]) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "performedTasks": {"type": "string"},
            "issues": {"type": "string"},
            "reflection": {"type": "string"},
            "competencies": {"type": "array", "items": {"type": "string"}},
            "systems": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["performedTasks", "issues", "reflection", "competencies", "systems"],
        "additionalProperties": False,
    }
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": schema,
        "messages": [
            {
                "role": "system",
                "content": "입력된 사실만 사용해 간결한 한국어 데일리 OJT 일지를 작성한다. 반드시 제공된 JSON 형식을 지킨다.",
            },
            {"role": "user", "content": build_prompt(data)},
        ],
        "options": {"temperature": 0, "num_ctx": 4096},
        "keep_alive": "10m",
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"로컬 AI 요청 실패 ({exc.code}): {detail[:400]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("로컬 AI 서버에 연결할 수 없습니다.") from exc
    content = result.get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("로컬 AI 응답에서 작성 결과를 찾지 못했습니다.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("로컬 AI 작성 결과의 형식을 확인할 수 없습니다.") from exc
    return {"entry": sanitize_entry(parsed, data), "model": OLLAMA_MODEL, "provider": "로컬 AI"}


BROWSER_SUFFIX = re.compile(
    r"\s+[-\u2013\u2014]\s+(Google Chrome|Microsoft.Edge|Chrome|Edge|Mozilla Firefox|Whale)$",
    re.IGNORECASE,
)
TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|\u00b7]\s+")

# UI 자동화가 읽어오는 이름에는 업무와 무관한 것이 섞인다.
# 창 제목을 되읽은 값, 페이지 본문, 숫자 배지, 브라우저 껍데기 버튼이 그렇다.
CLICK_LABEL_MAX_LENGTH = 40
GENERIC_CLICK_NAMES = {
    "닫기", "최소화", "최대화", "홈", "새 탭", "새 창", "뒤로", "앞으로",
    "새로 고침", "검색창 열기", "검색어 삭제", "탭 검색", "즐겨찾기",
    "이 페이지 즐겨찾기 추가", "맞춤설정 및 제어", "프로필", "확장 프로그램",
    "close", "minimize", "maximize", "home", "new tab", "back", "forward",
    "reload", "refresh", "search", "bookmark",
}
LETTERS_ONLY = re.compile(r"[^가-힣A-Za-z]")
# 화면에 늘 떠 있는 상태 표시. 클릭 이름으로 잡혀도 업무 내용이 아니다.
STATUS_LABEL_PARTS = ("메모리 사용량", "memory usage", "cpu 사용", "남은 시간", "배터리")


def is_meaningful_click_label(label: str, window_title: str) -> bool:
    """업무 단서가 되는 클릭 이름만 남긴다."""
    text = " ".join(str(label or "").split())
    if not text or len(text) > CLICK_LABEL_MAX_LENGTH:
        return False
    if text.casefold() in GENERIC_CLICK_NAMES:
        return False
    lowered = text.casefold()
    if any(part in lowered for part in STATUS_LABEL_PARTS):
        return False
    # 숫자 배지("3", "99+")처럼 글자가 거의 없는 값은 뜻이 없다.
    if len(LETTERS_ONLY.sub("", text)) < 2:
        return False
    screen = clean_window_title(window_title)
    if screen and (text == screen or text in screen or screen in text):
        # 창 제목을 그대로 되읽은 라벨은 새로운 정보가 없다.
        return False
    return True


def display_process_name(process_name: str) -> str:
    lowered = process_name.casefold()
    if lowered in PROCESS_LABELS:
        return PROCESS_LABELS[lowered]
    return Path(process_name).stem


TITLE_STATUS_SUFFIX = re.compile(
    r"\s*[-\u2013\u2014]\s*(메모리 사용량|memory usage)\s*[-\u2013\u2014]?\s*[\d.,]*\s*(MB|GB|KB)?\s*$",
    re.IGNORECASE,
)


def clean_window_title(title: str) -> str:
    text = BROWSER_SUFFIX.sub("", str(title or "")).strip()
    # 창 제목 끝에 붙는 상태 표시는 반복해서 떨어뜨린다.
    for _ in range(3):
        stripped = TITLE_STATUS_SUFFIX.sub("", text).strip(" -\u2013\u2014")
        if stripped == text:
            break
        text = stripped
    return text


def group_key_for(process_name: str, window_title: str) -> tuple[str, str]:
    """같은 프로그램의 같은 화면에서 이어진 활동을 하나로 묶는 기준."""
    lowered = process_name.casefold()
    cleaned = clean_window_title(window_title)
    if lowered in MESSENGER_PROCESSES:
        return (lowered, "")
    if lowered in BROWSER_PROCESSES:
        # "YeokJeon - 전자결재" 처럼 앞 구간이 같은 화면은 한 업무로 본다.
        return (lowered, TITLE_SPLIT.split(cleaned)[0].strip()[:60])
    return (lowered, cleaned[:60])


def group_activity_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        process_name = str(row.get("process", ""))
        window_title = str(row.get("windowTitle") or row.get("title") or "")
        key = group_key_for(process_name, window_title)
        unit = units.get(key)
        if unit is None:
            unit = {
                "process": process_name,
                "app": display_process_name(process_name),
                "isMessenger": process_name.casefold() in MESSENGER_PROCESSES,
                "screens": [],
                "clicks": {},
                "seconds": 0,
                "clickCount": 0,
                "sourceIds": [],
            }
            units[key] = unit
        unit["sourceIds"].append(int(row.get("sourceId", 0)))
        unit["seconds"] += int(row.get("seconds", 0))
        if row.get("kind") == "click":
            unit["clickCount"] += int(row.get("clickCount", 0))
            label = str(row.get("controlName") or row.get("automationId") or "").strip()
            if label and is_meaningful_click_label(label, window_title):
                context = str(row.get("parentContext") or "").strip()
                click = unit["clicks"].setdefault(label, {"count": 0, "context": context})
                click["count"] += int(row.get("clickCount", 0))
        if not unit["isMessenger"]:
            screen = clean_window_title(window_title)
            if screen and screen not in unit["screens"] and len(unit["screens"]) < 4:
                unit["screens"].append(screen)

    for key, unit in list(units.items()):
        # 클릭 없이 2분도 머물지 않은 창은 스쳐 지나간 것으로 본다.
        if not unit["isMessenger"] and unit["clickCount"] == 0 and unit["seconds"] < 120:
            units.pop(key, None)

    def unit_score(unit: dict[str, Any]) -> tuple[int, float]:
        # 클릭 수로 줄세우면 잡다한 브라우징이 위로 온다. 실제로 붙잡고 있던
        # 시간과 서로 다른 조작의 가짓수가 업무에 가깝다. 메신저는 항상 뒤로.
        kinds = len(unit.get("clicks") or {})
        return (0 if unit.get("isMessenger") else 1, unit["seconds"] + kinds * 90)

    ordered = sorted(units.values(), key=unit_score, reverse=True)[:12]
    result = []
    for index, unit in enumerate(ordered, start=1):
        clicks = sorted(unit["clicks"].items(), key=lambda item: item[1]["count"], reverse=True)[:6]
        unit["unitId"] = index
        unit["clickList"] = [
            {"name": name, "count": info["count"], "context": info["context"]}
            for name, info in clicks
        ]
        unit.pop("clicks", None)
        unit["minutes"] = max(1, round(unit["seconds"] / 60))
        result.append(unit)
    return result


def describe_unit(unit: dict[str, Any]) -> tuple[str, str]:
    """로컬 AI 가 없거나 실패했을 때 쓰는 기본 문장. 기록에 있는 말만 쓴다."""
    app = unit["app"]
    screens = unit.get("screens") or []
    clicks = unit.get("clickList") or []
    minutes = unit["minutes"]
    if unit.get("isMessenger"):
        return app, f"{app}으로 업무 연락을 주고받음 ({minutes}분)"
    screen = screens[0] if screens else app
    place = app if screen.casefold() == app.casefold() else f"{app} {screen}"
    if clicks:
        names = [click["name"] for click in clicks[:3]]
        title = f"{screen} {names[0]}" if screen.casefold() != app.casefold() else f"{app} {names[0]}"
        return title[:80], f"{place} 화면에서 {' · '.join(names)} 작업을 {minutes}분 진행"
    extra = f" 외 {len(screens) - 1}개 화면" if len(screens) > 1 else ""
    return screen[:80], f"{place}{extra}을 {minutes}분 확인"


# 로컬 모델의 업무/개인 판정이 실행마다 뒤집혀서, 확실한 것만 규칙으로 고정한다.
PERSONAL_TOPIC_PARTS = (
    "헬스", "운동", "루틴", "다이어트", "칼로리", "식단",
    "배달", "쇼핑", "장바구니", "최저가", "쿠팡",
    "길찾기", "지도", "네이버지도", "맛집", "여행", "항공", "호텔",
    "선물 추천", "상품 추천", "게임", "유튜브", "넷플릭스", "웹툰",
    "부동산", "주식", "코인", "로또",
)


def looks_personal(topic: dict[str, Any]) -> bool:
    text = f"{topic.get('title', '')} {topic.get('description', '')}".casefold()
    return any(part in text for part in PERSONAL_TOPIC_PARTS)


def build_unit_topics(activity_date: str, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    topics = []
    for unit in units:
        title, description = describe_unit(unit)
        if not title:
            continue
        topics.append(
            {
                "id": f"{activity_date}-{unit['unitId']}",
                "unitId": unit["unitId"],
                "title": title[:80],
                "description": description[:220],
                "minutes": unit["minutes"],
                "clickCount": unit["clickCount"],
                "workRelated": True,
                "apps": [unit["process"]],
                "sourceIds": unit["sourceIds"],
            }
        )
    return topics


def request_ollama_unit_summaries(units: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """묶기는 규칙으로 끝냈고, 로컬 AI 는 각 단위를 업무 문장으로 다듬는다."""
    schema = {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unitId": {"type": "integer"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "workRelated": {"type": "boolean"},
                    },
                    "required": ["unitId", "title", "summary", "workRelated"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["topics"],
        "additionalProperties": False,
    }
    lines = []
    for unit in units:
        parts = [f"[{unit['unitId']}] 프로그램: {unit['app']} | 사용 {unit['minutes']}분"]
        if unit.get("screens"):
            parts.append("화면: " + " / ".join(unit["screens"][:3]))
        if unit.get("clickList"):
            parts.append("조작한 항목: " + ", ".join(click["name"] for click in unit["clickList"]))
        lines.append(" | ".join(parts))
    prompt = """다음은 이미 업무 단위로 묶인 노트북 사용 기록이다.

<work_units>
""" + chr(10).join(lines) + """
</work_units>

<task>
번호마다 title, summary, workRelated 를 하나씩 만든다. 번호를 합치거나 빼지 않는다.
</task>

<rules>
- 기록에 등장한 프로그램명, 화면 이름, 조작한 항목 이름만 사용한다.
- title 은 8~25자의 업무 이름으로 쓴다. 예: "일 판매 내역 조회 및 엑셀 저장"
- summary 는 30~70자의 한 문장으로, 어느 화면에서 무엇을 했는지 적는다.
- summary 는 "~함", "~진행", "~확인" 처럼 명사형으로 끝낸다.
- 클릭 횟수, "N회", "클릭함" 같은 조작 표현은 쓰지 않는다. 업무 행위로 바꿔 쓴다.
- 기록에 없는 목적, 결과, 성과, 문제 해결 여부를 지어내지 않는다.
- 조작한 항목이 없으면 열어서 확인한 수준까지만 쓴다.
- 메신저 창의 대화 내용은 추측하지 않는다.
- workRelated 는 회사 업무로 보이면 true, 개인 검색·쇼핑·건강·길찾기·오락이면 false 로 둔다.
</rules>"""
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": schema,
        "messages": [
            {
                "role": "system",
                "content": "묶인 기록에 적힌 단어만 사용해 한국어 업무 제목과 한 문장 설명을 만든다. 반드시 JSON 형식을 지킨다.",
            },
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 900},
        "keep_alive": "10m",
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.loads(response.read().decode("utf-8"))
        parsed = json.loads(result.get("message", {}).get("content", ""))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("활동 기록을 로컬 AI로 정리하지 못했습니다.") from exc

    named: dict[int, dict[str, Any]] = {}
    for item in parsed.get("topics", []):
        try:
            unit_id = int(item.get("unitId"))
        except (TypeError, ValueError):
            continue
        title = " ".join(str(item.get("title", "")).split()).strip()
        summary = " ".join(str(item.get("summary", "")).split()).strip()
        if not title:
            continue
        named[unit_id] = {
            "title": title[:80],
            "summary": summary[:220],
            "workRelated": bool(item.get("workRelated", True)),
        }
    return named


def generate_activity_topics(activity_date: str, force: bool = False) -> dict[str, Any]:
    rows = get_activity_evidence(activity_date)
    if not rows:
        return {"topics": [], "sourceCount": 0, "provider": "기록 없음", "cached": False}
    fingerprint = evidence_fingerprint(rows)
    if not force:
        # 기록이 계속 쌓여도 다시 정리하기 전에는 저장된 결과를 보여준다.
        cached = read_topic_cache(activity_date)
        if cached:
            topics, created_at = cached
            return {
                "topics": topics,
                "sourceCount": len(rows),
                "provider": "저장된 정리 결과",
                "cached": True,
                "cachedAt": created_at,
            }
    # 묶는 작업은 규칙으로 먼저 끝낸다. 로컬 AI가 느리거나 실패해도 후보는 나온다.
    units = group_activity_units(rows)
    topics = build_unit_topics(activity_date, units)
    provider = "로컬 정리"
    if units and ollama_ready():
        try:
            named = request_ollama_unit_summaries(units)
        except RuntimeError:
            named = {}
        if named:
            messenger_units = {unit["unitId"] for unit in units if unit.get("isMessenger")}
            for topic in topics:
                polished = named.get(topic["unitId"])
                if not polished:
                    continue
                topic["workRelated"] = polished["workRelated"]
                # 메신저는 대화 내용을 추측할 수 없어 제목·설명을 그대로 둔다.
                if topic["unitId"] in messenger_units:
                    continue
                topic["title"] = polished["title"]
                if polished["summary"]:
                    topic["description"] = polished["summary"]
            provider = "로컬 AI"
    # 확실한 개인 활동은 모델 판정과 무관하게 개인으로 못박는다.
    for topic in topics:
        if looks_personal(topic):
            topic["workRelated"] = False
    # 업무로 보이는 항목을 위로 올린다. 개인 활동도 지우지는 않는다.
    topics.sort(key=lambda topic: (not topic.get("workRelated", True),))
    write_topic_cache(activity_date, fingerprint, topics)
    return {
        "topics": topics,
        "sourceCount": len(rows),
        "provider": provider,
        "cached": False,
    }


# OJT 일지는 "-함/-음/-예정" 형태로 끝낸다. 작은 모델이 자주 어겨서 뒤에서 고친다.
JOURNAL_TONE_RULES = (
    (r"하였다", "함"),
    (r"했다", "함"),
    (r"되었다", "됨"),
    (r"됐다", "됨"),
    (r"할 예정이다", "할 예정"),
    (r"예정이다", "예정"),
    (r"필요하다", "필요함"),
    (r"있다", "있음"),
    (r"없다", "없음"),
    (r"이다", "임"),
)


def normalize_journal_tone(text: str) -> str:
    lines = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.rstrip(". ")
        for pattern, replacement in JOURNAL_TONE_RULES:
            if re.search(pattern + r"$", stripped):
                stripped = re.sub(pattern + r"$", replacement, stripped)
                break
        if stripped:
            lines.append(stripped)
    return chr(10).join(lines)


def request_ollama_reflection(data: dict[str, Any]) -> str:
    """학습 내용만 짧게 다듬는다. 사용자가 버튼을 누를 때만 호출한다."""
    schema = {
        "type": "object",
        "properties": {"reflection": {"type": "string"}},
        "required": ["reflection"],
        "additionalProperties": False,
    }
    performed = str(data.get("performedTasks", "")).strip()[:1500]
    issues = str(data.get("issues", "")).strip()[:600]
    stage = str(data.get("stage", "")).strip()
    competencies = ", ".join(str(item) for item in (data.get("competencyCandidates") or []))
    prompt = f"""<ojt_context>
OJT 단계: {stage}
연결 후보 역량: {competencies or "미지정"}
</ojt_context>

<performed_tasks>
{performed}
</performed_tasks>

<issues>
{issues or "특이사항 없음"}
</issues>

<rules>
- 위 수행 업무에서 확인된 사실만 사용해 학습 내용을 2줄로 쓴다.
- 각 줄은 "• "로 시작하고 한 문장으로 끝낸다.
- 일지 문체로 쓴다. 문장은 "-함", "-음", "-예정"으로 끝내고 "-했다", "-이다"는 쓰지 않는다.
- 수행 업무에 없는 기능, 성과, 교육 내용을 새로 만들지 않는다.
- 두 번째 줄에는 앞으로 숙지할 사항을 과장 없이 한 문장만 쓴다.
- 전체 120자를 넘기지 않는다.
</rules>"""
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": schema,
        "messages": [
            {
                "role": "system",
                "content": "입력된 수행 업무 사실만으로 짧은 한국어 학습 내용을 쓴다. 반드시 JSON 형식을 지킨다.",
            },
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": 200},
        "keep_alive": "10m",
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        parsed = json.loads(result.get("message", {}).get("content", ""))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("학습 내용을 다듬지 못했습니다.") from exc
    reflection = normalize_journal_tone(str(parsed.get("reflection", "")).strip())
    if not reflection:
        raise RuntimeError("학습 내용을 다듬지 못했습니다.")
    return reflection


def quick_entry(data: dict[str, Any]) -> dict[str, Any]:
    """AI 없이 선택한 업무만 옮겨 담은 최소 결과."""
    return {"entry": sanitize_entry({}, data), "model": "선택 항목 그대로", "provider": "로컬 정리"}


def generate_with_available_ai(data: dict[str, Any]) -> dict[str, Any]:
    # 예전에는 선택 항목이 있으면 모델을 건너뛰었다. 지금은 선택한 업무를
    # 소제목과 • 항목으로 풀어 쓰는 일을 모델이 하므로 반드시 호출해야 한다.
    if ollama_ready():
        try:
            return request_ollama(data)
        except RuntimeError:
            if has_selected_topics(data):
                return quick_entry(data)
            raise
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return request_openai(data)
    if has_selected_topics(data):
        return quick_entry(data)
    raise RuntimeError("AI_NOT_CONFIGURED")


def launch_mini_app() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else Path(sys.executable)
    subprocess.Popen(
        [str(executable), str(ROOT / "mini_app.py")],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class OJTHandler(BaseHTTPRequestHandler):
    server_version = "OJTAssistant/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        if sys.stdout:
            sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        if path == "/api/status":
            models = get_ollama_models()
            desired_base = OLLAMA_MODEL.split(":", 1)[0]
            local_ready = any(name == OLLAMA_MODEL or name.split(":", 1)[0] == desired_base for name in models)
            openai_ready = bool(os.environ.get("OPENAI_API_KEY", "").strip())
            provider = "local-ai" if local_ready else "openai" if openai_ready else "local-rules"
            self.send_json(
                200,
                {
                    "aiConfigured": local_ready or openai_ready,
                    "provider": provider,
                    "model": OLLAMA_MODEL if local_ready else os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL) if openai_ready else "규칙 기반",
                    "ollamaDetected": bool(models),
                    "ollamaModels": models,
                    "storage": "local-db",
                },
            )
            return

        if path == "/api/activity/status":
            query = urllib.parse.parse_qs(parsed_url.query)
            activity_date = str((query.get("date") or [""])[0]).strip()
            if activity_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", activity_date):
                self.send_json(400, {"error": "날짜 형식이 올바르지 않습니다."})
                return
            self.send_json(200, get_activity_status(activity_date or None))
            return

        if path == "/api/entries":
            try:
                self.send_json(200, {"entries": load_saved_entries()})
            except sqlite3.Error:
                self.send_json(500, {"error": "저장된 기록을 읽지 못했습니다."})
            return

        if path == "/api/settings":
            stored = read_config("ui_settings", "")
            try:
                settings = json.loads(stored) if stored else {}
            except json.JSONDecodeError:
                settings = {}
            self.send_json(200, {"settings": settings if isinstance(settings, dict) else {}})
            return

        if path == "/":
            path = "/index.html"
        # 화면 구성 파일만 내보낸다. .env 나 activity.db 는 응답하지 않는다.
        name = path.lstrip("/")
        candidate = ROOT / name
        if name not in ALLOWED_STATIC_FILES or not candidate.is_file():
            self.send_error(404)
            return

        content = candidate.read_bytes()
        mime_type, _ = mimetypes.guess_type(candidate.name)
        self.send_response(200)
        self.send_header("Content-Type", (mime_type or "application/octet-stream") + ("; charset=utf-8" if candidate.suffix in {".html", ".css", ".js"} else ""))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path not in {
            "/api/generate",
            "/api/refine",
            "/api/activity/topics",
            "/api/activity/toggle",
            "/api/entries",
            "/api/entries/delete",
            "/api/settings",
            "/api/mini/launch",
        }:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "요청 크기를 확인할 수 없습니다."})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "입력 내용이 너무 깁니다."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "요청 형식이 올바르지 않습니다."})
            return

        if path == "/api/mini/launch":
            try:
                launch_mini_app()
            except OSError:
                self.send_json(500, {"error": "미니 프로그램을 열지 못했습니다."})
                return
            self.send_json(200, {"launched": True})
            return

        if path == "/api/activity/toggle":
            enabled = bool(payload.get("enabled"))
            set_monitor_enabled(enabled)
            self.send_json(200, get_activity_status())
            return

        if path == "/api/activity/topics":
            activity_date = str(payload.get("date", "")).strip() or datetime.now().date().isoformat()
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", activity_date):
                self.send_json(400, {"error": "날짜 형식이 올바르지 않습니다."})
                return
            self.send_json(200, generate_activity_topics(activity_date, bool(payload.get("force"))))
            return

        if path == "/api/entries":
            try:
                saved = save_entry(payload if isinstance(payload, dict) else {})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            except (sqlite3.Error, OSError):
                self.send_json(500, {"error": "기록을 저장하지 못했습니다."})
                return
            self.send_json(200, {"entry": saved})
            return

        if path == "/api/entries/delete":
            entry_id = str(payload.get("id", "")).strip()
            if not entry_id:
                self.send_json(400, {"error": "삭제할 기록을 찾지 못했습니다."})
                return
            try:
                delete_saved_entry(entry_id)
            except sqlite3.Error:
                self.send_json(500, {"error": "기록을 삭제하지 못했습니다."})
                return
            self.send_json(200, {"deleted": entry_id})
            return

        if path == "/api/settings":
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                self.send_json(400, {"error": "설정 형식이 올바르지 않습니다."})
                return
            try:
                write_config("ui_settings", json.dumps(settings, ensure_ascii=False))
            except sqlite3.Error:
                self.send_json(500, {"error": "설정을 저장하지 못했습니다."})
                return
            self.send_json(200, {"settings": settings})
            return

        if path == "/api/refine":
            if not ollama_ready():
                self.send_json(503, {"error": "AI_NOT_CONFIGURED"})
                return
            try:
                reflection = request_ollama_reflection(payload)
            except RuntimeError as exc:
                self.send_json(502, {"error": str(exc)})
                return
            self.send_json(200, {"reflection": reflection, "model": OLLAMA_MODEL})
            return

        if not str(payload.get("workMemo", "")).strip():
            self.send_json(400, {"error": "수행한 업무를 먼저 입력해주세요."})
            return
        try:
            result = generate_with_available_ai(payload)
        except RuntimeError as exc:
            if str(exc) == "AI_NOT_CONFIGURED":
                self.send_json(503, {"error": "AI_NOT_CONFIGURED"})
            else:
                self.send_json(502, {"error": str(exc)})
            return
        self.send_json(200, result)


def open_browser() -> None:
    webbrowser.open(f"http://{HOST}:{PORT}")


def main() -> None:
    background_mode = "--background" in sys.argv
    ensure_ollama_running()
    try:
        server = ThreadingHTTPServer((HOST, PORT), OJTHandler)
    except OSError:
        # 로그인 시 이미 실행된 모니터가 포트를 쓰고 있는 정상 상황이다.
        if not background_mode:
            if sys.stdout:
                print(f"이미 실행 중인 OJT 작성 도우미가 있어 http://{HOST}:{PORT} 을 엽니다.")
            open_browser()
        return
    initialize_activity_db()
    start_activity_monitor()
    click_monitor_started = start_interaction_monitor()
    if sys.stdout:
        print(f"OJT 작성 도우미가 http://{HOST}:{PORT} 에서 실행 중입니다.")
        print("업무 활동 모니터링이 로컬에서 실행 중입니다.")
        print("버튼·메뉴 클릭 기록이 실행 중입니다." if click_monitor_started else "버튼·메뉴 클릭 기록을 시작하지 못했습니다.")
        print("종료하려면 이 창에서 Ctrl+C를 누르세요.")
    if not background_mode and not os.environ.get("OJT_NO_BROWSER", "").strip():
        threading.Timer(0.7, open_browser).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sys.stdout:
            print("\nOJT 작성 도우미를 종료합니다.")
    finally:
        monitor_stop.set()
        stop_interaction_monitor()
        server.server_close()


if __name__ == "__main__":
    main()

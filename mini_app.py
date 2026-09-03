"""CustomTkinter 기반 OJT 미니 창.

네이티브 창은 활동 데이터를 직접 읽지 않고 ``server.py``의 로컬 HTTP API만 사용한다.
후보 선택·인라인 편집·초안 생성과 함께 항상 위, 투명도, 단일 인스턴스 같은 Windows 창
상태를 관리한다. 서버 계약이나 후보 필드를 변경하면 브라우저 클라이언트와 함께 검증한다.
"""

from __future__ import annotations

import ctypes
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from ctypes import wintypes
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import BooleanVar, Label, StringVar, Toplevel, messagebox
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))

import customtkinter as ctk  # noqa: E402

from mini_launcher import ensure_server, target_geometry  # noqa: E402


BASE_URL = "http://127.0.0.1:8765"

# 좁은 창이라 색 수를 줄이고 명도 차이로 층을 나눈다.
BG = "#EDF0F5"
SURFACE = "#FFFFFF"
SOFT = "#F5F7FB"
BORDER = "#E3E8EF"
LINE = "#EDF1F6"
TEXT = "#101828"
MUTED = "#667085"
FAINT = "#98A2B3"
BLUE = "#2F6FED"
BLUE_HOVER = "#2461D8"
BLUE_SOFT = "#EEF3FF"
BLUE_EDGE = "#B9CDF9"
GREEN = "#067647"
GREEN_SOFT = "#ECFDF3"
DANGER = "#B42318"
DANGER_SOFT = "#FEF3F2"
CHIP = "#E9EDF3"
CHIP_HOVER = "#DEE4EC"
CAPTION_HOVER = "#E4E9F0"
CLOSE_HOVER = "#E5484D"
CARD_HOVER = "#F8FAFF"
CARD_SELECTED = "#F2F6FF"
CARD_SELECTED_BORDER = "#A9C3F7"
TOOLTIP_BG = "#101828"
FONT = "Malgun Gothic"
# 340px 폭에서 읽히는 최소 크기를 기준으로 잡은 5단계 스케일.
FS_TITLE = 13
FS_HEADING = 12
FS_BODY = 11
FS_LABEL = 10
FS_CAPTION = 9
PILL = "#EEF3FF"
FOOTER_BG = "#E7EBF1"
PRIVACY_NOTE = "로컬 암호화 기록 · 키 입력/비밀번호 수집 안 함"
MEMO_PLACEHOLDER = "추가 메모 (선택)"
OPACITY_PANEL_WIDTH = 208
OPACITY_PANEL_HEIGHT = 46
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SW_MINIMIZE = 6
INSTANCE_MUTEX: Any = None


def blend(start: str, end: str, ratio: float) -> str:
    """두 색 사이를 비율로 섞는다. 버튼 색 전이에 쓴다."""
    ratio = max(0.0, min(1.0, ratio))
    first = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
    second = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(
        round(first[i] + (second[i] - first[i]) * ratio) for i in range(3)
    )


class ApiError(RuntimeError):
    pass


def api_request(path: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> dict[str, Any]:
    data = None
    method = "GET"
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = ""
        raise ApiError(detail or f"요청에 실패했습니다. ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError("OJT 백그라운드 프로그램에 연결하지 못했습니다.") from exc


def process_name_for_window(window: int) -> str:
    process_id = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
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
    process = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name.casefold()
    finally:
        kernel32.CloseHandle(process)
    return ""


def enum_titled_windows(action: Callable[[int, str, str], bool]) -> None:
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(window: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(window)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, title, len(title))
        if "OJT 미니 도우미" not in title.value:
            return True
        return action(window, title.value, process_name_for_window(window))

    user32.EnumWindows(callback, 0)


def close_legacy_web_mini() -> None:
    browsers = {"chrome.exe", "msedge.exe", "brave.exe", "whale.exe"}

    def close_if_browser(window: int, _title: str, process_name: str) -> bool:
        if process_name in browsers:
            ctypes.windll.user32.PostMessageW(window, 0x0010, 0, 0)
        return True

    try:
        enum_titled_windows(close_if_browser)
    except Exception:
        pass


def another_instance_running() -> bool:
    global INSTANCE_MUTEX
    # ctypes.windll 은 호출 사이에 마지막 오류 값을 자기 것으로 바꿔치기해서
    # GetLastError() 를 따로 부르면 엉뚱한 값이 온다. use_last_error 로 받아야 한다.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\OjtNativeMiniAssistant")
    if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
        return False

    def activate(window: int, _title: str, process_name: str) -> bool:
        if process_name in {"python.exe", "pythonw.exe"}:
            ctypes.windll.user32.ShowWindow(window, 9)
            ctypes.windll.user32.SetForegroundWindow(window)
            return False
        return True

    enum_titled_windows(activate)
    return True


class MiniOjtApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.settings = {"startDate": "2026-08-10", "notionUrl": ""}
        self.topic_controls: list[dict[str, Any]] = []
        self.current_entry_id: str | None = None
        self.opacity = 1.0
        self.opacity_panel_open = False
        self.opacity_panel_y = 0.0
        self.opacity_animation_token = 0
        self.preference_after_id: str | None = None
        self.topmost = True
        self.monitor_enabled = True
        self.busy = False
        self.status_after_id: str | None = None
        self.footer_hold_until = 0.0
        self.memo_placeholder_on = False
        self.window_handle: int | None = None
        self.drag_origin: tuple[int, int, int, int] | None = None
        self.motion_tokens: dict[int, int] = {}
        self.motion_bases: dict[int, str] = {}
        self.tooltip_window: Any = None
        self.tooltip_after_id: str | None = None

        x, y, _old_width, _old_height = target_geometry()
        width, height = 340, 520
        self.root.title("OJT 미니 도우미")
        self.root.geometry(f"{width}x{height}+{x + 80}+{y}")
        self.root.configure(fg_color=BG)
        self.root.attributes("-topmost", True)
        self._set_icon()
        self._use_custom_titlebar()
        self.root.geometry(f"{width}x{height}+{x + 80}+{y}")
        self._build_ui()
        self._bind_shortcuts()
        self.root.after(0, self.register_taskbar_button)
        self.refresh_status()
        self.run_async(lambda: api_request("/api/settings"), self.apply_settings)

    def _use_custom_titlebar(self) -> None:
        """Tk 에게 장식 없는 창임을 알린다(overrideredirect).

        WS_CAPTION 만 직접 떼어내면 Tk 는 여전히 캡션이 있다고 계산해서 실제
        클라이언트 영역과 어긋나고, 창을 끌 때 다시 그려지지 않은 띠가 남는다.
        """
        try:
            self.root.overrideredirect(True)
            self.refresh_window_handle()
            self._round_corners()
        except Exception:
            # 실패하면 기본 상단바가 남을 뿐 기능에는 영향이 없다.
            self.window_handle = None

    def register_taskbar_button(self) -> None:
        """장식 없는 창을 작업 표시줄에 올린다.

        셸은 창이 표시되는 순간의 확장 스타일만 보므로 숨겼다 다시 띄워야 한다.
        이 과정을 __init__ 안에서 하면 Tk 의 창 매핑과 엉켜 창이 숨은 채로 남는
        일이 있어, 이벤트 루프가 돌기 시작한 뒤에 실행한다.
        """
        try:
            self.root.withdraw()
            self.refresh_window_handle()
            self._register_in_taskbar()
            self.root.deiconify()
            self.root.update_idletasks()
            self.refresh_window_handle()
            self._register_in_taskbar()
            self._round_corners()
            self.apply_topmost()
        except Exception:
            pass
        self.root.after(300, self.ensure_visible)

    def ensure_visible(self) -> None:
        """숨김 상태로 남는 경우를 대비한 안전망."""
        try:
            if self.root.state() == "withdrawn":
                self.root.deiconify()
                self.apply_topmost()
        except Exception:
            pass
    def _register_in_taskbar(self) -> None:
        if self.window_handle is None:
            return
        try:
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            handle = wintypes.HWND(self.window_handle)
            style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
            user32.SetWindowLongW(handle, GWL_EXSTYLE, (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
        except Exception:
            pass

    def refresh_window_handle(self) -> None:
        """장식을 껐다 켜면 Tk 가 창을 새로 만들어 핸들이 바뀐다."""
        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            self.window_handle = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        except Exception:
            self.window_handle = None
    def _round_corners(self) -> None:
        """장식을 없애면 Windows 11 의 둥근 모서리도 함께 사라져서 직접 요청한다."""
        try:
            preference = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(self.window_handle),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except Exception:
            pass

    def minimize_window(self) -> None:
        self.close_opacity_panel()
        self.hide_tooltip()
        # overrideredirect 를 껐다 켜면 Tk 가 창을 새로 만들면서 최소화가 취소된다.
        # 핸들에 직접 요청하면 창 구조를 건드리지 않는다.
        if self.window_handle is not None:
            try:
                ctypes.windll.user32.ShowWindow(wintypes.HWND(self.window_handle), SW_MINIMIZE)
                return
            except Exception:
                pass
        self.root.iconify()

    def apply_topmost(self) -> None:
        """항상 위 고정을 켜고 끈다.

        장식 없는 창에서는 Tk 의 `-topmost` 해제가 z 순서에 바로 반영되지 않고,
        Tk 의 wm attributes 구현이 확장 스타일을 통째로 다시 써서 작업 표시줄
        등록(WS_EX_APPWINDOW)까지 지운다. 그래서 창 위치를 건드리지 않는
        SetWindowPos 로 밴드만 직접 옮기고 등록을 다시 걸어 준다.
        """
        if self.window_handle is None:
            self.root.attributes("-topmost", self.topmost)
            return
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            user32.SetWindowPos(
                wintypes.HWND(self.window_handle),
                wintypes.HWND(HWND_TOPMOST if self.topmost else HWND_NOTOPMOST),
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception:
            self.root.attributes("-topmost", self.topmost)
        self._register_in_taskbar()

    def apply_opacity(self) -> None:
        """투명도도 wm attributes 를 거치므로 직후에 확장 스타일을 되돌린다."""
        self.root.attributes("-alpha", self.opacity)
        self._register_in_taskbar()
    def fade_color(self, widget: Any, start: str, end: str, option: str = "fg_color", steps: int = 7, delay: int = 16) -> None:
        """색을 몇 프레임에 걸쳐 바꾼다. CTk 는 색이 한 번에 튀어서 직접 그린다."""
        token = self.motion_tokens.get(id(widget), 0) + 1
        self.motion_tokens[id(widget)] = token

        def step(index: int) -> None:
            if self.motion_tokens.get(id(widget)) != token:
                return
            ratio = index / steps
            eased = 1 - (1 - ratio) ** 3
            try:
                widget.configure(**{option: blend(start, end, eased)})
            except Exception:
                return
            if index < steps:
                self.root.after(delay, lambda: step(index + 1))

        step(0)

    def attach_motion(self, button: Any, base: str, hover: str) -> None:
        """올릴 때는 부드럽게, 누를 때는 즉시 반응하게 만든다."""
        button.configure(hover=False)
        pressed = blend(hover, "#000000", 0.14)

        def usable() -> bool:
            try:
                return str(button.cget("state")) != "disabled"
            except Exception:
                return False

        def on_enter(_event: Any = None) -> None:
            if usable():
                self.fade_color(button, base, hover)

        def on_leave(_event: Any = None) -> None:
            if usable():
                self.fade_color(button, hover, base)

        def on_press(_event: Any = None) -> None:
            if not usable():
                return
            self.motion_tokens[id(button)] = self.motion_tokens.get(id(button), 0) + 1
            try:
                button.configure(fg_color=pressed)
            except Exception:
                pass

        def on_release(_event: Any = None) -> None:
            if usable():
                self.fade_color(button, pressed, hover, steps=5)

        button.bind("<Enter>", on_enter, add="+")
        button.bind("<Leave>", on_leave, add="+")
        button.bind("<Button-1>", on_press, add="+")
        button.bind("<ButtonRelease-1>", on_release, add="+")
        self.motion_bases[id(button)] = base

    def reset_motion(self, button: Any) -> None:
        """비활성에서 돌아올 때 색이 hover 로 남지 않게 되돌린다."""
        base = self.motion_bases.get(id(button))
        if base is None:
            return
        self.motion_tokens[id(button)] = self.motion_tokens.get(id(button), 0) + 1
        try:
            button.configure(fg_color=base)
        except Exception:
            pass

    def set_running(self, running: bool) -> None:
        """진행 중임을 얇은 막대로 보여준다. 글자만 바꾸면 눈에 안 띈다."""
        try:
            if running:
                self.progress.configure(progress_color=BLUE)
                self.progress.start()
            else:
                self.progress.stop()
                self.progress.configure(progress_color=BG)
        except Exception:
            pass

    def attach_tooltip(self, widget: Any, text: Any) -> None:
        """아이콘만으로는 뜻이 안 드러나는 버튼에 짧은 설명을 붙인다."""
        widget.bind("<Enter>", lambda _event: self.schedule_tooltip(widget, text), add="+")
        widget.bind("<Leave>", lambda _event: self.hide_tooltip(), add="+")
        widget.bind("<Button-1>", lambda _event: self.hide_tooltip(), add="+")

    def schedule_tooltip(self, widget: Any, text: Any) -> None:
        self.hide_tooltip()
        self.tooltip_after_id = self.root.after(420, lambda: self.show_tooltip(widget, text))

    def show_tooltip(self, widget: Any, text: Any) -> None:
        self.tooltip_after_id = None
        try:
            message = text() if callable(text) else text
            tip = Toplevel(self.root)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            Label(tip, text=message, bg=TOOLTIP_BG, fg="#FFFFFF", font=(FONT, FS_CAPTION), padx=9, pady=5, bd=0).pack()
            tip.update_idletasks()
            x = widget.winfo_rootx() + widget.winfo_width() // 2 - tip.winfo_width() // 2
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            tip.geometry(f"+{max(4, x)}+{y}")
            self.tooltip_window = tip
        except Exception:
            self.tooltip_window = None

    def hide_tooltip(self, _event: Any = None) -> None:
        if self.tooltip_after_id:
            try:
                self.root.after_cancel(self.tooltip_after_id)
            except Exception:
                pass
            self.tooltip_after_id = None
        if self.tooltip_window is not None:
            try:
                self.tooltip_window.destroy()
            except Exception:
                pass
            self.tooltip_window = None
    def bind_window_drag(self, *widgets: Any) -> None:
        for widget in widgets:
            widget.bind("<Button-1>", self.start_window_drag, add="+")
            widget.bind("<B1-Motion>", self.on_window_drag, add="+")
            widget.bind("<ButtonRelease-1>", self.end_window_drag, add="+")

    def start_window_drag(self, event: Any) -> None:
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def on_window_drag(self, event: Any) -> None:
        """헤더를 끌어 창을 옮긴다.

        OS 이동 루프(WM_NCLBUTTONDOWN)를 쓰면 그 안에서 Tk 콜백이 GIL 없이
        재진입해 인터프리터가 죽는다. 그래서 위치를 직접 갱신하되, 매 이동마다
        보류된 그리기를 비워 레이어드 창에 잔상이 남지 않게 한다.
        """
        if self.drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self.drag_origin
        self.root.geometry(f"+{window_x + event.x_root - start_x}+{window_y + event.y_root - start_y}")
        self.root.update_idletasks()

    def end_window_drag(self, _event: Any = None) -> None:
        self.drag_origin = None

    def _bind_shortcuts(self) -> None:
        # 최상위 창은 모든 자식의 bindtag 안에 있어 자식 위젯 클릭도 여기서 받는다.
        self.root.bind("<Button-1>", self.on_root_click, add="+")
        self.root.bind("<Escape>", lambda _event: self.close_opacity_panel(), add="+")
        self.root.bind("<Control-Return>", self.on_generate_shortcut, add="+")
        self.root.bind("<Configure>", self.on_root_configure, add="+")

    def _set_icon(self) -> None:
        image = __import__("tkinter").PhotoImage(width=32, height=32)
        image.put(BLUE, to=(0, 0, 32, 32))
        image.put("#FFFFFF", to=(7, 8, 25, 12))
        image.put("#FFFFFF", to=(7, 15, 12, 25))
        image.put("#FFFFFF", to=(15, 15, 25, 19))
        image.put("#FFFFFF", to=(21, 19, 25, 25))
        self.root.iconphoto(True, image)
        self.root._ojt_icon = image

    def font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        return ctk.CTkFont(family=FONT, size=size, weight=weight)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self.root, height=44, corner_radius=0, fg_color=SURFACE)
        self.header_frame = header
        header.pack(fill="x")
        header.pack_propagate(False)
        brand = ctk.CTkLabel(header, text="O", width=28, height=28, corner_radius=9, fg_color=BLUE, text_color="#FFFFFF", font=self.font(FS_HEADING, "bold"))
        brand.pack(side="left", padx=(10, 8), pady=8)
        title_label = ctk.CTkLabel(header, text="OJT 미니", text_color=TEXT, font=self.font(FS_TITLE, "bold"), anchor="w")
        title_label.pack(side="left")

        # OS 캡션을 없앴으므로 닫기·최소화는 헤더가 직접 갖는다. 창 버튼은 배경 없이
        # 평평하게 둬서 앱 기능 버튼(고정·투명도)과 구분되게 한다.
        self.close_button = ctk.CTkButton(header, text="\u2715", width=28, height=28, corner_radius=7, fg_color=SURFACE, hover_color=CLOSE_HOVER, text_color=MUTED, font=self.font(FS_BODY), command=self.root.destroy)
        self.close_button.pack(side="right", padx=(0, 6), pady=8)
        self.attach_motion(self.close_button, SURFACE, CLOSE_HOVER)
        self.close_button.bind("<Enter>", lambda _event: self.close_button.configure(text_color="#FFFFFF"), add="+")
        self.close_button.bind("<Leave>", lambda _event: self.close_button.configure(text_color=MUTED), add="+")
        self.minimize_button = ctk.CTkButton(header, text="\u2013", width=28, height=28, corner_radius=7, fg_color=SURFACE, hover_color=CAPTION_HOVER, text_color=MUTED, font=self.font(FS_TITLE), command=self.minimize_window)
        self.minimize_button.pack(side="right", pady=8)
        self.attach_motion(self.minimize_button, SURFACE, CAPTION_HOVER)

        self.pin_button = ctk.CTkButton(header, text="⌖", width=28, height=26, corner_radius=8, fg_color=SOFT, hover_color=CAPTION_HOVER, text_color=TEXT, font=self.font(FS_HEADING), command=self.toggle_topmost)
        self.pin_button.pack(side="right", padx=(0, 10), pady=9)
        self.alpha_button = ctk.CTkButton(header, text="◐", width=28, height=26, corner_radius=8, fg_color=SOFT, hover_color=CAPTION_HOVER, text_color=TEXT, font=self.font(FS_HEADING), command=self.cycle_transparency)
        self.alpha_button.pack(side="right", padx=(0, 5), pady=9)

        self.attach_tooltip(self.alpha_button, "창 투명도 조절")
        self.attach_tooltip(self.pin_button, lambda: "항상 위 고정 켜짐 · 누르면 뒤로 보낼 수 있음" if self.topmost else "항상 위 고정 꺼짐 · 누르면 앞에 고정")
        self.attach_tooltip(self.minimize_button, "최소화")
        self.attach_tooltip(self.close_button, "닫기")

        self.bind_window_drag(header, brand, title_label)

        # 푸터는 탭보다 먼저 바닥을 잡아야 한다. pack 은 순서대로 공간을 나눠주므로
        # expand 하는 탭 뒤에 두면 푸터가 통째로 잘려 나간다.
        self.footer_var = StringVar(value="준비 중...")
        footer = ctk.CTkFrame(self.root, height=22, corner_radius=0, fg_color=FOOTER_BG)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        ctk.CTkLabel(footer, textvariable=self.footer_var, text_color=MUTED, font=self.font(FS_CAPTION), anchor="w").pack(side="left", padx=10)

        # 진행 중임을 알리는 얇은 막대. 자리 흔들림이 없도록 늘 깔아 두고
        # 쉬는 동안에는 배경색으로 칠해 보이지 않게 한다.
        self.progress = ctk.CTkProgressBar(
            self.root,
            height=3,
            corner_radius=0,
            border_width=0,
            fg_color=BG,
            progress_color=BG,
            mode="indeterminate",
            indeterminate_speed=1.6,
        )
        self.progress.pack(side="top", fill="x")

        self.tabview = ctk.CTkTabview(
            self.root,
            corner_radius=12,
            fg_color=SURFACE,
            segmented_button_fg_color=CHIP,
            segmented_button_selected_color=BLUE,
            segmented_button_selected_hover_color=BLUE_HOVER,
            segmented_button_unselected_color=CHIP,
            segmented_button_unselected_hover_color=CHIP_HOVER,
            text_color=TEXT,
        )
        self.tabview.pack(fill="both", expand=True, padx=8, pady=(6, 5))
        segmented = getattr(self.tabview, "_segmented_button", None)
        if segmented is not None:
            segmented.configure(font=self.font(FS_BODY, "bold"), height=28)
        self.work_tab = self.tabview.add("업무")
        self.result_tab = self.tabview.add("결과")
        self.work_tab.configure(fg_color=SURFACE)
        self.result_tab.configure(fg_color=SURFACE)
        self._build_work_tab()
        self._build_result_tab()
        self.tabview.set("업무")

        self.opacity_panel = ctk.CTkFrame(
            self.root,
            width=OPACITY_PANEL_WIDTH,
            height=OPACITY_PANEL_HEIGHT,
            corner_radius=12,
            fg_color=SURFACE,
            border_width=1,
            border_color=BORDER,
        )
        self.opacity_panel.pack_propagate(False)
        opacity_content = ctk.CTkFrame(self.opacity_panel, fg_color="transparent")
        opacity_content.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(opacity_content, text="투명도", width=40, text_color=MUTED, font=self.font(FS_CAPTION, "bold")).pack(side="left")
        self.opacity_slider = ctk.CTkSlider(
            opacity_content,
            from_=45,
            to=100,
            number_of_steps=55,
            width=110,
            height=14,
            button_length=14,
            button_corner_radius=7,
            fg_color="#DCE4ED",
            progress_color=BLUE,
            button_color=BLUE,
            button_hover_color=BLUE_HOVER,
            command=self.on_opacity_change,
        )
        self.opacity_slider.set(100)
        self.opacity_slider.pack(side="left", padx=5)
        self.opacity_value_var = StringVar(value="100%")
        ctk.CTkLabel(opacity_content, textvariable=self.opacity_value_var, width=36, text_color=TEXT, font=self.font(FS_CAPTION, "bold")).pack(side="right")
        for widget in (self.opacity_slider, self.opacity_panel, opacity_content):
            widget.bind("<MouseWheel>", self.on_opacity_wheel, add="+")
    def _build_work_tab(self) -> None:
        toolbar = ctk.CTkFrame(self.work_tab, height=34, corner_radius=9, fg_color=SOFT)
        toolbar.pack(fill="x", padx=2, pady=(2, 7))
        toolbar.pack_propagate(False)
        self.date_var = StringVar(value=date.today().isoformat())
        for label, offset in (("어제", -1), ("오늘", 0)):
            button = ctk.CTkButton(toolbar, text=label, width=40, height=26, corner_radius=7, fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, font=self.font(FS_LABEL), command=lambda step=offset: self.set_date(step))
            button.pack(side="left", padx=(4, 0) if label == "어제" else (3, 0), pady=4)
            self.attach_motion(button, CHIP, CHIP_HOVER)
        self.date_entry = ctk.CTkEntry(toolbar, textvariable=self.date_var, width=88, height=26, corner_radius=7, border_width=1, border_color=BORDER, fg_color=SURFACE, font=self.font(FS_LABEL), justify="center")
        self.date_entry.pack(side="left", padx=4, pady=4)
        self.date_entry.bind("<Return>", self.on_date_commit, add="+")
        self.date_entry.bind("<FocusOut>", self.on_date_commit, add="+")
        self.status_var = StringVar(value="확인 중")
        self.status_label = ctk.CTkLabel(toolbar, textvariable=self.status_var, height=24, corner_radius=12, fg_color=GREEN_SOFT, text_color=GREEN, font=self.font(FS_CAPTION, "bold"))
        self.status_label.pack(side="right", padx=(2, 5), pady=5)

        actions = ctk.CTkFrame(self.work_tab, fg_color="transparent")
        actions.pack(fill="x", padx=2)
        self.load_button = ctk.CTkButton(actions, text="기록 정리", height=32, corner_radius=9, fg_color=BLUE, hover_color=BLUE_HOVER, font=self.font(FS_BODY, "bold"), command=lambda: self.load_topics(False))
        self.load_button.pack(side="left", fill="x", expand=True)
        self.attach_motion(self.load_button, BLUE, BLUE_HOVER)
        self.refresh_button = ctk.CTkButton(actions, text="↻", width=36, height=32, corner_radius=9, fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, font=self.font(FS_TITLE), command=lambda: self.load_topics(True))
        self.refresh_button.pack(side="left", padx=(5, 0))
        self.attach_motion(self.refresh_button, CHIP, CHIP_HOVER)
        self.monitor_button = ctk.CTkButton(actions, text="Ⅱ", width=36, height=32, corner_radius=9, fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, font=self.font(FS_LABEL, "bold"), command=self.toggle_monitor)
        self.monitor_button.pack(side="left", padx=(5, 0))
        self.attach_motion(self.monitor_button, CHIP, CHIP_HOVER)
        self.attach_tooltip(self.refresh_button, "AI 로 다시 정리")
        self.attach_tooltip(self.monitor_button, lambda: "활동 기록 일시정지" if self.monitor_enabled else "활동 기록 재개")

        self.topic_frame = ctk.CTkScrollableFrame(self.work_tab, height=150, corner_radius=10, fg_color=SOFT, scrollbar_button_color="#CDD6E0", scrollbar_button_hover_color="#AEBBC9")
        self.memo_text = ctk.CTkTextbox(self.work_tab, height=44, corner_radius=9, border_width=1, border_color=BORDER, fg_color=SURFACE, text_color=TEXT, font=self.font(FS_LABEL), wrap="word")
        self.memo_text.bind("<FocusIn>", self.on_memo_focus_in, add="+")
        self.memo_text.bind("<FocusOut>", self.on_memo_focus_out, add="+")
        self.generate_button = ctk.CTkButton(self.work_tab, text="업무를 선택해 OJT 생성", height=36, corner_radius=9, fg_color=BLUE, hover_color=BLUE_HOVER, font=self.font(FS_BODY, "bold"), command=self.generate_draft)
        self.attach_motion(self.generate_button, BLUE, BLUE_HOVER)

        # 아래쪽 고정 요소가 먼저 자리를 잡고, 남는 높이는 후보 목록이 전부 가져간다.
        # 반대로 두면 목록이 공간을 다 먹어 생성 버튼이 잘린다.
        self.generate_button.pack(side="bottom", fill="x", padx=2, pady=(0, 2))
        self.memo_text.pack(side="bottom", fill="x", padx=2, pady=(6, 7))
        self.topic_frame.pack(fill="both", expand=True, padx=2, pady=(7, 0))

        self.show_memo_placeholder()
        self.render_topics([])
    def _build_result_tab(self) -> None:
        self.result_scroll = ctk.CTkScrollableFrame(self.result_tab, corner_radius=0, fg_color="transparent", scrollbar_button_color="#CDD6E0", scrollbar_button_hover_color="#AEBBC9")
        self.result_scroll.pack(fill="both", expand=True)
        heading = ctk.CTkFrame(self.result_scroll, fg_color="transparent")
        heading.pack(fill="x", pady=(2, 0))
        ctk.CTkLabel(heading, text="OJT 작성 결과", text_color=TEXT, font=self.font(FS_HEADING, "bold")).pack(side="left")
        self.draft_status_var = StringVar(value="작성 전")
        ctk.CTkLabel(heading, textvariable=self.draft_status_var, height=22, corner_radius=11, fg_color=GREEN_SOFT, text_color=GREEN, font=self.font(FS_CAPTION, "bold")).pack(side="right")
        self.performed_text = self.result_field("수행한 업무", 116)
        self.issues_text = self.result_field("이슈·어려운 점", 56)
        self.reflection_text = self.result_field("기타·학습 내용", 70)

        actions = ctk.CTkFrame(self.result_scroll, fg_color="transparent")
        actions.pack(fill="x", pady=(10, 4))
        save_button = ctk.CTkButton(actions, text="저장", width=58, height=32, corner_radius=9, fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, font=self.font(FS_LABEL, "bold"), command=self.save_entry)
        save_button.pack(side="left")
        copy_button = ctk.CTkButton(actions, text="노션용 복사", height=32, corner_radius=9, fg_color=BLUE, hover_color=BLUE_HOVER, font=self.font(FS_LABEL, "bold"), command=self.copy_notion_row)
        copy_button.pack(side="left", fill="x", expand=True, padx=5)
        open_button = ctk.CTkButton(actions, text="노션 ↗", width=60, height=32, corner_radius=9, fg_color=CHIP, hover_color=CHIP_HOVER, text_color=TEXT, font=self.font(FS_LABEL, "bold"), command=self.open_notion)
        open_button.pack(side="right")
        self.attach_motion(save_button, CHIP, CHIP_HOVER)
        self.attach_motion(copy_button, BLUE, BLUE_HOVER)
        self.attach_motion(open_button, CHIP, CHIP_HOVER)
    def result_field(self, label: str, height: int) -> ctk.CTkTextbox:
        ctk.CTkLabel(self.result_scroll, text=label, text_color=MUTED, font=self.font(FS_LABEL, "bold"), anchor="w").pack(fill="x", pady=(10, 3))
        box = ctk.CTkTextbox(self.result_scroll, height=height, corner_radius=9, border_width=1, border_color=BORDER, fg_color=SOFT, text_color=TEXT, font=self.font(FS_LABEL), wrap="word")
        box.pack(fill="x")
        self.forward_wheel(box, self.result_scroll)
        return box
    def run_async(self, work: Callable[[], Any], success: Callable[[Any], None], failure: Callable[[Exception], None] | None = None) -> None:
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, lambda: (failure or self.show_error)(exc))
            else:
                self.root.after(0, lambda: success(result))

        threading.Thread(target=runner, daemon=True).start()

    def apply_settings(self, result: dict[str, Any]) -> None:
        if isinstance(result.get("settings"), dict):
            self.settings.update(result["settings"])
        try:
            self.opacity = max(0.45, min(1.0, float(self.settings.get("miniOpacity", 1.0))))
        except (TypeError, ValueError):
            self.opacity = 1.0
        self.topmost = bool(self.settings.get("miniTopmost", True))
        self.apply_opacity()
        self.apply_topmost()
        self.opacity_slider.set(round(self.opacity * 100))
        self.opacity_value_var.set(f"{round(self.opacity * 100)}%")
        self.paint_pin_button()

    def forward_wheel(self, textbox: ctk.CTkTextbox, scroll_frame: ctk.CTkScrollableFrame) -> None:
        """텍스트박스가 스스로 스크롤할 게 없으면 휠을 바깥 목록으로 넘긴다."""
        inner = getattr(textbox, "_textbox", None)
        canvas = getattr(scroll_frame, "_parent_canvas", None)
        if inner is None or canvas is None:
            return

        def on_wheel(event: Any) -> str | None:
            if inner.yview() != (0.0, 1.0):
                return None
            if canvas.yview() != (0.0, 1.0):
                canvas.yview("scroll", -int(event.delta / 6), "units")
            return "break"

        inner.bind("<MouseWheel>", on_wheel, add="+")

    def parse_date(self) -> str | None:
        value = self.date_var.get().strip()
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
        return value

    def valid_date(self) -> str | None:
        value = self.parse_date()
        if value is None:
            messagebox.showwarning("날짜 확인", "YYYY-MM-DD 형식으로 입력해주세요.", parent=self.root)
        return value

    def on_date_commit(self, _event: Any = None) -> None:
        if self.parse_date():
            self.refresh_status()

    def show_memo_placeholder(self) -> None:
        self.memo_placeholder_on = True
        self.memo_text.delete("1.0", "end")
        self.memo_text.insert("1.0", MEMO_PLACEHOLDER)
        self.memo_text.configure(text_color=MUTED)

    def on_memo_focus_in(self, _event: Any = None) -> None:
        if self.memo_placeholder_on:
            self.memo_placeholder_on = False
            self.memo_text.delete("1.0", "end")
            self.memo_text.configure(text_color=TEXT)

    def on_memo_focus_out(self, _event: Any = None) -> None:
        if not self.memo_placeholder_on and not self.memo_text.get("1.0", "end-1c").strip():
            self.show_memo_placeholder()

    def memo_value(self) -> str:
        if self.memo_placeholder_on:
            return ""
        return self.memo_text.get("1.0", "end-1c").strip()

    def set_date(self, offset: int) -> None:
        self.date_var.set((date.today() + timedelta(days=offset)).isoformat())
        self.render_topics([])
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.status_after_id:
            try:
                self.root.after_cancel(self.status_after_id)
            except Exception:
                pass
        self.status_after_id = self.root.after(30000, self.refresh_status)
        # 30초마다 도는 경로라 경고창을 띄우면 안 된다. 조용히 표시만 바꾼다.
        activity_date = self.parse_date()
        if not activity_date:
            self.status_var.set("날짜 확인")
            self.status_label.configure(fg_color=DANGER_SOFT, text_color=DANGER)
            return
        path = "/api/activity/status?" + urllib.parse.urlencode({"date": activity_date})
        self.run_async(lambda: api_request(path), self.apply_status, lambda _exc: self.status_var.set("연결 실패"))

    def apply_status(self, status: dict[str, Any]) -> None:
        self.monitor_enabled = bool(status.get("enabled"))
        minutes = round(int(status.get("trackedSeconds", 0)) / 60)
        time_text = f"{minutes}분" if minutes < 60 else f"{minutes // 60}h {minutes % 60}m"
        clicks = int(status.get("interactionCount", 0))
        self.status_var.set(f"● {time_text} · {clicks}")
        self.status_label.configure(fg_color=GREEN_SOFT if self.monitor_enabled else "#F0F2F5", text_color=GREEN if self.monitor_enabled else MUTED)
        self.monitor_button.configure(text="Ⅱ" if self.monitor_enabled else "▶")
        # 폴링이 진행 중 안내나 방금 띄운 결과 메시지를 지우지 않도록 한다.
        if not self.busy and time.monotonic() >= self.footer_hold_until:
            self.footer_var.set(PRIVACY_NOTE)

    def toggle_monitor(self) -> None:
        self.run_async(lambda: api_request("/api/activity/toggle", {"enabled": not self.monitor_enabled}), self.apply_status)

    def set_footer(self, message: str, hold: float = 8.0) -> None:
        self.footer_var.set(message)
        self.footer_hold_until = time.monotonic() + hold

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.load_button, self.refresh_button, self.generate_button):
            button.configure(state=state)
            if not busy:
                self.reset_motion(button)
        self.load_button.configure(text="정리 중…" if busy and "정리" in message else "기록 정리")
        self.generate_button.configure(text="작성 중…" if busy and "OJT" in message else self.generate_label())
        self.set_running(busy)
        if message:
            self.set_footer(message)
    def load_topics(self, force: bool) -> None:
        activity_date = self.valid_date()
        if not activity_date or self.busy:
            return
        self.set_busy(True, "세부 조작을 업무별로 정리 중...")

        def done(result: dict[str, Any]) -> None:
            self.set_busy(False)
            topics = result.get("topics") if isinstance(result.get("topics"), list) else []
            self.render_topics(topics)
            source = "저장된 결과" if result.get("cached") else result.get("provider", "로컬 정리")
            self.set_footer(f"{source} · 후보 {len(topics)}개")

        self.run_async(lambda: api_request("/api/activity/topics", {"date": activity_date, "force": force}, 240), done, lambda exc: (self.set_busy(False), self.show_error(exc)))

    def render_topics(self, topics: list[dict[str, Any]]) -> None:
        self.topic_controls.clear()
        for child in self.topic_frame.winfo_children():
            child.destroy()
        if not topics:
            holder = ctk.CTkFrame(self.topic_frame, fg_color="transparent")
            holder.pack(expand=True, pady=38)
            ctk.CTkLabel(holder, text="◔", text_color=BLUE_EDGE, font=self.font(26)).pack()
            ctk.CTkLabel(
                holder,
                text="'기록 정리'를 누르면\n오늘 한 일이 업무 후보로 정리됩니다.",
                text_color=MUTED,
                font=self.font(FS_LABEL),
                justify="center",
            ).pack(pady=(6, 0))
            self.update_selected_count()
            return
        for topic in topics:
            work_related = bool(topic.get("workRelated", True))
            card = ctk.CTkFrame(self.topic_frame, corner_radius=10, fg_color=SURFACE, border_width=1, border_color=BORDER)
            card.pack(fill="x", pady=(0, 7))
            selected = BooleanVar(value=False)
            check = ctk.CTkCheckBox(card, text="", variable=selected, width=26, height=26, checkbox_width=18, checkbox_height=18, corner_radius=5, border_width=2, border_color="#C3CEDB", fg_color=BLUE, hover_color=BLUE_HOVER)
            check.grid(row=0, column=0, rowspan=2, sticky="n", padx=(9, 3), pady=(10, 8))
            title_var = StringVar(value=str(topic.get("title", "")))
            title = ctk.CTkEntry(card, textvariable=title_var, height=26, corner_radius=6, border_width=0, fg_color="transparent", text_color=TEXT if work_related else MUTED, font=self.font(FS_BODY, "bold"))
            title.grid(row=0, column=1, sticky="ew", pady=(7, 0))
            badges = ctk.CTkFrame(card, fg_color="transparent")
            badges.grid(row=0, column=2, padx=(4, 9), pady=(8, 0), sticky="e")
            if not work_related:
                # 개인 활동도 지우지 않되 업무와 구분해 둔다.
                ctk.CTkLabel(badges, text="개인", height=20, corner_radius=10, fg_color=CHIP, text_color=FAINT, font=self.font(FS_CAPTION, "bold")).pack(side="left", padx=(0, 4))
            detail = f"{topic.get('minutes', 1)}분"
            ctk.CTkLabel(badges, text=detail, height=20, corner_radius=10, fg_color=PILL if work_related else CHIP, text_color=BLUE if work_related else FAINT, font=self.font(FS_CAPTION, "bold")).pack(side="left")
            description = ctk.CTkTextbox(card, height=40, corner_radius=7, border_width=0, fg_color=SOFT, text_color=MUTED, font=self.font(FS_LABEL), wrap="word")
            description.insert("1.0", str(topic.get("description", "")))
            description.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(0, 9), pady=(3, 9))
            card.grid_columnconfigure(1, weight=1)
            control = {
                "selected": selected,
                "title": title_var,
                "title_entry": title,
                "description": description,
                "card": card,
                "check": check,
            }
            check.configure(command=lambda item=control: self.toggle_topic(item))
            self.forward_wheel(description, self.topic_frame)
            self.bind_card_hover(control)
            self.topic_controls.append(control)
        self.update_selected_count()
    def paint_card(self, item: dict[str, Any], hover: bool = False) -> None:
        card = item["card"]
        current = card.cget("fg_color")
        if bool(item["selected"].get()):
            target, border = CARD_SELECTED, CARD_SELECTED_BORDER
        else:
            target, border = (CARD_HOVER if hover else SURFACE), BORDER
        card.configure(border_color=border)
        if isinstance(current, str) and current.startswith("#") and current != target:
            self.fade_color(card, current, target, steps=5, delay=14)
        else:
            card.configure(fg_color=target)
    def pointer_in_card(self, item: dict[str, Any]) -> bool:
        try:
            node = self.root.winfo_containing(*self.root.winfo_pointerxy())
        except Exception:
            return False
        while node is not None:
            if node is item["card"]:
                return True
            node = getattr(node, "master", None)
        return False

    def bind_card_hover(self, item: dict[str, Any]) -> None:
        # CTk 위젯은 각자 내부 캔버스를 갖고 있어 자식 위로 지나갈 때도 Leave 가 뜬다.
        # Leave 에서는 실제 커서 위치를 다시 확인해 깜빡임을 막는다.
        for widget in (item["card"], item["check"], item["title_entry"], item["description"]):
            widget.bind("<Enter>", lambda _event, entry=item: self.paint_card(entry, True), add="+")
            widget.bind(
                "<Leave>",
                lambda _event, entry=item: self.root.after(30, lambda: self.paint_card(entry, self.pointer_in_card(entry))),
                add="+",
            )

    def toggle_topic(self, item: dict[str, Any]) -> None:
        self.paint_card(item, True)
        self.update_selected_count()

    def on_root_click(self, event: Any) -> None:
        chain = []
        node = getattr(event, "widget", None)
        while node is not None and len(chain) < 32:
            chain.append(node)
            node = getattr(node, "master", None)
        if self.opacity_panel_open and self.opacity_panel not in chain and self.alpha_button not in chain:
            self.close_opacity_panel()
        for item in self.topic_controls:
            if item["card"] not in chain:
                continue
            # 편집 가능한 칸과 체크박스 자신은 각자의 동작을 그대로 둔다.
            if item["check"] in chain or item["title_entry"] in chain or item["description"] in chain:
                return
            item["selected"].set(not item["selected"].get())
            self.toggle_topic(item)
            return

    def on_generate_shortcut(self, _event: Any = None) -> str:
        if not self.busy:
            self.generate_draft()
        return "break"

    def on_root_configure(self, event: Any) -> None:
        if self.opacity_panel_open and getattr(event, "widget", None) is self.root:
            x, _hidden_y, _shown_y = self.opacity_panel_anchor()
            self.opacity_panel.place_configure(x=x)

    def generate_label(self) -> str:
        count = sum(1 for item in self.topic_controls if item["selected"].get())
        return f"선택한 {count}건으로 OJT 생성  →" if count else "업무를 선택해 OJT 생성"

    def update_selected_count(self) -> None:
        if not self.busy:
            self.generate_button.configure(text=self.generate_label())
    def selected_topics(self) -> list[dict[str, str]]:
        result = []
        for item in self.topic_controls:
            if item["selected"].get() and item["title"].get().strip():
                result.append({"title": item["title"].get().strip(), "description": item["description"].get("1.0", "end-1c").strip()})
        return result

    def infer_context(self, text: str) -> tuple[list[str], list[str]]:
        systems = []
        for pattern, label in ((r"acorn|에이콘", "Acorn"), (r"\bone\b|원 시스템", "ONE"), (r"다우\s*오피스|그룹웨어", "다우오피스"), (r"\bpos\b|포스|매장", "POS·매장"), (r"excel|엑셀", "Office")):
            if re.search(pattern, text, re.I) and label not in systems:
                systems.append(label)
        competencies = []
        if re.search(r"매출|데이터|대조|검증|조회|엑셀", text, re.I):
            competencies.append("운영 데이터 검증 및 분석")
        if re.search(r"테스트|오류|문제|장애|조치", text):
            competencies.append("장애·운영 대응 경험")
        return systems, competencies or ["시스템 운영 프로세스 이해"]

    def generate_draft(self) -> None:
        activity_date = self.valid_date()
        selected = self.selected_topics()
        memo = self.memo_value()
        if not activity_date or (not selected and not memo):
            messagebox.showinfo("업무 선택", "업무 후보를 선택하거나 메모를 입력해주세요.", parent=self.root)
            return
        lines = [f"{index + 1}. {item['title']} - {item['description']}" for index, item in enumerate(selected)]
        work_memo = "\n".join(lines + ([memo] if memo else []))
        systems, competencies = self.infer_context(work_memo)
        try:
            start = datetime.strptime(str(self.settings.get("startDate", "2026-08-10")), "%Y-%m-%d").date()
            week = max(1, min(12, (datetime.strptime(activity_date, "%Y-%m-%d").date() - start).days // 7 + 1))
        except ValueError:
            week = 1
        payload = {"date": activity_date, "stage": f"{week}주차", "workMemo": work_memo, "issueMemo": "", "reflectionMemo": "", "selectedTopics": selected, "manualMemo": memo, "systems": systems, "competencyCandidates": competencies}
        self.set_busy(True, "로컬 AI가 OJT 초안을 작성 중...")

        def done(result: dict[str, Any]) -> None:
            self.set_busy(False)
            self.apply_draft(result.get("entry", {}), result.get("provider", "로컬 AI"))

        def failed(exc: Exception) -> None:
            self.set_busy(False)
            performed = "\n\n".join(f"{i + 1}. {item['title']}" + (f"\n• {item['description']}" if item["description"] else "") for i, item in enumerate(selected))
            if memo:
                performed += ("\n\n" if performed else "") + f"{len(selected) + 1}. 추가 메모\n• {memo}"
            self.apply_draft({"performedTasks": performed, "issues": "특이사항 없음", "reflection": f"• 금일 수행 업무를 통해 {competencies[0]} 관련 업무 흐름을 확인함"}, "로컬 정리")
            self.set_footer(f"로컬 형식으로 작성 · {exc}")

        self.run_async(lambda: api_request("/api/generate", payload, 240), done, failed)

    def apply_draft(self, entry: dict[str, Any], status: str) -> None:
        for box, value in ((self.performed_text, entry.get("performedTasks", "")), (self.issues_text, entry.get("issues", "특이사항 없음")), (self.reflection_text, entry.get("reflection", ""))):
            box.delete("1.0", "end")
            box.insert("1.0", str(value))
        self.draft_status_var.set(status)
        self.tabview.set("결과")
        canvas = getattr(self.result_scroll, "_parent_canvas", None)
        if canvas is not None:
            self.root.after(40, lambda: canvas.yview_moveto(0))
        self.set_footer("초안 완료 · 확인 후 저장 또는 복사")

    def save_entry(self) -> None:
        performed = self.performed_text.get("1.0", "end-1c").strip()
        if not performed:
            messagebox.showinfo("작성 결과", "먼저 OJT 초안을 생성해주세요.", parent=self.root)
            return
        selected = self.selected_topics()
        memo = self.memo_value()
        systems, competencies = self.infer_context(performed)
        entry = {"id": self.current_entry_id or str(uuid.uuid4()), "date": self.date_var.get().strip(), "workMemo": "\n".join(item["title"] for item in selected) or memo, "manualMemo": memo, "selectedTopics": selected, "performedTasks": performed, "issues": self.issues_text.get("1.0", "end-1c").strip() or "특이사항 없음", "reflection": self.reflection_text.get("1.0", "end-1c").strip(), "competencies": competencies, "systems": systems}

        def done(result: dict[str, Any]) -> None:
            self.current_entry_id = str(result.get("entry", {}).get("id", entry["id"]))
            self.set_footer("암호화된 로컬 DB에 저장 완료")

        self.run_async(lambda: api_request("/api/entries", entry), done)

    def copy_notion_row(self) -> None:
        performed = self.performed_text.get("1.0", "end-1c").strip()
        if not performed:
            messagebox.showinfo("작성 결과", "먼저 OJT 초안을 생성해주세요.", parent=self.root)
            return
        compact = lambda value: re.sub(r"\s*\n+\s*", " / ", value).replace("\t", " ").strip()
        row = "\t".join((self.date_var.get().replace("-", "/"), compact(performed), compact(self.issues_text.get("1.0", "end-1c")), compact(self.reflection_text.get("1.0", "end-1c"))))
        self.root.clipboard_clear()
        self.root.clipboard_append(row)
        self.set_footer("노션용 한 행 복사 완료")

    def open_notion(self) -> None:
        url = str(self.settings.get("notionUrl", "")).strip()
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo("노션 주소", "전체 화면 설정에서 노션 주소를 저장해주세요.", parent=self.root)

    def opacity_panel_anchor(self) -> tuple[int, int, int]:
        """패널의 x 와 (숨은 y, 보이는 y) 를 돌려준다."""
        button_right = self.alpha_button.winfo_rootx() - self.root.winfo_rootx() + self.alpha_button.winfo_width()
        x = min(button_right - OPACITY_PANEL_WIDTH, self.root.winfo_width() - OPACITY_PANEL_WIDTH - 6)
        header_bottom = self.alpha_button.winfo_rooty() - self.root.winfo_rooty() + self.alpha_button.winfo_height() + 9
        return max(6, x), header_bottom - OPACITY_PANEL_HEIGHT - 4, header_bottom

    def place_opacity_panel(self) -> None:
        # 크기를 매 프레임 바꾸면 CTk 가 위젯 전체를 다시 그려 뚝뚝 끊긴다.
        # 크기는 고정해 두고 위치만 옮겨 헤더 뒤에서 미끄러져 나오게 한다.
        x, hidden_y, _shown_y = self.opacity_panel_anchor()
        self.opacity_panel.configure(width=OPACITY_PANEL_WIDTH, height=OPACITY_PANEL_HEIGHT)
        self.opacity_panel.place(x=x, y=int(self.opacity_panel_y or hidden_y))
    def cycle_transparency(self) -> None:
        if self.opacity_panel_open:
            self.close_opacity_panel()
            return
        self.opacity_panel_open = True
        self.opacity_animation_token += 1
        x, hidden_y, shown_y = self.opacity_panel_anchor()
        self.opacity_panel_y = hidden_y
        self.opacity_panel.configure(width=OPACITY_PANEL_WIDTH, height=OPACITY_PANEL_HEIGHT)
        self.opacity_panel.place(x=x, y=hidden_y)
        # 헤더를 위로 올려 패널이 그 뒤에서 나오는 것처럼 보이게 한다.
        self.opacity_panel.lift()
        self.header_frame.lift()
        self.animate_opacity_panel(shown_y, self.opacity_animation_token)
        self.alpha_button.configure(fg_color=BLUE_SOFT, text_color=BLUE)
    def close_opacity_panel(self) -> None:
        if not self.opacity_panel_open:
            return
        self.opacity_panel_open = False
        self.opacity_animation_token += 1
        _x, hidden_y, _shown_y = self.opacity_panel_anchor()
        self.animate_opacity_panel(hidden_y, self.opacity_animation_token)
        self.fade_color(self.alpha_button, BLUE_SOFT, SOFT, steps=5)
        self.alpha_button.configure(text_color=TEXT)
    def animate_opacity_panel(self, target_y: int, token: int) -> None:
        if token != self.opacity_animation_token:
            return
        distance = target_y - self.opacity_panel_y
        # 남은 거리가 몇 px 안 되면 1px 씩 기어가는 구간이 생겨 끈적하게 보인다.
        if abs(distance) <= 3:
            self.opacity_panel_y = float(target_y)
            if self.opacity_panel_open:
                self.opacity_panel.place_configure(y=int(target_y))
            else:
                self.opacity_panel.place_forget()
            return
        # 남은 거리에 비례해 움직여 끝에서 감속한다 (ease-out).
        self.opacity_panel_y += distance * 0.34
        self.opacity_panel.place_configure(y=int(round(self.opacity_panel_y)))
        self.root.after(10, lambda: self.animate_opacity_panel(target_y, token))
    def on_opacity_change(self, value: float) -> None:
        percent = round(float(value))
        self.opacity = percent / 100
        self.apply_opacity()
        self.opacity_value_var.set(f"{percent}%")
        self.set_footer(f"창 투명도 {percent}%")
        self.schedule_preference_save()

    def on_opacity_wheel(self, event: Any) -> str:
        delta = 2 if event.delta > 0 else -2
        value = max(45, min(100, round(self.opacity * 100) + delta))
        self.opacity_slider.set(value)
        self.on_opacity_change(value)
        return "break"

    def schedule_preference_save(self) -> None:
        if self.preference_after_id:
            try:
                self.root.after_cancel(self.preference_after_id)
            except Exception:
                pass
        self.preference_after_id = self.root.after(500, self.persist_window_preferences)

    def persist_window_preferences(self) -> None:
        self.preference_after_id = None
        self.settings["miniOpacity"] = round(self.opacity, 2)
        self.settings["miniTopmost"] = self.topmost
        self.run_async(
            lambda: api_request("/api/settings", {"settings": self.settings}),
            lambda _result: None,
            lambda _error: None,
        )

    def toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self.apply_topmost()
        self.paint_pin_button()
        self.set_footer("항상 위 고정 켜짐 · 다른 창 위에 머무릅니다" if self.topmost else "항상 위 고정 꺼짐 · 다른 창 뒤로 갈 수 있습니다")
        self.schedule_preference_save()

    def paint_pin_button(self) -> None:
        self.pin_button.configure(
            text="⌖" if self.topmost else "○",
            fg_color="#E9F0FF" if self.topmost else SOFT,
            text_color=BLUE if self.topmost else MUTED,
        )
    def show_error(self, error: Exception) -> None:
        self.set_busy(False)
        messagebox.showerror("OJT 미니 도우미", str(error), parent=self.root)
        self.set_footer(str(error))


def main() -> None:
    if another_instance_running():
        return
    close_legacy_web_mini()
    if not ensure_server():
        messagebox.showerror("OJT 미니 도우미", "백그라운드 프로그램을 시작하지 못했습니다.")
        return
    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    MiniOjtApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

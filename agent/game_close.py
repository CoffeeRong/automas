"""Close the game instance bound to the current controller.

The state machine has no MaaFramework or ctypes dependency so it can be
tested offline; Win32Api is the only part that touches the operating system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PureWindowsPath
import re
import time

# Same rule as interface.json controller.win32 and start_game.game_window().
GAME_CLASS = "UnrealWindow"
GAME_TITLE_PATTERN = r"^\s*(Infinity\s*Nikki|无限暖暖|無限暖暖)\s*$"
_GAME_TITLE = re.compile(GAME_TITLE_PATTERN)
# Client executables already listed in start_game.PROGRAMS.
KNOWN_IMAGES = ("InfinityNikki.exe", "X6Game.exe", "X6Game-Win64-Shipping.exe")
_KNOWN_IMAGES = frozenset(name.lower() for name in KNOWN_IMAGES)
# Official launcher executables closed by the optional CloseLauncher switch.
LAUNCHER_IMAGES = frozenset({"launcher.exe", "xstarter.exe"})
LAUNCHER_GRACE_SECONDS = 5.0

GRACE_SECONDS = 15.0
FORCE_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.1

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
ERROR_ACCESS_DENIED = 5
WM_CLOSE = 0x0010

SUCCESS_STATUSES = frozenset({"closed", "forced_exit", "already_stopped"})


@dataclass
class CloseResult:
    status: str
    message: str
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS_STATUSES


@dataclass
class _Target:
    hwnd: int
    pid: int
    handle: object
    image: str
    created: int
    bound: bool
    can_terminate: bool

    def describe(self) -> dict:
        return {"hwnd": self.hwnd, "pid": self.pid, "image": self.image,
                "created": self.created, "bound": self.bound}


def bound_game_hwnd(controller):
    """The HWND the client connected to, mirroring navigation.controller."""
    try:
        info = controller.info
    except Exception:
        return None
    if not isinstance(info, dict) or info.get("type") != "win32":
        return None
    hwnd = info.get("hwnd") or info.get("handle")
    return hwnd if type(hwnd) is int and hwnd > 0 else None


def _denied(error) -> bool:
    return getattr(error, "winerror", None) == ERROR_ACCESS_DENIED


def _image_name(path) -> str:
    return PureWindowsPath(path).name.lower() if path else ""


def _is_game_window(api, hwnd) -> bool:
    return (api.window_class(hwnd) == GAME_CLASS
            and _GAME_TITLE.fullmatch(api.window_title(hwnd) or "") is not None)


def _pin(api, hwnd, *, bound, need_terminate):
    """Hold a process handle first, then prove the window still belongs to it."""
    pid = api.window_pid(hwnd)
    if not pid:
        return CloseResult("ambiguous", "无法读取游戏窗口所属进程，未执行关闭。", {"hwnd": hwnd})
    base = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    attempts = ((base | PROCESS_TERMINATE, True), (base, False)) if need_terminate else ((base, False),)
    handle, can_terminate = None, False
    for access, terminate in attempts:
        try:
            handle, can_terminate = api.open_process(pid, access), terminate
            break
        except OSError as error:
            if not _denied(error):
                raise
    if handle is None:
        return CloseResult("access_denied",
                           "没有权限访问游戏进程（游戏可能以更高权限运行），未执行关闭。",
                           {"hwnd": hwnd, "pid": pid})
    try:
        # A recycled PID can no longer swap targets once this handle exists.
        if not api.is_window(hwnd) or api.window_pid(hwnd) != pid:
            api.close_handle(handle)
            return CloseResult("ambiguous", "游戏窗口在定位过程中发生变化，未执行关闭。",
                               {"hwnd": hwnd, "pid": pid})
        image = api.process_image(handle)
        created = api.process_creation_time(handle)
    except BaseException:
        api.close_handle(handle)
        raise
    # The controller binding is authoritative; a fallback candidate must also
    # be a known client executable.
    if not bound and _image_name(image) not in _KNOWN_IMAGES:
        api.close_handle(handle)
        return CloseResult("ambiguous", "找到的窗口不属于已知的无限暖暖客户端程序，未执行关闭。",
                           {"hwnd": hwnd, "pid": pid, "image": image})
    return _Target(hwnd, pid, handle, image, created, bound, can_terminate)


def _locate(api, bound_hwnd, *, need_terminate):
    if bound_hwnd and api.is_window(bound_hwnd):
        if not _is_game_window(api, bound_hwnd):
            return CloseResult("ambiguous", "当前连接的窗口不是无限暖暖游戏窗口，未执行关闭。",
                               {"hwnd": bound_hwnd})
        return _pin(api, bound_hwnd, bound=True, need_terminate=need_terminate)
    # Enumeration failures raise OSError and are reported, never read as "not running".
    windows = [hwnd for hwnd in api.enum_windows()
               if api.is_visible(hwnd) and _is_game_window(api, hwnd)]
    pids = sorted({pid for pid, name in api.list_processes() if name.lower() in _KNOWN_IMAGES})
    details = {"bound_hwnd": bound_hwnd, "windows": windows, "pids": pids}
    if not windows and not pids:
        return CloseResult("already_stopped", "未检测到运行中的无限暖暖，无需关闭。", details)
    if bound_hwnd:
        return CloseResult("ambiguous",
                           "连接的游戏窗口已经关闭，但仍检测到其他无限暖暖窗口或进程，无法确认归属，未执行关闭。",
                           details)
    if not windows:
        return CloseResult("ambiguous", "检测到无限暖暖进程但没有可确认的游戏窗口，未执行关闭。", details)
    if len(windows) > 1:
        return CloseResult("ambiguous", "检测到多个无限暖暖窗口，无法确定要关闭哪一个，未执行关闭。", details)
    return _pin(api, windows[0], bound=False, need_terminate=need_terminate)


def _wait_exit(api, handle, seconds, stopped, clock):
    deadline = clock() + seconds
    while True:
        remaining = deadline - clock()
        if api.wait_process(handle, max(0.0, min(POLL_SECONDS, remaining))):
            return "exited"
        if stopped():
            return "cancelled"
        if deadline - clock() <= 0:
            return "timeout"


def _close(api, target, *, force_on_timeout, stopped, before_close,
           grace_seconds, force_wait_seconds, clock):
    details = target.describe()
    closed = lambda: CloseResult("closed", "无限暖暖已关闭。", details)
    if api.wait_process(target.handle, 0):
        return CloseResult("already_stopped", "游戏已经退出，无需关闭。", details)
    if stopped():
        return CloseResult("cancelled", "任务已停止，未关闭游戏。", details)
    if api.is_window(target.hwnd) and api.window_pid(target.hwnd) == target.pid:
        if before_close is not None:
            try:
                before_close(target.hwnd)
            except Exception as error:  # Window restoration must never block closing.
                details["before_close_error"] = f"{type(error).__name__}: {error}"
        try:
            api.post_close(target.hwnd)
        except OSError as error:
            if api.wait_process(target.handle, 0):
                return closed()
            if _denied(error):
                return CloseResult("access_denied",
                                   "没有权限向游戏窗口发送关闭请求（游戏可能以更高权限运行），未执行关闭。",
                                   details)
            raise
        details["close_requested"] = True
    else:
        # The window is already gone but the pinned process is not: just wait.
        details["close_requested"] = False

    outcome = _wait_exit(api, target.handle, grace_seconds, stopped, clock)
    if outcome == "exited":
        return closed()
    if outcome == "cancelled":
        return CloseResult("cancelled", "任务已停止；已请求游戏退出，未强制结束。", details)
    if not force_on_timeout:
        return CloseResult(
            "timeout",
            f"已请求游戏退出，但 {grace_seconds:g} 秒内未退出（可能停在退出确认框），未强制结束。"
            "如需兜底，可在任务选项中开启「正常退出超时后强制结束」。",
            details)
    if not target.can_terminate:
        return CloseResult("access_denied", "游戏未在限定时间内退出，且没有权限强制结束游戏进程。", details)
    if stopped():
        return CloseResult("cancelled", "任务已停止；已请求游戏退出，未强制结束。", details)
    try:
        api.terminate(target.handle)
    except OSError as error:
        if api.wait_process(target.handle, 0):
            return closed()
        if _denied(error):
            return CloseResult("access_denied", "没有权限强制结束游戏进程。", details)
        raise
    details["forced"] = True
    outcome = _wait_exit(api, target.handle, force_wait_seconds, stopped, clock)
    if outcome == "exited":
        return CloseResult("forced_exit", f"游戏在 {grace_seconds:g} 秒内未正常退出，已强制结束。", details)
    if outcome == "cancelled":
        return CloseResult("cancelled", "已强制结束游戏进程，任务被停止，未确认游戏是否已退出。", details)
    return CloseResult("force_timeout",
                       f"已强制结束游戏进程，但 {force_wait_seconds:g} 秒内仍未确认退出，请手动检查。",
                       details)


def close_connected_game(api, bound_hwnd, *, force_on_timeout=False, stopped=lambda: False,
                         before_close=None, grace_seconds=GRACE_SECONDS,
                         force_wait_seconds=FORCE_WAIT_SECONDS, clock=time.monotonic):
    """Request a normal exit of one confirmed game process and wait for it."""
    target = None
    try:
        located = _locate(api, bound_hwnd, need_terminate=force_on_timeout)
        if isinstance(located, CloseResult):
            return located
        target = located
        return _close(api, target, force_on_timeout=force_on_timeout, stopped=stopped,
                      before_close=before_close, grace_seconds=grace_seconds,
                      force_wait_seconds=force_wait_seconds, clock=clock)
    except OSError as error:
        details = target.describe() if target else {"bound_hwnd": bound_hwnd}
        details["winerror"] = getattr(error, "winerror", None)
        status = "access_denied" if _denied(error) else "error"
        return CloseResult(status, f"关闭游戏时系统调用失败：{error}", details)
    finally:
        if target is not None:
            try:
                api.close_handle(target.handle)
            except OSError:
                pass


def close_launcher(api, *, grace_seconds=LAUNCHER_GRACE_SECONDS, clock=time.monotonic):
    """Best-effort close of official launcher windows; tray-only launchers are left alone.

    Launcher closing is a convenience: it is never force-terminated and a timeout
    here never fails the task. Only visible top-level windows owned by a known
    launcher executable are requested to close.
    """
    handles = []
    try:
        windows = api.enum_windows()
    except OSError:
        return CloseResult("error", "枚举窗口以关闭官方启动器时失败。", {})
    for hwnd in windows:
        if not api.is_visible(hwnd):
            continue
        pid = api.window_pid(hwnd)
        if not pid:
            continue
        try:
            handle = api.open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            continue
        try:
            image = api.process_image(handle)
        except OSError:
            api.close_handle(handle)
            continue
        if _image_name(image) not in LAUNCHER_IMAGES:
            api.close_handle(handle)
            continue
        try:
            api.post_close(hwnd)
        except OSError:
            pass
        handles.append(handle)
    if not handles:
        return CloseResult("already_stopped", "未检测到官方启动器窗口，无需关闭。", {})
    deadline = clock() + grace_seconds
    pending = list(handles)
    while pending:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        pending = [h for h in pending if not api.wait_process(h, min(POLL_SECONDS, remaining))]
    for handle in handles:
        try:
            api.close_handle(handle)
        except OSError:
            pass
    if pending:
        return CloseResult("timeout", "已请求官方启动器退出，但未在限定时间内确认。", {"requested": len(handles)})
    return CloseResult("closed", "官方启动器已关闭。", {"requested": len(handles)})


class Win32Api:
    """Thin ctypes binding; every failure raises OSError carrying the Win32 code."""

    WAIT_OBJECT_0 = 0x0
    WAIT_TIMEOUT = 0x102
    TH32CS_SNAPPROCESS = 0x2
    ERROR_NO_MORE_FILES = 18

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        self._entry_type = PROCESSENTRY32W
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def bind(dll, name, argtypes, restype):
            func = getattr(dll, name)
            func.argtypes, func.restype = argtypes, restype
            return func

        HWND, DWORD, BOOL, HANDLE = wintypes.HWND, wintypes.DWORD, wintypes.BOOL, wintypes.HANDLE
        self._is_window = bind(user32, "IsWindow", [HWND], BOOL)
        self._is_visible = bind(user32, "IsWindowVisible", [HWND], BOOL)
        self._is_hung = bind(user32, "IsHungAppWindow", [HWND], BOOL)
        self._window_pid = bind(user32, "GetWindowThreadProcessId",
                                [HWND, ctypes.POINTER(DWORD)], DWORD)
        self._class_name = bind(user32, "GetClassNameW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._window_text = bind(user32, "GetWindowTextW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
        self._enum_windows = bind(user32, "EnumWindows", [self._callback_type, wintypes.LPARAM], BOOL)
        self._post_message = bind(user32, "PostMessageW",
                                  [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], BOOL)
        self._open_process = bind(kernel32, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
        self._image_name = bind(kernel32, "QueryFullProcessImageNameW",
                                [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)], BOOL)
        self._process_times = bind(kernel32, "GetProcessTimes",
                                   [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, BOOL)
        self._wait = bind(kernel32, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
        self._terminate = bind(kernel32, "TerminateProcess", [HANDLE, wintypes.UINT], BOOL)
        self._close_handle = bind(kernel32, "CloseHandle", [HANDLE], BOOL)
        self._snapshot = bind(kernel32, "CreateToolhelp32Snapshot", [DWORD, DWORD], HANDLE)
        self._first = bind(kernel32, "Process32FirstW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._next = bind(kernel32, "Process32NextW", [HANDLE, ctypes.POINTER(PROCESSENTRY32W)], BOOL)
        self._invalid_handle = ctypes.c_void_p(-1).value

    def _error(self, code=None):
        return self._ctypes.WinError(self._ctypes.get_last_error() if code is None else code)

    def is_window(self, hwnd):
        return bool(self._is_window(hwnd))

    def is_visible(self, hwnd):
        return bool(self._is_visible(hwnd))

    def is_hung(self, hwnd):
        return bool(self._is_hung(hwnd))

    def window_pid(self, hwnd):
        pid = self._wintypes.DWORD()
        if not self._window_pid(hwnd, self._ctypes.byref(pid)):
            return 0
        return int(pid.value)

    def window_class(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._class_name(hwnd, buffer, len(buffer)) else ""

    def window_title(self, hwnd):
        buffer = self._ctypes.create_unicode_buffer(257)
        return buffer.value if self._window_text(hwnd, buffer, len(buffer)) else ""

    def enum_windows(self):
        found = []

        @self._callback_type
        def visit(hwnd, _):
            found.append(int(hwnd))
            return True

        if not self._enum_windows(visit, 0):
            raise self._error()
        return found

    def list_processes(self):
        snapshot = self._snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == self._invalid_handle:
            raise self._error()
        try:
            entry = self._entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            found = []
            ok = self._first(snapshot, self._ctypes.byref(entry))
            while ok:
                found.append((int(entry.th32ProcessID), entry.szExeFile))
                ok = self._next(snapshot, self._ctypes.byref(entry))
            code = self._ctypes.get_last_error()
            if code != self.ERROR_NO_MORE_FILES:
                raise self._error(code)
            return found
        finally:
            self._close_handle(snapshot)

    def open_process(self, pid, access):
        handle = self._open_process(access, False, int(pid))
        if not handle:
            raise self._error()
        return handle

    def process_image(self, handle):
        buffer = self._ctypes.create_unicode_buffer(32768)
        length = self._wintypes.DWORD(len(buffer))
        if not self._image_name(handle, 0, buffer, self._ctypes.byref(length)):
            raise self._error()
        return buffer.value

    def process_creation_time(self, handle):
        times = [self._wintypes.FILETIME() for _ in range(4)]
        if not self._process_times(handle, *(self._ctypes.byref(item) for item in times)):
            raise self._error()
        return (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)

    def wait_process(self, handle, seconds):
        result = self._wait(handle, max(0, int(seconds * 1000)))
        if result == self.WAIT_OBJECT_0:
            return True
        if result == self.WAIT_TIMEOUT:
            return False
        raise self._error()

    def post_close(self, hwnd):
        # PostMessage never blocks on an unresponsive window.
        if not self._post_message(hwnd, WM_CLOSE, 0, 0):
            raise self._error()

    def terminate(self, handle):
        if not self._terminate(handle, 1):
            raise self._error()

    def close_handle(self, handle):
        if handle and not self._close_handle(handle):
            raise self._error()
"""Offline close-game regressions: python/python.exe -B scripts/test_close_game.py."""
import ast
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

import game_close
from game_close import (PROCESS_TERMINATE, CloseResult, bound_game_hwnd,
                        close_connected_game, close_launcher)


class WinError(OSError):
    def __init__(self, code):
        super().__init__(code, f"win32 error {code}")
        self.winerror = code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeApi:
    """Scripted Win32 surface; time only moves inside wait_process."""

    def __init__(self, clock):
        self.clock = clock
        self.windows = {}
        self.processes = {}
        self.handles = {}
        self.closed_handles = []
        self.calls = []
        self.errors = {}
        self.denied_access = 0
        self.after_open = None
        self.before_wait = None
        self._next_handle = 100

    def add_game(self, hwnd=1, pid=10, image=r"C:\Game\X6Game-Win64-Shipping.exe", *,
                 on_close=1.0, on_terminate=0.5, title="无限暖暖", cls="UnrealWindow",
                 window_gone_after_close=None):
        self.processes[pid] = {"image": image, "exit_at": None, "on_close": on_close,
                               "on_terminate": on_terminate, "created": 1234}
        self.windows[hwnd] = {"pid": pid, "title": title, "cls": cls, "visible": True,
                              "gone_at": None, "gone_after_close": window_gone_after_close}
        return hwnd

    def _raise(self, name):
        error = self.errors.get(name)
        if error is not None:
            raise error

    def _alive(self, pid):
        proc = self.processes.get(pid)
        return bool(proc) and (proc["exit_at"] is None or self.clock.now < proc["exit_at"])

    def _window(self, hwnd):
        window = self.windows.get(hwnd)
        if not window or not self._alive(window["pid"]):
            return None
        if window["gone_at"] is not None and self.clock.now >= window["gone_at"]:
            return None
        return window

    def is_window(self, hwnd):
        return self._window(hwnd) is not None

    def is_visible(self, hwnd):
        window = self._window(hwnd)
        return bool(window and window["visible"])

    def is_hung(self, hwnd):
        return False

    def window_pid(self, hwnd):
        window = self._window(hwnd)
        return window["pid"] if window else 0

    def window_class(self, hwnd):
        window = self._window(hwnd)
        return window["cls"] if window else ""

    def window_title(self, hwnd):
        window = self._window(hwnd)
        return window["title"] if window else ""

    def enum_windows(self):
        self._raise("enum_windows")
        return [hwnd for hwnd in self.windows if self._window(hwnd)]

    def list_processes(self):
        self._raise("list_processes")
        return [(pid, proc["image"].split("\\")[-1])
                for pid, proc in self.processes.items() if self._alive(pid)]

    def open_process(self, pid, access):
        self.calls.append(("open_process", pid, access))
        if access & self.denied_access:
            raise WinError(5)
        self._raise("open_process")
        handle = self._next_handle
        self._next_handle += 1
        self.handles[handle] = pid
        if self.after_open:
            self.after_open()
        return handle

    def process_image(self, handle):
        return self.processes[self.handles[handle]]["image"]

    def process_creation_time(self, handle):
        return self.processes[self.handles[handle]]["created"]

    def wait_process(self, handle, seconds):
        if self.before_wait:
            self.before_wait()
        proc = self.processes[self.handles[handle]]
        end = self.clock.now + seconds
        if proc["exit_at"] is not None and proc["exit_at"] <= end:
            self.clock.now = max(self.clock.now, proc["exit_at"])
            return True
        self.clock.now = end
        return False

    def post_close(self, hwnd):
        self.calls.append(("post_close", hwnd))
        self._raise("post_close")
        window = self.windows[hwnd]
        proc = self.processes[window["pid"]]
        if window["gone_after_close"] is not None:
            window["gone_at"] = self.clock.now + window["gone_after_close"]
        if proc["on_close"] is not None:
            proc["exit_at"] = self.clock.now + proc["on_close"]

    def terminate(self, handle):
        self.calls.append(("terminate", handle))
        self._raise("terminate")
        proc = self.processes[self.handles[handle]]
        if proc["on_terminate"] is not None:
            proc["exit_at"] = self.clock.now + proc["on_terminate"]

    def close_handle(self, handle):
        self.closed_handles.append(handle)

    def called(self, name):
        return [call for call in self.calls if call[0] == name]


class CloseFlowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeApi(self.clock)

    def run_close(self, hwnd, **kwargs):
        kwargs.setdefault("clock", self.clock)
        result = close_connected_game(self.api, hwnd, **kwargs)
        self.assertIsInstance(result, CloseResult)
        # Every opened handle is released exactly once on every branch.
        self.assertEqual(sorted(self.api.handles), sorted(self.api.closed_handles))
        return result

    def test_not_running_is_idempotent_success(self):
        result = self.run_close(None)
        self.assertEqual(result.status, "already_stopped")
        self.assertTrue(result.ok)
        self.assertEqual(self.api.calls, [])

    def test_bound_window_closes_normally_without_terminate(self):
        hwnd = self.api.add_game(on_close=2.0)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertEqual(len(self.api.called("post_close")), 1)
        self.assertEqual(self.api.called("terminate"), [])
        self.assertFalse(self.api.called("open_process")[0][2] & PROCESS_TERMINATE)

    def test_window_gone_before_process_keeps_waiting(self):
        hwnd = self.api.add_game(on_close=6.0, window_gone_after_close=0.5)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertGreaterEqual(self.clock.now, 6.0)

    def test_window_already_gone_but_pinned_process_alive(self):
        hwnd = self.api.add_game(on_close=None)

        def vanish():
            # The window disappears right after pinning; the process exits later.
            self.api.windows[hwnd]["gone_at"] = self.clock.now
            self.api.processes[10]["exit_at"] = 3.0

        self.api.before_wait = vanish
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [])
        self.assertFalse(result.details["close_requested"])

    def test_timeout_without_force_never_terminates(self):
        hwnd = self.api.add_game(on_close=None)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.ok)
        self.assertEqual(self.api.called("terminate"), [])
        self.assertAlmostEqual(self.clock.now, game_close.GRACE_SECONDS, places=3)

    def test_timeout_with_force_terminates_pinned_handle_once(self):
        hwnd = self.api.add_game(on_close=None, on_terminate=0.5)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "forced_exit")
        self.assertTrue(result.ok)
        terminate = self.api.called("terminate")
        self.assertEqual(len(terminate), 1)
        self.assertEqual(self.api.handles[terminate[0][1]], 10)
        # No PID re-open after waiting.
        self.assertEqual(len(self.api.called("open_process")), 1)

    def test_force_falls_back_to_graceful_when_terminate_right_denied(self):
        hwnd = self.api.add_game(on_close=1.0)
        self.api.denied_access = PROCESS_TERMINATE
        self.assertEqual(self.run_close(hwnd, force_on_timeout=True).status, "closed")

    def test_force_without_terminate_right_reports_access_denied(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.denied_access = PROCESS_TERMINATE
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "access_denied")
        self.assertEqual(self.api.called("terminate"), [])

    def test_open_process_denied(self):
        hwnd = self.api.add_game()
        self.api.errors["open_process"] = WinError(5)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "access_denied")
        self.assertEqual(self.api.called("post_close"), [])

    def test_post_close_denied(self):
        hwnd = self.api.add_game()
        self.api.errors["post_close"] = WinError(5)
        self.assertEqual(self.run_close(hwnd).status, "access_denied")

    def test_post_close_generic_failure_never_forces(self):
        hwnd = self.api.add_game()
        self.api.errors["post_close"] = WinError(1400)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.api.called("terminate"), [])

    def test_terminate_denied(self):
        hwnd = self.api.add_game(on_close=None)
        self.api.errors["terminate"] = WinError(5)
        self.assertEqual(self.run_close(hwnd, force_on_timeout=True).status, "access_denied")

    def test_terminate_then_wait_timeout(self):
        hwnd = self.api.add_game(on_close=None, on_terminate=None)
        result = self.run_close(hwnd, force_on_timeout=True)
        self.assertEqual(result.status, "force_timeout")
        self.assertFalse(result.ok)

    def test_target_already_exited_after_pinning(self):
        hwnd = self.api.add_game()
        self.api.before_wait = lambda: self.api.processes[10].update(exit_at=self.clock.now)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "already_stopped")
        self.assertEqual(self.api.called("post_close"), [])

    def test_cancel_during_wait_never_escalates(self):
        hwnd = self.api.add_game(on_close=None)
        result = self.run_close(hwnd, force_on_timeout=True, stopped=lambda: self.clock.now >= 3)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.api.called("terminate"), [])
        self.assertLess(self.clock.now, game_close.GRACE_SECONDS)

    def test_cancel_before_request(self):
        hwnd = self.api.add_game()
        result = self.run_close(hwnd, stopped=lambda: True)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.api.called("post_close"), [])

    def test_window_owner_changes_after_open(self):
        hwnd = self.api.add_game()
        self.api.processes[11] = dict(self.api.processes[10])
        self.api.after_open = lambda: self.api.windows[hwnd].update(pid=11)
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("post_close"), [])

    def test_bound_window_must_match_game_rule(self):
        hwnd = self.api.add_game(title="记事本", cls="Notepad")
        result = self.run_close(hwnd)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_bound_window_gone_with_other_instance_is_not_touched(self):
        self.api.add_game(hwnd=2, pid=20)
        result = self.run_close(1)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_bound_window_gone_and_nothing_left(self):
        self.assertEqual(self.run_close(1).status, "already_stopped")

    def test_fallback_requires_single_window(self):
        self.api.add_game(hwnd=1, pid=10)
        self.api.add_game(hwnd=2, pid=20)
        self.assertEqual(self.run_close(None).status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_fallback_single_window_closes(self):
        self.api.add_game(hwnd=1, pid=10)
        self.assertEqual(self.run_close(None).status, "closed")

    def test_fallback_rejects_unknown_executable_but_binding_does_not(self):
        self.api.add_game(hwnd=1, pid=10, image=r"C:\Other\Unknown.exe")
        self.assertEqual(self.run_close(None).status, "ambiguous")
        self.assertEqual(self.api.called("post_close"), [])
        self.assertEqual(self.run_close(1).status, "closed")

    def test_background_process_without_window_is_not_killed(self):
        self.api.add_game(hwnd=1, pid=10)
        self.api.windows[1]["visible"] = False
        result = self.run_close(None, force_on_timeout=True)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(self.api.called("open_process"), [])

    def test_enumeration_failure_is_not_treated_as_stopped(self):
        self.api.errors["enum_windows"] = WinError(8)
        result = self.run_close(None)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.ok)

    def test_before_close_runs_once_and_errors_do_not_block(self):
        hwnd = self.api.add_game()
        seen = []

        def hook(target):
            seen.append(target)
            raise RuntimeError("restore failed")

        result = self.run_close(hwnd, before_close=hook)
        self.assertEqual(result.status, "closed")
        self.assertEqual(seen, [hwnd])
        self.assertIn("before_close_error", result.details)

    def test_repeat_run_after_close_is_idempotent(self):
        hwnd = self.api.add_game()
        self.assertEqual(self.run_close(hwnd).status, "closed")
        self.assertEqual(self.run_close(hwnd).status, "already_stopped")

    def test_close_launcher_requests_known_launcher_and_ignores_others(self):
        self.api.processes[20] = {"image": r"C:\Game\launcher.exe", "exit_at": None,
                                  "on_close": 1.0, "on_terminate": None, "created": 100}
        self.api.windows[5] = {"pid": 20, "title": "launcher", "cls": "Qt5QWindowIcon", "visible": True,
                               "gone_at": None, "gone_after_close": None}
        self.api.processes[30] = {"image": r"C:\Windows\notepad.exe", "exit_at": None,
                                  "on_close": None, "on_terminate": None, "created": 200}
        self.api.windows[6] = {"pid": 30, "title": "记事本", "cls": "Notepad", "visible": True,
                               "gone_at": None, "gone_after_close": None}
        result = close_launcher(self.api, clock=self.clock)
        self.assertEqual(result.status, "closed")
        self.assertEqual(self.api.called("post_close"), [("post_close", 5)])
        self.assertEqual(self.api.called("terminate"), [])

    def test_close_launcher_none_is_idempotent(self):
        self.assertEqual(close_launcher(self.api, clock=self.clock).status, "already_stopped")


class BindingTests(unittest.TestCase):
    def test_bound_hwnd_reads_win32_controller_info_only(self):
        controller = type("C", (), {})()
        controller.info = {"type": "win32", "hwnd": 4242}
        self.assertEqual(bound_game_hwnd(controller), 4242)
        controller.info = {"type": "adb", "hwnd": 4242}
        self.assertIsNone(bound_game_hwnd(controller))
        controller.info = {"type": "win32", "hwnd": "4242"}
        self.assertIsNone(bound_game_hwnd(controller))

        class Broken:
            @property
            def info(self):
                raise RuntimeError("disconnected")

        self.assertIsNone(bound_game_hwnd(Broken()))


def read_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8-sig"))


class ConfigTests(unittest.TestCase):
    def test_task_is_opt_in_and_after_regular_tasks(self):
        imports = read_json("interface.json")["import"]
        self.assertIn("tasks/CloseGame.json", imports)
        index = imports.index("tasks/CloseGame.json")
        for name in imports[:index]:
            groups = {group for task in read_json(name).get("task", []) for group in task.get("group", [])}
            self.assertFalse(any(group.startswith("developer") for group in groups), name)
        task = read_json("tasks/CloseGame.json")["task"][0]
        self.assertEqual(task["name"], "close_game")
        self.assertFalse(task["default_check"])
        self.assertEqual(task["group"], ["posttask"])
        labels = {group["name"]: group["label"] for group in read_json("interface.json")["group"]}
        self.assertEqual(labels.get("posttask"), "后置任务")
        presets = read_json("tasks/Presets.json")["preset"]
        self.assertFalse(any(item["name"] == "close_game" for preset in presets for item in preset["task"]))

    def test_force_option_defaults_off_everywhere(self):
        option = read_json("tasks/CloseGame.json")["option"]["ForceCloseOnTimeout"]
        self.assertEqual(option["type"], "switch")
        self.assertEqual(option["default_case"], "No")
        cases = {case["name"]: case["pipeline_override"]["MaaNikki_CloseGame"]["custom_action_param"]
                 for case in option["cases"]}
        self.assertEqual(cases, {"Yes": {"force_on_timeout": True}, "No": {"force_on_timeout": False}})
        launcher = read_json("tasks/CloseGame.json")["option"]["CloseLauncher"]
        self.assertEqual(launcher["type"], "switch")
        self.assertEqual(launcher["default_case"], "No")
        launcher_cases = {case["name"]: case["pipeline_override"]["MaaNikki_CloseGame"]["custom_action_param"]
                          for case in launcher["cases"]}
        self.assertEqual(launcher_cases, {"Yes": {"close_launcher": True}, "No": {"close_launcher": False}})
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        self.assertEqual(node["custom_action_param"], {"force_on_timeout": False, "close_launcher": False})

    def test_entry_needs_no_screenshot_before_or_after_action(self):
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        self.assertEqual(node["recognition"], "DirectHit")
        self.assertEqual(node["next"], [])
        self.assertEqual((node["pre_delay"], node["post_delay"]), (0, 0))
        for key in ("pre_wait_freezes", "post_wait_freezes", "on_error"):
            self.assertNotIn(key, node)

    def test_action_name_and_registration_match(self):
        node = read_json("resource/pipeline/close_game.json")["MaaNikki_CloseGame"]
        source = (ROOT / "agent/action/close_game.py").read_text(encoding="utf-8")
        self.assertIn(f'@agent_action("{node["custom_action"]}")', source)
        self.assertNotIn("stop_on_failure", source)
        main = (ROOT / "agent/main.py").read_text(encoding="utf-8")
        self.assertIn("from action import close_game as _close_game", main)

    def test_window_rule_matches_controller_and_launcher(self):
        controller = read_json("interface.json")["controller"][0]["win32"]
        self.assertEqual(controller["window_regex"], game_close.GAME_TITLE_PATTERN)
        self.assertEqual(controller["class_regex"], f"^{game_close.GAME_CLASS}$")
        tree = ast.parse((ROOT / "agent/start_game.py").read_text(encoding="utf-8"))
        programs = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign)
                        and any(getattr(target, "id", None) == "PROGRAMS" for target in node.targets))
        self.assertEqual(set(programs), set(game_close.KNOWN_IMAGES))
        for title in ("无限暖暖", " Infinity Nikki ", "無限暖暖"):
            self.assertTrue(re.fullmatch(game_close.GAME_TITLE_PATTERN, title))
        self.assertIsNone(re.fullmatch(game_close.GAME_TITLE_PATTERN, "无限暖暖 启动器"))


if __name__ == "__main__":
    unittest.main()

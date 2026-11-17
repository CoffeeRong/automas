"""「关闭游戏」任务：只关闭当前控制器连接的游戏实例。"""
from __future__ import annotations

import os

from client_config import parse_custom_param
from daily.runtime import Runtime
from game_close import (Win32Api, bound_game_hwnd, close_connected_game,
                        close_launcher)
from registration import agent_action

from maa.custom_action import CustomAction


def _restore_window_size(api):
    """Undo this session's 16:9 auto-adjustment before the game can persist it.

    A hung window is skipped because SetWindowPos would block on it.
    """
    def restore(hwnd):
        if api.is_hung(hwnd):
            return
        from tall_window import restore_session_16_9_windows
        restore_session_16_9_windows()
    return restore


@agent_action("nikki.close_game")
class CloseGameAction(CustomAction):
    """Runs only when the queue reaches it; it is not a finally/posttask hook."""

    def run(self, context, argv: CustomAction.RunArg) -> bool:
        runtime = Runtime(context)
        try:
            options = parse_custom_param(argv.custom_action_param)
        except ValueError as error:
            runtime.log(f"关闭游戏参数无效：{error}")
            return False
        if os.name != "nt":
            runtime.log("关闭游戏任务仅支持 Windows。")
            return False
        force = options.get("force_on_timeout") is True
        close_launcher_too = options.get("close_launcher") is True
        api = Win32Api()
        result = close_connected_game(
            api, bound_game_hwnd(runtime.controller),
            force_on_timeout=force,
            stopped=lambda: runtime.stopped,
            before_close=_restore_window_size(api),
        )
        if close_launcher_too and result.ok:
            # Launcher closing is best-effort and never fails the task.
            runtime.log(close_launcher(api).message)
        if not result.ok and result.status != "cancelled":
            # FailureEvidence never captures; this action keeps no frame either.
            runtime.diagnostic("close_game", result.message, details={
                "status": result.status, "force_on_timeout": force, **result.details})
        runtime.log(result.message)
        return result.ok

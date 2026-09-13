import threading
import time

from PyQt5.QtCore import QObject, pyqtSignal


class HotkeyService(QObject):
    start_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    route_record_start_requested = pyqtSignal()
    route_record_finish_requested = pyqtSignal()
    availability_changed = pyqtSignal(bool, str)

    def __init__(self):
        """初始化热键句柄集合和触发去抖状态。"""
        super().__init__()
        self._keyboard = None
        self._handles = []
        self._lock = threading.Lock()
        self._last_event = {
            "start": 0.0,
            "pause": 0.0,
            "stop": 0.0,
            "route_record_start": 0.0,
            "route_record_finish": 0.0,
        }

    def register(self) -> bool:
        """注册全局启动、停止及备用停止热键。"""
        if self._handles:
            return True
        try:
            import keyboard

            self._keyboard = keyboard
            self._handles = [
                keyboard.on_press_key(68, lambda _event: self._emit("start"), suppress=False),
                keyboard.on_press_key(87, lambda _event: self._emit("stop"), suppress=False),
                keyboard.on_press_key(88, lambda _event: self._emit("stop"), suppress=False),
                keyboard.add_hotkey("f9", lambda: self._emit("pause"), suppress=False),
                keyboard.add_hotkey("ctrl+alt+q", lambda: self._emit("stop"), suppress=False),
                keyboard.add_hotkey(
                    "f5",
                    lambda: self._emit("route_record_start"),
                    suppress=False,
                ),
                keyboard.add_hotkey(
                    "f6",
                    lambda: self._emit("route_record_finish"),
                    suppress=False,
                ),
            ]
            message = (
                "录制 F5 开始 / F6 结束 · "
                "F9 暂停/继续 · F10 启动 · F11 停止 · F12 / Ctrl+Alt+Q 备用停止"
            )
            print("[热键] {}".format(message))
            self.availability_changed.emit(True, message)
            return True
        except Exception as exc:
            self._handles = []
            message = "全局热键注册失败：{}".format(exc)
            print("[热键] {}".format(message))
            self.availability_changed.emit(False, message)
            return False

    def unregister(self) -> None:
        """注销当前服务创建的全部全局热键。"""
        keyboard = self._keyboard
        if keyboard is None:
            return
        for handle in self._handles:
            try:
                keyboard.unhook(handle)
            except (KeyError, ValueError, TypeError):
                try:
                    keyboard.remove_hotkey(handle)
                except (KeyError, ValueError, TypeError):
                    pass
        self._handles = []

    def _emit(self, action: str) -> None:
        """在短时间内合并重复热键事件后发送 Qt 信号。"""
        now = time.monotonic()
        with self._lock:
            if now - self._last_event[action] < 0.5:
                return
            self._last_event[action] = now
        if action == "start":
            self.start_requested.emit()
        elif action == "pause":
            self.pause_requested.emit()
        elif action == "stop":
            self.stop_requested.emit()
        elif action == "route_record_start":
            self.route_record_start_requested.emit()
        elif action == "route_record_finish":
            self.route_record_finish_requested.emit()

import sys
import threading
from typing import Optional, TextIO

from PyQt5.QtCore import QObject, pyqtSignal


class LogStream(QObject):
    """把任意工作线程的 print 安全转发到 Qt 日志面板。"""

    text_written = pyqtSignal(str)

    def __init__(self, original: Optional[TextIO] = None):
        """保存原输出流，并创建线程安全写锁。"""
        super().__init__()
        self.original = original
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        """把文本发送到 Qt，同时可选地镜像到原控制台。"""
        if not text:
            return 0
        self.text_written.emit(str(text))
        if self.original is not None:
            with self._lock:
                try:
                    self.original.write(text)
                    self.original.flush()
                except (OSError, ValueError):
                    pass
        return len(text)

    def flush(self) -> None:
        """刷新原输出流；Qt 信号本身不需要刷新。"""
        if self.original is not None:
            try:
                self.original.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        """声明该桥接流不是交互式终端。"""
        return False

    @property
    def encoding(self) -> str:
        """返回原流编码，缺失时使用 UTF-8。"""
        return getattr(self.original, "encoding", None) or "utf-8"


class LogRedirector:
    def __init__(self):
        """记录进程原始标准流并准备统一桥接流。"""
        self.stdout = sys.stdout
        self.stderr = sys.stderr
        self.stream = LogStream(self.stdout)

    def install(self) -> LogStream:
        """把标准输出和错误输出切换到 Qt 日志桥。"""
        sys.stdout = self.stream
        sys.stderr = self.stream
        return self.stream

    def restore(self) -> None:
        """仅在仍由本实例接管时恢复原标准流。"""
        if sys.stdout is self.stream:
            sys.stdout = self.stdout
        if sys.stderr is self.stream:
            sys.stderr = self.stderr

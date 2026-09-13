import sys
import traceback

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QMessageBox

from .config import ConfigStore, default_config_path
from .hotkeys import HotkeyService
from .logging_bridge import LogRedirector
from .main_window import MainWindow
from .mode_service import ModeSwitchingService
from .theme import APP_STYLE


def main(test_mode: bool = False) -> int:
    """启动 3.0 界面。

    ``test_mode=True`` 兼容旧入口，让页面首次默认勾选截图测试模式；每次
    实际启动任务时仍以页面保存的运行模式为准。
    """
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("QQ炫舞 3.0")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)

    # 页面在每次启动任务时选择正常自动化或完全独立的只读检测服务。
    service = ModeSwitchingService()
    store = ConfigStore(default_config_path())
    window = MainWindow(service, store, test_mode=test_mode)
    mode_name = (
        "纯识别测试模式"
        if window.detection_only_mode
        else ("截图测试模式" if window.test_mode else "正常模式")
    )
    app.setApplicationName("QQ炫舞 3.0 - {}".format(mode_name))
    hotkeys = HotkeyService()
    redirector = LogRedirector()
    stream = redirector.install()
    stream.text_written.connect(window.append_log)

    hotkeys.start_requested.connect(window.request_start)
    hotkeys.pause_requested.connect(window.request_toggle_pause)
    hotkeys.stop_requested.connect(window.request_stop)
    hotkeys.route_record_start_requested.connect(window.start_pending_route_segment)
    hotkeys.route_record_finish_requested.connect(window.finish_route_segment)
    hotkeys.availability_changed.connect(window.set_hotkey_status)
    window.add_close_callback(hotkeys.unregister)
    window.add_close_callback(redirector.restore)

    def handle_exception(exc_type, exc_value, exc_traceback):
        """记录未捕获异常，并在主线程向用户显示错误信息。"""
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        print(details)
        QMessageBox.critical(window, "程序异常", str(exc_value))

    sys.excepthook = handle_exception
    window.show()
    hotkeys.register()
    print("[系统] QQ炫舞 3.0 已就绪，页面当前选择：{}。".format(mode_name))

    try:
        return app.exec_()
    finally:
        service.stop()
        hotkeys.unregister()
        redirector.restore()

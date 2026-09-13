"""在完整自动化、带截图自动化和纯识别测试服务之间切换。"""

from typing import Optional

from PyQt5.QtCore import QObject, pyqtSignal

from .automation_service import AutomationService
from .config import AppConfig
from .detection_test_service import DetectionTestService


class ModeSwitchingService(QObject):
    """对主窗口暴露统一接口，并严格隔离纯识别模式的所有游戏按键。"""

    status_changed = pyqtSignal(str, str)
    started = pyqtSignal()
    stopped = pyqtSignal()
    failed = pyqtSignal(str)
    frame_ready = pyqtSignal(object, object)
    rest_snapshot_ready = pyqtSignal(object, object)
    rest_schedule_changed = pyqtSignal(object)

    def __init__(self):
        """创建自动化与纯识别服务，并转发当前活动服务的全部信号。"""
        super().__init__()
        self._automation = AutomationService()
        self._detection_test = DetectionTestService()
        self._active: Optional[QObject] = None
        self._last_config = AppConfig()
        self._connect_service(self._automation)
        self._connect_service(self._detection_test)

    @property
    def is_running(self) -> bool:
        """返回自动化或纯识别服务是否仍在运行。"""
        return self._automation.is_running or self._detection_test.is_running

    @property
    def last_config(self) -> AppConfig:
        """返回最近一次从页面提交的完整配置。"""
        return self._last_config

    @property
    def is_paused(self) -> bool:
        """返回当前活动服务是否已暂停。"""
        return bool(
            self._active is not None
            and getattr(self._active, "is_paused", False)
        )

    @property
    def is_test_mode(self) -> bool:
        """返回当前或最近一次启动是否启用了任意截图测试选项。"""
        return bool(
            self._last_config.test_mode
            or self._last_config.detection_only_mode
        )

    def start(self, config: AppConfig, trace_suffix=None) -> bool:
        """纯识别选项启动只读服务，其余模式启动完整自动化服务。"""
        config.validate()
        if self.is_running:
            print("[运行] 当前任务仍在运行或停止中，请稍后再启动。")
            return False
        self._last_config = config
        self._active = (
            self._detection_test
            if config.detection_only_mode
            else self._automation
        )
        mode_name = (
            "纯识别测试模式"
            if config.detection_only_mode
            else ("截图测试模式" if config.test_mode else "正常模式")
        )
        print("[系统] 页面选择运行模式：{}。".format(mode_name))
        if config.detection_only_mode:
            print("[测试] 纯识别安全模式已启用：只截图和识别怪物，不执行任何游戏按键。")
        elif config.test_mode:
            print("[测试] 实时截图已启用：地图路线、移动、攻击和运行日志保持正常。")
        if self._active is self._automation:
            started = self._active.start(config, trace_suffix=trace_suffix)
        else:
            started = self._active.start(config)
        if not started:
            self._active = None
        return started

    def stop(self) -> None:
        """停止当前活动服务，并兼容退出时回收两个后台对象。"""
        if self._active is not None:
            self._active.stop()
            return
        self._automation.stop()
        self._detection_test.stop()

    def pause(self) -> bool:
        """暂停当前完整自动化或纯识别测试。"""
        if self._active is None:
            return False
        pause = getattr(self._active, "pause", None)
        return bool(pause and pause())

    def resume(self) -> bool:
        """继续当前完整自动化或纯识别测试。"""
        if self._active is None:
            return False
        resume = getattr(self._active, "resume", None)
        return bool(resume and resume())

    def toggle_pause(self) -> bool:
        """在暂停和继续之间切换。"""
        return self.resume() if self.is_paused else self.pause()

    def capture_yellow_position(self):
        """复用正常服务的小地图黄色人物坐标采集能力。"""
        return self._automation.capture_yellow_position()

    def trigger_rest_point_test(self) -> bool:
        """把一键休息点测试请求转发给当前完整自动化服务。"""
        if self._active is not self._automation:
            return False
        return self._automation.trigger_rest_point_test()

    def _connect_service(self, service) -> None:
        """转发当前活动服务的状态、错误、生命周期和实时截图信号。"""
        service.status_changed.connect(
            lambda state, text, source=service: self._forward_status(
                source,
                state,
                text,
            )
        )
        service.started.connect(
            lambda source=service: self._forward_started(source)
        )
        service.stopped.connect(
            lambda source=service: self._forward_stopped(source)
        )
        service.failed.connect(
            lambda message, source=service: self._forward_failed(source, message)
        )
        frame_signal = getattr(service, "frame_ready", None)
        if frame_signal is not None:
            frame_signal.connect(
                lambda frame, info, source=service: self._forward_frame(
                    source,
                    frame,
                    info,
                )
            )
        rest_snapshot_signal = getattr(service, "rest_snapshot_ready", None)
        if rest_snapshot_signal is not None:
            rest_snapshot_signal.connect(
                lambda frame, details, source=service: self._forward_rest_snapshot(
                    source,
                    frame,
                    details,
                )
            )
        rest_schedule_signal = getattr(service, "rest_schedule_changed", None)
        if rest_schedule_signal is not None:
            rest_schedule_signal.connect(
                lambda details, source=service: self._forward_rest_schedule(
                    source,
                    details,
                )
            )

    def _forward_status(self, source, state: str, text: str) -> None:
        """仅转发当前活动服务的状态变化。"""
        if source is self._active:
            self.status_changed.emit(state, text)

    def _forward_started(self, source) -> None:
        """仅转发当前活动服务的启动完成信号。"""
        if source is self._active:
            self.started.emit()

    def _forward_stopped(self, source) -> None:
        """转发当前活动服务停止信号并解除活动引用。"""
        if source is not self._active:
            return
        self.stopped.emit()
        self._active = None

    def _forward_failed(self, source, message: str) -> None:
        """仅转发当前活动服务的异常信息。"""
        if source is self._active:
            self.failed.emit(message)

    def _forward_frame(self, source, frame, info) -> None:
        """仅转发当前活动服务生成的实时识别截图。"""
        if source is self._active:
            self.frame_ready.emit(frame, info)
        else:
            consumed = getattr(source, "mark_preview_consumed", None)
            if callable(consumed):
                consumed()

    def mark_preview_consumed(self) -> None:
        """把界面消费回执传给底层服务，解除单帧在途限制。"""
        for service in (self._automation, self._detection_test):
            consumed = getattr(service, "mark_preview_consumed", None)
            if callable(consumed):
                consumed()

    def _forward_rest_snapshot(self, source, frame, details) -> None:
        """仅转发完整自动化确认到达休息点后的截图。"""
        if source is self._active:
            self.rest_snapshot_ready.emit(frame, details)

    def _forward_rest_schedule(self, source, details) -> None:
        """仅转发完整自动化路线中的真实休息计划状态。"""
        if source is self._active:
            self.rest_schedule_changed.emit(details)

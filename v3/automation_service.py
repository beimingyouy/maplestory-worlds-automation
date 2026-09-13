import importlib
import threading
import time
import traceback
from pathlib import Path
from types import ModuleType
from typing import List, Optional

from PyQt5.QtCore import QObject, pyqtSignal

from .config import AppConfig, default_config_path
from .public.monster_detection import (
    custom_template_files,
    ensure_custom_template_directory,
)
from .public.minimap_tracking import (
    capture_yellow_position as capture_minimap_yellow_position,
    run_minimap_tracking,
)
from .public.server_error_guard import (
    detect_server_connection_error,
    load_server_error_template,
    normalize_bgr_frame,
    save_server_error_screenshot,
)
from .map_registry import get_map_spec
from .person_locator import require_character_templates
from .runtime_trace import start_runtime_trace, stop_runtime_trace, trace_event


GAME_FOCUS_LOST_GRACE_SECONDS = 5.0
GAME_FOCUS_CHECK_INTERVAL_SECONDS = 0.20
GAME_FOCUS_RETRY_INTERVAL_SECONDS = 1.0
GAME_WINDOW_TITLE_KEYWORD = "冒险岛怀旧服"
SERVER_ERROR_CHECK_INTERVAL_SECONDS = 0.50
SERVER_ERROR_CONFIRMATION_FRAMES = 2


class AutomationService(QObject):
    status_changed = pyqtSignal(str, str)
    started = pyqtSignal()
    stopped = pyqtSignal()
    failed = pyqtSignal(str)
    frame_ready = pyqtSignal(object, object)
    rest_snapshot_ready = pyqtSignal(object, object)
    rest_schedule_changed = pyqtSignal(object)
    route_monitor_changed = pyqtSignal(object)

    def __init__(self):
        """初始化任务状态、停止事件及兼容引擎引用。"""
        super().__init__()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._engine: Optional[ModuleType] = None
        self._child_threads: List[threading.Thread] = []
        self._preview_frame_lock = threading.Lock()
        self._preview_frame_in_flight = False
        self._last_config = AppConfig()
        self._running_route_label = ""
        self._runtime_trace_started = False
        self._runtime_trace_suffix = ""

    @property
    def is_running(self) -> bool:
        """返回主自动化线程当前是否仍存活。"""
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def last_config(self) -> AppConfig:
        """返回最近一次成功提交给服务的配置。"""
        return self._last_config

    @property
    def is_paused(self) -> bool:
        """返回运行中的完整自动化是否由用户暂停。"""
        return self.is_running and not self._pause_event.is_set()

    def start(self, config: AppConfig, trace_suffix=None) -> bool:
        """校验配置并启动单实例后台任务。"""
        config.validate()
        normalized_trace_suffix = str(trace_suffix or "").strip().strip("_.-")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                print("[运行] 自动化仍在运行或停止中，请稍后再启动。")
                return False
            self._last_config = config
            self._runtime_trace_started = False
            self._runtime_trace_suffix = normalized_trace_suffix
            self._cancel.clear()
            self._pause_event.set()
            with self._preview_frame_lock:
                self._preview_frame_in_flight = False
            self._worker = threading.Thread(
                target=self._run,
                args=(config, normalized_trace_suffix),
                name="v3-automation-run",
                daemon=True,
            )
            # 必须先通知页面进入初始化，再放行后台线程。否则路线加载很快时，
            # running/休息计划信号可能先到，随后又被 starting 覆盖，造成按钮灰色。
            self.status_changed.emit("starting", "正在初始化")
            self._worker.start()
        return True

    def stop(self) -> None:
        """广播停止信号、释放按键并请求所有工作线程退出。"""
        self._cancel.set()
        self._pause_event.set()
        engine = self._engine
        if engine is not None:
            trace_event("stop_requested")
            engine.stop_event2 = 0
            if hasattr(engine, "用户运行许可事件"):
                engine.用户运行许可事件.set()
            if hasattr(engine, "游戏窗口焦点事件"):
                engine.游戏窗口焦点事件.set()
            engine.检测预览回调 = None
            engine.recorded_rest_arrival_callback = None
            engine.recorded_rest_schedule_callback = None
            engine.recorded_route_monitor_callback = None
            engine.休息点测试进行中 = False
            engine.zant = 0
            engine.清除攻击意图()
            if engine.stop_event is not None:
                engine.stop_event.set()
            self._release_keys(engine, self._last_config)
        if self.is_running:
            print("[运行] 收到停止请求，正在安全退出……")
            self.status_changed.emit("stopping", "正在停止")

    def pause(self) -> bool:
        """暂停路线、战斗和定时按键，并立即释放所有可能按住的键。"""
        engine = self._engine
        if not self.is_running or engine is None or not self._pause_event.is_set():
            return False
        self._pause_event.clear()
        engine.用户运行许可事件.clear()
        engine.zant = 0
        engine.清除攻击意图()
        reset_attack = getattr(engine, "重置攻击状态", None)
        if callable(reset_attack):
            reset_attack()
        reset_combat = getattr(engine, "重置战斗感知", None)
        if callable(reset_combat):
            reset_combat()
        self._release_keys(engine, self._last_config)
        trace_event("automation_paused", route=self._running_route_label)
        print("[运行] 已暂停：移动、攻击和定时按键均已释放。")
        self.status_changed.emit("paused", "已暂停")
        return True

    def resume(self) -> bool:
        """从当前位置继续运行，并要求定位线程发布最新人物坐标。"""
        engine = self._engine
        if not self.is_running or engine is None or self._pause_event.is_set():
            return False
        engine.清除攻击意图()
        engine.zant = 0
        reset_attack = getattr(engine, "重置攻击状态", None)
        if callable(reset_attack):
            reset_attack()
        reset_combat = getattr(engine, "重置战斗感知", None)
        if callable(reset_combat):
            reset_combat()
        relocate = getattr(engine, "请求人物重新定位", None)
        if callable(relocate):
            relocate(reason="user_resume")
        else:
            engine.人物全图重定位事件.set()
        engine.用户运行许可事件.set()
        self._pause_event.set()
        trace_event("automation_resumed", route=self._running_route_label)
        print("[运行] 已继续：将按当前位置重新定位并恢复路线。")
        label = "运行中 · {}".format(self._running_route_label) if self._running_route_label else "运行中"
        self.status_changed.emit("running", label)
        return True

    def toggle_pause(self) -> bool:
        """在暂停和继续之间切换。"""
        return self.resume() if self.is_paused else self.pause()

    def update_auto_group_attack_over_count(self, value: int) -> bool:
        """运行中热更新150像素范围自动群攻阈值，并同步当前公共快照。"""
        value = max(0, min(99, int(value)))
        self._last_config.auto_group_attack_over_count = value
        engine = self._engine
        if not self.is_running or engine is None:
            return False
        engine.自动群攻大于数量 = value
        perception_lock = getattr(engine, "追怪感知锁", None)
        if perception_lock is not None:
            with perception_lock:
                engine.当前自动群攻大于数量 = value
        trace_event(
            "auto_group_attack_threshold_updated",
            configured_over_count=value,
            horizontal_range=150.0,
        )
        print(
            "[战斗] 自动群攻阈值已更新：人物X正负150像素内怪物数量 > {} 时使用群攻。".format(
                value
            )
        )
        return True

    def trigger_rest_point_test(self) -> bool:
        """让运行中的自定义录制路线立即执行一次完整休息流程。"""
        engine = self._engine
        if (
            not self.is_running
            or engine is None
            or self._cancel.is_set()
            or self._last_config.map_name != "自定义录制路线"
            or self._last_config.detection_only_mode
        ):
            return False
        request_event = getattr(engine, "测试休息点事件", None)
        if request_event is None:
            return False
        current_is_rest_trace = (
            self._runtime_trace_started
            and self._runtime_trace_suffix == "rest"
        )
        if not current_is_rest_trace:
            trace_path = start_runtime_trace(
                default_config_path().parent,
                {
                    "map": self._last_config.map_name,
                    "selected_map": self._last_config.map_name,
                    "recorded_route_file": str(
                        self._last_config.mushroom_v3_route_file
                    ),
                    "rest_point_test": True,
                    "trace_rotated_from_active_run": self.is_running,
                },
                suffix="rest",
            )
            self._runtime_trace_started = True
            self._runtime_trace_suffix = "rest"
            print("[轨迹] 休息点测试日志：{}".format(trace_path))
        request_event.set()
        trace_event("recorded_route_rest_point_test_button_clicked")
        print("[定时休息] 已请求立即测试休息点，路线将在当前安全帧接管。")
        return True

    @staticmethod
    def capture_yellow_position():
        """同步采集一次小地图黄色人物点，供自定义边界记录使用。"""
        engine = importlib.import_module(".legacy_engine", package=__package__)
        return capture_minimap_yellow_position(engine)

    def _run(self, config: AppConfig, trace_suffix="") -> None:
        """装配兼容引擎、检测线程和地图路线，并统一回收资源。"""
        try:
            self.status_changed.emit("starting", "正在加载运行引擎")
            engine = importlib.import_module(".legacy_engine", package=__package__)
            self._engine = engine
            if self._cancel.is_set():
                return

            self._configure_engine(engine, config)
            self._install_preview_callback(engine, config)
            selected_spec = get_map_spec(config.map_name)
            legacy_boundary_mode = (
                bool(config.custom_mode)
                and selected_spec.name != "自定义录制路线"
            )
            # 自定义模式完全忽略下拉地图路线；截图区域等基础配置复用“蘑菇半图上层”，
            # 怪物判定、追怪和攻击统一由public公共层处理。
            runtime_spec = (
                get_map_spec("蘑菇半层上层") if legacy_boundary_mode else selected_spec
            )
            # 蘑菇V3和独立录制路线固定复用img/自定义模板，避免意外回退YOLO。
            custom_templates_enabled = (
                legacy_boundary_mode
                or config.use_custom_templates
                or runtime_spec.name in ("蘑菇V3", "自定义录制路线")
            )
            effective_detector_name = (
                "template/custom"
                if custom_templates_enabled
                else runtime_spec.detector
            )
            trace_path = None
            trace_enabled = bool(config.runtime_logging_enabled or trace_suffix == "rest")
            if trace_enabled:
                trace_path = start_runtime_trace(
                    default_config_path().parent,
                    {
                    "map": runtime_spec.name,
                    "handler": runtime_spec.handler,
                    "detector": effective_detector_name,
                    "combat_pipeline": (
                        "common_v2_optimized"
                        if legacy_boundary_mode
                        or runtime_spec.handler in (
                            "蘑菇地图V2",
                            "蘑菇地图V3",
                            "自定义录制路线",
                        )
                        else "legacy"
                    ),
                    "selected_map": selected_spec.name,
                    "custom_route": (
                        legacy_boundary_mode
                        or selected_spec.name == "自定义录制路线"
                    ),
                    "legacy_boundary_route": legacy_boundary_mode,
                    "recorded_json_route": selected_spec.name == "自定义录制路线",
                    "custom_template_override": bool(config.use_custom_templates),
                    "attack_distance": float(config.attack_distance),
                    "attack_height": float(config.attack_height),
                    "auto_group_attack_over_count": int(
                        config.auto_group_attack_over_count
                    ),
                    "auto_group_attack_horizontal_range": 150.0,
                    "close_group_attack_horizontal_range": 50.0,
                    "yolo_confidence": float(config.yolo_confidence),
                    "screenshot_preview": bool(config.test_mode),
                    "wheel_detection_enabled": bool(
                        config.wheel_detection_enabled
                    ),
                    "rope_rest_interval_minutes": float(
                        config.rope_rest_interval_minutes
                    ),
                    "rope_rest_duration_minutes": float(
                        config.rope_rest_duration_minutes
                    ),
                    "ignored_platform_numbers": list(
                        config.ignored_platform_numbers
                    ),
                    "scheduled_keys": [
                        {"key": key, "interval_seconds": float(interval)}
                        for key, interval in config.scheduled_keys
                    ],
                    "mushroom_v3_route_file": str(config.mushroom_v3_route_file),
                    "recorded_route_file": str(config.mushroom_v3_route_file),
                    },
                    suffix=trace_suffix,
                )
                self._runtime_trace_started = True
                print("[轨迹] 本次运行记录：{}".format(trace_path))
            else:
                print("[轨迹] 运行日志已关闭，本次不会创建轨迹文件。")
            custom_directory = None
            if custom_templates_enabled:
                custom_directory = ensure_custom_template_directory()
                if not custom_template_files(custom_directory):
                    raise ValueError(
                        "自定义模板目录中没有 guai* 图片：{}".format(custom_directory)
                    )
            detector_label = "TEMPLATE（自定义）" if custom_templates_enabled else runtime_spec.detector.upper()
            route_label = (
                "自定义模式（公共优化战斗）"
                if legacy_boundary_mode
                else runtime_spec.name
            )
            self._running_route_label = route_label
            print("[运行] 路线：{} · 检测方式：{} · 横向距离：{} · 纵向高度：{}".format(
                route_label,
                detector_label,
                int(config.attack_distance),
                int(config.attack_height),
            ))
            if (
                config.rope_rest_interval_minutes > 0
                and config.rope_rest_duration_minutes > 0
            ):
                print(
                    "[休息] 每运行{:.1f}分钟，优先前往JSON录制休息点；"
                    "未录制时使用下一条绳子中点，休息{:.1f}分钟。".format(
                        config.rope_rest_interval_minutes,
                        config.rope_rest_duration_minutes,
                    )
                )
            if runtime_spec.detector == "yolo" and not custom_templates_enabled:
                print("[运行] YOLO 怪物置信度：{:.2f}".format(config.yolo_confidence))
            if custom_directory is not None:
                print("[运行] 自定义怪物模板目录：{}".format(custom_directory))
            if config.use_custom_templates and not legacy_boundary_mode:
                print(
                    "[运行] 仅替换怪物识别：保留“{}”原地图路线，"
                    "怪物坐标改用自定义模板。".format(runtime_spec.name)
                )
            elif runtime_spec.name == "蘑菇V3":
                print(
                    "[运行] 蘑菇V3固定使用录制路线和img\\自定义怪物模板，"
                    "本次不会加载YOLO。"
                )
            elif runtime_spec.name == "自定义录制路线":
                print(
                    "[运行] 自定义录制路线使用公共智能战斗：有怪优先追击攻击，"
                    "清怪后短暂复查，再恢复所选JSON平台巡逻拾取。"
                )

            # 在任何地图线程和按键动作启动前完成资源预检。两张人物模板都缺失时
            # 由当前主工作线程抛错，确保任务整体退出，而不是只让检测子线程静默死亡。
            require_character_templates(
                engine.cv2,
                Path(engine.get_base_dir()),
            )
            engine.prepare_game_window(
                keyword=GAME_WINDOW_TITLE_KEYWORD,
                capture_mode=config.test_mode,
            )
            if self._cancel.is_set():
                return

            game_handles = engine.find_window_by_title(GAME_WINDOW_TITLE_KEYWORD)
            game_hwnd = game_handles[0] if game_handles else None

            locator = threading.Thread(
                target=self._run_child_worker,
                args=("character_locator", run_minimap_tracking, (engine,)),
                name="v3-character-locator",
                daemon=True,
            )
            detector_target = (
                engine.detection_thread
                if runtime_spec.detector == "template"
                else engine.detection_threadyolo
            )
            detector_args = (
                runtime_spec.detection_resource_name,
                config.attack_distance,
                config.attack_height,
                str(custom_directory) if custom_directory is not None else None,
                runtime_spec.name,
            )
            detector = threading.Thread(
                target=self._run_child_worker,
                args=("monster_detector", detector_target, detector_args),
                name="v3-monster-detector",
                daemon=True,
            )
            server_error_guard = threading.Thread(
                target=self._run_child_worker,
                args=(
                    "server_error_guard",
                    self._run_server_error_guard,
                    (engine,),
                ),
                name="v3-server-error-guard",
                daemon=True,
            )
            self._child_threads = [locator, detector, server_error_guard]
            focus_guard_worker = None
            if game_hwnd is not None:
                focus_guard_worker = threading.Thread(
                    target=self._run_child_worker,
                    args=(
                        "game_focus_guard",
                        self._run_game_focus_guard,
                        (engine, game_hwnd, GAME_WINDOW_TITLE_KEYWORD),
                    ),
                    name="v3-game-focus-guard",
                    daemon=True,
                )
                self._child_threads.append(focus_guard_worker)
            else:
                trace_event(
                    "game_focus_guard_unavailable",
                    keyword=GAME_WINDOW_TITLE_KEYWORD,
                    reason="window_not_found",
                )
                print("[焦点保护] 未找到游戏窗口，本次无法启用焦点自动恢复。")
            scheduled_key_worker = None
            if config.scheduled_keys:
                scheduled_key_worker = threading.Thread(
                    target=self._run_child_worker,
                    args=(
                        "scheduled_keys",
                        self._run_scheduled_keys,
                        (engine, config.scheduled_keys),
                    ),
                    name="v3-scheduled-keys",
                    daemon=True,
                )
                self._child_threads.append(scheduled_key_worker)
            locator.start()
            detector.start()
            server_error_guard.start()
            if focus_guard_worker is not None:
                focus_guard_worker.start()
            if scheduled_key_worker is not None:
                scheduled_key_worker.start()

            self.status_changed.emit("running", "运行中 · {}".format(route_label))
            self.started.emit()
            print("[运行] 自动化已启动。")
            if config.test_mode:
                print("[测试] 截图预览与完整自动化已同时启动。")
            trace_event("automation_started", route=route_label)
            if legacy_boundary_mode:
                engine.自定义路线(
                    config.custom_left_position,
                    config.custom_right_position,
                )
            else:
                getattr(engine, runtime_spec.handler)()
        except Exception as exc:
            message = "{}: {}".format(type(exc).__name__, exc)
            trace_event("automation_error", message=message)
            print("[错误] 自动化异常退出：{}".format(message))
            self.failed.emit(message)
            self.status_changed.emit("error", "运行异常")
        finally:
            self._cancel.set()
            self._pause_event.set()
            engine = self._engine
            if engine is not None:
                engine.stop_event2 = 0
                if hasattr(engine, "用户运行许可事件"):
                    engine.用户运行许可事件.set()
                if hasattr(engine, "游戏窗口焦点事件"):
                    engine.游戏窗口焦点事件.set()
                engine.检测预览回调 = None
                engine.recorded_rest_arrival_callback = None
                engine.recorded_rest_schedule_callback = None
                engine.recorded_route_monitor_callback = None
                engine.休息点测试进行中 = False
                engine.清除攻击意图()
                if engine.stop_event is not None:
                    engine.stop_event.set()
                self._release_keys(engine, config)
            for thread in self._child_threads:
                if thread.is_alive():
                    thread.join(timeout=1.5)
            self._child_threads = []
            if engine is not None:
                close_capture = getattr(engine, "close_thread_capture", None)
                if callable(close_capture):
                    close_capture()
            with self._preview_frame_lock:
                self._preview_frame_in_flight = False
            if self._runtime_trace_started:
                trace_event("automation_stopped")
                trace_path = stop_runtime_trace()
                if trace_path is not None:
                    print("[轨迹] 运行记录已保存：{}".format(trace_path))
            self._runtime_trace_started = False
            self._runtime_trace_suffix = ""
            with self._lock:
                self._worker = None
            self._running_route_label = ""
            print("[运行] 自动化线程已退出。")
            self.status_changed.emit("idle", "已停止")
            self.stopped.emit()

    def _run_child_worker(self, worker_name, target, args):
        """运行定位或检测子线程，并把启停与异常完整写入轨迹。"""
        trace_event("worker_started", worker=worker_name)
        try:
            target(*args)
        except Exception as exc:
            message = "{}: {}".format(type(exc).__name__, exc)
            trace_event(
                "worker_error",
                worker=worker_name,
                message=message,
                traceback=traceback.format_exc(),
            )
            print("[错误] {} 线程异常：{}".format(worker_name, message))
            self._cancel.set()
            self._pause_event.set()
            engine = self._engine
            if engine is not None:
                engine.stop_event2 = 0
                if hasattr(engine, "用户运行许可事件"):
                    engine.用户运行许可事件.set()
                engine.zant = 0
                if engine.stop_event is not None:
                    engine.stop_event.set()
        finally:
            engine = self._engine
            close_capture = getattr(engine, "close_thread_capture", None)
            if callable(close_capture):
                close_capture()
            trace_event("worker_stopped", worker=worker_name)

    def _run_scheduled_keys(self, engine: ModuleType, scheduled_keys) -> None:
        """按各自独立周期轻按多个自定义按键，并响应任务停止信号。"""
        now = time.monotonic()
        schedules = [
            {
                "key": str(key).strip().lower(),
                "interval": float(interval),
                "next_at": now + float(interval),
                "press_count": 0,
            }
            for key, interval in scheduled_keys
        ]
        trace_event(
            "scheduled_keys_started",
            schedules=[
                {"key": item["key"], "interval_seconds": item["interval"]}
                for item in schedules
            ],
        )
        print(
            "[定时按键] 已启动：{}".format(
                "，".join(
                    "{}={}秒".format(item["key"], item["interval"])
                    for item in schedules
                )
            )
        )
        try:
            while not self._cancel.is_set() and engine.stop_event2 != 0:
                if not self._pause_event.is_set():
                    paused_at = time.monotonic()
                    while not self._pause_event.wait(timeout=0.1):
                        if self._cancel.is_set() or engine.stop_event2 == 0:
                            return
                    paused_seconds = time.monotonic() - paused_at
                    for item in schedules:
                        item["next_at"] += paused_seconds
                    continue
                focus_event = getattr(engine, "游戏窗口焦点事件", None)
                if focus_event is not None and not focus_event.is_set():
                    self._cancel.wait(GAME_FOCUS_CHECK_INTERVAL_SECONDS)
                    continue
                now = time.monotonic()
                due_items = [item for item in schedules if now >= item["next_at"]]
                for item in due_items:
                    key = item["key"]
                    pressed_at = time.monotonic()
                    interrupted = False
                    try:
                        engine.pydirectinput.keyDown(key)
                        interrupted = self._cancel.wait(0.05)
                    finally:
                        engine.pydirectinput.keyUp(key)
                    item["press_count"] += 1
                    # 若系统卡顿跨过多个周期，只补一次并从当前时间重新计时，
                    # 避免恢复后连续补按多次。
                    item["next_at"] = pressed_at + item["interval"]
                    trace_event(
                        "scheduled_key_pressed",
                        key=key,
                        interval_seconds=item["interval"],
                        press_count=item["press_count"],
                    )
                    if interrupted or self._cancel.is_set():
                        return
                if not schedules:
                    return
                next_due = min(item["next_at"] for item in schedules)
                remaining = max(0.01, next_due - time.monotonic())
                self._cancel.wait(min(remaining, 0.1))
        finally:
            for item in schedules:
                try:
                    engine.pydirectinput.keyUp(item["key"])
                except Exception:
                    pass
            trace_event(
                "scheduled_keys_stopped",
                press_counts={item["key"]: item["press_count"] for item in schedules},
            )

    def _run_game_focus_guard(
        self,
        engine: ModuleType,
        game_hwnd: int,
        window_keyword: str,
    ) -> None:
        """游戏连续失焦五秒后恢复前台；失焦期间暂停公共动作并释放按键。"""
        focus_lost_at = 0.0
        last_restore_attempt_at = 0.0
        focus_event = engine.游戏窗口焦点事件
        focus_event.set()
        trace_event(
            "game_focus_guard_started",
            hwnd=game_hwnd,
            keyword=window_keyword,
            grace_seconds=GAME_FOCUS_LOST_GRACE_SECONDS,
        )
        print(
            "[焦点保护] 已启用：游戏连续失焦 {:.1f} 秒后自动恢复。".format(
                GAME_FOCUS_LOST_GRACE_SECONDS
            )
        )
        try:
            while not self._cancel.is_set() and engine.stop_event2 != 0:
                now = time.monotonic()
                if not engine.win32gui.IsWindow(game_hwnd):
                    handles = engine.find_window_by_title(window_keyword)
                    if handles:
                        game_hwnd = handles[0]
                        trace_event("game_focus_window_relocated", hwnd=game_hwnd)
                    else:
                        if focus_event.is_set():
                            focus_event.clear()
                            self._release_keys(engine, self._last_config)
                        self._cancel.wait(GAME_FOCUS_CHECK_INTERVAL_SECONDS)
                        continue

                foreground_hwnd = engine.win32gui.GetForegroundWindow()
                if foreground_hwnd == game_hwnd:
                    if focus_lost_at > 0:
                        lost_ms = round((now - focus_lost_at) * 1000, 1)
                        engine.游戏窗口失焦累计秒数 = float(
                            getattr(engine, "游戏窗口失焦累计秒数", 0.0)
                        ) + max(0.0, now - focus_lost_at)
                        trace_event(
                            "game_focus_restored",
                            hwnd=game_hwnd,
                            lost_ms=lost_ms,
                            recovery="automatic_or_user",
                        )
                        relocate = getattr(engine, "请求人物重新定位", None)
                        if callable(relocate):
                            relocate(reason="game_focus_restored")
                        else:
                            engine.人物全图重定位事件.set()
                        self.route_monitor_changed.emit(
                            {
                                "focus_status": "focused",
                                "status": "relocalizing",
                                "message": "游戏窗口已恢复，正在重新定位角色",
                                "lost_ms": lost_ms,
                            }
                        )
                    focus_lost_at = 0.0
                    last_restore_attempt_at = 0.0
                    focus_event.set()
                    self._cancel.wait(GAME_FOCUS_CHECK_INTERVAL_SECONDS)
                    continue

                if focus_lost_at <= 0:
                    focus_lost_at = now
                    focus_event.clear()
                    engine.清除攻击意图()
                    self._release_keys(engine, self._last_config)
                    trace_event(
                        "game_focus_lost",
                        game_hwnd=game_hwnd,
                        foreground_hwnd=foreground_hwnd,
                        foreground_title=engine.win32gui.GetWindowText(foreground_hwnd),
                        grace_seconds=GAME_FOCUS_LOST_GRACE_SECONDS,
                        action="pause_input_and_wait",
                    )
                    self.route_monitor_changed.emit(
                        {
                            "focus_status": "lost",
                            "status": "focus_lost",
                            "message": "已切出游戏，暂停路线与卡死计时",
                        }
                    )

                lost_seconds = now - focus_lost_at
                restore_due = lost_seconds >= GAME_FOCUS_LOST_GRACE_SECONDS
                retry_due = (
                    last_restore_attempt_at <= 0
                    or now - last_restore_attempt_at
                    >= GAME_FOCUS_RETRY_INTERVAL_SECONDS
                )
                if restore_due and retry_due:
                    last_restore_attempt_at = now
                    restored = self._restore_game_window_focus(engine, game_hwnd)
                    trace_event(
                        "game_focus_restore_attempted",
                        hwnd=game_hwnd,
                        lost_ms=round(lost_seconds * 1000, 1),
                        restored=restored,
                    )
                    if restored:
                        engine.游戏窗口失焦累计秒数 = float(
                            getattr(engine, "游戏窗口失焦累计秒数", 0.0)
                        ) + max(0.0, lost_seconds)
                        relocate = getattr(engine, "请求人物重新定位", None)
                        if callable(relocate):
                            relocate(reason="game_focus_auto_restored")
                        else:
                            engine.人物全图重定位事件.set()
                        focus_event.set()
                        focus_lost_at = 0.0
                        last_restore_attempt_at = 0.0
                        print("[焦点保护] 游戏失焦超过5秒，已恢复游戏窗口。")
                        self.route_monitor_changed.emit(
                            {
                                "focus_status": "focused",
                                "status": "relocalizing",
                                "message": "已自动恢复游戏窗口，正在重新定位角色",
                            }
                        )
                self._cancel.wait(GAME_FOCUS_CHECK_INTERVAL_SECONDS)
        finally:
            focus_event.set()
            trace_event("game_focus_guard_stopped")

    def _run_server_error_guard(self, engine: ModuleType) -> None:
        """连续识别连接错误画面；先保存截图，再安全结束自动化。"""
        project_root = Path(engine.get_base_dir())
        reference = load_server_error_template(project_root)
        confirmation_count = 0
        last_capture_error_at = 0.0
        trace_event(
            "server_error_guard_started",
            check_interval_seconds=SERVER_ERROR_CHECK_INTERVAL_SECONDS,
            confirmation_frames=SERVER_ERROR_CONFIRMATION_FRAMES,
            template_size=[int(reference.shape[1]), int(reference.shape[0])],
        )
        print(
            "[断线保护] 已启用：识别到服务器连接错误后自动截图并结束脚本。"
        )
        while not self._cancel.is_set() and engine.stop_event2 != 0:
            try:
                capture_width = max(
                    1,
                    int(getattr(engine, "游戏窗口外框宽度", 1280)),
                    int(getattr(engine, "游戏客户区屏幕左", 0))
                    + int(getattr(engine, "游戏客户区宽度", 1280)),
                )
                capture_height = max(
                    1,
                    int(getattr(engine, "游戏窗口外框高度", 831)),
                    int(getattr(engine, "游戏客户区屏幕顶", 31))
                    + int(getattr(engine, "游戏客户区高度", 800)),
                )
                screenshot = engine.grab_screen(
                    {
                        "left": 0,
                        "top": 0,
                        "width": capture_width,
                        "height": capture_height,
                    }
                )
                frame = normalize_bgr_frame(engine.np.asarray(screenshot))
                result = detect_server_connection_error(frame, reference)
                if result.matched:
                    confirmation_count += 1
                    trace_event(
                        "server_connection_error_candidate",
                        confirmation_count=confirmation_count,
                        required_confirmations=SERVER_ERROR_CONFIRMATION_FRAMES,
                        message_confidence=round(result.message_confidence, 4),
                        button_confidence=round(result.button_confidence, 4),
                        message_location=result.message_location,
                        button_location=result.button_location,
                        popup_top_left=result.popup_top_left,
                    )
                else:
                    confirmation_count = 0

                if confirmation_count >= SERVER_ERROR_CONFIRMATION_FRAMES:
                    screenshot_path = save_server_error_screenshot(
                        frame,
                        project_root,
                    )
                    trace_event(
                        "server_connection_error_detected",
                        message_confidence=round(result.message_confidence, 4),
                        button_confidence=round(result.button_confidence, 4),
                        message_location=result.message_location,
                        button_location=result.button_location,
                        popup_top_left=result.popup_top_left,
                        screenshot_path=str(screenshot_path),
                        action="save_screenshot_and_stop_automation",
                    )
                    print(
                        "[断线保护] 检测到服务器连接错误，截图已保存：{}".format(
                            screenshot_path
                        )
                    )
                    self.stop()
                    self.status_changed.emit(
                        "stopping",
                        "检测到服务器连接错误，已截图并停止",
                    )
                    return
            except Exception as exc:
                now = time.monotonic()
                if now - last_capture_error_at >= 5.0:
                    last_capture_error_at = now
                    trace_event(
                        "server_error_guard_capture_failed",
                        message="{}: {}".format(type(exc).__name__, exc),
                    )
                    print(
                        "[断线保护] 截图或识别失败，将继续重试：{}".format(exc)
                    )
            self._cancel.wait(SERVER_ERROR_CHECK_INTERVAL_SECONDS)
        trace_event("server_error_guard_stopped")

    @staticmethod
    def _restore_game_window_focus(engine: ModuleType, game_hwnd: int) -> bool:
        """还原并激活游戏窗口；必要时点击安全的标题栏区域帮助获得焦点。"""
        try:
            import win32con

            engine.win32gui.ShowWindow(game_hwnd, win32con.SW_RESTORE)
            try:
                engine.win32gui.SetForegroundWindow(game_hwnd)
            except Exception:
                pass
            if engine.win32gui.GetForegroundWindow() != game_hwnd:
                outer_left, outer_top, outer_right, _outer_bottom = (
                    engine.win32gui.GetWindowRect(game_hwnd)
                )
                _client_left, client_top = engine.win32gui.ClientToScreen(
                    game_hwnd,
                    (0, 0),
                )
                title_height = max(0, int(client_top - outer_top))
                if title_height >= 6:
                    click_x = int((outer_left + outer_right) / 2)
                    click_y = int(outer_top + max(3, title_height // 2))
                    engine.pydirectinput.click(click_x, click_y)
                try:
                    engine.win32gui.SetForegroundWindow(game_hwnd)
                except Exception:
                    pass
            return engine.win32gui.GetForegroundWindow() == game_hwnd
        except Exception as exc:
            trace_event(
                "game_focus_restore_failed",
                hwnd=game_hwnd,
                message="{}: {}".format(type(exc).__name__, exc),
            )
            return False

    def _configure_engine(self, engine: ModuleType, config: AppConfig) -> None:
        """把界面配置写入旧引擎仍在使用的模块级运行状态。"""
        engine.stop_event = threading.Event()
        engine.stop_event2 = 1
        engine.zant = 0
        # 每次任务都是独立会话，不能继承上一次攻击意图、版本或复检冷却。
        engine.重置攻击状态()
        engine.重置战斗感知()
        engine.重置药水检测状态()
        engine.renwu_pos = None
        engine.重置人物轨迹记录()
        engine.上次小地图人物发现时间 = 0.0
        engine.人物全图重定位事件.clear()
        engine.怪物检测就绪事件 = threading.Event()
        engine.游戏窗口焦点事件 = threading.Event()
        engine.游戏窗口焦点事件.set()
        engine.游戏窗口失焦累计秒数 = 0.0
        engine.用户运行许可事件 = self._pause_event
        engine.用户运行许可事件.set()
        engine.单体按键 = config.single_attack_key.strip()
        engine.群攻按键 = config.group_attack_key.strip()
        engine.闪现按键 = config.flash_key.strip()
        engine.didd = 1 if config.group_attack else 0
        engine.自动群攻大于数量 = int(config.auto_group_attack_over_count)
        engine.didd闪烁 = 1 if config.flash_enabled else 0
        engine.didd红 = str(config.red_percent)
        engine.didd蓝 = str(config.blue_percent)
        engine.YOLO怪物置信度 = float(config.yolo_confidence)
        engine.测试截图模式 = bool(config.test_mode)
        engine.启用轮检测 = bool(config.wheel_detection_enabled)
        engine.检测预览回调 = None
        engine.上次检测预览时间 = 0.0
        engine.DRAW_DETECTION_BOXES = bool(config.draw_detection_boxes)
        engine.recorded_rest_arrival_callback = None
        engine.recorded_rest_schedule_callback = None
        engine.recorded_route_monitor_callback = None
        engine.休息点测试进行中 = False
        existing_rest_test_event = getattr(engine, "测试休息点事件", None)
        if existing_rest_test_event is None:
            engine.测试休息点事件 = threading.Event()
        else:
            existing_rest_test_event.clear()
        engine.绳子延迟 = config.rope_delay
        engine.绳子休息间隔分钟 = float(config.rope_rest_interval_minutes)
        engine.绳子休息时长分钟 = float(config.rope_rest_duration_minutes)
        engine.recorded_route_ignored_platform_numbers = tuple(
            int(number) for number in config.ignored_platform_numbers
        )
        engine.蘑菇V3路线文件 = str(config.mushroom_v3_route_file)
        engine.自定义录制路线文件 = str(config.mushroom_v3_route_file)

    def _install_preview_callback(self, engine: ModuleType, config: AppConfig) -> None:
        """安装需要访问当前服务实例的页面回调。"""
        engine.检测预览回调 = self._emit_preview_frame if config.test_mode else None
        engine.recorded_rest_arrival_callback = self._capture_recorded_rest_snapshot
        engine.recorded_rest_schedule_callback = self._emit_recorded_rest_schedule
        engine.recorded_route_monitor_callback = self._emit_recorded_route_monitor

    def _emit_preview_frame(self, rgb_frame, info) -> None:
        """最多保留一张等待 Qt 界面消费的 RGB 预览帧。"""
        if not self._last_config.test_mode or self._cancel.is_set():
            return
        with self._preview_frame_lock:
            if self._preview_frame_in_flight:
                return
            self._preview_frame_in_flight = True
        self.frame_ready.emit(rgb_frame, info)

    def mark_preview_consumed(self) -> None:
        """界面显示或丢弃预览后允许检测线程发送下一张。"""
        with self._preview_frame_lock:
            self._preview_frame_in_flight = False

    def _capture_recorded_rest_snapshot(self, details) -> None:
        """休息点确认抵达后抓取当前游戏画面，并安全转交给 Qt 页面。"""
        if self._cancel.is_set() or self._engine is None:
            return
        engine = self._engine
        payload = dict(details or {})
        payload["arrived_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            # 使用引擎启动时读取的真实窗口尺寸，避免固定 1280x630 导致游戏
            # 底部被截断或分辨率变化后抓到窗口外区域。
            capture_width = max(1, int(getattr(engine, "游戏窗口外框宽度", 1280)))
            capture_height = max(1, int(getattr(engine, "游戏窗口外框高度", 831)))
            screenshot = engine.grab_screen(
                {
                    "left": 0,
                    "top": 0,
                    "width": capture_width,
                    "height": capture_height,
                }
            )
            frame = engine.np.asarray(screenshot)
            rgb_frame = engine.cv2.cvtColor(frame, engine.cv2.COLOR_BGRA2RGB)
            payload["snapshot_available"] = True
            self.rest_snapshot_ready.emit(rgb_frame.copy(), payload)
        except Exception as exc:
            payload["snapshot_available"] = False
            payload["snapshot_error"] = "{}: {}".format(type(exc).__name__, exc)
            self.rest_snapshot_ready.emit(None, payload)

    def _emit_recorded_rest_schedule(self, details) -> None:
        """把路线线程中的真实休息计划安全转交给 Qt 页面。"""
        if self._cancel.is_set():
            return
        self.rest_schedule_changed.emit(dict(details or {}))

    def _emit_recorded_route_monitor(self, details) -> None:
        """Forward recorded-route platform and recovery state to the monitor UI."""
        if self._cancel.is_set():
            return
        self.route_monitor_changed.emit(dict(details or {}))

    @staticmethod
    def _release_keys(engine: ModuleType, config: AppConfig) -> None:
        """尽力释放可能被按住的移动、攻击和药水按键。"""
        keys = {
            "left", "right", "up", "down", "pageup", "z", "x", "f", "c", "v", "1", "2",
            config.single_attack_key.strip(), config.group_attack_key.strip(), config.flash_key.strip(),
        }
        keys.update(key.strip() for key, _interval in config.scheduled_keys)
        for key in keys:
            if not key:
                continue
            try:
                engine.pydirectinput.keyUp(key)
            except Exception:
                pass

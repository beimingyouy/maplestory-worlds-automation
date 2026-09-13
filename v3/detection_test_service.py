import importlib
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from .config import AppConfig
from .public.monster_detection import (
    MONSTER_TEMPLATE_CONFIDENCE,
    TEMPLATE_TARGET_FRAME_SECONDS,
    TEST_YOLO_TARGET_FRAME_SECONDS,
    YOLO_MAX_DETECTIONS,
    custom_template_files,
    effective_detector,
    ensure_custom_template_directory,
    detect_monster_health_bars,
    match_monster_templates,
)
from .public.minimap_tracking import (
    MINIMAP_MONITOR,
    absolute_minimap_position,
    capture_yellow_position as capture_minimap_yellow_position,
    find_yellow_center,
)
from .facing_direction import CharacterFacingTracker
from .map_registry import get_map_spec
from .person_locator import (
    CharacterTracker,
    PERSON_MATCH_THRESHOLD as SELF_MATCH_THRESHOLD,
    require_character_templates,
    select_character_template,
)


class DetectionTestService(QObject):
    """只运行视觉识别的安全测试服务，不执行移动、攻击或地图动作。"""

    status_changed = pyqtSignal(str, str)
    started = pyqtSignal()
    stopped = pyqtSignal()
    failed = pyqtSignal(str)
    frame_ready = pyqtSignal(object, object)

    def __init__(self):
        """初始化只读检测线程及停止事件。"""
        super().__init__()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._preview_frame_lock = threading.Lock()
        self._preview_frame_in_flight = False
        self._last_config = AppConfig()

    @property
    def is_running(self) -> bool:
        """返回检测测试线程是否仍在运行。"""
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def last_config(self) -> AppConfig:
        """返回最近一次测试使用的配置。"""
        return self._last_config

    @property
    def is_paused(self) -> bool:
        """返回纯识别测试是否由用户暂停。"""
        return self.is_running and not self._pause_event.is_set()

    def start(self, config: AppConfig) -> bool:
        """校验配置并启动单实例安全检测任务。"""
        config.validate()
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                print("[测试] 检测测试仍在运行。")
                return False
            self._last_config = config
            self._stop_event.clear()
            self._pause_event.set()
            with self._preview_frame_lock:
                self._preview_frame_in_flight = False
            self._worker = threading.Thread(
                target=self._run,
                args=(config,),
                name="v3-detection-test",
                daemon=True,
            )
            self._worker.start()
        self.status_changed.emit("starting", "正在初始化测试")
        return True

    def stop(self) -> None:
        """请求测试线程停止，不发送任何游戏输入。"""
        self._stop_event.set()
        self._pause_event.set()
        if self.is_running:
            self.status_changed.emit("stopping", "正在停止测试")
            print("[测试] 收到停止请求。")

    def pause(self) -> bool:
        """暂停截图和识别循环。"""
        if not self.is_running or not self._pause_event.is_set():
            return False
        self._pause_event.clear()
        print("[测试] 纯识别已暂停。")
        self.status_changed.emit("paused", "识别已暂停")
        return True

    def resume(self) -> bool:
        """继续截图和识别循环。"""
        if not self.is_running or self._pause_event.is_set():
            return False
        self._pause_event.set()
        print("[测试] 纯识别已继续。")
        self.status_changed.emit("running", "纯识别中")
        return True

    def toggle_pause(self) -> bool:
        """在暂停和继续之间切换。"""
        return self.resume() if self.is_paused else self.pause()

    @staticmethod
    def capture_yellow_position():
        """采集一次小地图黄色人物坐标。"""
        engine = importlib.import_module(".legacy_engine", package=__package__)
        return capture_minimap_yellow_position(engine)

    def _run(self, config: AppConfig) -> None:
        """执行截图、人物/怪物识别并持续发送预览帧。"""
        engine = None
        try:
            import cv2
            import numpy as np

            engine = importlib.import_module(".legacy_engine", package=__package__)
            spec = get_map_spec(config.map_name)
            base_dir = Path(engine.get_base_dir())
            engine.prepare_game_window(capture_mode=True)
            monitor = {
                "top": 0,
                "left": 0,
                "width": int(engine.游戏窗口外框宽度),
                "height": int(engine.游戏窗口外框高度),
            }
            detection_monitor = engine.获取怪物检测区域(spec.name)
            detection_offset_x = int(
                detection_monitor["left"] - monitor["left"]
            )
            detection_offset_y = int(
                detection_monitor["top"] - monitor["top"]
            )
            custom_templates_enabled = (
                config.custom_mode
                or config.use_custom_templates
                or spec.name in ("蘑菇V3", "自定义录制路线")
            )
            detector_mode = effective_detector(spec.detector, custom_templates_enabled)

            templates = []
            model = None
            if detector_mode == "template":
                template_directory = base_dir / "img" / spec.detection_resource_name
                if custom_templates_enabled:
                    template_directory = ensure_custom_template_directory()
                    if not custom_template_files(template_directory):
                        raise ValueError(
                            "自定义模板目录中没有 guai* 图片：{}".format(template_directory)
                        )
                templates = engine.load_monster_templates(template_directory)
                template_label = "自定义" if custom_templates_enabled else spec.name
                print("[测试] 模板模式：{}，目录：{}，模板数量：{}".format(
                    template_label, template_directory, len(templates)
                ))
            else:
                model_path = base_dir / "moxing" / "{}.pt".format(
                    spec.detection_resource_name
                )
                model = engine.get_yolo_model(str(model_path))
                print("[测试] YOLO 模式：{}".format(model_path.name))
                print("[测试] YOLO 目标识别频率：60Hz（实际FPS取决于推理耗时）。")

            person_templates = self._load_person_templates(cv2, base_dir)
            print("[测试] 人物模板匹配阈值：{:.2f}".format(SELF_MATCH_THRESHOLD))
            selected_person_template = self._select_person_template(
                cv2,
                np,
                engine,
                detection_monitor,
                person_templates,
                self._stop_event,
            )
            if selected_person_template is None:
                print("[测试警告] 未找到 dengpao.png 或 xueliang.png，SELF 将无法显示。")
            else:
                print(
                    "[测试] 本次运行固定使用人物模板：{}".format(
                        selected_person_template[0]
                    )
                )
            character_tracker = CharacterTracker(
                selected_person_template,
                detection_monitor,
            )
            facing_tracker = (
                CharacterFacingTracker(cv2, np)
                if spec.name in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
                else None
            )
            if self._stop_event.is_set():
                return
            print("[测试] 完整截图范围：{}".format(monitor))
            print("[测试] 人物/怪物识别范围：{}".format(detection_monitor))
            print("[测试] 安全限制：不运行地图路线，不移动、不攻击、不补药。")
            self.status_changed.emit("running", "纯识别中 · {}".format(spec.name))
            self.started.emit()

            previous_time = time.monotonic()
            while not self._stop_event.is_set():
                while not self._pause_event.wait(timeout=0.1):
                    if self._stop_event.is_set():
                        return
                frame_started = time.perf_counter()
                screenshot = engine.grab_screen(monitor)
                frame = np.asarray(screenshot)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                lower_frame = frame[
                    detection_offset_y:
                    detection_offset_y + int(detection_monitor["height"]),
                    detection_offset_x:
                    detection_offset_x + int(detection_monitor["width"]),
                ]
                monster_detection_started = time.perf_counter()

                if detector_mode == "template":
                    monster_boxes = self._detect_templates(
                        cv2,
                        np,
                        lower_frame,
                        templates,
                        y_offset=detection_offset_y,
                    )
                else:
                    monster_boxes = self._detect_yolo(
                        model,
                        lower_frame,
                        y_offset=detection_offset_y,
                        confidence=config.yolo_confidence,
                    )
                health_bar_matches = (
                    detect_monster_health_bars(
                        cv2,
                        np,
                        lower_frame,
                        x_offset=detection_offset_x,
                        y_offset=detection_offset_y,
                    )
                    if spec.name in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
                    and detector_mode == "template"
                    else ()
                )
                health_bar_boxes = [
                    (*match.output_box, match.green_ratio)
                    for match in health_bar_matches
                ]
                monster_match_ms = (
                    time.perf_counter() - monster_detection_started
                ) * 1000

                yellow_position, yellow_preview = self._find_yellow_position(engine, frame)
                self_position, self_preview = self._find_self_position(
                    cv2,
                    lower_frame,
                    character_tracker,
                )
                facing_observation = (
                    facing_tracker.observe(
                        cv2,
                        np,
                        lower_frame,
                        detection_monitor,
                        self_position,
                        detected_at=time.monotonic(),
                    )
                    if facing_tracker is not None
                    else None
                )

                self._draw_preview(
                    cv2,
                    frame,
                    monster_boxes,
                    yellow_position,
                    yellow_preview,
                    self_position,
                    self_preview,
                    health_bar_boxes,
                    facing_observation,
                )

                now = time.monotonic()
                fps = 1.0 / max(now - previous_time, 0.001)
                previous_time = now
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                info = {
                    "map": "自定义模式" if config.custom_mode else spec.name,
                    "detector": "template/custom" if custom_templates_enabled else detector_mode,
                    "monster_count": len(monster_boxes),
                    "health_bar_count": len(health_bar_matches),
                    "health_bar_green_ratios": [
                        round(float(match.green_ratio), 4)
                        for match in health_bar_matches
                    ],
                    "monster_confidences": [
                        round(float(box[4]), 4) for box in monster_boxes
                    ],
                    "max_monster_confidence": (
                        max(float(box[4]) for box in monster_boxes)
                        if monster_boxes
                        else None
                    ),
                    "yellow_position": yellow_position,
                    "self_position": self_position,
                    "self_template": (
                        selected_person_template[0]
                        if selected_person_template is not None
                        else None
                    ),
                    "self_search_mode": character_tracker.last_mode,
                    "self_match_ms": character_tracker.last_match_ms,
                    "facing_direction": (
                        facing_observation.detected_direction
                        if facing_observation is not None else None
                    ),
                    "facing_reliable_direction": (
                        facing_observation.reliable_direction
                        if facing_observation is not None else None
                    ),
                    "facing_left_confidence": (
                        facing_observation.left_confidence
                        if facing_observation is not None else None
                    ),
                    "facing_right_confidence": (
                        facing_observation.right_confidence
                        if facing_observation is not None else None
                    ),
                    "facing_black_flicker": (
                        facing_observation.black_flicker_suspected
                        if facing_observation is not None else None
                    ),
                    "monster_match_ms": monster_match_ms,
                    "fps": fps,
                    "capture_region": "left=0, top=0, width={}, height={}".format(
                        monitor["width"],
                        monitor["height"],
                    ),
                    "recognition_only": True,
                }
                self._emit_preview_frame(rgb_frame, info)
                target_seconds = (
                    TEMPLATE_TARGET_FRAME_SECONDS
                    if detector_mode == "template"
                    else TEST_YOLO_TARGET_FRAME_SECONDS
                )
                remaining = max(
                    0.0,
                    target_seconds - (time.perf_counter() - frame_started),
                )
                self._stop_event.wait(remaining)
        except Exception as exc:
            message = "{}: {}".format(type(exc).__name__, exc)
            print("[测试错误] {}".format(message))
            self.failed.emit(message)
            self.status_changed.emit("error", "测试异常")
        finally:
            close_capture = getattr(engine, "close_thread_capture", None)
            if callable(close_capture):
                close_capture()
            with self._preview_frame_lock:
                self._preview_frame_in_flight = False
            self._pause_event.set()
            with self._lock:
                self._worker = None
            self.status_changed.emit("idle", "测试已停止")
            self.stopped.emit()
            print("[测试] 检测测试线程已退出。")

    def _emit_preview_frame(self, rgb_frame, info) -> None:
        """最多向 Qt 队列放入一张尚未消费的预览帧。"""
        if self._stop_event.is_set():
            return
        with self._preview_frame_lock:
            if self._preview_frame_in_flight:
                return
            self._preview_frame_in_flight = True
        self.frame_ready.emit(rgb_frame, info)

    def mark_preview_consumed(self) -> None:
        """界面显示或丢弃预览后允许发送下一张。"""
        with self._preview_frame_lock:
            self._preview_frame_in_flight = False

    @staticmethod
    def _load_person_templates(cv2, base_dir: Path):
        """从资源目录加载可用的人物定位模板。"""
        return require_character_templates(cv2, base_dir)

    @classmethod
    def _select_person_template(
        cls,
        cv2,
        np,
        engine,
        monitor,
        person_templates,
        stop_event,
    ):
        """启动时选择一次人物模板，并在本次测试中固定使用。"""
        return select_character_template(
            cv2,
            np,
            engine.grab_screen,
            person_templates,
            monitor=monitor,
            stop_requested=stop_event.is_set,
            wait=stop_event.wait,
            log_prefix="[测试]",
        )

    @staticmethod
    def _detect_templates(cv2, np, frame, templates, y_offset=0):
        """在局部帧中执行怪物模板匹配并返回预览坐标框。"""
        matches = match_monster_templates(
            cv2,
            np,
            frame,
            templates,
            threshold=MONSTER_TEMPLATE_CONFIDENCE,
            y_offset=y_offset,
        )
        return [(*match.output_box, match.confidence) for match in matches]

    @staticmethod
    def _detect_yolo(model, frame, y_offset=0, confidence=0.65):
        """执行 YOLO 怪物检测并转换为预览坐标框。"""
        boxes = []
        # 与正式运行共用阈值，避免测试结果和实际攻击结果不一致。
        for result in model(
            frame,
            verbose=False,
            conf=float(confidence),
            classes=[0],
            max_det=YOLO_MAX_DETECTIONS,
        ):
            for box in result.boxes:
                if int(box.cls[0]) != 0:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                confidence = float(box.conf[0].cpu().numpy())
                boxes.append((int(x1), int(y1 + y_offset), int(x2), int(y2 + y_offset), confidence))
        return boxes

    @staticmethod
    def _find_yellow_position(engine, frame) -> Tuple[Optional[tuple], Optional[tuple]]:
        """从预览帧的小地图区域定位黄色人物点。"""
        top = int(MINIMAP_MONITOR["top"])
        left = int(MINIMAP_MONITOR["left"])
        bottom = top + int(MINIMAP_MONITOR["height"])
        right = left + int(MINIMAP_MONITOR["width"])
        minimap = frame[top:bottom, left:right]
        center = find_yellow_center(engine.cv2, engine.np, minimap)
        screen_position = absolute_minimap_position(center)
        if screen_position is None:
            return None, None
        preview_position = screen_position
        return screen_position, preview_position

    @staticmethod
    def _find_self_position(cv2, lower_frame, character_tracker):
        """使用有状态灰度跟踪器定位人物，并换算到完整预览帧坐标。"""
        confidence, screen_position, _local_position = character_tracker.locate(
            cv2,
            lower_frame,
        )
        if confidence < SELF_MATCH_THRESHOLD or screen_position is None:
            return None, None
        preview_position = screen_position
        return screen_position, preview_position

    @staticmethod
    def _draw_preview(
        cv2,
        frame,
        monster_boxes,
        yellow_position,
        yellow_preview,
        self_position,
        self_preview,
        health_bar_boxes=(),
        facing_observation=None,
    ):
        """在预览帧上绘制怪物、绿色血条、人物位置和可靠朝向。"""
        for index, (x1, y1, x2, y2, confidence) in enumerate(monster_boxes, start=1):
            cv2.rectangle(frame, (x1, y1), (x2, y2), (70, 235, 90), 2)
            cv2.putText(
                frame,
                "MONSTER {} {:.2f}".format(index, confidence),
                (x1, max(18, y1 - 6)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.48,
                (70, 235, 90),
                1,
            )

        for index, (x1, y1, x2, y2, green_ratio) in enumerate(
            health_bar_boxes,
            start=1,
        ):
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 0), 1)
            cv2.putText(
                frame,
                "HP {} {:.0f}%".format(index, green_ratio * 100),
                (x1, max(18, y1 - 5)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (255, 255, 0),
                1,
            )

        if yellow_preview is not None:
            cv2.circle(frame, yellow_preview, 7, (0, 255, 255), 2)
            cv2.putText(
                frame,
                "YELLOW {}".format(yellow_position),
                (yellow_preview[0] + 10, max(18, yellow_preview[1] - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (0, 255, 255),
                2,
            )

        if self_preview is not None:
            cv2.circle(frame, self_preview, 8, (255, 220, 40), 2)
            cv2.putText(
                frame,
                "SELF {}".format(self_position),
                (self_preview[0] + 10, max(18, self_preview[1] - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.52,
                (255, 220, 40),
                2,
            )
            if facing_observation is not None:
                facing_text = (
                    facing_observation.detected_direction.upper()
                    if facing_observation.detected_direction
                    else "HOLD"
                )
                cv2.putText(
                    frame,
                    "FACING {} L{:.2f} R{:.2f}".format(
                        facing_text,
                        facing_observation.left_confidence,
                        facing_observation.right_confidence,
                    ),
                    (self_preview[0] + 10, min(frame.shape[0] - 12, self_preview[1] + 24)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (255, 220, 40),
                    1,
                )

        cv2.putText(
            frame,
            "TEST MODE - NO KEY INPUT",
            (18, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (80, 180, 255),
            2,
        )

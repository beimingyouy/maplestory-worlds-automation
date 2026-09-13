"""小地图黄色人物点识别、坐标发布和人物轨迹记录公共模块。"""

import time
from dataclasses import dataclass
from typing import Optional, Tuple


MINIMAP_MONITOR = {"top": 110, "left": 20, "width": 270, "height": 190}
YELLOW_MARKER_LOWER_HSV = (29, 220, 220)
YELLOW_MARKER_UPPER_HSV = (35, 255, 255)
MINIMAP_TRACK_INTERVAL_SECONDS = 0.05
YELLOW_MARKER_MAX_CONTOUR_AREA = 20.0


def find_yellow_center(cv2, np, frame) -> Optional[Tuple[int, int]]:
    """在一张BGR小地图画面中返回最大有效黄色人物点的局部中心。"""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lower = np.asarray(YELLOW_MARKER_LOWER_HSV, dtype=np.uint8)
    upper = np.asarray(YELLOW_MARKER_UPPER_HSV, dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    contours, _hierarchy = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    largest_area = 0.0
    largest_center = None
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if not (0.0 < area <= YELLOW_MARKER_MAX_CONTOUR_AREA):
            continue
        if area <= largest_area:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        largest_area = area
        largest_center = (
            int(moments["m10"] / moments["m00"]),
            int(moments["m01"] / moments["m00"]),
        )
    return largest_center


def absolute_minimap_position(
    local_center: Optional[Tuple[int, int]],
    monitor=None,
) -> Optional[Tuple[int, int]]:
    """把小地图局部黄色点坐标转换成屏幕绝对坐标。"""
    if local_center is None:
        return None
    active_monitor = MINIMAP_MONITOR if monitor is None else monitor
    return (
        int(active_monitor["left"]) + int(local_center[0]),
        int(active_monitor["top"]) + int(local_center[1]),
    )


def capture_yellow_position(engine, monitor=None) -> Optional[Tuple[int, int]]:
    """单次截图识别黄色人物点，不发送任何键盘或鼠标操作。"""
    active_monitor = MINIMAP_MONITOR if monitor is None else monitor
    screenshot = engine.grab_screen(active_monitor)
    frame = engine.np.asarray(screenshot)
    frame = engine.cv2.cvtColor(frame, engine.cv2.COLOR_BGRA2BGR)
    center = find_yellow_center(engine.cv2, engine.np, frame)
    return absolute_minimap_position(center, active_monitor)


def request_minimap_relocation(runtime, reason="route_recovery"):
    """清空缓存的小地图人物点，让持续定位线程发布下一帧的新坐标。"""
    with runtime.lock:
        previous_position = runtime.renwu_pos
        runtime.renwu_pos = None
    runtime.上次小地图人物发现时间 = 0.0
    runtime.trace_event(
        "minimap_relocation_requested",
        reason=reason,
        previous_position=previous_position,
    )
    return previous_position


def run_minimap_tracking(runtime, frame_interval=MINIMAP_TRACK_INTERVAL_SECONDS):
    """持续识别黄色人物点，并向兼容运行时发布最新坐标和定位性能日志。"""
    previous_frame_started_at = None
    frame_sequence = 0
    while runtime.stop_event is not None and not runtime.stop_event.is_set():
        frame_started_at = time.monotonic()
        processing_started_at = time.perf_counter()
        frame_interval_ms = (
            None
            if previous_frame_started_at is None
            else (frame_started_at - previous_frame_started_at) * 1000
        )
        previous_frame_started_at = frame_started_at
        frame_sequence += 1

        screenshot = runtime.grab_screen(MINIMAP_MONITOR)
        frame = runtime.np.asarray(screenshot)
        frame = runtime.cv2.cvtColor(frame, runtime.cv2.COLOR_BGRA2BGR)
        center = find_yellow_center(runtime.cv2, runtime.np, frame)
        detected_position = absolute_minimap_position(center)
        if detected_position is not None:
            with runtime.lock:
                runtime.renwu_pos = detected_position
                published_position = runtime.renwu_pos
            runtime.上次小地图人物发现时间 = time.monotonic()
        else:
            with runtime.lock:
                published_position = runtime.renwu_pos

        now = time.monotonic()
        stale_ms = (
            round((now - runtime.上次小地图人物发现时间) * 1000, 3)
            if runtime.上次小地图人物发现时间 > 0
            else None
        )
        runtime.trace_event(
            "minimap_tracking_frame",
            frame_sequence=frame_sequence,
            found=detected_position is not None,
            detected_position=detected_position,
            published_position=published_position,
            stale_ms=stale_ms,
            frame_interval_ms=(
                round(frame_interval_ms, 3)
                if frame_interval_ms is not None
                else None
            ),
            processing_ms=round(
                (time.perf_counter() - processing_started_at) * 1000,
                3,
            ),
        )
        if runtime.stop_event.wait(max(0.0, float(frame_interval))):
            break


@dataclass
class CharacterTrajectoryRecorder:
    """保存轨迹日志的采样时间、上一坐标和控制台限频状态。"""

    last_log_at: float = 0.0
    last_console_at: float = 0.0
    last_position: Optional[Tuple[int, int]] = None
    last_position_at: float = 0.0

    def reset(self) -> None:
        """开始新任务时清空上一轮轨迹和限频时间。"""
        self.last_log_at = 0.0
        self.last_console_at = 0.0
        self.last_position = None
        self.last_position_at = 0.0

    def record(self, runtime, position, interval=0.25, **context) -> None:
        """记录坐标、位移速度、静止状态和同一时刻的运行诊断快照。"""
        if position is None:
            return
        now = time.monotonic()
        if now - self.last_console_at >= 1.0:
            self.last_console_at = now
            print("[路线] 当前人物位置：{}".format(position))
        if now - self.last_log_at < max(0.0, float(interval)):
            return

        previous_position = self.last_position
        elapsed_seconds = (
            now - self.last_position_at
            if self.last_position_at > 0
            else None
        )
        delta_x = (
            int(position[0]) - int(previous_position[0])
            if previous_position is not None
            else None
        )
        delta_y = (
            int(position[1]) - int(previous_position[1])
            if previous_position is not None
            else None
        )
        self.last_log_at = now
        self.last_position = (int(position[0]), int(position[1]))
        self.last_position_at = now

        fields = {
            "x": int(position[0]),
            "y": int(position[1]),
            "delta_x": delta_x,
            "delta_y": delta_y,
            "sample_interval_ms": (
                round(elapsed_seconds * 1000, 3)
                if elapsed_seconds
                else None
            ),
            "speed_x_per_second": (
                round(delta_x / elapsed_seconds, 3)
                if delta_x is not None and elapsed_seconds
                else None
            ),
            "speed_y_per_second": (
                round(delta_y / elapsed_seconds, 3)
                if delta_y is not None and elapsed_seconds
                else None
            ),
            "stationary": (
                abs(delta_x) <= 1 and abs(delta_y) <= 1
                if delta_x is not None and delta_y is not None
                else None
            ),
        }
        fields.update(runtime.获取运行诊断快照())
        fields.update(context)
        runtime.trace_event("character_trajectory", **fields)


__all__ = (
    "CharacterTrajectoryRecorder",
    "MINIMAP_MONITOR",
    "MINIMAP_TRACK_INTERVAL_SECONDS",
    "YELLOW_MARKER_LOWER_HSV",
    "YELLOW_MARKER_MAX_CONTOUR_AREA",
    "YELLOW_MARKER_UPPER_HSV",
    "absolute_minimap_position",
    "capture_yellow_position",
    "find_yellow_center",
    "request_minimap_relocation",
    "run_minimap_tracking",
)

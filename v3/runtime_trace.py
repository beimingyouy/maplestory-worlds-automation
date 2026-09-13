"""以后台线程写入结构化运行轨迹，供识别和动作流畅度分析。"""

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# 这些事件都有更高层的对应结果，逐帧写入只会放大日志而不会增加分析信息。
_VERBOSE_EVENTS_TO_SKIP = frozenset(
    {
        "minimap_tracking_frame",
        "mushroom_v2_action_decision",
        "mushroom_v2_action_applied",
        "mushroom_v2_phase_changed",
        "mushroom_v2_target_loss_confirmation",
        "mushroom_v2_target_side_locked",
        "mushroom_v2_target_side_lock_released",
        "mushroom_v2_facing_verified",
        "mushroom_v2_combat_started",
        "mushroom_v2_combat_finished",
        "mushroom_v2_chase_started",
        "mushroom_v2_chase_stopped",
        "attack_intent_published",
        "attack_intent_consumed",
        "attack_intent_cleared",
        "attack_intent_stale_frame_dropped",
        "mushroom_v2_climb_probe_result",
        "mushroom_v2_rope_slope_align_started",
        "mushroom_v2_rope_slope_align_completed",
        "mushroom_v2_rope_exit_started",
        "mushroom_v2_rope_exit_failed",
        "mushroom_v2_climb_verified",
        "mushroom_v2_climb_rearmed",
    }
)


def _json_default(value):
    """把 NumPy 标量、数组和路径转换为 JSON 可写的基础类型。"""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError("Object of type {} is not JSON serializable".format(
        value.__class__.__name__
    ))


class RuntimeTrace:
    """管理单次自动化会话的非阻塞 JSON Lines 轨迹文件。"""

    def __init__(self):
        """初始化轨迹队列和空闲状态。"""
        self._lock = threading.Lock()
        self._queue = None
        self._thread = None
        self._path: Optional[Path] = None
        self._started_monotonic = 0.0
        self._dropped = 0
        self._last_recorded_at = {}
        self._last_signatures = {}

    @property
    def path(self) -> Optional[Path]:
        """返回当前或最近一次轨迹文件路径。"""
        with self._lock:
            return self._path

    def start(self, base_dir: Path, context=None, suffix=None) -> Path:
        """创建新轨迹文件并启动后台写入线程。"""
        self.stop()
        log_dir = Path(base_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        normalized_suffix = str(suffix or "").strip().strip("_.-")
        filename = "运行轨迹_{}{}.log".format(
            timestamp,
            "_{}".format(normalized_suffix) if normalized_suffix else "",
        )
        path = log_dir / filename
        event_queue = queue.Queue(maxsize=50000)
        with self._lock:
            self._queue = event_queue
            self._path = path
            self._started_monotonic = time.perf_counter()
            self._dropped = 0
            self._last_recorded_at = {}
            self._last_signatures = {}
            self._thread = threading.Thread(
                target=self._writer,
                args=(path, event_queue),
                name="v3-runtime-trace",
                daemon=True,
            )
            self._thread.start()
        self.record("session_start", **(context or {}))
        return path

    def record(self, event: str, **fields) -> None:
        """把一条轨迹事件放入内存队列，不阻塞识别和按键线程。"""
        event = str(event)
        fields = self._compact_fields(event, fields)
        now = time.perf_counter()
        with self._lock:
            if not self._should_record(event, fields, now):
                return
            event_queue = self._queue
            started = self._started_monotonic
        if event_queue is None:
            return
        payload = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": round((now - started) * 1000, 3),
            "thread": threading.current_thread().name,
            "event": event,
        }
        # 业务事件以前偶尔把自己的阶段耗时也命名为 elapsed_ms，覆盖了统一
        # 会话时间轴，导致日志排序和卡顿时长分析失真。保留业务值，但统一改名。
        if "elapsed_ms" in fields and "duration_ms" not in fields:
            fields["duration_ms"] = fields.pop("elapsed_ms")
        for reserved_key in ("time", "thread", "event"):
            fields.pop(reserved_key, None)
        payload.update(fields)
        try:
            event_queue.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    @staticmethod
    def _compact_fields(event: str, fields):
        """为高频诊断事件只保留排查所需字段，降低日志体积和分析成本。"""
        if event == "detection_frame":
            left_right = fields.get("lr")
            if not isinstance(left_right, (list, tuple)):
                left_right = [
                    fields.get("left", 0),
                    fields.get("right", 0),
                ]
            return {
                key: value
                for key, value in {
                    "d": fields.get("d", fields.get("detector")),
                    "p": fields.get("p"),
                    "m": fields.get("m"),
                    "mr": fields.get("mr"),
                    "n": fields.get("n", fields.get("targets", 0)),
                    "a": fields.get("a", fields.get("nearby", 0)),
                    "c": fields.get("c", fields.get("chase_targets", 0)),
                    "lr": list(left_right),
                    "cd": fields.get("cd", fields.get("chase_direction")),
                    "dd": fields.get("dd", fields.get("decision_direction")),
                    "da": fields.get("da", fields.get("decision_attackable")),
                    "cs": fields.get("cs", fields.get("combat_state")),
                    "hb": fields.get("hb", fields.get("health_bar_targets", 0)),
                    "lf": fields.get("lf", fields.get("lost_frames", 0)),
                    "pc": fields.get("pc", fields.get("person_confidence")),
                    "pmode": fields.get("pmode", fields.get("person_mode")),
                    "ph": fields.get("ph", fields.get("person_position_held")),
                    "pa": fields.get("pa", fields.get("person_position_age_ms")),
                    "fps": fields.get("fps", fields.get("instant_fps")),
                }.items()
                if value is not None
            }
        if event == "template_detection_performance":
            return {
                key: value
                for key, value in {
                    "f": fields.get("f", fields.get("frame_number", fields.get("frame_sequence"))),
                    "fi": fields.get("fi", fields.get("frame_interval_ms")),
                    "fm": fields.get("fm", fields.get("frame_total_ms", fields.get("frame_ms"))),
                    "cap": fields.get("cap", fields.get("capture_ms")),
                    "pm": fields.get("pm", fields.get("person_ms")),
                    "mm": fields.get("mm", fields.get("monster_ms")),
                    "hm": fields.get("hm", fields.get("health_bar_ms")),
                    "pot": fields.get("pot", fields.get("potion_ms")),
                    "pv": fields.get("pv", fields.get("preview_ms")),
                    "tm": fields.get("tm", fields.get("template_count")),
                    "n": fields.get("n", fields.get("targets", 0)),
                    "a": fields.get("a", fields.get("nearby", 0)),
                }.items()
                if value is not None
            }
        keep_by_event = {
            "character_facing_frame": (
                "detected_direction", "reliable_direction", "left_confidence",
                "right_confidence", "confidence_margin", "reliable",
                "black_flicker_suspected", "match_ms", "template_source",
                "decision_source", "aux_direction", "aux_left_confidence",
                "aux_right_confidence", "aux_confidence_margin",
                "aux_reliable", "aux_roi", "action",
            ),
            "mushroom_v2_facing_corrected": (
                "detected_direction", "desired_direction", "left_confidence",
                "right_confidence", "confidence_margin", "age_ms", "match_ms",
                "same_direction_attack_streak", "action",
            ),
            "mushroom_v2_facing_reasserted": (
                "detected_direction", "desired_direction", "left_confidence",
                "right_confidence", "confidence_margin", "age_ms", "match_ms",
                "same_direction_attack_streak", "action",
            ),
            "mushroom_v2_facing_fallback": (
                "detected_direction", "reliable_direction", "desired_direction",
                "age_ms", "black_flicker_suspected", "action",
            ),
            "character_trajectory": (
                "x", "y", "delta_x", "delta_y", "stationary", "route",
                "phase", "rope_zone", "climb_stage", "on_upper_platform",
            ),
        }
        keys = keep_by_event.get(event)
        if keys is None:
            return fields
        return {key: fields.get(key) for key in keys if key in fields}

    def _should_record(self, event: str, fields, now: float) -> bool:
        """过滤重复逐帧事件，仅保留状态变化、周期摘要和关键动作。"""
        if event in _VERBOSE_EVENTS_TO_SKIP:
            return False

        if event == "detection_frame":
            relative_positions = fields.get("mr") or ()
            quantized_relative = tuple(
                (
                    int(round(float(item[0]) / 25.0)),
                    int(round(float(item[1]) / 20.0)),
                )
                for item in relative_positions[:6]
                if isinstance(item, (list, tuple)) and len(item) >= 2
            )
            signature = (
                fields.get("n", 0),
                fields.get("a", 0),
                fields.get("c", 0),
                tuple(fields.get("lr") or (0, 0)),
                fields.get("cd"),
                fields.get("dd"),
                fields.get("cs"),
                bool(fields.get("hb")),
                quantized_relative,
            )
            return self._allow_signature_event(
                event,
                signature,
                now,
                periodic_seconds=(0.75 if fields.get("n", 0) else 2.0),
                transition_seconds=0.35,
            )

        if event == "template_detection_performance":
            frame_ms = float(fields.get("fm") or 0.0)
            interval_ms = float(fields.get("fi") or 0.0)
            slow_level = 2 if frame_ms >= 160.0 or interval_ms >= 240.0 else (
                1 if frame_ms >= 80.0 or interval_ms >= 120.0 else 0
            )
            last_recorded = self._last_recorded_at.get(event, 0.0)
            previous_level = self._last_signatures.get(event)
            elapsed = now - last_recorded
            if slow_level > 0:
                allowed = previous_level != slow_level or elapsed >= 0.5
            else:
                allowed = previous_level is None or elapsed >= 3.0
            if allowed:
                self._last_signatures[event] = slow_level
                self._last_recorded_at[event] = now
            return allowed

        if event == "character_trajectory":
            signature = (
                fields.get("rope_zone"),
                fields.get("climb_stage"),
                fields.get("on_upper_platform"),
            )
            return self._allow_signature_event(
                event,
                signature,
                now,
                periodic_seconds=2.0,
                transition_seconds=1.0,
            )

        if event == "character_facing_frame":
            signature = (
                fields.get("action"),
                fields.get("detected_direction"),
                fields.get("reliable_direction"),
                fields.get("black_flicker_suspected"),
            )
            return self._allow_signature_event(
                event,
                signature,
                now,
                periodic_seconds=5.0,
                transition_seconds=0.2,
            )

        return True

    def _allow_signature_event(
        self,
        event: str,
        signature,
        now: float,
        periodic_seconds: float,
        transition_seconds: float,
    ) -> bool:
        """状态变化限速记录，并按固定周期保留一条可供性能分析的摘要。"""
        previous_signature = self._last_signatures.get(event)
        last_recorded = self._last_recorded_at.get(event, 0.0)
        elapsed = now - last_recorded
        changed = signature != previous_signature
        allowed = (
            previous_signature is None
            or elapsed >= periodic_seconds
            or (changed and elapsed >= transition_seconds)
        )
        if allowed:
            self._last_signatures[event] = signature
            self._last_recorded_at[event] = now
        return allowed

    def stop(self, timeout=2.0) -> Optional[Path]:
        """结束当前轨迹写入，等待后台线程刷新剩余事件。"""
        with self._lock:
            event_queue = self._queue
            writer = self._thread
            path = self._path
            dropped = self._dropped
            self._queue = None
            self._thread = None
        if event_queue is None:
            return path
        final_event = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.perf_counter() - self._started_monotonic) * 1000, 3),
            "thread": threading.current_thread().name,
            "event": "session_stop",
            "dropped_events": dropped,
        }
        try:
            event_queue.put(final_event, timeout=0.2)
            event_queue.put(None, timeout=0.2)
        except queue.Full:
            pass
        if writer is not None:
            writer.join(timeout=timeout)
        return path

    @staticmethod
    def _writer(path: Path, event_queue) -> None:
        """持续写入 JSON 轨迹，单条数据异常时也不让记录线程退出。"""
        with Path(path).open("w", encoding="utf-8", buffering=1) as handle:
            while True:
                payload = event_queue.get()
                if payload is None:
                    break
                try:
                    line = json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=_json_default,
                    )
                except (TypeError, ValueError) as exc:
                    line = json.dumps(
                        {
                            "time": datetime.now().isoformat(timespec="milliseconds"),
                            "event": "trace_serialization_error",
                            "original_event": payload.get("event"),
                            "message": str(exc),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                handle.write(line)
                handle.write("\n")


_TRACE = RuntimeTrace()


def start_runtime_trace(base_dir: Path, context=None, suffix=None) -> Path:
    """启动全局运行轨迹并返回新文件路径。"""
    return _TRACE.start(base_dir, context, suffix=suffix)


def trace_event(event: str, **fields) -> None:
    """非阻塞记录一条全局运行轨迹事件。"""
    _TRACE.record(event, **fields)


def stop_runtime_trace(timeout=2.0) -> Optional[Path]:
    """停止全局运行轨迹并返回最近文件路径。"""
    return _TRACE.stop(timeout)


def current_runtime_trace_path() -> Optional[Path]:
    """返回当前或最近一次运行轨迹文件路径。"""
    return _TRACE.path

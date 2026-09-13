"""公共的分段路线录制、路线图分析和回放文件生成模块。"""

import json
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from .minimap_tracking import MINIMAP_MONITOR, find_yellow_center


ROUTE_RECORD_FPS = 30
ROUTE_RECORD_INTERVAL_SECONDS = 1.0 / ROUTE_RECORD_FPS
ROUTE_POINT_MIN_DISTANCE = 1
PLATFORM_DIRECTION_NORMALIZE_MIN_SPAN = 4

SEGMENT_PLATFORM = "platform"
SEGMENT_ROPE = "rope"
SEGMENT_DOWN_JUMP = "down_jump"
# ``right_return`` 是旧JSON的单点向右回程标记，只作兼容读取。
SEGMENT_RIGHT_RETURN = "right_return"
SEGMENT_WALK_OFF_LEFT = "walk_off_left"
SEGMENT_WALK_OFF_RIGHT = "walk_off_right"
SEGMENT_TRIAL_WALK_V2 = "trial_walk_v2"
SEGMENT_PLATFORM_JUMP_LEFT = "platform_jump_left"
SEGMENT_PLATFORM_JUMP_RIGHT = "platform_jump_right"
SEGMENT_PLATFORM_JUMP_NEUTRAL = "platform_jump_neutral"
SEGMENT_PLATFORM_TO_ROPE_LEFT = "platform_to_rope_left"
SEGMENT_PLATFORM_TO_ROPE_RIGHT = "platform_to_rope_right"
SEGMENT_ROPE_TO_PLATFORM_LEFT = "rope_to_platform_left"
SEGMENT_ROPE_TO_PLATFORM_RIGHT = "rope_to_platform_right"
V2_TRANSITION_SEGMENT_TYPES = frozenset(
    {
        SEGMENT_PLATFORM_JUMP_LEFT,
        SEGMENT_PLATFORM_JUMP_RIGHT,
        SEGMENT_PLATFORM_JUMP_NEUTRAL,
        SEGMENT_PLATFORM_TO_ROPE_LEFT,
        SEGMENT_PLATFORM_TO_ROPE_RIGHT,
        SEGMENT_ROPE_TO_PLATFORM_LEFT,
        SEGMENT_ROPE_TO_PLATFORM_RIGHT,
    }
)
WALK_OFF_SEGMENT_TYPES = frozenset(
    {SEGMENT_WALK_OFF_LEFT, SEGMENT_WALK_OFF_RIGHT}
)
RETURN_TRANSITION_SEGMENT_TYPES = frozenset(
    {
        SEGMENT_DOWN_JUMP,
        SEGMENT_RIGHT_RETURN,
        SEGMENT_WALK_OFF_LEFT,
        SEGMENT_WALK_OFF_RIGHT,
        *V2_TRANSITION_SEGMENT_TYPES,
    }
)
VALID_SEGMENT_TYPES = frozenset(
    {
        SEGMENT_PLATFORM,
        SEGMENT_ROPE,
        SEGMENT_DOWN_JUMP,
        SEGMENT_RIGHT_RETURN,
        SEGMENT_WALK_OFF_LEFT,
        SEGMENT_WALK_OFF_RIGHT,
        *V2_TRANSITION_SEGMENT_TYPES,
    }
)

# 绳子上下端与平台Y允许存在数像素误差；X也允许人物攀爬时轻微左右抖动。
ROPE_PLATFORM_Y_TOLERANCE = 8
ROPE_UPPER_PLATFORM_Y_TOLERANCE = 12
ROPE_LOWER_PLATFORM_Y_TOLERANCE = 36
ROPE_PLATFORM_X_TOLERANCE = 16
ROPE_X_FUZZY_TOLERANCE = 1
ROPE_TOP_Y_FUZZY_TOLERANCE = 2
ROPE_BOTTOM_Y_FUZZY_TOLERANCE = 3
ROPE_ENTRY_OFFSET_X = 2
WALK_OFF_DEPARTURE_MIN_Y = 4
WALK_OFF_LANDING_STABLE_FRAMES = 4
WALK_OFF_PLATFORM_Y_TOLERANCE = 8
WALK_OFF_PLATFORM_X_TOLERANCE = 16
V2_STABLE_NODE_MIN_FRAMES = 3
V2_PLATFORM_LABEL_Y_TOLERANCE = 5
V2_PLATFORM_LABEL_X_TOLERANCE = 10
V2_ROPE_LABEL_X_TOLERANCE = 3
V2_ROPE_LABEL_Y_TOLERANCE = 4

# 平台段只应该记录人物在同一层水平移动的轨迹。黄色人物点偶尔会被
# 小地图中的其他黄色图标抢占，产生一次跨越上百像素的假坐标；人物被
# 击落时则会连续、平滑地向下移动。下面的参数用于区分这两种情况。
PLATFORM_RECORD_BASELINE_SAMPLE_COUNT = 8
PLATFORM_RECORD_Y_TOLERANCE = 6
PLATFORM_RECORD_OFF_PLATFORM_FRAMES = 5
PLATFORM_RECORD_MAX_FRAME_DISTANCE = 32
PLATFORM_RECORD_MIN_POINTS = 2
# 斜坡在小地图上表现为人物横向移动时Y逐点缓慢变化。跟随局部平台高度，
# 但不跟随一次跳跃或纯纵向下坠，避免把跳起/掉层误当成新的平台基准。
PLATFORM_RECORD_SLOPE_MAX_STEP_Y = 3
PLATFORM_RECORD_SLOPE_HORIZONTAL_GRACE_FRAMES = 2
PLATFORM_RECORD_FALL_MIN_DROP_Y = 8
PLATFORM_RECORD_SLOPE_MIN_HORIZONTAL_RATIO = 0.5


def default_mushroom_v3_route_path(project_root: Path) -> Path:
    """返回蘑菇V3默认使用的可编辑路线JSON文件。"""
    return mushroom_v3_recordings_directory(project_root) / "蘑菇V3路线.json"


def mushroom_v3_recordings_directory(project_root: Path) -> Path:
    """返回蘑菇V3多路线JSON和预览图的统一保存目录。"""
    return Path(project_root) / "v3" / "map" / "recordings"


def resolve_mushroom_v3_route_path(project_root: Path, configured_path: str) -> Path:
    """解析路线JSON；打包迁移后旧绝对路径失效时按文件名回退到包内目录。"""
    text = str(configured_path or "").strip()
    if not text:
        return default_mushroom_v3_route_path(project_root)
    candidate = Path(text)
    if candidate.is_absolute():
        if candidate.is_file():
            return candidate
        packaged_candidate = (
            mushroom_v3_recordings_directory(project_root) / candidate.name
        )
        if packaged_candidate.is_file():
            return packaged_candidate
        return candidate
    return mushroom_v3_recordings_directory(project_root) / candidate.name


def mushroom_v3_preview_path(route_path: Path) -> Path:
    """返回与路线JSON同目录的最后路线预览图路径。"""
    return Path(route_path).with_suffix(".png")


def read_route_rest_point(route_path: Path) -> Optional[Dict[str, object]]:
    """读取路线JSON中的休息落点；旧路线未录制时返回 ``None``。"""
    path = Path(route_path)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_point = data.get("rest_point") if isinstance(data, dict) else None
    if not isinstance(raw_point, dict):
        return None
    try:
        return {
            "x": int(raw_point["x"]),
            "y": int(raw_point["y"]),
            "approach_y": (
                int(raw_point["approach_y"])
                if raw_point.get("approach_y") is not None
                else None
            ),
            "interval_minutes": (
                float(raw_point["interval_minutes"])
                if raw_point.get("interval_minutes") is not None
                else None
            ),
            "duration_minutes": (
                float(raw_point["duration_minutes"])
                if raw_point.get("duration_minutes") is not None
                else None
            ),
            "jump_key": str(raw_point.get("jump_key", "c")),
            "recorded_at": raw_point.get("recorded_at"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def save_route_rest_point(
    route_path: Path,
    position: Tuple[int, int],
    interval_minutes: Optional[float] = None,
    duration_minutes: Optional[float] = None,
) -> Dict[str, object]:
    """把人物当前小地图坐标作为休息落点写入一份已有路线JSON。"""
    path = Path(route_path)
    if not path.is_file():
        raise FileNotFoundError("请先加载或保存一份已有路线JSON")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("路线JSON根节点必须是对象")
    variants = data.get("route_variants")
    points = data.get("points")
    has_route = (
        isinstance(points, list)
        and len(points) >= 2
    ) or (
        isinstance(variants, list)
        and any(
            isinstance(variant, dict)
            and isinstance(variant.get("points"), list)
            and len(variant["points"]) >= 2
            for variant in variants
        )
    )
    if not has_route:
        raise ValueError("路线JSON中没有可运行的路线坐标")
    route_points = []
    if isinstance(points, list):
        route_points.extend(point for point in points if isinstance(point, dict))
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict) and isinstance(variant.get("points"), list):
                route_points.extend(
                    point
                    for point in variant["points"]
                    if isinstance(point, dict)
                )
    rest_x, rest_y = int(position[0]), int(position[1])
    platform_candidates = []
    for point in route_points:
        if str(point.get("segment_type", "")) != SEGMENT_PLATFORM:
            continue
        try:
            point_x, point_y = int(point["x"]), int(point["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if point_y <= rest_y + 2:
            continue
        horizontal_distance = abs(point_x - rest_x)
        platform_candidates.append(
            (
                0 if horizontal_distance <= 5 else 1,
                point_y - rest_y,
                horizontal_distance,
                point_y,
            )
        )
    approach_y = min(platform_candidates)[3] if platform_candidates else None
    rest_point = {
        "x": rest_x,
        "y": rest_y,
        "approach_y": approach_y,
        "interval_minutes": (
            max(0.0, float(interval_minutes))
            if interval_minutes is not None
            else None
        ),
        "duration_minutes": (
            max(0.0, float(duration_minutes))
            if duration_minutes is not None
            else None
        ),
        "jump_key": "c",
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    data["rest_point"] = rest_point
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dict(rest_point)


def _read_pressed_keys(keyboard_module) -> Dict[str, bool]:
    """一次读取V2试走需要的全部按键，避免同一帧重复查询产生偏差。"""
    return {
        "left": bool(keyboard_module.is_pressed("left")),
        "right": bool(keyboard_module.is_pressed("right")),
        "up": bool(keyboard_module.is_pressed("up")),
        "down": bool(keyboard_module.is_pressed("down")),
        "jump": bool(keyboard_module.is_pressed("c")),
    }


def _command_from_key_state(
    key_state: Dict[str, bool],
    jump_was_pressed: bool,
    segment_type: str,
) -> Tuple[str, bool]:
    """把一次原子读取的按键状态转换成现有播放器使用的三段命令。"""
    left = bool(key_state.get("left"))
    right = bool(key_state.get("right"))
    up = bool(key_state.get("up"))
    down = bool(key_state.get("down"))
    jump = bool(key_state.get("jump"))

    if segment_type == SEGMENT_ROPE:
        vertical = "up" if up and not down else "down" if down and not up else "none"
        return "none {} none".format(vertical), jump
    if segment_type == SEGMENT_DOWN_JUMP:
        action = "jump" if jump and not jump_was_pressed else "none"
        vertical = "down" if down or action == "jump" else "none"
        return "none {} {}".format(vertical, action), jump
    if segment_type == SEGMENT_RIGHT_RETURN:
        return "right none none", jump
    if segment_type == SEGMENT_WALK_OFF_LEFT:
        return "left none none", jump
    if segment_type == SEGMENT_WALK_OFF_RIGHT:
        return "right none none", jump

    horizontal = "left" if left and not right else "right" if right and not left else "none"
    vertical = "up" if up and not down else "down" if down and not up else "none"
    action = "jump" if jump and not jump_was_pressed else "none"
    if segment_type == SEGMENT_PLATFORM:
        vertical = "none"
    return "{} {} {}".format(horizontal, vertical, action), jump


def _command_from_pressed_keys(
    keyboard_module,
    jump_was_pressed: bool,
    segment_type: str,
) -> Tuple[str, bool]:
    """按当前分段类型把真实按键转换成可回放命令。"""
    return _command_from_key_state(
        _read_pressed_keys(keyboard_module),
        jump_was_pressed,
        segment_type,
    )


def _point_kind(command: str, segment_type: str) -> str:
    """根据分段类型和命令返回平台、绳子、跳跃或停留类型。"""
    _horizontal, vertical, action = command.split()
    if action == "jump":
        return "jump"
    if segment_type in RETURN_TRANSITION_SEGMENT_TYPES or segment_type in V2_TRANSITION_SEGMENT_TYPES:
        return segment_type
    if segment_type == SEGMENT_ROPE and vertical in ("up", "down"):
        return "rope"
    if segment_type == SEGMENT_PLATFORM and command.split()[0] in ("left", "right"):
        return "platform"
    return "idle"


def _manhattan_distance(first: Tuple[int, int], second: Tuple[int, int]) -> int:
    """计算两个小地图坐标之间的曼哈顿距离。"""
    return abs(int(first[0]) - int(second[0])) + abs(int(first[1]) - int(second[1]))


def _median_int(values: List[int]) -> int:
    """返回整数坐标集合的四舍五入中位数。"""
    return int(round(float(statistics.median(values))))


def _group_points_by_segment(points: List[Dict[str, object]]) -> Dict[int, List[Dict[str, object]]]:
    """按分段编号保存轨迹点，并保持每段内部的录制顺序。"""
    grouped: Dict[int, List[Dict[str, object]]] = {}
    for point in points:
        grouped.setdefault(int(point["segment_id"]), []).append(point)
    return grouped


def _deduplicate_recorded_points(
    points: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    """每个录制分段只保留一份坐标，避免同一点不同命令造成回放折返。"""
    result: List[Dict[str, object]] = []
    coordinate_indices = {}
    right_return_segments = set()
    for point in points:
        segment_id = int(point["segment_id"])
        segment_type = str(point["segment_type"])
        coordinate = (int(point["x"]), int(point["y"]))
        if segment_type == SEGMENT_RIGHT_RETURN:
            if segment_id in right_return_segments:
                continue
            right_return_segments.add(segment_id)
        coordinate_key = (
            segment_id,
            segment_type,
            str(point.get("platform_id")),
            str(point.get("route_pass", "primary")),
            coordinate,
        )
        existing_index = coordinate_indices.get(coordinate_key)
        if existing_index is not None:
            existing = result[existing_index]
            existing_action = str(existing.get("command", "none none none")).split()[-1]
            current_action = str(point.get("command", "none none none")).split()[-1]
            # 同一坐标同时采到普通移动和一次性跳跃时，保留跳跃点；其余情况
            # 保留首次经过的方向，不能让后续反向采样把路线改成来回折返。
            if existing_action != "jump" and current_action == "jump":
                result[existing_index] = point
            continue
        coordinate_indices[coordinate_key] = len(result)
        result.append(point)
    # 平台录制过程中人物在边缘停下或被碰撞时，30Hz采样可能留下1～3px的
    # 反向命令。若整段首尾净位移足够明确，则普通水平移动统一采用净方向；
    # 真正回到原处的往返段、跳跃段和绳子段不满足本条件，保持原样。
    grouped_indices: Dict[int, List[int]] = {}
    for index, point in enumerate(result):
        command_parts = str(point.get("command", "none none none")).split()
        if (
            str(point.get("segment_type")) == SEGMENT_PLATFORM
            and len(command_parts) == 3
            and command_parts[1:] == ["none", "none"]
        ):
            grouped_indices.setdefault(
                (
                    int(point["segment_id"]),
                    str(point.get("route_pass", "primary")),
                ),
                [],
            ).append(index)
    for indices in grouped_indices.values():
        if len(indices) < 2:
            continue
        first_x = int(result[indices[0]]["x"])
        last_x = int(result[indices[-1]]["x"])
        if abs(last_x - first_x) < PLATFORM_DIRECTION_NORMALIZE_MIN_SPAN:
            continue
        dominant = "right" if last_x > first_x else "left"
        for index in indices:
            horizontal, vertical, action = str(result[index]["command"]).split()
            if horizontal not in ("left", "right") or horizontal == dominant:
                continue
            normalized = dict(result[index])
            normalized["command"] = "{} {} {}".format(
                dominant,
                vertical,
                action,
            )
            result[index] = normalized
    return result


def _filter_discarded_platform_points(
    points: List[Dict[str, object]],
    discarded_platform_ids,
) -> List[Dict[str, object]]:
    """删除指定平台段及从该平台开始录制的回程标记。"""
    discarded = {str(platform_id) for platform_id in discarded_platform_ids}
    if not discarded:
        return list(points)
    removable_types = {
        SEGMENT_PLATFORM,
        SEGMENT_DOWN_JUMP,
        SEGMENT_RIGHT_RETURN,
        SEGMENT_WALK_OFF_LEFT,
        SEGMENT_WALK_OFF_RIGHT,
        SEGMENT_PLATFORM_JUMP_LEFT,
        SEGMENT_PLATFORM_JUMP_RIGHT,
        SEGMENT_PLATFORM_JUMP_NEUTRAL,
        SEGMENT_PLATFORM_TO_ROPE_LEFT,
        SEGMENT_PLATFORM_TO_ROPE_RIGHT,
        SEGMENT_ROPE_TO_PLATFORM_LEFT,
        SEGMENT_ROPE_TO_PLATFORM_RIGHT,
    }
    return [
        point
        for point in points
        if not (
            str(point.get("segment_type")) in removable_types
            and str(point.get("platform_id")) in discarded
        )
    ]


def _platform_recording_baseline_y(
    segment_points: List[Dict[str, object]],
) -> int:
    """使用平台段最开始的稳定坐标确定本段平台高度。"""
    sample = segment_points[:PLATFORM_RECORD_BASELINE_SAMPLE_COUNT]
    return _median_int([int(point["y"]) for point in sample])


def _sanitize_recorded_platform_points(
    points: List[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], Dict[int, Dict[str, int]]]:
    """沿录制轨迹保留平地和渐进斜坡，删除瞬移误识别及真实掉层尾段。"""
    grouped = _group_points_by_segment(points)
    platform_cleanup: Dict[int, Dict[str, int]] = {}
    retained_ids = set()
    for segment_id, segment_points in grouped.items():
        if str(segment_points[0].get("segment_type")) != SEGMENT_PLATFORM:
            continue
        observation_state: Dict[str, object] = {}
        retained = []
        departed = False
        for point in segment_points:
            observation = _observe_platform_recording_position(
                observation_state,
                (int(point["x"]), int(point["y"])),
            )
            if observation == "outlier":
                continue
            if observation == "departed":
                # 当前帧尚未加入；此前等待确认的疑似下坠帧需要一起回滚，
                # 保存结果停在最后一个可信平台/斜坡坐标。
                rollback = max(
                    0,
                    int(observation_state.get("off_platform_frames", 0)) - 1,
                )
                if rollback:
                    del retained[max(0, len(retained) - rollback):]
                departed = True
                break
            retained.append(point)
        if len(retained) < PLATFORM_RECORD_MIN_POINTS:
            platform_id = str(segment_points[0].get("platform_id") or segment_id)
            raise ValueError(
                "{}有效平台坐标不足{}个；录制中人物可能已掉层，"
                "请重新录制该平台。".format(
                    platform_id,
                    PLATFORM_RECORD_MIN_POINTS,
                )
            )
        baseline_y = int(
            observation_state.get(
                "baseline_y",
                _platform_recording_baseline_y(retained),
            )
        )
        retained_ids.update(id(point) for point in retained)
        retained_ys = [int(point["y"]) for point in retained]
        platform_cleanup[segment_id] = {
            "baseline_y": baseline_y,
            "original_points": len(segment_points),
            "retained_points": len(retained),
            "removed_points": len(segment_points) - len(retained),
            "surface_min_y": min(retained_ys),
            "surface_max_y": max(retained_ys),
            "slope_points": int(observation_state.get("slope_points", 0)),
            "departed_tail_removed": departed,
        }
    sanitized = [
        point
        for point in points
        if str(point.get("segment_type")) != SEGMENT_PLATFORM
        or id(point) in retained_ids
    ]
    return sanitized, platform_cleanup


def _observe_platform_recording_position(
    state: Dict[str, object],
    position: Tuple[int, int],
) -> str:
    """跟随横向渐变的斜坡Y；仅把连续纯下坠判为离开平台。"""
    current = (int(position[0]), int(position[1]))
    previous = state.get("last_plausible_position")
    horizontal_delta = 0
    vertical_delta = 0
    if previous is not None:
        previous_position = (int(previous[0]), int(previous[1]))
        if _manhattan_distance(previous_position, current) > PLATFORM_RECORD_MAX_FRAME_DISTANCE:
            state["recognition_outliers"] = int(state.get("recognition_outliers", 0)) + 1
            return "outlier"
        horizontal_delta = abs(current[0] - previous_position[0])
        vertical_delta = current[1] - previous_position[1]
    if horizontal_delta > 0:
        state["frames_since_horizontal_progress"] = 0
    else:
        state["frames_since_horizontal_progress"] = (
            int(state.get("frames_since_horizontal_progress", 0)) + 1
        )
    state["last_plausible_position"] = current

    samples = state.setdefault("baseline_samples", [])
    baseline_y = state.get("baseline_y")
    if baseline_y is None:
        samples.append(current[1])
        if len(samples) >= PLATFORM_RECORD_BASELINE_SAMPLE_COUNT:
            baseline_y = _median_int([int(value) for value in samples])
            state["baseline_y"] = baseline_y
            baseline_steps = [
                abs(int(current_y) - int(previous_y))
                for previous_y, current_y in zip(samples, samples[1:])
            ]
            started_on_slope = bool(baseline_steps) and all(
                step <= PLATFORM_RECORD_SLOPE_MAX_STEP_Y
                for step in baseline_steps
            )
            initial_surface_y = current[1] if started_on_slope else baseline_y
            state["surface_y"] = initial_surface_y
            state["surface_min_y"] = min(int(value) for value in samples)
            state["surface_max_y"] = max(int(value) for value in samples)
            if started_on_slope:
                state["slope_points"] = sum(step > 0 for step in baseline_steps)
        else:
            return "valid"

    baseline_y = int(baseline_y)
    surface_y = int(state.get("surface_y", baseline_y))
    recent_horizontal_progress = (
        int(state.get("frames_since_horizontal_progress", 0))
        <= PLATFORM_RECORD_SLOPE_HORIZONTAL_GRACE_FRAMES
    )
    # 只有从当前局部平台高度逐步变化，且附近确有横向推进，才更新斜坡高度。
    # 一次跳起通常会瞬间跨过数个Y，之后即使横向移动也不会吸附成新平台。
    follows_slope = (
        recent_horizontal_progress
        and abs(current[1] - surface_y) <= PLATFORM_RECORD_SLOPE_MAX_STEP_Y
        and abs(vertical_delta) <= PLATFORM_RECORD_SLOPE_MAX_STEP_Y
    )
    if follows_slope:
        if current[1] != surface_y:
            state["slope_points"] = int(state.get("slope_points", 0)) + 1
        state["surface_y"] = current[1]
        state["surface_min_y"] = min(
            int(state.get("surface_min_y", current[1])),
            current[1],
        )
        state["surface_max_y"] = max(
            int(state.get("surface_max_y", current[1])),
            current[1],
        )
        surface_y = current[1]

    # 位于当前局部平台高度附近、斜坡上方或跳起时都继续录制。
    if current[1] <= surface_y + PLATFORM_RECORD_Y_TOLERANCE:
        state["off_platform_frames"] = 0
        state.pop("fall_anchor_x", None)
        state.pop("fall_anchor_y", None)
        return "valid"

    if int(state.get("off_platform_frames", 0)) == 0:
        state["fall_anchor_x"] = current[0]
        state["fall_anchor_y"] = surface_y
    off_platform_frames = int(state.get("off_platform_frames", 0)) + 1
    state["off_platform_frames"] = off_platform_frames
    fall_anchor_x = int(state.get("fall_anchor_x", current[0]))
    fall_anchor_y = int(state.get("fall_anchor_y", surface_y))
    vertical_drop = current[1] - fall_anchor_y
    horizontal_travel = abs(current[0] - fall_anchor_x)
    maximum_fall_horizontal = max(
        2,
        int(round(vertical_drop * PLATFORM_RECORD_SLOPE_MIN_HORIZONTAL_RATIO)),
    )
    if (
        off_platform_frames >= PLATFORM_RECORD_OFF_PLATFORM_FRAMES
        and vertical_drop >= PLATFORM_RECORD_FALL_MIN_DROP_Y
        and horizontal_travel <= maximum_fall_horizontal
    ):
        return "departed"
    return "valid"


def _validate_platform_geometry(segments: List[Dict[str, object]]) -> None:
    """阻止跨越多个高度的平台段进入可运行路线。"""
    for segment in segments:
        if str(segment.get("type")) != SEGMENT_PLATFORM:
            continue
        minimum_y, maximum_y = [int(value) for value in segment["y_range"]]
        if (
            maximum_y - minimum_y > PLATFORM_RECORD_Y_TOLERANCE * 2
            and not bool(segment.get("slope_compatible"))
        ):
            raise ValueError(
                "{}录制中存在非连续高度变化（Y={}～{}，最大单步Y={}），"
                "疑似人物掉落后仍在录制；坐标仍会保存并标记为需要检查。".format(
                    segment.get("platform_id") or "平台段#{}".format(segment["id"]),
                    minimum_y,
                    maximum_y,
                    segment.get("max_consecutive_y_step", 0),
                )
            )


def _build_segment_summaries(points: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """汇总每个平台段和绳子段的坐标范围及代表坐标。"""
    grouped = _group_points_by_segment(points)
    summaries: List[Dict[str, object]] = []
    for order, segment_id in enumerate(sorted(grouped), start=1):
        segment_points = grouped[segment_id]
        segment_type = str(segment_points[0]["segment_type"])
        xs = [int(point["x"]) for point in segment_points]
        ys = [int(point["y"]) for point in segment_points]
        consecutive_steps = [
            (
                abs(int(current["x"]) - int(previous["x"])),
                abs(int(current["y"]) - int(previous["y"])),
            )
            for previous, current in zip(segment_points, segment_points[1:])
        ]
        horizontal_span = max(xs) - min(xs)
        vertical_span = max(ys) - min(ys)
        slope_compatible = (
            vertical_span <= PLATFORM_RECORD_Y_TOLERANCE * 2
            or (
                horizontal_span
                >= vertical_span * PLATFORM_RECORD_SLOPE_MIN_HORIZONTAL_RATIO
                and all(
                    step_y
                    <= max(
                        PLATFORM_RECORD_SLOPE_MAX_STEP_Y,
                        step_x + 1,
                    )
                    for step_x, step_y in consecutive_steps
                )
            )
        )
        summary = {
            "id": segment_id,
            "order": order,
            "type": segment_type,
            "point_count": len(segment_points),
            "x_range": [min(xs), max(xs)],
            "y_range": [min(ys), max(ys)],
            "representative_x": _median_int(xs),
            "representative_y": _median_int(ys),
            "start": [int(segment_points[0]["x"]), int(segment_points[0]["y"])],
            "end": [int(segment_points[-1]["x"]), int(segment_points[-1]["y"])],
            "slope_compatible": slope_compatible,
            "horizontal_span": horizontal_span,
            "vertical_span": vertical_span,
            "max_consecutive_y_step": max(
                (step_y for _step_x, step_y in consecutive_steps),
                default=0,
            ),
        }
        if segment_type == SEGMENT_PLATFORM:
            summary["platform_id"] = str(
                segment_points[0].get("platform_id") or "未编号"
            )
        elif (
            segment_type in {
                SEGMENT_ROPE_TO_PLATFORM_LEFT,
                SEGMENT_ROPE_TO_PLATFORM_RIGHT,
            }
            and segment_points[0].get("platform_id")
        ):
            summary["recorded_to_platform_id"] = str(
                segment_points[0]["platform_id"]
            )
        elif segment_points[0].get("platform_id"):
            summary["recorded_from_platform_id"] = str(
                segment_points[0]["platform_id"]
            )
        if segment_type == SEGMENT_ROPE:
            summary.update(
                {
                    "rope_x": _median_int(xs),
                    "top_y": min(ys),
                    "bottom_y": max(ys),
                    "fuzzy_x_range": [
                        min(xs) - ROPE_X_FUZZY_TOLERANCE,
                        max(xs) + ROPE_X_FUZZY_TOLERANCE,
                    ],
                }
            )
        summaries.append(summary)
    return summaries


def _match_recorded_platform_at_position(
    points: List[Dict[str, object]],
    source_platform_id: Optional[str],
    position: Tuple[int, int],
) -> Optional[str]:
    """把走出平台后的实际落点关联到已录制的其他平台。"""
    grouped: Dict[str, List[Tuple[int, int]]] = {}
    for point in points:
        if str(point.get("segment_type")) != SEGMENT_PLATFORM:
            continue
        platform_id = str(point.get("platform_id") or "")
        if not platform_id or platform_id == str(source_platform_id or ""):
            continue
        grouped.setdefault(platform_id, []).append(
            (int(point["x"]), int(point["y"]))
        )
    current_x, current_y = int(position[0]), int(position[1])
    candidates = []
    for platform_id, coordinates in grouped.items():
        xs = [coordinate[0] for coordinate in coordinates]
        ys = [coordinate[1] for coordinate in coordinates]
        platform_y = _median_int(ys)
        y_delta = abs(current_y - platform_y)
        if y_delta > WALK_OFF_PLATFORM_Y_TOLERANCE:
            continue
        minimum_x, maximum_x = min(xs), max(xs)
        if current_x < minimum_x - WALK_OFF_PLATFORM_X_TOLERANCE:
            continue
        if current_x > maximum_x + WALK_OFF_PLATFORM_X_TOLERANCE:
            continue
        x_gap = (
            minimum_x - current_x
            if current_x < minimum_x
            else current_x - maximum_x
            if current_x > maximum_x
            else 0
        )
        candidates.append((y_delta * 10 + x_gap, platform_id))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _observe_walk_off_landing(
    state: Dict[str, object],
    recorded_points: List[Dict[str, object]],
    source_platform_id: Optional[str],
    position: Tuple[int, int],
) -> Optional[str]:
    """跟踪走出平台的下落过程，连续稳定落在其他平台时返回其编号。"""
    current_y = int(position[1])
    if "source_y" not in state:
        state.update(
            {
                "source_y": current_y,
                "departed": False,
                "landing_platform_id": None,
                "stable_frames": 0,
                "last_landing_y": None,
            }
        )
        return None
    source_y = int(state["source_y"])
    if not bool(state.get("departed")):
        if current_y < source_y + WALK_OFF_DEPARTURE_MIN_Y:
            return None
        state["departed"] = True
    target_platform_id = _match_recorded_platform_at_position(
        recorded_points,
        source_platform_id,
        position,
    )
    if target_platform_id is None:
        state["landing_platform_id"] = None
        state["stable_frames"] = 0
        state["last_landing_y"] = None
        return None
    same_platform = target_platform_id == state.get("landing_platform_id")
    previous_landing_y = state.get("last_landing_y")
    y_is_stable = (
        previous_landing_y is not None
        and abs(current_y - int(previous_landing_y)) <= 1
    )
    if same_platform and y_is_stable:
        state["stable_frames"] = int(state.get("stable_frames", 0)) + 1
    else:
        state["landing_platform_id"] = target_platform_id
        state["stable_frames"] = 1
    state["last_landing_y"] = current_y
    if int(state["stable_frames"]) < WALK_OFF_LANDING_STABLE_FRAMES:
        return None
    return str(target_platform_id)


def _validate_platform_ids(segments: List[Dict[str, object]]) -> None:
    """确保一次录制中每个平台编号只对应一个平台段。"""
    platform_ids = [
        str(segment.get("platform_id"))
        for segment in segments
        if segment.get("type") == SEGMENT_PLATFORM
    ]
    duplicates = sorted(
        {
            platform_id
            for platform_id in platform_ids
            if platform_ids.count(platform_id) > 1
        }
    )
    if duplicates:
        raise ValueError(
            "平台编号{}被重复录制；每个平台请只录制一次，并依次使用平台1、平台2、平台3。".format(
                "、".join(duplicates)
            )
        )


def _horizontal_gap(platform: Dict[str, object], rope_x: int) -> int:
    """返回绳子X到平台横向范围的最短距离。"""
    minimum, maximum = [int(value) for value in platform["x_range"]]
    if minimum <= rope_x <= maximum:
        return 0
    return minimum - rope_x if rope_x < minimum else rope_x - maximum


def _platform_y_near_x(
    platform: Dict[str, object],
    target_x: int,
    platform_points_by_id: Optional[Dict[int, List[Dict[str, object]]]] = None,
) -> int:
    """返回平台在目标X附近的局部Y，使绳子能连接斜坡端点而不是全段中位Y。"""
    if not platform_points_by_id:
        return int(platform["representative_y"])
    points = platform_points_by_id.get(int(platform["id"]), [])
    if not points:
        return int(platform["representative_y"])
    minimum_x_delta = min(abs(int(point["x"]) - int(target_x)) for point in points)
    nearby = [
        point
        for point in points
        if abs(int(point["x"]) - int(target_x)) <= minimum_x_delta + 2
    ]
    return _median_int([int(point["y"]) for point in nearby])


def _select_connected_platform(
    platforms: List[Dict[str, object]],
    target_y: int,
    rope_x: int,
    excluded_id: Optional[int] = None,
    y_tolerance: int = ROPE_PLATFORM_Y_TOLERANCE,
    x_tolerance: int = ROPE_PLATFORM_X_TOLERANCE,
    vertical_role: Optional[str] = None,
    platform_points_by_id: Optional[Dict[int, List[Dict[str, object]]]] = None,
) -> Optional[Dict[str, object]]:
    """按端点方向、局部平台Y和X邻接距离选择连接平台。"""
    candidates = []
    for platform in platforms:
        if excluded_id is not None and int(platform["id"]) == excluded_id:
            continue
        platform_y = _platform_y_near_x(
            platform,
            rope_x,
            platform_points_by_id,
        )
        if vertical_role == "lower" and platform_y < target_y - ROPE_BOTTOM_Y_FUZZY_TOLERANCE:
            continue
        if vertical_role == "upper" and platform_y > target_y + ROPE_TOP_Y_FUZZY_TOLERANCE:
            continue
        y_delta = abs(platform_y - target_y)
        x_gap = _horizontal_gap(platform, rope_x)
        if y_delta > y_tolerance:
            continue
        if x_gap > x_tolerance:
            continue
        candidates.append((y_delta * 10 + x_gap, platform))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _select_legacy_walk_off_target(
    platforms: List[Dict[str, object]],
    source: Dict[str, object],
    marker_x: int,
    direction: str,
) -> Optional[Dict[str, object]]:
    """为旧版单点标记按移动方向推导下方落点平台。"""
    source_y = int(source["representative_y"])
    candidates = [
        platform
        for platform in platforms
        if int(platform["id"]) != int(source["id"])
        and int(platform["representative_y"]) > source_y
    ]
    if not candidates:
        return None
    direction_reachable = [
        platform
        for platform in candidates
        if (
            int(platform["x_range"][1])
            >= marker_x - ROPE_PLATFORM_X_TOLERANCE
            if direction == "right"
            else int(platform["x_range"][0])
            <= marker_x + ROPE_PLATFORM_X_TOLERANCE
        )
    ]
    if direction_reachable:
        candidates = direction_reachable
    candidates.sort(
        key=lambda platform: (
            -int(platform["representative_y"]),
            _horizontal_gap(platform, marker_x),
        )
    )
    return candidates[0]


def _select_rope_exit_direction(
    upper_platform: Dict[str, object],
    rope_x: int,
) -> str:
    """根据绳子X和上层平台范围选择离绳后应走的方向。"""
    minimum_x, maximum_x = [int(value) for value in upper_platform["x_range"]]
    if rope_x < minimum_x:
        return "right"
    if rope_x > maximum_x:
        return "left"
    left_space = max(0, rope_x - minimum_x)
    right_space = max(0, maximum_x - rope_x)
    return "right" if right_space >= left_space else "left"


def _select_lower_platform_entry_y(
    raw_points: List[Dict[str, object]],
    lower_platform_id: int,
    rope_x: int,
    fallback_y: int,
) -> int:
    """读取绳子附近的下层平台坐标，返回实际起跳所处的平台Y。"""
    grouped = _group_points_by_segment(raw_points)
    platform_points = grouped.get(int(lower_platform_id), [])
    if not platform_points:
        return int(fallback_y)
    minimum_x_delta = min(
        abs(int(point["x"]) - int(rope_x)) for point in platform_points
    )
    nearby_points = [
        point
        for point in platform_points
        if abs(int(point["x"]) - int(rope_x)) <= minimum_x_delta + 2
    ]
    if not nearby_points:
        return int(fallback_y)
    return _median_int([int(point["y"]) for point in nearby_points])


def _build_route_graph(
    segments: List[Dict[str, object]],
    transition_probabilities: Optional[Dict[str, int]] = None,
    raw_points: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """建立平台、绳子及上层返回下层的概率分支连接。"""
    platforms = [segment for segment in segments if segment["type"] == SEGMENT_PLATFORM]
    ropes = [segment for segment in segments if segment["type"] == SEGMENT_ROPE]
    transition_segments = [
        segment
        for segment in segments
        if segment["type"] in RETURN_TRANSITION_SEGMENT_TYPES
    ]
    platform_points_by_id = _group_points_by_segment(raw_points or [])
    probabilities = transition_probabilities or {
        SEGMENT_DOWN_JUMP: 34,
        SEGMENT_WALK_OFF_LEFT: 33,
        SEGMENT_WALK_OFF_RIGHT: 33,
    }
    connections: List[Dict[str, object]] = []
    unmatched_rope_ids: List[int] = []
    return_transitions: List[Dict[str, object]] = []
    unmatched_transition_ids: List[int] = []

    for rope in ropes:
        rope_id = int(rope["id"])
        rope_x = int(rope["rope_x"])
        top_y = int(rope["top_y"])
        bottom_y = int(rope["bottom_y"])
        lower = _select_connected_platform(
            platforms,
            bottom_y,
            rope_x,
            y_tolerance=ROPE_LOWER_PLATFORM_Y_TOLERANCE,
            vertical_role="lower",
            platform_points_by_id=platform_points_by_id,
        )
        upper = _select_connected_platform(
            platforms,
            top_y,
            rope_x,
            excluded_id=(int(lower["id"]) if lower else None),
            y_tolerance=ROPE_UPPER_PLATFORM_Y_TOLERANCE,
            vertical_role="upper",
            platform_points_by_id=platform_points_by_id,
        )
        if lower is None or upper is None:
            unmatched_rope_ids.append(rope_id)
            continue
        lower_y = _platform_y_near_x(
            lower,
            rope_x,
            platform_points_by_id,
        )
        upper_y = _platform_y_near_x(
            upper,
            rope_x,
            platform_points_by_id,
        )
        # 屏幕坐标Y越大位置越低；斜坡必须使用绳子X附近的局部高度判断上下层。
        if lower_y < upper_y:
            lower, upper = upper, lower
            lower_y, upper_y = upper_y, lower_y
        lower_entry_y = _select_lower_platform_entry_y(
            raw_points or [],
            int(lower["id"]),
            rope_x,
            lower_y,
        )
        top_exit_direction = _select_rope_exit_direction(upper, rope_x)
        connections.append(
            {
                "rope_segment_id": rope_id,
                "lower_platform_id": int(lower["id"]),
                "upper_platform_id": int(upper["id"]),
                "lower_platform_name": str(lower.get("platform_id", lower["id"])),
                "upper_platform_name": str(upper.get("platform_id", upper["id"])),
                "rope_x": rope_x,
                "rope_x_range": list(rope["fuzzy_x_range"]),
                "rope_y_range": [top_y, bottom_y],
                "top_point": [rope_x, top_y],
                "bottom_point": [rope_x, bottom_y],
                "lower_platform_y": lower_y,
                "lower_entry_y": lower_entry_y,
                "lower_entry_y_source": "lower_platform_near_rope",
                "upper_platform_y": upper_y,
                "top_exit": {
                    "position": [rope_x, top_y],
                    "direction": top_exit_direction,
                    "command": "{} none none".format(top_exit_direction),
                    "trigger_y_range": [
                        top_y - ROPE_TOP_Y_FUZZY_TOLERANCE,
                        top_y + ROPE_TOP_Y_FUZZY_TOLERANCE,
                    ],
                },
                "right_jump": {
                    "x_range": [
                        rope_x - ROPE_ENTRY_OFFSET_X,
                        rope_x - ROPE_ENTRY_OFFSET_X,
                    ],
                    "y_range": [
                        lower_entry_y - ROPE_BOTTOM_Y_FUZZY_TOLERANCE,
                        lower_entry_y + ROPE_BOTTOM_Y_FUZZY_TOLERANCE,
                    ],
                    "command": "right up jump",
                },
                "left_jump": {
                    "x_range": [
                        rope_x + ROPE_ENTRY_OFFSET_X,
                        rope_x + ROPE_ENTRY_OFFSET_X,
                    ],
                    "y_range": [
                        lower_entry_y - ROPE_BOTTOM_Y_FUZZY_TOLERANCE,
                        lower_entry_y + ROPE_BOTTOM_Y_FUZZY_TOLERANCE,
                    ],
                    "command": "left up jump",
                },
                "top_overlap": {
                    "rope_y_range": [
                        top_y - ROPE_TOP_Y_FUZZY_TOLERANCE,
                        top_y + ROPE_TOP_Y_FUZZY_TOLERANCE,
                    ],
                    "platform_y_range": [
                        upper_y - ROPE_TOP_Y_FUZZY_TOLERANCE,
                        upper_y + ROPE_TOP_Y_FUZZY_TOLERANCE,
                    ],
                },
            }
        )

    for transition in transition_segments:
        transition_type = str(transition["type"])
        transition_id = int(transition["id"])
        start_x, start_y = [int(value) for value in transition["start"]]
        end_x, end_y = [int(value) for value in transition["end"]]
        declared_source = transition.get("recorded_from_platform_id")
        source = next(
            (
                platform
                for platform in platforms
                if declared_source
                and str(platform.get("platform_id")) == str(declared_source)
            ),
            None,
        )
        if source is not None:
            source_x_min, source_x_max = [int(value) for value in source["x_range"]]
            source_y = _platform_y_near_x(
                source,
                start_x,
                platform_points_by_id,
            )
            source_x_gap = max(source_x_min - start_x, start_x - source_x_max, 0)
            if (
                abs(source_y - start_y) > ROPE_UPPER_PLATFORM_Y_TOLERANCE
                or source_x_gap > ROPE_PLATFORM_X_TOLERANCE
            ):
                source = None
        if source is None:
            source = _select_connected_platform(
                platforms,
                start_y,
                start_x,
                y_tolerance=ROPE_UPPER_PLATFORM_Y_TOLERANCE,
                platform_points_by_id=platform_points_by_id,
            )
        legacy_marker_only = transition_type == SEGMENT_RIGHT_RETURN
        if legacy_marker_only and source is not None:
            target = _select_legacy_walk_off_target(
                platforms,
                source,
                start_x,
                "right",
            )
            end_x, end_y = start_x, start_y
        else:
            target = _select_connected_platform(
                platforms,
                end_y,
                end_x,
                excluded_id=(int(source["id"]) if source else None),
                platform_points_by_id=platform_points_by_id,
            )
        if source is None or target is None:
            unmatched_transition_ids.append(transition_id)
            continue
        # 返回分支必须从屏幕上方平台落到Y更大的下方平台；斜坡用连接点局部Y。
        source_local_y = _platform_y_near_x(
            source,
            start_x,
            platform_points_by_id,
        )
        target_local_y = _platform_y_near_x(
            target,
            end_x,
            platform_points_by_id,
        )
        if source_local_y >= target_local_y:
            unmatched_transition_ids.append(transition_id)
            continue
        probability_type = (
            SEGMENT_WALK_OFF_RIGHT
            if transition_type == SEGMENT_RIGHT_RETURN
            else transition_type
        )
        direction = (
            "left"
            if transition_type == SEGMENT_WALK_OFF_LEFT
            else "right"
            if transition_type in (SEGMENT_WALK_OFF_RIGHT, SEGMENT_RIGHT_RETURN)
            else None
        )
        probability_value = probabilities.get(
            transition_type,
            probabilities.get(probability_type, 0),
        )
        return_transitions.append(
            {
                "segment_id": transition_id,
                "type": transition_type,
                "from_platform_id": int(source["id"]),
                "to_platform_id": int(target["id"]),
                "from_platform_name": str(source.get("platform_id", source["id"])),
                "to_platform_name": str(target.get("platform_id", target["id"])),
                "start": [start_x, start_y],
                "end": [end_x, end_y],
                "direction": direction,
                "marker_only": legacy_marker_only,
                "probability": max(0, min(100, int(probability_value))),
            }
        )

    return {
        "platforms": platforms,
        "ropes": ropes,
        "connections": connections,
        "return_transitions": return_transitions,
        "unmatched_rope_ids": unmatched_rope_ids,
        "unmatched_transition_ids": unmatched_transition_ids,
        "fuzzy_tolerance": {
            "platform_y": ROPE_PLATFORM_Y_TOLERANCE,
            "upper_platform_y": ROPE_UPPER_PLATFORM_Y_TOLERANCE,
            "lower_platform_y": ROPE_LOWER_PLATFORM_Y_TOLERANCE,
            "platform_x": ROPE_PLATFORM_X_TOLERANCE,
            "rope_x": ROPE_X_FUZZY_TOLERANCE,
            "rope_top_y": ROPE_TOP_Y_FUZZY_TOLERANCE,
            "rope_bottom_y": ROPE_BOTTOM_Y_FUZZY_TOLERANCE,
        },
    }


def _route_graph_recording_warnings(
    route_graph: Dict[str, object],
    *,
    is_v2: bool,
    rope_count: int,
    legacy_transition_count: int,
) -> List[Dict[str, object]]:
    """把可分析的连接异常转换为保存警告，不再丢弃本次录制数据。"""
    if is_v2:
        return []
    warnings: List[Dict[str, object]] = []
    unmatched_rope_ids = [
        int(segment_id)
        for segment_id in route_graph.get("unmatched_rope_ids", [])
    ]
    if rope_count and unmatched_rope_ids:
        warnings.append(
            {
                "code": "unmatched_rope_platforms",
                "severity": "warning",
                "segment_ids": unmatched_rope_ids,
                "message": (
                    "绳子段{}没有同时匹配到上下平台；本次坐标已保留，可根据JSON继续分析。"
                    "建议确认双方平台均已录制，并从绳底完整爬到绳顶。"
                ).format(unmatched_rope_ids),
                "tolerance": {
                    "lower_platform_y": ROPE_LOWER_PLATFORM_Y_TOLERANCE,
                    "upper_platform_y": ROPE_UPPER_PLATFORM_Y_TOLERANCE,
                    "platform_x": ROPE_PLATFORM_X_TOLERANCE,
                },
            }
        )
    unmatched_transition_ids = [
        int(segment_id)
        for segment_id in route_graph.get("unmatched_transition_ids", [])
    ]
    if legacy_transition_count and unmatched_transition_ids:
        warnings.append(
            {
                "code": "unmatched_return_transition",
                "severity": "warning",
                "segment_ids": unmatched_transition_ids,
                "message": (
                    "返回连接{}没有匹配到不同的上下平台；本次起点、过程和落点坐标已保留。"
                ).format(unmatched_transition_ids),
                "tolerance": {
                    "platform_y": ROPE_UPPER_PLATFORM_Y_TOLERANCE,
                    "platform_x": ROPE_PLATFORM_X_TOLERANCE,
                },
            }
        )
    return warnings


def _v2_platform_node_id(platform_name: object, segment_id: int) -> str:
    """把“平台1”等用户编号转换成稳定的V2节点ID。"""
    digits = "".join(character for character in str(platform_name) if character.isdigit())
    return "P{}".format(digits or int(segment_id))


def _build_v2_nodes(segments: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """从结构标注生成唯一物理节点；平台和绳子只各保留一个实体。"""
    nodes: List[Dict[str, object]] = []
    used_ids = set()
    for segment in segments:
        segment_type = str(segment.get("type"))
        if segment_type == SEGMENT_PLATFORM:
            node_id = _v2_platform_node_id(
                segment.get("platform_id"),
                int(segment["id"]),
            )
            base_id = node_id
            suffix = 2
            while node_id in used_ids:
                node_id = "{}_{}".format(base_id, suffix)
                suffix += 1
            used_ids.add(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "kind": "platform",
                    "name": str(segment.get("platform_id") or node_id),
                    "segment_id": int(segment["id"]),
                    "geometry": {
                        "x_range": [int(value) for value in segment["x_range"]],
                        "y": int(segment["representative_y"]),
                    },
                }
            )
    rope_number = 0
    for segment in segments:
        if str(segment.get("type")) != SEGMENT_ROPE:
            continue
        rope_number += 1
        node_id = "R{}".format(rope_number)
        nodes.append(
            {
                "id": node_id,
                "kind": "rope",
                "name": "绳子{}".format(rope_number),
                "segment_id": int(segment["id"]),
                "geometry": {
                    "x": int(segment["rope_x"]),
                    "x_range": [int(value) for value in segment["fuzzy_x_range"]],
                    "top_y": int(segment["top_y"]),
                    "bottom_y": int(segment["bottom_y"]),
                },
            }
        )
    return nodes


def _v2_frame_key_state(frame: Dict[str, object]) -> Dict[str, bool]:
    """兼容读取V2逐帧按键以及测试或旧草稿中的command字段。"""
    keys = frame.get("keys")
    if isinstance(keys, dict):
        return {
            "left": bool(keys.get("left")),
            "right": bool(keys.get("right")),
            "up": bool(keys.get("up")),
            "down": bool(keys.get("down")),
            "jump": bool(keys.get("jump")),
        }
    parts = str(frame.get("command", "none none none")).split()
    if len(parts) != 3:
        parts = ["none", "none", "none"]
    horizontal, vertical, action = parts
    return {
        "left": horizontal == "left",
        "right": horizontal == "right",
        "up": vertical == "up",
        "down": vertical == "down",
        "jump": action == "jump",
    }


def _label_v2_position(
    position: Tuple[int, int],
    nodes: List[Dict[str, object]],
    key_state: Optional[Dict[str, bool]] = None,
    loose: bool = False,
) -> Optional[str]:
    """按结构几何把一个试走坐标回标为平台、绳子或过渡区。"""
    current_x, current_y = int(position[0]), int(position[1])
    keys = key_state or {}
    vertical_pressed = bool(keys.get("up") or keys.get("down"))
    platform_y_tolerance = V2_PLATFORM_LABEL_Y_TOLERANCE + (5 if loose else 0)
    platform_x_tolerance = V2_PLATFORM_LABEL_X_TOLERANCE + (8 if loose else 0)
    rope_x_tolerance = V2_ROPE_LABEL_X_TOLERANCE + (3 if loose else 0)
    rope_y_tolerance = V2_ROPE_LABEL_Y_TOLERANCE + (5 if loose else 0)
    candidates = []
    for node in nodes:
        geometry = node["geometry"]
        if node["kind"] == "platform":
            minimum_x, maximum_x = [int(value) for value in geometry["x_range"]]
            y_delta = abs(current_y - int(geometry["y"]))
            x_gap = max(minimum_x - current_x, current_x - maximum_x, 0)
            if y_delta <= platform_y_tolerance and x_gap <= platform_x_tolerance:
                score = y_delta * 10 + x_gap + (6 if vertical_pressed else 0)
                candidates.append((score, 1, str(node["id"])))
        elif node["kind"] == "rope":
            rope_x = int(geometry["x"])
            top_y = int(geometry["top_y"])
            bottom_y = int(geometry["bottom_y"])
            x_delta = abs(current_x - rope_x)
            y_gap = max(top_y - current_y, current_y - bottom_y, 0)
            if x_delta <= rope_x_tolerance and y_gap <= rope_y_tolerance:
                score = x_delta * 10 + y_gap - (8 if vertical_pressed else 0)
                candidates.append((score, 0 if vertical_pressed else 2, str(node["id"])))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][2]


def _stable_v2_node_runs(labels: List[Optional[str]]) -> List[Dict[str, object]]:
    """压缩逐帧标签，只保留连续稳定至少三帧的物理节点访问。"""
    raw_runs: List[Dict[str, object]] = []
    start_index = 0
    current_label: Optional[str] = None
    for index in range(len(labels) + 1):
        label = labels[index] if index < len(labels) else None
        if index == 0:
            current_label = label
            continue
        if label == current_label:
            continue
        if current_label is not None and index - start_index >= V2_STABLE_NODE_MIN_FRAMES:
            raw_runs.append(
                {
                    "node_id": current_label,
                    "start_frame": start_index,
                    "end_frame": index - 1,
                    "frame_count": index - start_index,
                }
            )
        current_label = label
        start_index = index
    return raw_runs


def _v2_transition_signature(
    frames: List[Dict[str, object]],
    source_node: Dict[str, object],
    target_node: Dict[str, object],
) -> Tuple[str, Dict[str, object]]:
    """按起终节点、按键和位移判定一次有向过渡的机制。"""
    key_states = [_v2_frame_key_state(frame) for frame in frames]
    left_frames = sum(bool(keys["left"]) for keys in key_states)
    right_frames = sum(bool(keys["right"]) for keys in key_states)
    up_frames = sum(bool(keys["up"]) for keys in key_states)
    down_frames = sum(bool(keys["down"]) for keys in key_states)
    jump_frames = sum(bool(keys["jump"]) for keys in key_states)
    start_x = int(frames[0]["x"]) if frames else 0
    end_x = int(frames[-1]["x"]) if frames else start_x
    if left_frames > right_frames:
        direction = "left"
    elif right_frames > left_frames:
        direction = "right"
    elif end_x < start_x:
        direction = "left"
    elif end_x > start_x:
        direction = "right"
    else:
        direction = "neutral"

    source_kind = str(source_node["kind"])
    target_kind = str(target_node["kind"])
    if source_kind == "platform" and target_kind == "rope":
        transition_type = (
            SEGMENT_PLATFORM_TO_ROPE_LEFT
            if direction == "left"
            else SEGMENT_PLATFORM_TO_ROPE_RIGHT
        )
    elif source_kind == "rope" and target_kind == "platform":
        transition_type = (
            SEGMENT_ROPE_TO_PLATFORM_LEFT
            if direction == "left"
            else SEGMENT_ROPE_TO_PLATFORM_RIGHT
        )
    elif source_kind == "platform" and target_kind == "platform":
        if down_frames and jump_frames:
            transition_type = SEGMENT_DOWN_JUMP
        elif jump_frames:
            transition_type = {
                "left": SEGMENT_PLATFORM_JUMP_LEFT,
                "right": SEGMENT_PLATFORM_JUMP_RIGHT,
                "neutral": SEGMENT_PLATFORM_JUMP_NEUTRAL,
            }[direction]
        else:
            transition_type = (
                SEGMENT_WALK_OFF_LEFT
                if direction == "left"
                else SEGMENT_WALK_OFF_RIGHT
            )
    else:
        transition_type = "{}_to_{}".format(source_kind, target_kind)
    signature = {
        "direction": direction,
        "left_frames": left_frames,
        "right_frames": right_frames,
        "up_frames": up_frames,
        "down_frames": down_frames,
        "jump_frames": jump_frames,
    }
    return transition_type, signature


def _build_v2_annotations(
    segments: List[Dict[str, object]],
    nodes: List[Dict[str, object]],
    route_graph: Optional[Dict[str, object]],
) -> List[Dict[str, object]]:
    """把单独录制的过渡段整理成可与试走片段比对的结构标注。"""
    platform_node_by_name = {
        str(node["name"]): str(node["id"])
        for node in nodes
        if node["kind"] == "platform"
    }
    transition_by_segment_id = {
        int(item["segment_id"]): item
        for item in (route_graph or {}).get("return_transitions", [])
    }
    annotations = []
    supported_types = set(RETURN_TRANSITION_SEGMENT_TYPES) | set(V2_TRANSITION_SEGMENT_TYPES)
    for segment in segments:
        segment_type = str(segment.get("type"))
        if segment_type not in supported_types:
            continue
        segment_id = int(segment["id"])
        start = [int(value) for value in segment["start"]]
        end = [int(value) for value in segment["end"]]
        graph_transition = transition_by_segment_id.get(segment_id)
        source_node_id = None
        target_node_id = None
        if graph_transition is not None:
            source_node_id = platform_node_by_name.get(
                str(graph_transition.get("from_platform_name"))
            )
            target_node_id = platform_node_by_name.get(
                str(graph_transition.get("to_platform_name"))
            )
        recorded_from = segment.get("recorded_from_platform_id")
        recorded_to = segment.get("recorded_to_platform_id")
        if source_node_id is None and recorded_from is not None:
            source_node_id = platform_node_by_name.get(str(recorded_from))
        if target_node_id is None and recorded_to is not None:
            target_node_id = platform_node_by_name.get(str(recorded_to))
        if source_node_id is None:
            source_node_id = _label_v2_position(tuple(start), nodes, loose=True)
        if target_node_id is None:
            target_node_id = _label_v2_position(tuple(end), nodes, loose=True)
        annotations.append(
            {
                "segment_id": segment_id,
                "type": segment_type,
                "source_node": source_node_id,
                "target_node": target_node_id,
                "start": start,
                "end": end,
            }
        )
    return annotations


def build_v2_recording_graph(
    trial_frames: List[Dict[str, object]],
    segments: List[Dict[str, object]],
    route_graph: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """融合试走轨迹和结构标注，生成有向物理图及保留回头路的有序计划。"""
    nodes = _build_v2_nodes(segments)
    if not nodes:
        raise ValueError("V2融合前至少需要录制一个平台或绳子节点")
    node_by_id = {str(node["id"]): node for node in nodes}
    frame_labels = [
        _label_v2_position(
            (int(frame["x"]), int(frame["y"])),
            nodes,
            _v2_frame_key_state(frame),
        )
        for frame in trial_frames
    ]
    visits = _stable_v2_node_runs(frame_labels)
    if len(visits) < 2:
        raise ValueError("V2试走没有识别到至少两个稳定节点，请确认平台/绳子标注覆盖试走轨迹")
    annotations = _build_v2_annotations(segments, nodes, route_graph)
    used_annotation_ids = set()
    retry_samples = []
    edge_by_key: Dict[Tuple[object, ...], Dict[str, object]] = {}
    edges: List[Dict[str, object]] = []
    ordered_steps: List[str] = []

    for source_visit, target_visit in zip(visits, visits[1:]):
        source_node_id = str(source_visit["node_id"])
        target_node_id = str(target_visit["node_id"])
        start_index = int(source_visit["end_frame"])
        end_index = int(target_visit["start_frame"])
        transition_frames = trial_frames[start_index:end_index + 1]
        if not transition_frames:
            continue
        if source_node_id == target_node_id:
            transition_type, signature = _v2_transition_signature(
                transition_frames,
                node_by_id[source_node_id],
                node_by_id[target_node_id],
            )
            retry_samples.append(
                {
                    "node_id": source_node_id,
                    "type": transition_type,
                    "start_frame": start_index,
                    "end_frame": end_index,
                    "key_signature": signature,
                }
            )
            continue

        transition_type, signature = _v2_transition_signature(
            transition_frames,
            node_by_id[source_node_id],
            node_by_id[target_node_id],
        )
        departure = [
            int(trial_frames[start_index]["x"]),
            int(trial_frames[start_index]["y"]),
        ]
        arrival = [
            int(trial_frames[end_index]["x"]),
            int(trial_frames[end_index]["y"]),
        ]
        candidates = []
        for annotation in annotations:
            if str(annotation["type"]) != transition_type:
                continue
            if annotation.get("source_node") not in (None, source_node_id):
                continue
            if annotation.get("target_node") not in (None, target_node_id):
                continue
            score = _manhattan_distance(tuple(departure), tuple(annotation["start"]))
            score += _manhattan_distance(tuple(arrival), tuple(annotation["end"]))
            candidates.append((score, int(annotation["segment_id"]), annotation))
        candidates.sort(key=lambda item: (item[0], item[1]))
        annotation = candidates[0][2] if candidates else None
        annotation_id = int(annotation["segment_id"]) if annotation else None
        if annotation_id is not None:
            used_annotation_ids.add(annotation_id)
        edge_key = (
            source_node_id,
            target_node_id,
            transition_type,
            annotation_id if annotation_id is not None else "auto",
        )
        edge = edge_by_key.get(edge_key)
        elapsed_start = int(transition_frames[0].get("elapsed_ms", 0))
        elapsed_end = int(transition_frames[-1].get("elapsed_ms", elapsed_start))
        duration_ms = max(0, elapsed_end - elapsed_start)
        if edge is None:
            edge = {
                "id": "E{}".format(len(edges) + 1),
                "source": source_node_id,
                "target": target_node_id,
                "type": transition_type,
                "directed": True,
                "trigger": departure,
                "arrival": arrival,
                "annotation_segment_id": annotation_id,
                "sample_count": 0,
                "duration_ms_samples": [],
                "key_signature": signature,
            }
            edge_by_key[edge_key] = edge
            edges.append(edge)
        edge["sample_count"] = int(edge["sample_count"]) + 1
        edge["duration_ms_samples"].append(duration_ms)
        ordered_steps.append(str(edge["id"]))

    for edge in edges:
        durations = [int(value) for value in edge.pop("duration_ms_samples", [])]
        edge["calibrated_duration_ms"] = (
            int(round(sum(durations) / len(durations))) if durations else 0
        )
    if not ordered_steps:
        raise ValueError("V2试走没有生成有效的跨节点过渡")
    first_node = str(visits[0]["node_id"])
    final_node = str(visits[-1]["node_id"])
    closed_loop = first_node == "P1" and final_node == "P1"
    unmatched_annotation_ids = sorted(
        int(annotation["segment_id"])
        for annotation in annotations
        if int(annotation["segment_id"]) not in used_annotation_ids
    )
    unlabelled_frames = sum(label is None for label in frame_labels)
    return {
        "nodes": nodes,
        "edges": edges,
        "route_plans": [
            {
                "id": "main_loop",
                "start_node": first_node,
                "final_node": final_node,
                "closed_loop": closed_loop,
                "steps": ordered_steps,
            }
        ],
        "visits": visits,
        "frame_labels": frame_labels,
        "calibration": {
            "retry_samples": retry_samples,
            "unmatched_annotation_segment_ids": unmatched_annotation_ids,
            "trial_frame_count": len(trial_frames),
            "stable_visit_count": len(visits),
            "unlabelled_frame_count": unlabelled_frames,
            "unlabelled_frame_ratio": round(
                unlabelled_frames / max(1, len(trial_frames)),
                4,
            ),
        },
    }


def build_v2_compatibility_points(
    trial_frames: List[Dict[str, object]],
    frame_labels: List[Optional[str]],
    nodes: List[Dict[str, object]],
) -> List[Dict[str, object]]:
    """把V2逐帧试走压缩成旧播放器可直接读取的points。"""
    node_by_id = {str(node["id"]): node for node in nodes}
    points: List[Dict[str, object]] = []
    jump_was_pressed = False
    previous_signature = None
    for frame_index, frame in enumerate(trial_frames):
        keys = _v2_frame_key_state(frame)
        command, jump_was_pressed = _command_from_key_state(
            keys,
            jump_was_pressed,
            SEGMENT_TRIAL_WALK_V2,
        )
        node_id = frame_labels[frame_index] if frame_index < len(frame_labels) else None
        node = node_by_id.get(str(node_id)) if node_id is not None else None
        horizontal, vertical, action = command.split()
        rope_entry_node = None
        if action == "jump":
            for future_label in frame_labels[frame_index + 1:frame_index + 13]:
                future_node = (
                    node_by_id.get(str(future_label))
                    if future_label is not None
                    else None
                )
                if future_node is not None and future_node["kind"] == "platform":
                    if node_id is not None and str(future_node["id"]) == str(node_id):
                        continue
                    break
                if future_node is not None and future_node["kind"] == "rope":
                    rope_entry_node = future_node
                    break
        if rope_entry_node is not None:
            node = rope_entry_node
            node_id = str(node["id"])
            rope_x = int(node["geometry"]["x"])
            horizontal = "right" if int(frame["x"]) < rope_x else "left"
            command = "{} up jump".format(horizontal)
            vertical = "up"
            segment_type = "rope_entry"
        else:
            segment_type = str(node["kind"]) if node is not None else "v2_transition"
        kind = (
            "jump"
            if action == "jump"
            else "rope"
            if segment_type == "rope"
            else "platform"
            if segment_type == "platform"
            else "transition"
        )
        point = {
            "x": int(frame["x"]),
            "y": int(frame["y"]),
            "command": command,
            "kind": kind,
            "elapsed_ms": int(frame.get("elapsed_ms", 0)),
            "segment_id": 0,
            "segment_type": segment_type,
            "v2_node_id": node_id,
            "trial_frame_index": frame_index,
        }
        if node is not None and node["kind"] == "platform":
            point["platform_id"] = str(node["name"])
        if node is not None and node["kind"] == "rope":
            geometry = node["geometry"]
            point.update(
                {
                    "rope_x": int(geometry["x"]),
                    "rope_top_y": int(geometry["top_y"]),
                    "rope_bottom_y": int(geometry["bottom_y"]),
                }
            )
        signature = (
            int(point["x"]),
            int(point["y"]),
            str(point["command"]),
            str(point["segment_type"]),
            str(point.get("v2_node_id")),
        )
        if signature == previous_signature:
            continue
        points.append(point)
        previous_signature = signature
    return points


def _copy_route_point(
    point: Dict[str, object],
    command: Optional[str] = None,
    synthetic: bool = False,
    route_pass: Optional[str] = None,
) -> Dict[str, object]:
    """复制一个轨迹点，并可覆盖回放命令和合成标记。"""
    result = dict(point)
    if command is not None:
        result["command"] = command
        result["kind"] = _point_kind(command, str(point.get("segment_type", SEGMENT_PLATFORM)))
    result["synthetic"] = bool(synthetic)
    if route_pass is not None:
        result["route_pass"] = str(route_pass)
    return result


def _reverse_platform_command(command: str) -> str:
    """把平台命令的左右方向反转，供闭环返程使用。"""
    horizontal, vertical, action = command.split()
    if horizontal == "left":
        horizontal = "right"
    elif horizontal == "right":
        horizontal = "left"
    return "{} {} {}".format(horizontal, vertical, action)


def _oriented_platform_points(
    points: List[Dict[str, object]],
    incoming_rope_x: Optional[int],
    outgoing_rope_x: Optional[int],
) -> List[Dict[str, object]]:
    """自动选择平台正放或反放，使两端尽量靠近相邻绳子。"""
    if len(points) < 2:
        return [_copy_route_point(point) for point in points]

    def orientation_score(start_x: int, end_x: int):
        """优先让起点靠近落点、终点靠近下一连接，再比较总距离。"""
        incoming_distance = (
            abs(start_x - incoming_rope_x)
            if incoming_rope_x is not None
            else 0
        )
        outgoing_distance = (
            abs(end_x - outgoing_rope_x)
            if outgoing_rope_x is not None
            else 0
        )
        return (
            incoming_distance + outgoing_distance,
            incoming_distance,
            outgoing_distance,
        )

    first_x = int(points[0]["x"])
    last_x = int(points[-1]["x"])
    forward_score = orientation_score(first_x, last_x)
    reverse_score = orientation_score(last_x, first_x)
    if forward_score <= reverse_score:
        return [_copy_route_point(point) for point in points]
    return [
        _copy_route_point(
            point,
            command=_reverse_platform_command(str(point["command"])),
            synthetic=True,
        )
        for point in reversed(points)
    ]


def _platform_sweep_points(
    points: List[Dict[str, object]],
    incoming_x: Optional[int],
    outgoing_x: Optional[int],
) -> List[Dict[str, object]]:
    """从真实落点接入平台，完整覆盖后再连续走到下一连接。

    绳顶和下跳落点经常位于平台中部，而原始平台段只记录了从一端走到另一端
    的单向轨迹。若直接把平台端点接在连接后面，会生成 ``88 -> 125`` 这类
    硬断点；播放器随即触发全局重定位，并可能串到同一物理平台的下行副本。
    这里使用平台已有坐标生成两段无损合成路径：先从 incoming_x 附近走到
    选定扫描起点，完整扫描平台，再从扫描终点折返到 outgoing_x 附近。
    """
    oriented = _oriented_platform_points(points, incoming_x, outgoing_x)
    if len(oriented) < 2:
        return oriented

    result: List[Dict[str, object]] = []
    if incoming_x is not None:
        incoming_index = min(
            range(len(oriented)),
            key=lambda index: (
                abs(int(oriented[index]["x"]) - int(incoming_x)),
                index,
            ),
        )
        # reversed(oriented[:incoming_index + 1]) 从真实落点附近走向平台扫描
        # 起点。最后一个点会由主扫描再次加入，因此这里排除它避免零距离重复。
        incoming_connector = list(reversed(oriented[:incoming_index + 1]))
        result.extend(
            _copy_route_point(
                point,
                command=_reverse_platform_command(str(point["command"])),
                synthetic=True,
                route_pass="incoming_connection_to_platform_sweep",
            )
            for point in incoming_connector[:-1]
        )

    result.extend(oriented)
    if outgoing_x is None:
        return result

    outgoing_index = min(
        range(len(oriented)),
        key=lambda index: (
            abs(int(oriented[index]["x"]) - int(outgoing_x)),
            -index,
        ),
    )
    if outgoing_index >= len(oriented) - 1:
        return result

    # 从完整扫描的终点沿原路径折返到下一绳子/下跳点附近。首点与主扫描
    # 终点重复，排除后仍保留目标附近的最后一点，使相邻段间距保持在1～2px。
    outgoing_connector = list(reversed(oriented[outgoing_index:]))
    result.extend(
        _copy_route_point(
            point,
            command=_reverse_platform_command(str(point["command"])),
            synthetic=True,
            route_pass="return_to_outgoing_connection",
        )
        for point in outgoing_connector[1:]
    )
    return result


def _platform_approach_direction(platform_points: List[Dict[str, object]]) -> Optional[str]:
    """读取平台末段实际行进方向，用于选择绳子左右起跳规则。"""
    for point in reversed(platform_points):
        horizontal, _vertical, _action = str(point["command"]).split()
        if horizontal in ("left", "right"):
            return horizontal
    return None


def _rope_entry_rule_name(
    connection: Dict[str, object],
    platform_points: List[Dict[str, object]],
) -> str:
    """根据接近绳子的行进方向选择同侧起跳，避免走过绳子再折返。"""
    approach_direction = _platform_approach_direction(platform_points)
    if approach_direction == "left":
        return "left_jump"
    if approach_direction == "right":
        return "right_jump"
    rope_x = int(connection["rope_x"])
    last_x = int(platform_points[-1]["x"]) if platform_points else rope_x - 1
    return "right_jump" if last_x <= rope_x else "left_jump"


def _trim_platform_to_rope_entry(
    platform_points: List[Dict[str, object]],
    connection: Dict[str, object],
) -> List[Dict[str, object]]:
    """完整覆盖平台后回到绳子起跳侧，禁止预览范围与实际播放范围不一致。"""
    if not platform_points:
        return []
    rule = connection[_rope_entry_rule_name(connection, platform_points)]
    minimum_x, maximum_x = [int(value) for value in rule["x_range"]]
    target_x = int(round((minimum_x + maximum_x) / 2.0))
    selected_index = min(
        range(len(platform_points)),
        key=lambda index: (
            abs(int(platform_points[index]["x"]) - target_x),
            -index,
        ),
    )
    if selected_index >= len(platform_points) - 1:
        return list(platform_points[:selected_index + 1])

    # 绳子位于平台中段时，首次经过起跳带不能立即截断。否则原始录制和路线
    # 预览仍显示完整平台，实际播放却会永久丢掉绳子另一侧（例如 P1 的
    # x=26~52）。先走完剩余平台，再沿原路径自然折返回起跳带。
    return list(platform_points) + [
        _copy_route_point(
            point,
            command=_reverse_platform_command(str(point["command"])),
            synthetic=True,
            route_pass="return_to_rope_entry_after_full_sweep",
        )
        for point in reversed(platform_points[selected_index:-1])
    ]


def _oriented_rope_points(
    points: List[Dict[str, object]],
    direction: str,
) -> List[Dict[str, object]]:
    """把绳子点按向上或向下顺序排列并写入对应命令。"""
    ordered = sorted(
        points,
        key=lambda point: int(point["y"]),
        reverse=(direction == "up"),
    )
    command = "none {} none".format(direction)
    return [_copy_route_point(point, command=command, synthetic=True) for point in ordered]


def _synthetic_rope_entry(
    connection: Dict[str, object],
    platform_points: List[Dict[str, object]],
) -> Dict[str, object]:
    """按平台接近绳子的实际方向生成入口点并附带完整绳子几何。"""
    rope_x = int(connection["rope_x"])
    last_x = int(platform_points[-1]["x"]) if platform_points else rope_x - 1
    rule_name = _rope_entry_rule_name(connection, platform_points)
    rule = connection[rule_name]
    minimum_x, maximum_x = [int(value) for value in rule["x_range"]]
    entry_x = min(max(last_x, minimum_x), maximum_x)
    entry_y = int(
        connection.get(
            "lower_entry_y",
            (connection.get("bottom_point") or [rope_x, connection["lower_platform_y"]])[1],
        )
    )
    return {
        "x": entry_x,
        "y": entry_y,
        "command": str(rule["command"]),
        "kind": "jump",
        "elapsed_ms": 0,
        "segment_id": int(connection["rope_segment_id"]),
        "segment_type": "rope_entry",
        "synthetic": True,
        "entry_rule": rule_name,
        "rope_x": rope_x,
        "rope_top_y": int(connection["top_point"][1]),
        "rope_bottom_y": int(connection["bottom_point"][1]),
        "entry_x_range": [minimum_x, maximum_x],
        "entry_y_range": [int(value) for value in rule["y_range"]],
    }


def _synthetic_rope_exit(connection: Dict[str, object]) -> Dict[str, object]:
    """在绳顶生成释放上键并横向走上平台的离绳点。"""
    rule = connection["top_exit"]
    exit_x, exit_y = [int(value) for value in rule["position"]]
    return {
        "x": exit_x,
        "y": exit_y,
        "command": str(rule["command"]),
        "kind": "rope_exit",
        "elapsed_ms": 0,
        "segment_id": int(connection["rope_segment_id"]),
        "segment_type": "rope_exit",
        "synthetic": True,
        "exit_direction": str(rule["direction"]),
    }


def _ascent_route_chain(route_graph: Dict[str, object]):
    """返回从最下层平台开始的上行平台链和绳子链。"""
    platforms = {int(item["id"]): item for item in route_graph["platforms"]}
    connections = list(route_graph.get("connections", []))
    if not connections:
        return [], []
    lower_map = {int(item["lower_platform_id"]): item for item in connections}
    connected_platform_ids = {
        int(item["lower_platform_id"]) for item in connections
    } | {
        int(item["upper_platform_id"]) for item in connections
    }
    start_platform_id = max(
        connected_platform_ids,
        key=lambda platform_id: int(platforms[platform_id]["representative_y"]),
    )
    platform_chain = [start_platform_id]
    rope_chain: List[Dict[str, object]] = []
    visited_platforms = {start_platform_id}
    current_platform_id = start_platform_id
    while current_platform_id in lower_map:
        connection = lower_map[current_platform_id]
        upper_platform_id = int(connection["upper_platform_id"])
        if upper_platform_id in visited_platforms:
            break
        rope_chain.append(connection)
        platform_chain.append(upper_platform_id)
        visited_platforms.add(upper_platform_id)
        current_platform_id = upper_platform_id
    return platform_chain, rope_chain


def _complete_return_transition_chains(route_graph: Dict[str, object]):
    """组合最高平台到最低平台的完整已录制返程分支。"""
    platform_chain, _rope_chain = _ascent_route_chain(route_graph)
    if len(platform_chain) < 2:
        return []
    platforms = {
        int(item["id"]): item
        for item in route_graph.get("platforms", [])
    }
    start_platform_id = int(platform_chain[0])
    top_platform_id = int(platform_chain[-1])
    transitions_by_source: Dict[int, List[Dict[str, object]]] = {}
    for transition in route_graph.get("return_transitions", []):
        source_id = int(transition["from_platform_id"])
        source = platforms.get(source_id)
        if source is None:
            continue
        start_x, start_y = [int(value) for value in transition["start"]]
        source_x_min, source_x_max = [int(value) for value in source["x_range"]]
        source_x_gap = max(source_x_min - start_x, start_x - source_x_max, 0)
        if (
            abs(int(source["representative_y"]) - start_y)
            > ROPE_UPPER_PLATFORM_Y_TOLERANCE
            or source_x_gap > ROPE_PLATFORM_X_TOLERANCE
        ):
            continue
        transitions_by_source.setdefault(
            source_id,
            [],
        ).append(transition)

    completed: List[List[Dict[str, object]]] = []

    def visit(platform_id: int, chain, visited) -> None:
        if platform_id == start_platform_id:
            completed.append(list(chain))
            return
        transitions = transitions_by_source.get(platform_id, [])
        if not transitions:
            # 绳子只用于向上爬。返程缺少左/右走出、下跳或平台跳时，这条
            # 分支不是闭环，不能自行拿已有绳子合成下降路线。
            return
        for transition in transitions:
            target_id = int(transition["to_platform_id"])
            if target_id in visited:
                continue
            visit(
                target_id,
                chain + [transition],
                visited | {target_id},
            )

    visit(top_platform_id, [], {top_platform_id})
    return completed


def _assemble_playback_points(
    raw_points: List[Dict[str, object]],
    segments: List[Dict[str, object]],
    route_graph: Dict[str, object],
    return_transitions: Optional[List[Dict[str, object]]] = None,
) -> List[Dict[str, object]]:
    """按平台连接图组装一条包含完整上行和逐层回程的闭环路线。"""
    grouped = _group_points_by_segment(raw_points)
    connections = list(route_graph.get("connections", []))
    if not connections:
        result = []
        for segment in sorted(segments, key=lambda item: int(item["order"])):
            result.extend(_copy_route_point(point) for point in grouped[int(segment["id"])])
        return result

    platform_chain, rope_chain = _ascent_route_chain(route_graph)
    return_chain = list(return_transitions or [])
    first_return = return_chain[0] if return_chain else None
    result: List[Dict[str, object]] = []
    ascent_platform_points: Dict[int, List[Dict[str, object]]] = {}
    for index, platform_id in enumerate(platform_chain):
        incoming_rope_x = (
            int(rope_chain[index - 1]["rope_x"])
            if index > 0
            else (
                int(return_chain[-1]["end"][0])
                if return_chain
                else None
            )
        )
        outgoing_rope_x = (
            int(rope_chain[index]["rope_x"])
            if index < len(rope_chain)
            else None
        )
        if (
            outgoing_rope_x is None
            and first_return is not None
            and int(first_return["from_platform_id"]) == platform_id
        ):
            outgoing_rope_x = int(first_return["start"][0])
        platform_points = _platform_sweep_points(
            grouped.get(platform_id, []),
            incoming_rope_x,
            outgoing_rope_x,
        )
        if index < len(rope_chain):
            platform_points = _trim_platform_to_rope_entry(
                platform_points,
                rope_chain[index],
            )
        ascent_platform_points[platform_id] = platform_points
        result.extend(platform_points)
        if index >= len(rope_chain):
            continue
        connection = rope_chain[index]
        rope_points = grouped.get(int(connection["rope_segment_id"]), [])
        result.append(_synthetic_rope_entry(connection, platform_points))
        result.extend(_oriented_rope_points(rope_points, "up"))
        result.append(_synthetic_rope_exit(connection))

    if return_chain:
        for transition_index, transition in enumerate(return_chain):
            transition_points = grouped.get(int(transition["segment_id"]), [])
            result.extend(_copy_route_point(point) for point in transition_points)
            if transition_index + 1 >= len(return_chain):
                continue
            next_transition = return_chain[transition_index + 1]
            landed_platform_id = int(transition["to_platform_id"])
            landing_x = int(transition["end"][0])
            next_start_x = int(next_transition["start"][0])
            middle_points = _platform_sweep_points(
                grouped.get(landed_platform_id, []),
                landing_x,
                next_start_x,
            )
            result.extend(
                _copy_route_point(
                    point,
                    synthetic=True,
                    # _platform_sweep_points 在落点与下一连接位于同侧时会生成
                    # “覆盖另一端→折返回连接”的两遍坐标。保留其内部通行阶段，
                    # 否则去重会删掉折返半程，形成平台左端直跳右侧连接的大间距。
                    route_pass="return_transition_{}_{}".format(
                        transition_index + 1,
                        str(point.get("route_pass", "primary")),
                    ),
                )
                for point in middle_points
            )
        return result

    # 没有完整的已录制返程时只保留上行路线，不能倒爬绳子伪造闭环。
    return result


def _build_route_variants(
    raw_points: List[Dict[str, object]],
    segments: List[Dict[str, object]],
    route_graph: Dict[str, object],
) -> List[Dict[str, object]]:
    """为多平台生成“完整上行 + 逐层回程”的智能闭环变体。"""
    transition_chains = _complete_return_transition_chains(route_graph)
    if not transition_chains:
        return [
            {
                "name": "ascent_only_incomplete_return",
                "probability": 100,
                "transition_segment_id": None,
                "transition_segment_ids": [],
                "closed_loop": False,
                "points": _assemble_playback_points(
                    raw_points,
                    segments,
                    route_graph,
                ),
            }
        ]

    variants = []
    for chain_index, transition_chain in enumerate(transition_chains):
        segment_ids = [int(item["segment_id"]) for item in transition_chain]
        chain_weight = 1
        for transition in transition_chain:
            chain_weight *= max(0, int(transition.get("probability", 0)))
        variants.append(
            {
                "name": "return_chain_{}_{}".format(
                    chain_index + 1,
                    "_".join(str(segment_id) for segment_id in segment_ids),
                ),
                "probability": chain_weight,
                "transition_segment_id": segment_ids[0] if segment_ids else None,
                "transition_segment_ids": segment_ids,
                "closed_loop": True,
                "points": _assemble_playback_points(
                    raw_points,
                    segments,
                    route_graph,
                    return_transitions=transition_chain,
                ),
            }
        )
    positive_total = sum(max(0, int(item["probability"])) for item in variants)
    if positive_total <= 0:
        equal_probability = int(round(100 / len(variants)))
        for item in variants:
            item["probability"] = equal_probability
    elif positive_total != 100:
        assigned_total = 0
        for index, item in enumerate(variants):
            if index == len(variants) - 1:
                normalized_probability = 100 - assigned_total
            else:
                normalized_probability = int(round(
                    max(0, int(item["probability"])) * 100 / positive_total
                ))
                assigned_total += normalized_probability
            item["probability"] = max(0, normalized_probability)
    return variants


def build_route_variants_for_playback(
    raw_points: List[Dict[str, object]],
    segments: List[Dict[str, object]],
    route_graph: Dict[str, object],
) -> List[Dict[str, object]]:
    """供录制保存和运行时旧JSON兼容共同使用的路线变体生成入口。"""
    variants = _build_route_variants(raw_points, segments, route_graph)
    for variant in variants:
        variant["points"] = _deduplicate_recorded_points(
            list(variant.get("points") or [])
        )
    return variants


def _validate_generated_rope_entries(route_variants: List[Dict[str, object]]) -> None:
    """校验每条生成路线的绳子入口几何和起跳方向，阻止保存坏JSON。"""
    for variant in route_variants:
        for point in variant.get("points", []):
            if str(point.get("segment_type")) != "rope_entry":
                continue
            required_fields = ("rope_x", "rope_top_y", "rope_bottom_y")
            missing_fields = [field for field in required_fields if point.get(field) is None]
            if missing_fields:
                raise ValueError(
                    "路线变体{}的绳子入口缺少几何字段：{}".format(
                        variant.get("name", "unknown"),
                        ",".join(missing_fields),
                    )
                )
            rope_x = int(point["rope_x"])
            entry_x = int(point["x"])
            horizontal, vertical, action = str(point["command"]).split()
            expected_horizontal = "right" if entry_x < rope_x else "left"
            if (
                horizontal != expected_horizontal
                or vertical != "up"
                or action != "jump"
            ):
                raise ValueError(
                    "路线变体{}的绳子入口方向错误：entry_x={}，rope_x={}，command={}".format(
                        variant.get("name", "unknown"),
                        entry_x,
                        rope_x,
                        point["command"],
                    )
                )


def _preview_color(point: Dict[str, object]) -> Tuple[int, int, int]:
    """返回左红、右蓝、跳绿、绳子黄的BGR预览颜色。"""
    command = str(point["command"])
    horizontal, vertical, action = command.split()
    if action == "jump":
        return 0, 255, 0
    if vertical in ("up", "down") or str(point.get("segment_type")) == SEGMENT_ROPE:
        return 0, 210, 255
    if horizontal == "left":
        return 0, 0, 255
    if horizontal == "right":
        return 255, 0, 0
    return 100, 100, 100


def _write_route_preview(
    route_path: Path,
    raw_points: List[Dict[str, object]],
    route_graph: Dict[str, object],
) -> Path:
    """绘制最后录制路线：左红、右蓝、跳跃绿色、绳子黄色。"""
    import cv2
    import numpy as np

    preview = np.zeros(
        (MINIMAP_MONITOR["height"], MINIMAP_MONITOR["width"], 3),
        dtype=np.uint8,
    )
    grouped = _group_points_by_segment(raw_points)
    for segment_points in grouped.values():
        previous_local: Optional[Tuple[int, int]] = None
        for point in segment_points:
            local = (
                int(point["x"]) - MINIMAP_MONITOR["left"],
                int(point["y"]) - MINIMAP_MONITOR["top"],
            )
            color = _preview_color(point)
            if previous_local is not None and _manhattan_distance(previous_local, local) <= 12:
                cv2.line(preview, previous_local, local, color, 2)
            radius = 3 if str(point["kind"]) == "jump" else 2
            cv2.circle(preview, local, radius, color, -1)
            previous_local = local

    # 自动推导的左右跳区用绿色点显示，便于人工确认绳子入口是否覆盖正确。
    for connection in route_graph["connections"]:
        y = int(
            connection.get("lower_entry_y", connection["lower_platform_y"])
        ) - MINIMAP_MONITOR["top"]
        for rule_name in ("right_jump", "left_jump"):
            minimum_x, maximum_x = connection[rule_name]["x_range"]
            for x in range(int(minimum_x), int(maximum_x) + 1):
                cv2.circle(
                    preview,
                    (x - MINIMAP_MONITOR["left"], y),
                    3,
                    (0, 255, 0),
                    -1,
                )

        # 绳子上下端使用更大的圆点和T/B标签，便于确认端点与平台的Y差。
        rope_number = int(connection["rope_segment_id"])
        for endpoint_name, suffix, color in (
            ("top_point", "T", (255, 255, 0)),
            ("bottom_point", "B", (0, 165, 255)),
        ):
            endpoint_x, endpoint_y = [
                int(value) for value in connection[endpoint_name]
            ]
            local_endpoint = (
                endpoint_x - MINIMAP_MONITOR["left"],
                endpoint_y - MINIMAP_MONITOR["top"],
            )
            cv2.circle(preview, local_endpoint, 5, color, 1)
            cv2.putText(
                preview,
                "R{}{}".format(rope_number, suffix),
                (local_endpoint[0] + 5, max(10, local_endpoint[1] - 3)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                color,
                1,
                cv2.LINE_AA,
            )

        exit_rule = connection.get("top_exit") or {}
        exit_direction = str(exit_rule.get("direction", "right"))
        exit_x, exit_y = [
            int(value) for value in exit_rule.get("position", connection["top_point"])
        ]
        exit_start = (
            exit_x - MINIMAP_MONITOR["left"],
            exit_y - MINIMAP_MONITOR["top"],
        )
        exit_offset = 10 if exit_direction == "right" else -10
        cv2.arrowedLine(
            preview,
            exit_start,
            (exit_start[0] + exit_offset, exit_start[1]),
            (255, 80, 255),
            2,
            tipLength=0.35,
        )

    # 每个平台按用户选择的“平台1、平台2……”编号标注，汇总页可直接对照。
    for platform in route_graph["platforms"]:
        minimum_x, maximum_x = [int(value) for value in platform["x_range"]]
        platform_y = int(platform["representative_y"])
        local_left = minimum_x - MINIMAP_MONITOR["left"]
        local_right = maximum_x - MINIMAP_MONITOR["left"]
        local_y = platform_y - MINIMAP_MONITOR["top"]
        cv2.rectangle(
            preview,
            (local_left, local_y - 3),
            (local_right, local_y + 3),
            (210, 210, 210),
            1,
        )
        platform_name = str(platform.get("platform_id", platform["id"]))
        label = "P{}".format("".join(character for character in platform_name if character.isdigit()) or platform["id"])
        cv2.putText(
            preview,
            label,
            (local_left, max(10, local_y - 7)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    output_path = mushroom_v3_preview_path(route_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded, buffer = cv2.imencode(".png", preview)
    if not encoded:
        raise OSError("无法编码蘑菇V3路线预览图")
    buffer.tofile(str(output_path))
    return output_path


class RouteRecorderService(QObject):
    """在独立线程中以30Hz分段记录平台和绳子。"""

    state_changed = pyqtSignal(str, str)
    progress_changed = pyqtSignal(dict)
    route_saved = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, project_root: Path):
        """初始化保存路径、线程状态和当前分段信息。"""
        super().__init__()
        self.project_root = Path(project_root)
        self.route_path = default_mushroom_v3_route_path(self.project_root)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._recording_enabled = threading.Event()
        self._save_when_stopped = True
        self._active_segment_type: Optional[str] = None
        self._active_platform_id: Optional[str] = None
        self._active_segment_id = 0
        self._segment_finished = True
        self._discarded_platform_ids = set()
        self._transition_probabilities = {
            SEGMENT_DOWN_JUMP: 34,
            SEGMENT_WALK_OFF_LEFT: 33,
            SEGMENT_WALK_OFF_RIGHT: 33,
        }
        self._rest_point: Optional[Dict[str, object]] = None
        self._session_schema_version = 1
        self._trial_walk_frame_count = 0
        self._trial_walk_duration_ms = 0
        # 加载已有JSON后允许只补录一个过渡段。录制线程会先以旧文件中的
        # raw_points/trial_walk为种子，再追加本次分段；保存失败前不会覆盖旧文件。
        self._session_seed_points: List[Dict[str, object]] = []
        self._session_seed_trial_frames: List[Dict[str, object]] = []

    @property
    def is_running(self) -> bool:
        """返回分段录制会话是否仍在运行。"""
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    @property
    def is_paused(self) -> bool:
        """返回录制会话是否等待下一段开始。"""
        return self.is_running and not self._recording_enabled.is_set()

    @property
    def active_segment_type(self) -> Optional[str]:
        """返回当前正在录制的平台或绳子类型。"""
        with self._lock:
            return self._active_segment_type

    @property
    def has_trial_walk_v2(self) -> bool:
        """返回当前会话是否已采集到V2试走帧。"""
        with self._lock:
            return self._session_schema_version == 2 and self._trial_walk_frame_count > 0

    @property
    def trial_walk_frame_count(self) -> int:
        """返回当前V2试走的有效坐标帧数。"""
        with self._lock:
            return int(self._trial_walk_frame_count)

    def start_trial_walk_v2(self) -> bool:
        """启动V2完整试走；结束后线程保持暂停，继续接收结构分段。"""
        if self.is_running:
            raise RuntimeError("当前已有路线录制会话，V2试走必须作为第一步")
        with self._lock:
            self._stop_event = threading.Event()
            self._recording_enabled = threading.Event()
            self._save_when_stopped = True
            self._discarded_platform_ids = set()
            self._session_schema_version = 2
            self._trial_walk_frame_count = 0
            self._trial_walk_duration_ms = 0
            self._session_seed_points = []
            self._session_seed_trial_frames = []
            self._active_segment_id = 0
            self._active_segment_type = SEGMENT_TRIAL_WALK_V2
            self._active_platform_id = None
            self._segment_finished = False
            self._worker = threading.Thread(
                target=self._run,
                name="mushroom-v3-v2-recorder",
                daemon=True,
            )
            worker = self._worker
        self.state_changed.emit(
            "trial_recording",
            "V2试走录制中：从平台1出发，按实际刷图顺序完整走一圈并回到平台1；完成后点击“结束试走V2”。",
        )
        self._recording_enabled.set()
        worker.start()
        return True

    def finish_trial_walk_v2(self) -> None:
        """结束逐帧试走但保留会话，等待平台、绳子和过渡结构标注。"""
        if not self.is_running or self.active_segment_type != SEGMENT_TRIAL_WALK_V2:
            return
        self._recording_enabled.clear()
        with self._lock:
            self._segment_finished = True
            frame_count = int(self._trial_walk_frame_count)
            duration_ms = int(self._trial_walk_duration_ms)
        self.state_changed.emit(
            "trial_ready",
            "V2试走已结束：{}帧，约{:.1f}秒。现在录制平台、绳子和各类过渡点，最后按F7保存融合。".format(
                frame_count,
                duration_ms / 1000.0,
            ),
        )

    def start(
        self,
        segment_type: str = SEGMENT_PLATFORM,
        platform_id: Optional[str] = None,
    ) -> bool:
        """兼容旧入口并开始指定类型的第一段。"""
        return self.start_segment(segment_type, platform_id=platform_id)

    def set_transition_probabilities(
        self,
        down_jump: int,
        right_walk_off: int,
        left_walk_off: int = 0,
    ) -> None:
        """设置下跳、向左和向右走出平台的分支权重。"""
        with self._lock:
            self._transition_probabilities = {
                SEGMENT_DOWN_JUMP: max(0, min(100, int(down_jump))),
                SEGMENT_WALK_OFF_LEFT: max(0, min(100, int(left_walk_off))),
                SEGMENT_WALK_OFF_RIGHT: max(0, min(100, int(right_walk_off))),
            }

    def set_route_path(self, route_path: Path) -> None:
        """在未录制时切换新建或加载的路线JSON目标路径。"""
        if self.is_running:
            raise RuntimeError("路线录制进行中，不能切换路线文件")
        path = Path(route_path)
        if path.suffix.lower() != ".json":
            raise ValueError("路线文件必须使用.json扩展名")
        self.route_path = path
        self._rest_point = read_route_rest_point(path)

    def _read_existing_recording_seed(
        self,
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], int]:
        """读取已有路线的结构坐标，供单独补录连接段时无损追加。"""
        path = Path(self.route_path)
        if not path.is_file():
            return [], [], 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("已有路线JSON无法读取：{}".format(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("已有路线JSON根节点必须是对象")

        raw_points = data.get("raw_points")
        if not isinstance(raw_points, list) or not raw_points:
            # 兼容早期只保存points的路线；复制后再追加，不直接修改读取对象。
            raw_points = data.get("points")
        seed_points = [
            dict(point)
            for point in (raw_points or [])
            if isinstance(point, dict)
        ]
        trial_walk = data.get("trial_walk")
        trial_frames = (
            trial_walk.get("frames")
            if isinstance(trial_walk, dict)
            else None
        )
        seed_trial_frames = [
            dict(frame)
            for frame in (trial_frames or [])
            if isinstance(frame, dict)
        ]
        schema_version = 2 if int(data.get("schema_version", 1) or 1) == 2 else 1
        if schema_version == 2 and not seed_trial_frames:
            # 没有原始试走帧便无法重新融合V2图，按结构化V1安全追加。
            schema_version = 1
        return seed_points, seed_trial_frames, schema_version

    def set_rest_point(self, rest_point: Optional[Dict[str, object]]) -> None:
        """更新当前路线会话要在后续保存时继续保留的休息点。"""
        self._rest_point = dict(rest_point) if rest_point is not None else None

    def discard_platform(self, platform_id: str) -> None:
        """标记删除的平台，使保存时丢弃其平台段和回程标记。"""
        with self._lock:
            self._discarded_platform_ids.add(str(platform_id))

    def start_segment(
        self,
        segment_type: str,
        platform_id: Optional[str] = None,
    ) -> bool:
        """开始平台段或绳子段；可按任意顺序反复添加。"""
        if segment_type not in VALID_SEGMENT_TYPES:
            raise ValueError("未知路线分段类型：{}".format(segment_type))
        if segment_type == SEGMENT_PLATFORM and not platform_id:
            raise ValueError("录制平台段前必须选择平台编号")
        if segment_type in RETURN_TRANSITION_SEGMENT_TYPES and not platform_id:
            raise ValueError("录制返回连接前必须选择起点平台编号")

        start_worker = False
        seed_points: List[Dict[str, object]] = []
        seed_trial_frames: List[Dict[str, object]] = []
        seed_schema_version = 1
        if not self.is_running:
            seed_points, seed_trial_frames, seed_schema_version = (
                self._read_existing_recording_seed()
            )
        with self._lock:
            running = self._worker is not None and self._worker.is_alive()
            if not running:
                self._stop_event = threading.Event()
                self._recording_enabled = threading.Event()
                self._save_when_stopped = True
                self._discarded_platform_ids = set()
                self._session_schema_version = seed_schema_version
                self._session_seed_points = seed_points
                self._session_seed_trial_frames = seed_trial_frames
                self._trial_walk_frame_count = len(seed_trial_frames)
                self._trial_walk_duration_ms = int(
                    seed_trial_frames[-1].get("elapsed_ms", 0)
                ) if seed_trial_frames else 0
                existing_segment_ids = []
                for point in seed_points:
                    try:
                        existing_segment_ids.append(int(point.get("segment_id")))
                    except (TypeError, ValueError):
                        continue
                self._active_segment_id = max(existing_segment_ids, default=0) + 1
                self._active_segment_type = segment_type
                self._active_platform_id = platform_id
                self._segment_finished = False
                self._worker = threading.Thread(
                    target=self._run,
                    name="mushroom-v3-segment-recorder",
                    daemon=True,
                )
                start_worker = True
            else:
                self._active_segment_id += 1
                self._active_segment_type = segment_type
                self._active_platform_id = platform_id
                self._segment_finished = False
            if segment_type == SEGMENT_PLATFORM and platform_id:
                self._discarded_platform_ids.discard(str(platform_id))
            segment_id = self._active_segment_id

        labels = {
            SEGMENT_PLATFORM: "平台",
            SEGMENT_ROPE: "绳子",
            SEGMENT_DOWN_JUMP: "下跳回程",
            SEGMENT_RIGHT_RETURN: "旧版向右回程",
            SEGMENT_WALK_OFF_LEFT: "向左走出平台",
            SEGMENT_WALK_OFF_RIGHT: "向右走出平台",
            SEGMENT_PLATFORM_JUMP_LEFT: "平台向左跳",
            SEGMENT_PLATFORM_JUMP_RIGHT: "平台向右跳",
            SEGMENT_PLATFORM_JUMP_NEUTRAL: "平台原地跳",
            SEGMENT_PLATFORM_TO_ROPE_LEFT: "平台向左跳上绳",
            SEGMENT_PLATFORM_TO_ROPE_RIGHT: "平台向右跳上绳",
            SEGMENT_ROPE_TO_PLATFORM_LEFT: "绳子向左跳到平台",
            SEGMENT_ROPE_TO_PLATFORM_RIGHT: "绳子向右跳到平台",
        }
        label = labels[segment_type]
        if segment_type == SEGMENT_PLATFORM:
            label = "{}（{}）".format(label, platform_id)
        self.state_changed.emit(
            "recording",
            (
                "正在录制{}段#{} · 落到已录制的下层平台后自动结束"
                .format(label, segment_id)
                if segment_type in WALK_OFF_SEGMENT_TYPES
                else (
                    "正在标记{}#{}；识别到坐标后自动结束。".format(
                        label,
                        segment_id,
                    )
                    if segment_type == SEGMENT_RIGHT_RETURN
                    else "正在录制{}段#{} · F6结束当前段 · F7保存整条路线".format(
                        label,
                        segment_id,
                    )
                )
            ),
        )
        self._recording_enabled.set()
        if start_worker:
            self._worker.start()
        return True

    def finish_segment(self) -> None:
        """结束当前分段但保留会话，允许继续录制任意下一段。"""
        if not self.is_running:
            return
        self._recording_enabled.clear()
        with self._lock:
            self._segment_finished = True
            segment_id = self._active_segment_id
        self.state_changed.emit(
            "segment_ready",
            "分段#{}已结束，可继续录制平台段或绳子段，顺序不限。".format(segment_id),
        )

    def toggle_pause(self) -> None:
        """兼容旧F6入口并结束当前分段。"""
        self.finish_segment()

    def stop(self, save: bool = True) -> None:
        """结束整个录制会话，并按需保存JSON和预览图。"""
        if not self.is_running:
            return
        self._save_when_stopped = bool(save)
        self._stop_event.set()

    def _current_segment(self) -> Tuple[int, Optional[str], Optional[str]]:
        """线程安全地读取当前分段编号、类型和平台编号。"""
        with self._lock:
            return (
                self._active_segment_id,
                self._active_segment_type,
                self._active_platform_id,
            )

    def _run(self) -> None:
        """捕获黄色人物点，并在停止时分析平台与绳子的空间连接。"""
        keyboard_module = None
        hotkey_handles = []
        with self._lock:
            points = [dict(point) for point in self._session_seed_points]
            trial_frames = [
                dict(frame) for frame in self._session_seed_trial_frames
            ]
        started_at = time.monotonic()
        trial_started_at = started_at
        jump_was_pressed = False
        last_segment_id = 0
        missing_frames = 0
        segment_point_counts: Dict[int, int] = {}
        last_point_by_segment: Dict[int, Dict[str, object]] = {}
        walk_off_states: Dict[int, Dict[str, object]] = {}
        platform_recording_states: Dict[int, Dict[str, object]] = {}
        try:
            import cv2
            import keyboard
            import mss
            import numpy as np

            from .. import legacy_engine

            keyboard_module = keyboard
            if not legacy_engine.prepare_game_window(capture_mode=True):
                raise RuntimeError("没有找到游戏窗口，无法开始路线录制")
            hotkey_handles = [
                keyboard.add_hotkey("f7", lambda: self.stop(save=True), suppress=False),
            ]
            print("[路线录制] 分段录制已启动：平台段和绳子段可按任意顺序添加。")

            with mss.mss() as capture:
                while not self._stop_event.is_set():
                    frame_started_at = time.monotonic()
                    if not self._recording_enabled.is_set():
                        time.sleep(0.03)
                        continue

                    segment_id, segment_type, platform_id = self._current_segment()
                    if (
                        segment_type not in VALID_SEGMENT_TYPES
                        and segment_type != SEGMENT_TRIAL_WALK_V2
                    ):
                        time.sleep(0.03)
                        continue
                    if segment_id != last_segment_id:
                        jump_was_pressed = False
                        last_segment_id = segment_id

                    screenshot = capture.grab(MINIMAP_MONITOR)
                    frame = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)
                    yellow_center = find_yellow_center(cv2, np, frame)
                    if yellow_center is None:
                        missing_frames += 1
                    else:
                        missing_frames = 0
                        position = (
                            MINIMAP_MONITOR["left"] + int(yellow_center[0]),
                            MINIMAP_MONITOR["top"] + int(yellow_center[1]),
                        )
                        key_state = _read_pressed_keys(keyboard)
                        if segment_type == SEGMENT_TRIAL_WALK_V2:
                            elapsed_ms = int(
                                round((time.monotonic() - trial_started_at) * 1000)
                            )
                            command, jump_was_pressed = _command_from_key_state(
                                key_state,
                                jump_was_pressed,
                                SEGMENT_TRIAL_WALK_V2,
                            )
                            trial_frame = {
                                "frame_index": len(trial_frames),
                                "x": position[0],
                                "y": position[1],
                                "elapsed_ms": elapsed_ms,
                                "keys": dict(key_state),
                                "command": command,
                            }
                            trial_frames.append(trial_frame)
                            with self._lock:
                                self._trial_walk_frame_count = len(trial_frames)
                                self._trial_walk_duration_ms = elapsed_ms
                            self.progress_changed.emit(
                                {
                                    "recording_mode": SEGMENT_TRIAL_WALK_V2,
                                    "trial_frames": len(trial_frames),
                                    "trial_duration_ms": elapsed_ms,
                                    "position": position,
                                    "command": command,
                                    "fps": ROUTE_RECORD_FPS,
                                    "missing_frames": missing_frames,
                                }
                            )
                        else:
                            command, jump_was_pressed = _command_from_key_state(
                                key_state,
                                jump_was_pressed,
                                segment_type,
                            )
                            kind = _point_kind(command, segment_type)
                            if segment_type == SEGMENT_PLATFORM:
                                platform_observation = _observe_platform_recording_position(
                                    platform_recording_states.setdefault(segment_id, {}),
                                    position,
                                )
                                if platform_observation == "outlier":
                                    # 小地图黄色点瞬移到其他图标，不写入平台轨迹。
                                    kind = "idle"
                                elif platform_observation == "departed":
                                    kind = "idle"
                                    self._recording_enabled.clear()
                                    with self._lock:
                                        self._segment_finished = True
                                    baseline_y = int(
                                        platform_recording_states[segment_id]["baseline_y"]
                                    )
                                    surface_y = int(
                                        platform_recording_states[segment_id].get(
                                            "surface_y",
                                            baseline_y,
                                        )
                                    )
                                    self.state_changed.emit(
                                        "segment_ready",
                                        "检测到人物连续下坠并掉离{}（起始Y={}，当前斜坡局部Y={}），"
                                        "平台段#{}已在最后可信坐标自动结束。".format(
                                            platform_id or "当前平台",
                                            baseline_y,
                                            surface_y,
                                            segment_id,
                                        ),
                                    )
                            elapsed_ms = int(round((time.monotonic() - started_at) * 1000))
                            previous = last_point_by_segment.get(segment_id)
                            if segment_type == SEGMENT_ROPE and previous is None:
                                # 先保存人物尚未按上移动时的静止绳底，避免30Hz首帧
                                # 捕获时人物已经向上移动数像素而丢失真实入口坐标。
                                command = "none up none"
                                kind = "rope"
                            should_append = kind != "idle" and previous is None
                            if kind != "idle" and previous is not None:
                                previous_position = (int(previous["x"]), int(previous["y"]))
                                moved = _manhattan_distance(previous_position, position)
                                command_changed = str(previous["command"]) != command
                                should_append = (
                                    moved >= ROUTE_POINT_MIN_DISTANCE
                                    or command_changed
                                    or kind == "jump"
                                )
                            if should_append:
                                point = {
                                    "x": position[0],
                                    "y": position[1],
                                    "command": command,
                                    "kind": kind,
                                    "elapsed_ms": elapsed_ms,
                                    "segment_id": segment_id,
                                    "segment_type": segment_type,
                                    "platform_id": platform_id,
                                }
                                points.append(point)
                                last_point_by_segment[segment_id] = point
                                segment_point_counts[segment_id] = (
                                    segment_point_counts.get(segment_id, 0) + 1
                                )
                                self.progress_changed.emit(
                                    {
                                        "recording_mode": "structural_segment",
                                        "points": len(points),
                                        "segment_points": segment_point_counts[segment_id],
                                        "segment_id": segment_id,
                                        "segment_type": segment_type,
                                        "platform_id": platform_id,
                                        "position": position,
                                        "command": command,
                                        "kind": kind,
                                        "fps": ROUTE_RECORD_FPS,
                                        "missing_frames": missing_frames,
                                    }
                                )
                                if segment_type == SEGMENT_RIGHT_RETURN:
                                    self._recording_enabled.clear()
                                    with self._lock:
                                        self._segment_finished = True
                                    self.state_changed.emit(
                                        "segment_ready",
                                        "向右回程点已标记为{}；运行时到达该点会持续向右走。".format(
                                            position
                                        ),
                                    )
                            if segment_type in WALK_OFF_SEGMENT_TYPES:
                                landing_platform_id = _observe_walk_off_landing(
                                    walk_off_states.setdefault(segment_id, {}),
                                    points,
                                    platform_id,
                                    position,
                                )
                                if landing_platform_id is not None:
                                    self._recording_enabled.clear()
                                    with self._lock:
                                        self._segment_finished = True
                                    direction_text = (
                                        "左"
                                        if segment_type == SEGMENT_WALK_OFF_LEFT
                                        else "右"
                                    )
                                    self.state_changed.emit(
                                        "segment_ready",
                                        "向{}走出平台已录制完成：{} → {}，"
                                        "实际落点={}。".format(
                                            direction_text,
                                            platform_id or "未知平台",
                                            landing_platform_id,
                                            position,
                                        ),
                                    )

                    frame_elapsed = time.monotonic() - frame_started_at
                    if frame_elapsed < ROUTE_RECORD_INTERVAL_SECONDS:
                        time.sleep(ROUTE_RECORD_INTERVAL_SECONDS - frame_elapsed)

            if not self._save_when_stopped:
                self.state_changed.emit("idle", "分段路线录制已取消")
                print("[路线录制] 已取消，本次没有覆盖路线文件。")
                return
            with self._lock:
                discarded_platform_ids = set(self._discarded_platform_ids)
                session_schema_version = int(self._session_schema_version)
            points = _filter_discarded_platform_points(
                points,
                discarded_platform_ids,
            )
            points, platform_cleanup = _sanitize_recorded_platform_points(points)
            points = _deduplicate_recorded_points(points)
            if len(points) < 2:
                raise ValueError("有效路线坐标不足2个，请确认小地图黄色人物点可见")

            segments = _build_segment_summaries(points)
            recording_warnings: List[Dict[str, object]] = []
            for warning_code, validator in (
                ("platform_geometry", _validate_platform_geometry),
                ("platform_ids", _validate_platform_ids),
            ):
                try:
                    validator(segments)
                except ValueError as exc:
                    recording_warnings.append(
                        {
                            "code": warning_code,
                            "severity": "warning",
                            "segment_ids": [],
                            "message": str(exc),
                        }
                    )
            platform_count = sum(segment["type"] == SEGMENT_PLATFORM for segment in segments)
            rope_count = sum(segment["type"] == SEGMENT_ROPE for segment in segments)
            transition_count = sum(
                segment["type"] in (
                    set(RETURN_TRANSITION_SEGMENT_TYPES)
                    | set(V2_TRANSITION_SEGMENT_TYPES)
                )
                for segment in segments
            )
            if platform_count < 1:
                raise ValueError("至少需要录制一个平台段")
            with self._lock:
                transition_probabilities = dict(self._transition_probabilities)
            route_graph = _build_route_graph(
                segments,
                transition_probabilities,
                raw_points=points,
            )
            is_v2 = session_schema_version == 2
            if is_v2 and len(trial_frames) < V2_STABLE_NODE_MIN_FRAMES * 2:
                raise ValueError("V2试走有效帧过少，请重新完整试走后再保存")
            legacy_transition_count = sum(
                segment["type"] in RETURN_TRANSITION_SEGMENT_TYPES
                for segment in segments
            )
            recording_warnings.extend(
                _route_graph_recording_warnings(
                    route_graph,
                    is_v2=is_v2,
                    rope_count=rope_count,
                    legacy_transition_count=legacy_transition_count,
                )
            )
            v2_fusion = None
            saved_trial_frames = trial_frames
            if is_v2:
                v2_fusion = build_v2_recording_graph(
                    trial_frames,
                    segments,
                    route_graph=route_graph,
                )
                saved_trial_frames = [
                    {
                        **frame,
                        "node_id": v2_fusion["frame_labels"][index],
                    }
                    for index, frame in enumerate(trial_frames)
                ]
                playback_points = build_v2_compatibility_points(
                    trial_frames,
                    v2_fusion["frame_labels"],
                    v2_fusion["nodes"],
                )
                playback_points = _deduplicate_recorded_points(playback_points)
                route_variants = [
                    {
                        "name": "v2_trial_walk_compatibility",
                        "probability": 100,
                        "closed_loop": bool(
                            v2_fusion["route_plans"][0]["closed_loop"]
                        ),
                        "points": playback_points,
                    }
                ]
            else:
                route_variants = _build_route_variants(points, segments, route_graph)
                for route_variant in route_variants:
                    route_variant["points"] = _deduplicate_recorded_points(
                        list(route_variant.get("points") or [])
                    )
                try:
                    _validate_generated_rope_entries(route_variants)
                except ValueError as exc:
                    recording_warnings.append(
                        {
                            "code": "generated_rope_entry",
                            "severity": "warning",
                            "segment_ids": [],
                            "message": str(exc),
                        }
                    )
                route_variants.sort(
                    key=lambda item: int(item["probability"]),
                    reverse=True,
                )
                playback_points = list(route_variants[0]["points"])
            if len(playback_points) < 2:
                raise ValueError("分段路线无法生成有效回放轨迹")

            self.route_path.parent.mkdir(parents=True, exist_ok=True)
            route_data = {
                "version": 8 if is_v2 else 7,
                "schema_version": 2 if is_v2 else 1,
                "map_name": "自定义录制路线",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "coordinate_space": "screen_minimap_absolute",
                "record_fps": ROUTE_RECORD_FPS,
                "recording_mode": (
                    "trial_walk_fused_graph_v2" if is_v2 else "segmented_graph"
                ),
                "route_profile": (
                    "directed_recorded_plan_v2" if is_v2 else "strict_recorded"
                ),
                "coordinate_deduplicated": True,
                "rope_start_anchor_recorded": True,
                "rope_entry_offset_x": ROPE_ENTRY_OFFSET_X,
                "rope_bottom_is_not_jump_point": True,
                "right_return_mode": "legacy_single_marker_compatible",
                "walk_off_mode": "directional_recorded_landing",
                "minimap_monitor": dict(MINIMAP_MONITOR),
                "playback_mode": (
                    "v2_trial_walk_compatibility"
                    if is_v2
                    else "assembled_segment_loop"
                ),
                "movement_source": "json_only",
                "dynamic_rope_geometry": True,
                "chase_enabled": False,
                "recording_status": (
                    "needs_review" if recording_warnings else "ready"
                ),
                "recording_warnings": recording_warnings,
                "platform_recording_cleanup": {
                    str(segment_id): cleanup
                    for segment_id, cleanup in platform_cleanup.items()
                },
                "points": playback_points,
                "raw_points": points,
                "segments": segments,
                "route_graph": route_graph,
                "transition_probabilities": transition_probabilities,
                "route_variants": route_variants,
            }
            if v2_fusion is not None:
                route_data.update(
                    {
                        "trial_walk": {
                            "record_fps": ROUTE_RECORD_FPS,
                            "frame_count": len(saved_trial_frames),
                            "duration_ms": int(
                                saved_trial_frames[-1].get("elapsed_ms", 0)
                            ),
                            "frames": saved_trial_frames,
                        },
                        "nodes": v2_fusion["nodes"],
                        "edges": v2_fusion["edges"],
                        "route_plans": v2_fusion["route_plans"],
                        "calibration": v2_fusion["calibration"],
                        "v2_graph": {
                            "directed": True,
                            "nodes": v2_fusion["nodes"],
                            "edges": v2_fusion["edges"],
                        },
                    }
                )
            if self._rest_point is not None:
                route_data["rest_point"] = dict(self._rest_point)
            self.route_path.write_text(
                json.dumps(route_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            preview_path = _write_route_preview(self.route_path, points, route_graph)
            self.route_saved.emit(str(self.route_path), str(preview_path))
            self.state_changed.emit(
                "idle",
                (
                    "V2试走与结构标注已融合保存，可先用兼容points运行"
                    if is_v2
                    else (
                        "分段路线已保存，但有{}项连接/结构警告；坐标已完整保留，可稍后分析JSON。".format(
                            len(recording_warnings)
                        )
                        if recording_warnings
                        else "分段路线已保存，可选择“自定义录制路线”启动"
                    )
                ),
            )
            removed_platform_points = sum(
                int(cleanup["removed_points"])
                for cleanup in platform_cleanup.values()
            )
            if removed_platform_points:
                print(
                    "[路线录制] 保存前已自动清理{}个掉层或误识别的平台坐标。".format(
                        removed_platform_points
                    )
                )
            for warning in recording_warnings:
                print(
                    "[路线录制][已保存警告] {}".format(
                        warning.get("message", "未知录制警告")
                    )
                )
            print(
                "[路线录制] 已保存{}个平台段、{}个绳子段、{}个回程段、{}个路线变体：{}".format(
                    platform_count,
                    rope_count,
                    transition_count,
                    len(route_variants),
                    self.route_path,
                )
            )
        except Exception as exc:
            message = "{}: {}".format(type(exc).__name__, exc)
            self.failed.emit(message)
            self.state_changed.emit("error", "分段路线录制失败")
            print("[路线录制][错误] {}".format(message))
        finally:
            if keyboard_module is not None:
                for handle in hotkey_handles:
                    try:
                        keyboard_module.remove_hotkey(handle)
                    except Exception:
                        pass
            with self._lock:
                self._worker = None
                self._active_segment_type = None
                self._active_platform_id = None
                self._segment_finished = True
            self._recording_enabled.clear()


__all__ = (
    "MINIMAP_MONITOR",
    "ROUTE_RECORD_FPS",
    "ROUTE_RECORD_INTERVAL_SECONDS",
    "ROUTE_POINT_MIN_DISTANCE",
    "PLATFORM_RECORD_BASELINE_SAMPLE_COUNT",
    "PLATFORM_RECORD_MAX_FRAME_DISTANCE",
    "PLATFORM_RECORD_MIN_POINTS",
    "PLATFORM_RECORD_OFF_PLATFORM_FRAMES",
    "PLATFORM_RECORD_Y_TOLERANCE",
    "ROPE_BOTTOM_Y_FUZZY_TOLERANCE",
    "ROPE_ENTRY_OFFSET_X",
    "ROPE_LOWER_PLATFORM_Y_TOLERANCE",
    "ROPE_PLATFORM_X_TOLERANCE",
    "ROPE_PLATFORM_Y_TOLERANCE",
    "ROPE_TOP_Y_FUZZY_TOLERANCE",
    "ROPE_UPPER_PLATFORM_Y_TOLERANCE",
    "ROPE_X_FUZZY_TOLERANCE",
    "RouteRecorderService",
    "SEGMENT_TRIAL_WALK_V2",
    "SEGMENT_DOWN_JUMP",
    "SEGMENT_PLATFORM",
    "SEGMENT_PLATFORM_JUMP_LEFT",
    "SEGMENT_PLATFORM_JUMP_NEUTRAL",
    "SEGMENT_PLATFORM_JUMP_RIGHT",
    "SEGMENT_PLATFORM_TO_ROPE_LEFT",
    "SEGMENT_PLATFORM_TO_ROPE_RIGHT",
    "SEGMENT_RIGHT_RETURN",
    "SEGMENT_ROPE",
    "SEGMENT_ROPE_TO_PLATFORM_LEFT",
    "SEGMENT_ROPE_TO_PLATFORM_RIGHT",
    "SEGMENT_WALK_OFF_LEFT",
    "SEGMENT_WALK_OFF_RIGHT",
    "WALK_OFF_SEGMENT_TYPES",
    "VALID_SEGMENT_TYPES",
    "V2_TRANSITION_SEGMENT_TYPES",
    "build_v2_compatibility_points",
    "build_v2_recording_graph",
    "build_route_variants_for_playback",
    "default_mushroom_v3_route_path",
    "mushroom_v3_preview_path",
    "mushroom_v3_recordings_directory",
    "resolve_mushroom_v3_route_path",
)

"""按照分段路线图组装后的闭环轨迹运行蘑菇V3。"""

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..public import combat_actions as combat_logic
from ..public.recorded_route_behavior import rope_entry_target as common_rope_entry_target
from ..public.route_recording import resolve_mushroom_v3_route_path


ROUTE_LOOP_SECONDS = 0.02
ROUTE_LOOKAHEAD_POINTS = 24
ROUTE_BACKTRACK_POINTS = 3
ROUTE_RELOCALIZE_DISTANCE = 18
ROUTE_JUMP_TRIGGER_DISTANCE = 6
ROUTE_JUMP_LOOKAHEAD_POINTS = 12
ROUTE_JUMP_COOLDOWN_SECONDS = 0.28
ROUTE_JUMP_HOLD_SECONDS = 0.045
ROPE_ENTRY_OFFSET_X = 2
ROPE_ENTRY_TRIGGER_X = 0
ROPE_ENTRY_TRIGGER_Y = 4
ROPE_ENTRY_RECOVERY_X = 24
ROPE_ENTRY_RECOVERY_Y = 14
ROPE_ROUTE_COMMIT_X = 42
ROPE_ROUTE_COMMIT_Y = 16
ROPE_TRANSITION_LOOKAHEAD_POINTS = 72
ROPE_ENTRY_COMMIT_SECONDS = 0.75
ROPE_TOP_REACHED_TOLERANCE_Y = 2
ROUTE_STALL_RECOVERY_SECONDS = 0.9
ROUTE_STALL_RECOVERY_COOLDOWN_SECONDS = 1.2


@dataclass(frozen=True)
class MushroomV3RoutePoint:
    """保存录制路线中的一个有序小地图坐标和对应动作。"""

    x: int
    y: int
    horizontal: str
    vertical: str
    action: str
    kind: str
    segment_type: str
    rope_x: Optional[int] = None
    rope_top_y: Optional[int] = None
    rope_bottom_y: Optional[int] = None

    @property
    def position(self) -> Tuple[int, int]:
        """返回便于距离计算的小地图坐标元组。"""
        return self.x, self.y


@dataclass(frozen=True)
class MushroomV3RouteVariant:
    """保存一种从上层返回下层的完整闭环路线及其概率。"""

    name: str
    probability: int
    points: List[MushroomV3RoutePoint]


@dataclass(frozen=True)
class MushroomV3RoutePlan:
    """保存公共平台绳子逻辑和可按概率选择的回程变体。"""

    variants: List[MushroomV3RouteVariant]
    recording_mode: str


@dataclass
class MushroomV3State:
    """保存V3路线游标、按键状态以及复用的V2战斗状态。"""

    route_index: Optional[int] = None
    vertical_direction: Optional[str] = None
    last_jump_index: Optional[int] = None
    last_jump_at: float = 0.0
    rope_entry_retry_count: int = 0
    relocalize_count: int = 0
    last_relocalize_at: float = 0.0
    last_relocalized_index: Optional[int] = None
    active_variant_index: int = 0
    completed_cycles: int = 0
    last_rope_commit_log_at: float = 0.0
    last_rope_recovery_signature: Optional[tuple] = None
    rope_entry_direction: Optional[str] = None
    rope_entry_rope_x: Optional[int] = None
    rope_entry_top_y: Optional[int] = None
    rope_entry_bottom_y: Optional[int] = None
    rope_entry_started_at: float = 0.0
    route_progress_position: Optional[tuple] = None
    route_progress_at: float = 0.0
    last_route_stall_recovery_at: float = 0.0
    combat: combat_logic.RouteCombatState = None

    def __post_init__(self):
        """为每次V3运行创建独立的战斗状态，避免多个实例共享对象。"""
        if self.combat is None:
            self.combat = combat_logic.RouteCombatState()


def _route_path(runtime) -> Path:
    """返回源码运行和打包环境都可读取的蘑菇V3录制文件路径。"""
    project_root = Path(getattr(runtime, "PROJECT_ROOT", runtime.get_base_dir()))
    configured_path = getattr(runtime, "蘑菇V3路线文件", "蘑菇V3路线.json")
    return resolve_mushroom_v3_route_path(project_root, configured_path)


def _inject_missing_rope_exits(raw_points):
    """为旧路线的上爬终点补充同坐标横向离绳点。"""
    if not isinstance(raw_points, list):
        return raw_points
    expanded = []
    for index, raw_point in enumerate(raw_points):
        expanded.append(raw_point)
        if not isinstance(raw_point, dict) or index + 1 >= len(raw_points):
            continue
        if str(raw_point.get("segment_type")) != "rope":
            continue
        try:
            _horizontal, vertical, _action = str(raw_point["command"]).split()
        except (KeyError, ValueError):
            continue
        if vertical != "up":
            continue
        next_point = raw_points[index + 1]
        if not isinstance(next_point, dict):
            continue
        next_segment_type = str(next_point.get("segment_type"))
        if next_segment_type in ("rope", "rope_exit"):
            continue
        try:
            next_horizontal, _next_vertical, _next_action = str(
                next_point["command"]
            ).split()
        except (KeyError, ValueError):
            continue
        if next_horizontal not in ("left", "right"):
            continue
        rope_exit = dict(raw_point)
        rope_exit.update(
            {
                "command": "{} none none".format(next_horizontal),
                "kind": "rope_exit",
                "segment_type": "rope_exit",
                "synthetic": True,
                "legacy_injected": True,
            }
        )
        expanded.append(rope_exit)
    return expanded


def _median_coordinate(values):
    """返回一组整数坐标的中位值，供旧路线入口纠正使用。"""
    ordered = sorted(int(value) for value in values)
    if not ordered:
        raise ValueError("坐标集合不能为空")
    return ordered[len(ordered) // 2]


def _normalize_rope_entry_points(raw_points):
    """按绳子X、绳底Y和相邻平台Y纠正新旧JSON中的绳子入口。"""
    if not isinstance(raw_points, list):
        return raw_points
    normalized = [dict(point) if isinstance(point, dict) else point for point in raw_points]
    for index, raw_point in enumerate(normalized):
        if not isinstance(raw_point, dict):
            continue
        if str(raw_point.get("segment_type")) != "rope_entry":
            continue
        segment_id = raw_point.get("segment_id")
        rope_points = [
            point
            for point in normalized
            if isinstance(point, dict)
            and str(point.get("segment_type")) == "rope"
            and point.get("segment_id") == segment_id
        ]
        if not rope_points:
            continue
        rope_x = _median_coordinate(point["x"] for point in rope_points)
        rope_top_y = min(int(point["y"]) for point in rope_points)
        rope_bottom_y = max(int(point["y"]) for point in rope_points)
        platform_points = []
        previous_index = index - 1
        while previous_index >= 0:
            previous = normalized[previous_index]
            if not isinstance(previous, dict):
                break
            if str(previous.get("segment_type")) != "platform":
                break
            platform_points.append(previous)
            previous_index -= 1
        entry_y = int(raw_point["y"])
        if platform_points:
            minimum_x_delta = min(
                abs(int(point["x"]) - rope_x) for point in platform_points
            )
            nearby_points = [
                point
                for point in platform_points
                if abs(int(point["x"]) - rope_x) <= minimum_x_delta + 2
            ]
            if nearby_points:
                entry_y = _median_coordinate(point["y"] for point in nearby_points)
        horizontal, _vertical, _action = str(raw_point["command"]).split()
        if horizontal == "left":
            entry_x = rope_x + ROPE_ENTRY_OFFSET_X
        else:
            entry_x = rope_x - ROPE_ENTRY_OFFSET_X
            horizontal = "right"
        raw_point.update(
            {
                "x": entry_x,
                "y": entry_y,
                "command": "{} up jump".format(horizontal),
                "rope_x": rope_x,
                "rope_top_y": rope_top_y,
                "rope_bottom_y": rope_bottom_y,
                "runtime_normalized": True,
            }
        )
    return normalized


def _parse_route_points(raw_points, route_path, route_name):
    """把一个路线变体的JSON坐标校验并转换成回放点。"""
    raw_points = _inject_missing_rope_exits(raw_points)
    raw_points = _normalize_rope_entry_points(raw_points)
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError("{}没有足够的有效坐标：{}".format(route_name, route_path))
    points: List[MushroomV3RoutePoint] = []
    for index, raw_point in enumerate(raw_points):
        try:
            horizontal, vertical, action = str(raw_point["command"]).split()
            if horizontal not in ("left", "right", "none", "stop"):
                raise ValueError("未知水平动作")
            if vertical not in ("up", "down", "none", "stop"):
                raise ValueError("未知垂直动作")
            if action not in ("jump", "none"):
                raise ValueError("未知一次性动作")
            points.append(
                MushroomV3RoutePoint(
                    x=int(raw_point["x"]),
                    y=int(raw_point["y"]),
                    horizontal=horizontal,
                    vertical=vertical,
                    action=action,
                    kind=str(raw_point.get("kind", "unknown")),
                    segment_type=str(raw_point.get("segment_type", "unknown")),
                    rope_x=(
                        int(raw_point["rope_x"])
                        if raw_point.get("rope_x") is not None
                        else None
                    ),
                    rope_top_y=(
                        int(raw_point["rope_top_y"])
                        if raw_point.get("rope_top_y") is not None
                        else None
                    ),
                    rope_bottom_y=(
                        int(raw_point["rope_bottom_y"])
                        if raw_point.get("rope_bottom_y") is not None
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "{}第{}个坐标无效：{}".format(route_name, index, exc)
            ) from exc
    return points


def _load_recorded_route(runtime) -> MushroomV3RoutePlan:
    """读取分段路线计划，并兼容旧版单路线JSON。"""
    route_path = _route_path(runtime)
    if not route_path.exists():
        raise FileNotFoundError(
            "尚未录制蘑菇V3路线，请分别录制平台段和绳子段，"
            "完成后按F7保存：{}".format(route_path)
        )
    data = json.loads(route_path.read_text(encoding="utf-8"))
    variants: List[MushroomV3RouteVariant] = []
    raw_variants = data.get("route_variants")
    if isinstance(raw_variants, list):
        for index, raw_variant in enumerate(raw_variants):
            if not isinstance(raw_variant, dict):
                continue
            name = str(raw_variant.get("name", "variant_{}".format(index + 1)))
            points = _parse_route_points(
                raw_variant.get("points"),
                route_path,
                "路线变体{}".format(name),
            )
            variants.append(
                MushroomV3RouteVariant(
                    name=name,
                    probability=max(0, int(raw_variant.get("probability", 0))),
                    points=points,
                )
            )
    if not variants:
        variants.append(
            MushroomV3RouteVariant(
                name="legacy_ordered_loop",
                probability=100,
                points=_parse_route_points(
                    data.get("points"),
                    route_path,
                    "蘑菇V3路线",
                ),
            )
        )
    return MushroomV3RoutePlan(
        variants=variants,
        recording_mode=str(data.get("recording_mode", "legacy_ordered")),
    )


def _choose_variant_index(plan: MushroomV3RoutePlan) -> int:
    """按页面保存的下跳/向右概率选择下一圈路线。"""
    weights = [max(0, variant.probability) for variant in plan.variants]
    if sum(weights) <= 0:
        weights = [1 for _variant in plan.variants]
    return random.choices(range(len(plan.variants)), weights=weights, k=1)[0]


def _distance(first: Tuple[int, int], second: Tuple[int, int]) -> int:
    """计算两个小地图坐标之间的曼哈顿距离。"""
    return abs(first[0] - second[0]) + abs(first[1] - second[1])


def _cyclic_indices(start: int, count: int, total: int) -> List[int]:
    """返回从指定游标开始、允许跨路线末尾循环的一组索引。"""
    return [((start + offset) % total) for offset in range(max(0, count))]


def _nearest_index(
    points: List[MushroomV3RoutePoint],
    position: Tuple[int, int],
    candidates: List[int],
) -> Tuple[int, int]:
    """在候选索引中找离人物最近的路线点，并返回索引和距离。"""
    if not candidates:
        raise ValueError("路线候选点不能为空")
    best_index = candidates[0]
    best_distance = _distance(position, points[best_index].position)
    for index in candidates[1:]:
        current_distance = _distance(position, points[index].position)
        if current_distance <= best_distance:
            best_index = index
            best_distance = current_distance
    return best_index, best_distance


def _select_route_index(runtime, state, points, position):
    """优先沿当前有序路线向前匹配，偏离过远时才执行全路线重定位。"""
    total = len(points)
    if state.route_index is None:
        selected, distance = _nearest_index(points, position, list(range(total)))
        state.route_index = selected
        runtime.trace_event(
            "mushroom_v3_route_initialized",
            position=position,
            route_index=selected,
            distance=distance,
        )
        return selected

    local_start = (state.route_index - ROUTE_BACKTRACK_POINTS) % total
    local_candidates = _cyclic_indices(
        local_start,
        ROUTE_BACKTRACK_POINTS + ROUTE_LOOKAHEAD_POINTS,
        total,
    )
    selected, distance = _nearest_index(points, position, local_candidates)
    if distance > ROUTE_RELOCALIZE_DISTANCE:
        selected, distance = _nearest_index(points, position, list(range(total)))
        now = time.monotonic()
        should_log = (
            selected != state.last_relocalized_index
            or now - state.last_relocalize_at >= 1.0
        )
        if should_log:
            state.relocalize_count += 1
            state.last_relocalize_at = now
            state.last_relocalized_index = selected
            runtime.trace_event(
                "mushroom_v3_route_relocalized",
                position=position,
                route_index=selected,
                distance=distance,
                relocalize_count=state.relocalize_count,
            )
    if state.last_jump_index is not None:
        progressed = (selected - state.last_jump_index) % total
        if ROUTE_JUMP_LOOKAHEAD_POINTS < progressed < total - ROUTE_BACKTRACK_POINTS:
            # 已明显离开刚执行的跳点后解除索引锁；下一圈再次经过同一跳点时
            # 仍可正常触发，而短暂坐标回抖不会立即重复跳。
            state.last_jump_index = None
    state.route_index = selected
    return selected


def _select_jump_point(state, points, position, selected_index):
    """在当前路线点附近补查即将到达的跳跃点，避免高速移动时跨过单点标记。"""
    now = time.monotonic()
    if now - state.last_jump_at < ROUTE_JUMP_COOLDOWN_SECONDS:
        return None
    # 绳子入口按实际几何坐标全路线搜索，不能依赖当前路线索引。战斗追怪、
    # 从上层掉落或人物重定位都可能让索引暂时落在平台远端，但只要人物已经
    # 到达绳子X正负2和下层平台Y，就必须立即执行方向+C+上。
    for index, point in enumerate(points):
        if (
            point.segment_type != "rope_entry"
            or point.action != "jump"
            or index == state.last_jump_index
        ):
            continue
        target_x, _direction = _rope_entry_target(point, position[0])
        if (
            abs(position[0] - target_x) <= ROPE_ENTRY_TRIGGER_X
            and abs(position[1] - point.y) <= ROPE_ENTRY_TRIGGER_Y
        ):
            return index
    search_start = (selected_index - ROUTE_BACKTRACK_POINTS) % len(points)
    for index in _cyclic_indices(
        search_start,
        ROUTE_BACKTRACK_POINTS + ROUTE_JUMP_LOOKAHEAD_POINTS,
        len(points),
    ):
        point = points[index]
        if point.action != "jump" or index == state.last_jump_index:
            continue
        if point.segment_type == "rope_entry":
            continue
        if _distance(position, point.position) <= ROUTE_JUMP_TRIGGER_DISTANCE:
            return index
    return None


def _rope_entry_target(point, character_x):
    """调用公共绳子几何行为，返回动态入口坐标和起跳方向。"""
    return common_rope_entry_target(
        point,
        character_x,
        offset_x=ROPE_ENTRY_OFFSET_X,
    )


def _find_nearby_rope_entry(points, position, selected_index):
    """按实际坐标查找可用于错过起跳区后回头修正的绳子入口。"""
    candidates = []
    for index, point in enumerate(points):
        if point.segment_type != "rope_entry":
            continue
        if point.rope_bottom_y is not None and position[1] <= point.rope_bottom_y:
            continue
        target_x, _direction = _rope_entry_target(point, position[0])
        delta_x = abs(position[0] - target_x)
        delta_y = abs(position[1] - point.y)
        if delta_x > ROPE_ENTRY_RECOVERY_X or delta_y > ROPE_ENTRY_RECOVERY_Y:
            continue
        candidates.append((delta_x + delta_y, index, point, target_x))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], candidates[0][2], candidates[0][3]


def _is_near_rope_transition(points, position, selected_index):
    """判断人物是否接近绳子入口、绳身或绳顶离绳点。"""
    for point in points:
        if point.segment_type != "rope_entry":
            continue
        target_x, _direction = _rope_entry_target(point, position[0])
        if (
            abs(position[0] - target_x) <= ROPE_ROUTE_COMMIT_X
            and abs(position[1] - point.y) <= ROPE_ROUTE_COMMIT_Y
        ):
            return True
    search_start = (selected_index - ROUTE_BACKTRACK_POINTS) % len(points)
    for index in _cyclic_indices(
        search_start,
        ROUTE_BACKTRACK_POINTS + ROPE_TRANSITION_LOOKAHEAD_POINTS,
        len(points),
    ):
        point = points[index]
        delta_x = abs(position[0] - point.x)
        delta_y = abs(position[1] - point.y)
        if point.segment_type == "rope":
            if delta_x <= 10 and delta_y <= 12:
                return True
        elif point.segment_type == "rope_exit":
            if delta_x <= 12 and delta_y <= 10:
                return True
    return False


def _suppress_far_chase_near_rope(runtime, state, intent, position):
    """靠近绳子时取消无攻击目标的远距离追怪，保持路线方向稳定。"""
    combat_state = state.combat
    combat_state.chase_active = False
    combat_state.chase_direction = None
    combat_state.near_chase_pending_at = 0.0
    combat_state.near_chase_pending_direction = None
    combat_state.route_resume_pending_at = 0.0
    now = time.monotonic()
    if now - state.last_rope_commit_log_at < 0.8:
        return
    state.last_rope_commit_log_at = now
    runtime.trace_event(
        "mushroom_v3_rope_route_committed",
        position=position,
        suppressed_source=intent.source,
        target_direction=intent.target_direction,
        nearest_dx=intent.nearest_dx,
    )


def _apply_vertical(runtime, state, direction):
    """仅在录制路线的上下方向变化时切换绳子按键。"""
    if direction == state.vertical_direction:
        return
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("down")
    if direction in ("up", "down"):
        runtime.pydirectinput.keyDown(direction)
        state.vertical_direction = direction
    else:
        state.vertical_direction = None


def _apply_horizontal(runtime, state, direction, reason):
    """仅在录制路线的左右方向变化时切换持续移动键。"""
    combat_state = state.combat
    if direction in ("left", "right"):
        if combat_state.applied_direction != direction:
            runtime.切换持续移动(direction, reason=reason)
            combat_state.applied_direction = direction
        return
    if combat_state.applied_direction is not None:
        runtime.释放水平移动键(reason=reason)
        combat_state.applied_direction = None


def _clear_rope_entry_commit(state):
    """清空本次方向加跳跃加上的挂绳承诺状态。"""
    state.rope_entry_direction = None
    state.rope_entry_rope_x = None
    state.rope_entry_top_y = None
    state.rope_entry_bottom_y = None
    state.rope_entry_started_at = 0.0


def _apply_active_rope_entry(runtime, state, position):
    """跳起后持续执行方向和上键，接近绳子X后改为只按上避免反向拉走。"""
    if (
        state.rope_entry_direction not in ("left", "right")
        or state.rope_entry_rope_x is None
        or state.rope_entry_top_y is None
        or state.rope_entry_bottom_y is None
    ):
        return False
    rope_x = int(state.rope_entry_rope_x)
    top_y = int(state.rope_entry_top_y)
    bottom_y = int(state.rope_entry_bottom_y)
    elapsed = time.monotonic() - state.rope_entry_started_at
    if elapsed > ROPE_ENTRY_COMMIT_SECONDS and position[1] > bottom_y:
        failed_jump_index = state.last_jump_index
        state.last_jump_index = None
        state.last_jump_at = 0.0
        state.rope_entry_retry_count += 1
        _clear_rope_entry_commit(state)
        runtime.trace_event(
            "mushroom_v3_rope_entry_retry_rearmed",
            position=position,
            failed_jump_index=failed_jump_index,
            rope_x=rope_x,
            rope_top_y=top_y,
            rope_bottom_y=bottom_y,
            retry_count=state.rope_entry_retry_count,
            reason="entry_commit_timeout",
        )
        return False

    if position[1] <= bottom_y:
        if position[0] < rope_x - 1:
            horizontal = "right"
        elif position[0] > rope_x + 1:
            horizontal = "left"
        else:
            horizontal = "none"
    else:
        horizontal = state.rope_entry_direction

    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="rope_entry_commit",
    )
    _apply_horizontal(runtime, state, horizontal, reason="mushroom_v3_rope_entry")
    _apply_vertical(runtime, state, "up")
    if position[1] <= top_y + ROPE_TOP_REACHED_TOLERANCE_Y:
        state.rope_entry_retry_count = 0
        _clear_rope_entry_commit(state)
        runtime.trace_event(
            "mushroom_v3_rope_top_reached",
            position=position,
            rope_x=rope_x,
            rope_top_y=top_y,
            rope_bottom_y=bottom_y,
        )
    return True


def _try_recover_stalled_platform(runtime, state, point, position, horizontal):
    """平台无怪移动长期没有位移时重新按方向并跳跃一次脱困。"""
    if point.segment_type != "platform" or horizontal not in ("left", "right"):
        state.route_progress_position = position
        state.route_progress_at = time.monotonic()
        return False
    now = time.monotonic()
    previous = state.route_progress_position
    if previous is None or abs(int(position[0]) - int(previous[0])) >= 2:
        state.route_progress_position = position
        state.route_progress_at = now
        return False
    if state.route_progress_at <= 0:
        state.route_progress_at = now
        return False
    if now - state.route_progress_at < ROUTE_STALL_RECOVERY_SECONDS:
        return False
    if now - state.last_route_stall_recovery_at < ROUTE_STALL_RECOVERY_COOLDOWN_SECONDS:
        return False

    state.last_route_stall_recovery_at = now
    state.route_progress_at = now
    runtime.释放水平移动键(reason="mushroom_v3_platform_stall")
    state.combat.applied_direction = None
    _apply_horizontal(
        runtime,
        state,
        horizontal,
        reason="mushroom_v3_platform_stall_recovery",
    )
    runtime.pydirectinput.keyDown("c")
    completed = runtime.可中断等待(ROUTE_JUMP_HOLD_SECONDS, interval=0.01)
    runtime.pydirectinput.keyUp("c")
    runtime.trace_event(
        "mushroom_v3_platform_stall_recovery",
        position=position,
        direction=horizontal,
        route_index=state.route_index,
        completed=completed,
        stalled_ms=round(ROUTE_STALL_RECOVERY_SECONDS * 1000, 1),
    )
    return True


def _apply_recorded_route(runtime, state, points, position, selected_index):
    """执行人物附近录制点的持续方向，并对跳跃点只触发一次C键。"""
    if _apply_active_rope_entry(runtime, state, position):
        return
    point = points[selected_index]
    jump_index = _select_jump_point(state, points, position, selected_index)
    if jump_index is not None:
        point = points[jump_index]
        state.route_index = jump_index

    horizontal = point.horizontal
    vertical = point.vertical
    if jump_index is not None and point.segment_type == "rope_entry":
        _target_x, horizontal = _rope_entry_target(point, position[0])
        vertical = "up"
    if jump_index is None:
        recovery = _find_nearby_rope_entry(points, position, selected_index)
        if recovery is not None:
            entry_index, entry_point, target_x = recovery
            if position[0] < target_x:
                horizontal = "right"
            elif position[0] > target_x:
                horizontal = "left"
            else:
                # 已精确到达入口时等待跳跃冷却结束，不能继续把人物推离入口。
                horizontal = "none"
            vertical = "none"
            signature = (entry_index, horizontal)
            if signature != state.last_rope_recovery_signature:
                state.last_rope_recovery_signature = signature
                runtime.trace_event(
                    "mushroom_v3_rope_entry_recovery",
                    position=position,
                    route_index=selected_index,
                    entry_index=entry_index,
                    entry_position=(target_x, entry_point.y),
                    rope_x=entry_point.rope_x,
                    rope_bottom_y=entry_point.rope_bottom_y,
                    direction=horizontal,
                )
        else:
            state.last_rope_recovery_signature = None

    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="recorded_route",
    )
    _apply_horizontal(runtime, state, horizontal, reason="mushroom_v3_route")
    _apply_vertical(runtime, state, vertical)

    if (
        jump_index is None
        and _try_recover_stalled_platform(
            runtime,
            state,
            point,
            position,
            horizontal,
        )
    ):
        return
    if point.action != "jump" or jump_index is None:
        return
    if point.segment_type == "rope_entry":
        state.rope_entry_direction = horizontal
        state.rope_entry_rope_x = point.rope_x
        state.rope_entry_top_y = point.rope_top_y
        state.rope_entry_bottom_y = point.rope_bottom_y
        state.rope_entry_started_at = time.monotonic()
    runtime.pydirectinput.keyDown("c")
    completed = runtime.可中断等待(ROUTE_JUMP_HOLD_SECONDS, interval=0.01)
    runtime.pydirectinput.keyUp("c")
    if not completed:
        return
    state.last_jump_index = jump_index
    state.last_jump_at = time.monotonic()
    runtime.trace_event(
        "mushroom_v3_route_jump",
        position=position,
        route_index=jump_index,
        horizontal=horizontal,
        vertical=vertical,
        kind=point.kind,
        rope_x=point.rope_x,
        rope_top_y=point.rope_top_y,
        rope_bottom_y=point.rope_bottom_y,
    )


def _release_all_route_keys(runtime, state, reason):
    """释放V3可能持有的水平、垂直、跳跃和攻击按键。"""
    rope_entry_was_active = state.rope_entry_direction in ("left", "right")
    interrupted_jump_index = state.last_jump_index
    runtime.释放水平移动键(reason=reason)
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("down")
    runtime.pydirectinput.keyUp("c")
    runtime.释放攻击键()
    state.vertical_direction = None
    state.combat.applied_direction = None
    _clear_rope_entry_commit(state)
    if rope_entry_was_active and interrupted_jump_index is not None:
        state.last_jump_index = None
        state.last_jump_at = 0.0
        state.rope_entry_retry_count += 1
        runtime.trace_event(
            "mushroom_v3_rope_entry_retry_rearmed",
            position=runtime.读取人物位置(),
            failed_jump_index=interrupted_jump_index,
            retry_count=state.rope_entry_retry_count,
            reason=reason,
        )


def _switch_variant_after_cycle(
    runtime,
    plan,
    state,
    previous_index,
    selected_index,
    position,
):
    """路线游标从末尾回到开头时按概率选择下一圈回程方式。"""
    current_variant = plan.variants[state.active_variant_index]
    total = len(current_variant.points)
    wrapped = (
        previous_index is not None
        and previous_index >= max(1, int(total * 0.75))
        and selected_index <= max(1, int(total * 0.25))
    )
    if not wrapped:
        return current_variant.points, selected_index

    previous_variant_index = state.active_variant_index
    state.active_variant_index = _choose_variant_index(plan)
    state.completed_cycles += 1
    next_variant = plan.variants[state.active_variant_index]
    if state.active_variant_index != previous_variant_index:
        state.route_index = None
        state.last_jump_index = None
        state.last_relocalized_index = None
        selected_index = _select_route_index(
            runtime,
            state,
            next_variant.points,
            position,
        )
    runtime.trace_event(
        "mushroom_v3_route_variant_selected",
        completed_cycles=state.completed_cycles,
        previous_variant=plan.variants[previous_variant_index].name,
        selected_variant=next_variant.name,
        selected_probability=next_variant.probability,
        position=position,
    )
    return next_variant.points, selected_index


def run_mushroom_v3(runtime):
    """运行录制路线优先匹配、怪物战斗覆盖、无怪继续路线的蘑菇V3。"""
    plan = _load_recorded_route(runtime)
    state = MushroomV3State()
    state.active_variant_index = _choose_variant_index(plan)
    initial_variant = plan.variants[state.active_variant_index]
    runtime.trace_event(
        "mushroom_v3_route_started",
        route_path=str(_route_path(runtime)),
        route_points=len(initial_variant.points),
        route_variants=len(plan.variants),
        initial_variant=initial_variant.name,
        variant_probabilities={
            variant.name: variant.probability for variant in plan.variants
        },
        playback_mode="segmented_graph_loop",
        combat_pipeline="common_v2_optimized",
        route_record_fps=30,
    )
    print(
        "[蘑菇V3] 已加载{}种回程路线；每圈按概率选择，战斗优先。".format(
            len(plan.variants)
        )
    )
    if not combat_logic.wait_for_runtime_ready(runtime):
        return

    try:
        while not runtime.已请求停止():
            if not runtime.可中断等待(ROUTE_LOOP_SECONDS, interval=0.01):
                break
            position = runtime.读取人物位置()
            if position is None:
                _release_all_route_keys(runtime, state, "position_missing")
                continue

            active_variant = plan.variants[state.active_variant_index]
            points = active_variant.points

            runtime.记录人物位置(
                position,
                interval=0.2,
                route="mushroom_v3_recorded",
                route_index=state.route_index,
                route_points=len(points),
                route_variant=active_variant.name,
                completed_cycles=state.completed_cycles,
                phase=state.combat.phase,
            )
            if runtime.zant == 2:
                _release_all_route_keys(runtime, state, "combat_wait")
                runtime.等待战斗恢复()
                continue
            if runtime.攻击移动仍锁定():
                _release_all_route_keys(runtime, state, "move_lock")
                continue

            previous_index = state.route_index
            selected_index = _select_route_index(runtime, state, points, position)
            points, selected_index = _switch_variant_after_cycle(
                runtime,
                plan,
                state,
                previous_index,
                selected_index,
                position,
            )
            near_rope_transition = _is_near_rope_transition(
                points,
                position,
                selected_index,
            )
            snapshot = combat_logic.read_monster_snapshot(runtime)
            intent = combat_logic.build_action_intent(state.combat, snapshot)
            if intent.source != "route":
                suppress_far_chase = (
                    near_rope_transition
                    and intent.source in ("chase", "chase_hold")
                    and intent.attackable_count <= 0
                )
                if suppress_far_chase:
                    _suppress_far_chase_near_rope(
                        runtime,
                        state,
                        intent,
                        position,
                    )
                else:
                    _apply_vertical(runtime, state, "none")
                    decision_changed = combat_logic.trace_action_decision(
                        runtime,
                        state.combat,
                        intent,
                        position,
                    )
                    combat_logic.apply_action_intent(
                        runtime,
                        state.combat,
                        intent,
                        position,
                        decision_changed,
                    )
                    continue

            _apply_recorded_route(
                runtime,
                state,
                points,
                position,
                selected_index,
            )
    finally:
        combat_logic.finish_combat(runtime, state.combat)
        _release_all_route_keys(runtime, state, "route_stopped")
        runtime.trace_event("mushroom_v3_route_stopped")

"""为无怪巡逻增加低频、可中断的随机动作。"""

import random
import time


ROUTE_VARIATION_MIN_INTERVAL_SECONDS = 8.0
ROUTE_VARIATION_MAX_INTERVAL_SECONDS = 16.0
ROUTE_VARIATION_SAFE_MIN_X = 45
ROUTE_VARIATION_SAFE_MAX_X = 145
ROUTE_VARIATION_JUMP_WEIGHT = 0.58
ROUTE_VARIATION_TURN_SECONDS = 0.035
ROUTE_VARIATION_ATTACK_SECONDS = 0.055
ROUTE_VARIATION_JUMP_SECONDS = 0.045


def initialize_route_variation(state):
    """初始化下一次随机路线动作的时间。"""
    state.next_random_action_at = _next_action_at()


def _next_action_at(now=None):
    """返回带随机间隔的下一次动作时间。"""
    current = time.monotonic() if now is None else now
    return current + random.uniform(
        ROUTE_VARIATION_MIN_INTERVAL_SECONDS,
        ROUTE_VARIATION_MAX_INTERVAL_SECONDS,
    )


def _postpone_route_variation(state, seconds=1.5):
    """当前环境不安全时短暂推迟，而不是丢失本次随机动作。"""
    state.next_random_action_at = time.monotonic() + max(0.2, float(seconds))


def _is_safe_route_position(position):
    """判断人物是否远离绳子入口和左右地图边缘。"""
    if position is None:
        return False
    x, _y = position
    return ROUTE_VARIATION_SAFE_MIN_X <= x <= ROUTE_VARIATION_SAFE_MAX_X


def _tap_key(runtime, key, hold_seconds):
    """短按一次按键，并在中断或异常时保证释放。"""
    runtime.pydirectinput.keyDown(key)
    try:
        return runtime.可中断等待(hold_seconds, interval=0.01)
    finally:
        runtime.pydirectinput.keyUp(key)


def _perform_random_jump(runtime, state, position):
    """保持当前巡逻方向并执行一次短跳。"""
    latest_snapshot = runtime.读取追怪感知()
    if (
        runtime.zant != 0
        or runtime.攻击移动仍锁定()
        or int(latest_snapshot[2]) > 0
        or int(latest_snapshot[3]) > 0
        or state.combat_active
        or state.chase_active
        or state.locked_attack_direction in ("left", "right")
        or state.route_resume_pending_at > 0
    ):
        return False
    completed = _tap_key(runtime, "c", ROUTE_VARIATION_JUMP_SECONDS)
    if completed:
        runtime.trace_event(
            "mushroom_route_random_action",
            action="jump",
            position=position,
            route_direction=state.patrol_direction,
            action_count=state.random_action_count,
        )
    return completed


def _perform_turn_attack(runtime, state, position):
    """短暂回头攻击一次，再转回原巡逻方向。"""
    route_direction = state.patrol_direction
    reverse_direction = "left" if route_direction == "right" else "right"
    runtime.释放水平移动键(reason="mushroom_random_turn_attack")
    runtime.释放攻击键()
    state.applied_direction = None

    if not _tap_key(runtime, reverse_direction, ROUTE_VARIATION_TURN_SECONDS):
        return False
    if not _tap_key(runtime, runtime.单体按键, ROUTE_VARIATION_ATTACK_SECONDS):
        return False
    if not _tap_key(runtime, route_direction, ROUTE_VARIATION_TURN_SECONDS):
        return False

    runtime.trace_event(
        "mushroom_route_random_action",
        action="turn_attack",
        position=position,
        attack_direction=reverse_direction,
        resume_direction=route_direction,
        action_count=state.random_action_count,
    )
    return True


def try_route_variation(runtime, state, position, monster_snapshot):
    """仅在无怪且远离绳子和边界时尝试一次随机路线动作。"""
    now = time.monotonic()
    if now < state.next_random_action_at:
        return False
    if (
        runtime.zant != 0
        or runtime.攻击移动仍锁定()
        or monster_snapshot.chase_count > 0
        or monster_snapshot.attackable_count > 0
        or state.combat_active
        or state.chase_active
        or state.locked_attack_direction in ("left", "right")
    ):
        _postpone_route_variation(state, seconds=1.0)
        return False
    if not _is_safe_route_position(position):
        _postpone_route_variation(state)
        return False

    state.next_random_action_at = _next_action_at(now)
    state.random_action_count += 1
    if random.random() < ROUTE_VARIATION_JUMP_WEIGHT:
        state.last_random_action = "jump"
        return _perform_random_jump(runtime, state, position)

    state.last_random_action = "turn_attack"
    return _perform_turn_attack(runtime, state, position)

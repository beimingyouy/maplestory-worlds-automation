"""蘑菇地图V2的轻量路线调度入口。

怪物识别与攻击协调位于public目录，爬绳与脱困位于
mushroom_v2_rope.py；本文件只保留V2状态、地图边界和主循环。
"""

import time
from dataclasses import dataclass
from typing import Optional

from ..public.combat_actions import (
    RouteCombatState,
    V2_ATTACK_MOVE_LOCK_SECONDS,
    V2_ATTACK_RECHECK_SECONDS,
    V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE,
    V2_NEAR_CHASE_DEBOUNCE_SECONDS,
    V2_ROUTE_RESUME_DEBOUNCE_SECONDS,
    V2_TARGET_SNAPSHOT_TTL_SECONDS,
    apply_action_intent,
    build_action_intent,
    finish_combat,
    read_monster_snapshot,
    release_route_keys,
    set_route_phase,
    trace_action_decision,
    wait_for_runtime_ready,
)
from .mushroom_v2_rope import (
    RIGHT_BOUNDARY_X,
    ROPE_REARM_X,
    ROPE_REST_MIDDLE_Y,
    ROPE_RETRY_SECONDS,
    _enter_upper_platform,
    _is_in_rope_zone,
    _patrol_phase_name,
    _recover_from_suspended_rope,
    _rope_rest_duration_seconds,
    _rope_rest_interval_seconds,
    _try_climb_rope,
    _update_climb_arming,
    _update_platform_level,
    _update_rope_rest_schedule,
    _update_suspended_tracking,
)
from .mushroom_route_variation import (
    ROUTE_VARIATION_MAX_INTERVAL_SECONDS,
    ROUTE_VARIATION_MIN_INTERVAL_SECONDS,
    initialize_route_variation,
    try_route_variation,
)


LEFT_BOUNDARY_X = 25
RIGHT_EDGE_STALL_MIN_X = 150
RIGHT_EDGE_STALL_SECONDS = 0.8
ROUTE_LOOP_SECONDS = 0.02


@dataclass
class MushroomV2State(RouteCombatState):
    """保存蘑菇 V2 单次运行的巡逻、战斗和爬绳状态。"""

    last_climb_at: float = 0.0
    climb_armed: bool = True
    climb_deferred_for_monster: bool = False
    right_edge_last_x: Optional[int] = None
    right_edge_progress_at: float = 0.0
    next_rope_rest_at: float = 0.0
    rope_rest_pending: bool = False
    rope_rest_remaining_seconds: float = 0.0
    rope_rest_count: int = 0
    suspended_last_position: Optional[tuple] = None
    suspended_since: float = 0.0
    last_suspended_recovery_at: float = 0.0
    next_random_action_at: float = 0.0
    random_action_count: int = 0
    last_random_action: Optional[str] = None

def _update_patrol_boundary(runtime, state, position):
    """根据小地图边界更新方向，并在右侧持续原地不动时强制回头。"""
    x, _y = position
    if x <= LEFT_BOUNDARY_X and state.patrol_direction != "right":
        state.patrol_direction = "right"
        state.right_edge_last_x = x
        state.right_edge_progress_at = time.monotonic()
        runtime.trace_event(
            "mushroom_v2_boundary",
            side="left",
            position=position,
            platform=("upper" if state.on_upper_platform else "bottom"),
        )
    elif x >= RIGHT_BOUNDARY_X and state.patrol_direction != "left":
        state.patrol_direction = "left"
        state.right_edge_last_x = None
        state.right_edge_progress_at = 0.0
        runtime.trace_event(
            "mushroom_v2_boundary",
            side="right",
            position=position,
            platform=("upper" if state.on_upper_platform else "bottom"),
            reason="coordinate_boundary",
        )
    elif state.patrol_direction != "right" or x < RIGHT_EDGE_STALL_MIN_X:
        state.right_edge_last_x = None
        state.right_edge_progress_at = 0.0
    else:
        now = time.monotonic()
        if state.right_edge_last_x is None or x != state.right_edge_last_x:
            state.right_edge_last_x = x
            state.right_edge_progress_at = now
        elif now - state.right_edge_progress_at >= RIGHT_EDGE_STALL_SECONDS:
            state.patrol_direction = "left"
            state.right_edge_last_x = None
            state.right_edge_progress_at = 0.0
            runtime.trace_event(
                "mushroom_v2_boundary",
                side="right",
                position=position,
                platform=("upper" if state.on_upper_platform else "bottom"),
                reason="right_edge_stall_recovery",
                stalled_ms=round(RIGHT_EDGE_STALL_SECONDS * 1000, 1),
            )


def run_mushroom_v2(runtime):
    """运行仅属于蘑菇 V2 的统一巡逻、战斗和反馈式爬绳流程。"""
    state = MushroomV2State()
    rope_rest_interval_seconds = _rope_rest_interval_seconds(runtime)
    rope_rest_duration_seconds = _rope_rest_duration_seconds(runtime)
    if rope_rest_interval_seconds > 0 and rope_rest_duration_seconds > 0:
        state.next_rope_rest_at = time.monotonic() + rope_rest_interval_seconds
    initialize_route_variation(state)
    print("[蘑菇V2] 反馈式爬绳和逐帧战斗协调已启用。")
    runtime.trace_event(
        "mushroom_v2_route_started",
        attack_recheck_ms=round(V2_ATTACK_RECHECK_SECONDS * 1000, 1),
        attack_move_lock_ms=round(V2_ATTACK_MOVE_LOCK_SECONDS * 1000, 1),
        combat_release_mode="snapshot_overlay",
        climb_mode="trajectory_feedback",
        climb_rearm_x=ROPE_REARM_X,
        decision_mode="route_chase_attack_overlay",
        chase_extra_x=200,
        left_boundary_x=LEFT_BOUNDARY_X,
        right_boundary_x=RIGHT_BOUNDARY_X,
        right_edge_stall_min_x=RIGHT_EDGE_STALL_MIN_X,
        right_edge_stall_ms=round(RIGHT_EDGE_STALL_SECONDS * 1000, 1),
        target_snapshot_ttl_ms=round(
            V2_TARGET_SNAPSHOT_TTL_SECONDS * 1000,
            1,
        ),
        route_resume_debounce_ms=round(
            V2_ROUTE_RESUME_DEBOUNCE_SECONDS * 1000,
            1,
        ),
        near_chase_debounce_ms=round(
            V2_NEAR_CHASE_DEBOUNCE_SECONDS * 1000,
            1,
        ),
        near_chase_debounce_max_distance=V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE,
        rope_rest_interval_minutes=round(
            rope_rest_interval_seconds / 60,
            2,
        ),
        rope_rest_duration_seconds=rope_rest_duration_seconds,
        rope_rest_middle_y=ROPE_REST_MIDDLE_Y,
        random_action_min_seconds=ROUTE_VARIATION_MIN_INTERVAL_SECONDS,
        random_action_max_seconds=ROUTE_VARIATION_MAX_INTERVAL_SECONDS,
        random_actions=("jump", "turn_attack"),
    )
    if not wait_for_runtime_ready(runtime):
        return
    set_route_phase(
        runtime,
        state,
        "patrol",
        position=runtime.读取人物位置(),
    )

    try:
        while not runtime.已请求停止():
            if not runtime.可中断等待(ROUTE_LOOP_SECONDS, interval=0.01):
                break

            _update_rope_rest_schedule(runtime, state)
            position = runtime.读取人物位置()
            if position is not None:
                _update_platform_level(runtime, state, position)
                _update_climb_arming(runtime, state, position)
                runtime.记录人物位置(
                    position,
                    interval=0.2,
                    route="mushroom_v2",
                    phase=state.phase,
                    patrol_direction=state.patrol_direction,
                    applied_direction=state.applied_direction,
                    climb_armed=state.climb_armed,
                    on_upper_platform=state.on_upper_platform,
                    rope_zone=_is_in_rope_zone(position),
                )

            # 轮子等公共特殊流程仍保留原有阻塞语义；普通怪物战斗不再直接
            # 根据zant独占路线，而由本轮攻击/追怪/路线意图统一决定。
            if runtime.zant == 2:
                set_route_phase(runtime, state, "combat_wait", position=position)
                release_route_keys(runtime, state, reason="combat_wait")
                runtime.等待战斗恢复()
                continue
            if runtime.攻击移动仍锁定():
                set_route_phase(runtime, state, "move_lock", position=position)
                release_route_keys(runtime, state, reason="move_lock")
                continue
            if position is None:
                set_route_phase(runtime, state, "position_missing")
                release_route_keys(runtime, state, reason="position_missing")
                continue

            monster_snapshot = read_monster_snapshot(runtime)
            monster_has_priority = (
                monster_snapshot.fresh and monster_snapshot.chase_count > 0
            )
            suspended_stalled = _update_suspended_tracking(state, position)
            if suspended_stalled and not monster_has_priority:
                recovered, recovery_position = _recover_from_suspended_rope(
                    runtime,
                    state,
                    position,
                    reason="main_loop_stationary_guard",
                )
                if recovered:
                    state.climb_armed = True
                    state.last_climb_at = 0.0
                    set_route_phase(
                        runtime,
                        state,
                        "climb_retry",
                        position=recovery_position,
                        reason="suspended_stationary_recovered",
                    )
                else:
                    set_route_phase(
                        runtime,
                        state,
                        _patrol_phase_name(state),
                        position=recovery_position,
                        reason="suspended_stationary_recovery_failed",
                    )
                continue
            rope_climb_ready = (
                state.climb_armed
                and _is_in_rope_zone(position)
                and time.monotonic() - state.last_climb_at >= ROPE_RETRY_SECONDS
            )
            if rope_climb_ready and monster_has_priority:
                # 绳子脚下有怪时先沿用统一战斗/追怪决策。目标连续消失后，下一轮
                # 仍在绳区才开始爬绳，避免跳起动作与转身攻击互相抢键。
                if not state.climb_deferred_for_monster:
                    state.climb_deferred_for_monster = True
                    runtime.trace_event(
                        "mushroom_v2_climb_deferred_for_monster",
                        position=position,
                        attackable_count=monster_snapshot.attackable_count,
                        chase_count=monster_snapshot.chase_count,
                        target_direction=monster_snapshot.direction,
                        nearest_dx=monster_snapshot.nearest_dx,
                    )
            else:
                state.climb_deferred_for_monster = False

            if rope_climb_ready and not monster_has_priority:
                climbed = _try_climb_rope(runtime, state, position)
                if climbed:
                    upper_position = runtime.读取人物位置()
                    _enter_upper_platform(runtime, state, upper_position)
                    set_route_phase(
                        runtime,
                        state,
                        _patrol_phase_name(state),
                        position=upper_position,
                        reason="climb_completed",
                    )
                if (
                    not climbed
                    and not runtime.攻击移动仍锁定()
                    and not runtime.已请求停止()
                ):
                    recovery_position = runtime.读取人物位置()
                    if (
                        state.climb_armed
                        and _is_in_rope_zone(recovery_position)
                    ):
                        set_route_phase(
                            runtime,
                            state,
                            "climb_retry",
                            position=recovery_position,
                            reason="recoverable_climb_failure",
                        )
                        release_route_keys(
                            runtime,
                            state,
                            reason="climb_retry",
                        )
                        runtime.trace_event(
                            "mushroom_v2_climb_retry_waiting",
                            position=recovery_position,
                            climb_armed=state.climb_armed,
                        )
                        continue
                    set_route_phase(
                        runtime,
                        state,
                        _patrol_phase_name(state),
                        position=recovery_position,
                        reason="climb_failed_resume",
                    )
                    runtime.trace_event(
                        "mushroom_v2_climb_failed_resume",
                        pending_direction=state.patrol_direction,
                        position=recovery_position,
                        climb_armed=state.climb_armed,
                        reason="return_to_unified_decision",
                    )
                continue

            # 路线先更新默认方向，随后严格攻击范围或额外200px追怪范围只
            # 覆盖本轮动作，不修改state.patrol_direction。
            _update_patrol_boundary(runtime, state, position)
            action_intent = build_action_intent(state, monster_snapshot)
            if (
                action_intent.source == "route"
                and try_route_variation(
                    runtime,
                    state,
                    position,
                    monster_snapshot,
                )
            ):
                continue
            decision_changed = trace_action_decision(
                runtime,
                state,
                action_intent,
                position,
            )
            apply_action_intent(
                runtime,
                state,
                action_intent,
                position,
                decision_changed,
            )
    finally:
        finish_combat(runtime, state)
        release_route_keys(runtime, state, reason="route_stopped")
        runtime.释放攻击键()
        runtime.trace_event("mushroom_v2_route_stopped")

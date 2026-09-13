"""自定义录制路线的活性监控与卡死恢复。

本模块只负责判断“路线真的没有前进”以及选择恢复级别。地图结构、战斗策略、
绳子、休息点和按键实现仍由 ``recorded_route_player`` 提供。将看门狗隔离在此，
便于出现副作用时单独回滚。
"""

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteLivenessConfig:
    rope_entry_trigger_y: int
    loop_gap_reset_seconds: float
    move_no_progress_seconds: float
    combat_starvation_seconds: float
    control_silence_seconds: float
    recovery_jump_hold_seconds: float


def _latest_target_active(runtime, state, read_monster_snapshot):
    """读取最新快照，返回战斗/追怪是否仍应占用控制权。"""
    snapshot = read_monster_snapshot(runtime)
    active = (
        getattr(runtime, "zant", 0) == 1
        or state.smart_seek_target_active
        or state.combat.combat_active
        or state.combat.chase_active
        or state.combat.locked_attack_direction in ("left", "right")
        or state.combat.route_resume_pending_at > 0
        or snapshot.attackable_count > 0
        or snapshot.chase_count > 0
    )
    return active, snapshot


def _restart_route_progress_clock(state, position, now, kind):
    """从当前人物位置重新计算路线活性，丢弃战斗期间累计的静默时间。"""
    position_x = int(position[0])
    state.route_command_anchor_x = position_x
    state.route_command_progress_at = now
    state.last_route_progress_at = now
    state.last_control_intent_at = now
    state.last_control_intent_kind = kind
    state.route_recovery_stage = 0


def _safe_platform_recovery_direction(
    variant,
    state,
    position,
    current_platform_range_index,
):
    """卡住时朝当前平台内部恢复，避免固定方向从短平台边缘掉落。"""
    platform_index = current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return state.route_command_direction or "right"
    platform = variant.platform_ranges[platform_index]
    middle_x = (int(platform.minimum_x) + int(platform.maximum_x)) / 2.0
    return "right" if int(position[0]) <= middle_x else "left"


def apply_route_liveness_watchdog(
    runtime,
    state,
    variant,
    position,
    *,
    config,
    rope_bounds,
    has_complete_rope_geometry,
    rest_route_navigation_active,
    current_platform_range_index,
    read_monster_snapshot,
    force_route_resume,
    apply_vertical,
    apply_horizontal,
    clear_rope_entry_runup,
    clear_connection_priority,
):
    """监控路线进展；战斗不计入卡死时间，恢复按“重按→跳→重定位”升级。"""
    locked_connection_index = state.connection_priority_route_index
    rope_entry_connection_locked = False
    if (
        state.connection_priority_active
        and locked_connection_index is not None
        and 0 <= int(locked_connection_index) < len(variant.points)
        and state.active_rope_x is None
        and not state.rope_top_exit_pending
    ):
        locked_point = variant.points[int(locked_connection_index)]
        _rope_x, _top_y, locked_bottom_y = rope_bounds(locked_point)
        rope_entry_connection_locked = (
            locked_point.segment_type == "rope_entry"
            and has_complete_rope_geometry(locked_point)
            and locked_bottom_y is not None
            and int(position[1])
            >= int(locked_bottom_y) - int(config.rope_entry_trigger_y)
        )

    if (
        state.completed
        or state.rest_point_test_parked
        or state.rope_rest_test_active
        or state.rope_resting
        or state.rope_rest_pending
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
        or rest_route_navigation_active(state)
        or rope_entry_connection_locked
    ):
        return False
    if getattr(runtime, "zant", 0) == 2:
        return False
    movement_locked = getattr(runtime, "攻击移动仍锁定", None)
    if callable(movement_locked) and movement_locked():
        return False

    now = time.monotonic()
    watchdog_gap = (
        now - state.liveness_last_checked_at
        if state.liveness_last_checked_at > 0
        else 0.0
    )
    state.liveness_last_checked_at = now
    if watchdog_gap >= float(config.loop_gap_reset_seconds):
        _restart_route_progress_clock(state, position, now, "loop_resumed")
        return False

    target_active, _snapshot = _latest_target_active(
        runtime,
        state,
        read_monster_snapshot,
    )
    combat_was_active = bool(
        getattr(state, "_liveness_combat_was_active", False)
    )
    if target_active:
        state._liveness_combat_was_active = True
        _restart_route_progress_clock(state, position, now, "combat_active")
        return False
    if combat_was_active:
        # 战斗结束后的第一轮只恢复路线。从这一刻重新给足无位移窗口，不能把
        # 站桩攻击的时间带入路线卡死判定。
        state._liveness_combat_was_active = False
        _restart_route_progress_clock(state, position, now, "combat_released")
        runtime.trace_event(
            "recorded_route_liveness_combat_released",
            position=position,
            action="restart_route_progress_clock_without_jump",
        )
        return False

    direction = state.route_command_direction
    position_x = int(position[0])
    if direction in ("left", "right"):
        if state.route_command_observed_direction != direction:
            state.route_command_observed_direction = direction
            state.route_command_anchor_x = position_x
            state.route_command_progress_at = now
        else:
            anchor_x = (
                int(state.route_command_anchor_x)
                if state.route_command_anchor_x is not None
                else position_x
            )
            moved_as_commanded = (
                direction == "right" and position_x > anchor_x
            ) or (
                direction == "left" and position_x < anchor_x
            )
            if moved_as_commanded:
                state.route_command_anchor_x = position_x
                state.route_command_progress_at = now
                state.last_route_progress_at = now
                state.route_recovery_stage = 0
    else:
        state.route_command_observed_direction = None
        state.route_command_anchor_x = None

    if (
        not state.connection_priority_active
        and now - state.last_route_progress_at
        >= float(config.combat_starvation_seconds)
    ):
        state.last_route_progress_at = now
        state.route_recovery_count += 1
        force_route_resume(
            runtime,
            state,
            position,
            reason="combat_starved_route_progress",
        )
        return False

    if (
        direction in ("left", "right")
        and now - state.route_command_progress_at
        >= float(config.move_no_progress_seconds)
    ):
        stalled_ms = round((now - state.route_command_progress_at) * 1000.0, 1)
        state.route_command_progress_at = now
        state.route_command_anchor_x = position_x
        state.route_recovery_count += 1

        if state.route_recovery_stage == 0:
            # 第一次无位移只重新按住原路线方向，不跳。怪物碰撞、短暂识别抖动
            # 或刚恢复路线都不会立刻造成可见的回头跳。
            state.route_recovery_stage = 1
            state.last_control_intent_at = now
            state.last_control_intent_kind = "route_direction_reasserted"
            apply_vertical(runtime, state, "none")
            apply_horizontal(runtime, state, direction)
            runtime.trace_event(
                "recorded_route_liveness_direction_reasserted",
                position=position,
                direction=direction,
                stalled_ms=stalled_ms,
                recovery_count=state.route_recovery_count,
                action="reassert_route_direction_without_jump",
            )
            return True

        if state.route_recovery_stage == 1:
            # 真正按跳跃前再取一张最新战斗快照；只要怪物重新出现，就取消恢复
            # 跳并把活性时钟交回战斗链。
            target_active, latest_snapshot = _latest_target_active(
                runtime,
                state,
                read_monster_snapshot,
            )
            if target_active:
                state._liveness_combat_was_active = True
                _restart_route_progress_clock(
                    state,
                    position,
                    now,
                    "combat_reappeared_before_recovery_jump",
                )
                runtime.trace_event(
                    "recorded_route_liveness_jump_cancelled",
                    position=position,
                    attackable_count=latest_snapshot.attackable_count,
                    chase_count=latest_snapshot.chase_count,
                    action="return_control_to_combat",
                )
                return False

            recovery_direction = _safe_platform_recovery_direction(
                variant,
                state,
                position,
                current_platform_range_index,
            )
            state.route_recovery_stage = 2
            force_route_resume(
                runtime,
                state,
                position,
                reason="route_command_no_position_progress_after_reassert",
            )
            apply_vertical(runtime, state, "none")
            apply_horizontal(runtime, state, recovery_direction)
            runtime.pydirectinput.keyDown("c")
            try:
                completed = runtime.可中断等待(
                    float(config.recovery_jump_hold_seconds),
                    interval=0.01,
                )
            finally:
                runtime.pydirectinput.keyUp("c")
            runtime.trace_event(
                "recorded_route_liveness_jump",
                position=position,
                direction=recovery_direction,
                stalled_ms=stalled_ms,
                completed=completed,
                recovery_count=state.route_recovery_count,
                action="jump_after_direction_reassert_failed",
            )
            return True

        state.route_recovery_stage = 0
        state.route_index = None
        state.last_jump_index = None
        state.active_platform_range_index = None
        state.platform_coverage_target_x = None
        state.platform_coverage_route_index = None
        clear_rope_entry_runup(state)
        clear_connection_priority(state)
        force_route_resume(
            runtime,
            state,
            position,
            reason="route_command_still_stalled_after_jump",
        )
        runtime.trace_event(
            "recorded_route_liveness_relocalized",
            position=position,
            stalled_ms=stalled_ms,
            recovery_count=state.route_recovery_count,
            action="clear_transient_state_and_reselect_route",
        )
        return True

    if (
        now - state.last_control_intent_at
        >= float(config.control_silence_seconds)
    ):
        silent_ms = round((now - state.last_control_intent_at) * 1000.0, 1)
        state.route_recovery_count += 1
        state.route_index = None
        state.last_jump_index = None
        state.platform_coverage_target_x = None
        state.platform_coverage_route_index = None
        state.route_command_direction = None
        clear_rope_entry_runup(state)
        clear_connection_priority(state)
        force_route_resume(
            runtime,
            state,
            position,
            reason="no_attack_or_route_control_intent",
        )
        runtime.trace_event(
            "recorded_route_liveness_silence_recovered",
            position=position,
            silent_ms=silent_ms,
            recovery_count=state.route_recovery_count,
            action="clear_stale_state_and_reselect_route",
        )
        return True
    return False


__all__ = ("RouteLivenessConfig", "apply_route_liveness_watchdog")

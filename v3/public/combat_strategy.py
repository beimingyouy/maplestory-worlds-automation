"""所有地图、录制路线和自定义路线共用的AI战斗策略与动作执行逻辑。

路线模块只需维护人物路线状态，并调用本模块的公开接口生成和执行战斗意图。
蘑菇V2、蘑菇V3和后续自定义路线都复用这里的快照有效期、攻击优先级、
目标方向锁、追怪抗抖、死亡残影等待和无怪恢复路线逻辑。
"""

import time
from dataclasses import dataclass
from typing import Optional


V2_ATTACK_RECHECK_SECONDS = 0.0
V2_ATTACK_MOVE_LOCK_SECONDS = 0.0
V2_ATTACK_KEY_HOLD_SECONDS = 0.055
V2_DIRECTION_SETTLE_SECONDS = 0.02
V2_FACING_SNAPSHOT_TTL_SECONDS = 0.45
V2_FACING_REASSERT_ATTACKS = 6
V2_FACING_REASSERT_COOLDOWN_SECONDS = 0.90
V2_TARGET_SIDE_RECHECK_ATTACKS = 6
V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS = 2
V2_TEMPLATE_SIDE_SWITCH_COOLDOWN_SECONDS = 0.40
# 自定义模板检测在虚拟机或模板较多时，一张有效帧偶尔会超过0.45秒。
# 保留约两帧的目标快照，避免“刚打完一刀 -> 立刻恢复路线 -> 下一帧又停下打”。
V2_TARGET_SNAPSHOT_TTL_SECONDS = 0.85
V2_ROUTE_RESUME_DEBOUNCE_SECONDS = 0.26
V2_NEAR_CHASE_DEBOUNCE_SECONDS = 0.15
V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE = 340.0
V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS = 2
V2_CHASE_DIRECTION_COUNT_ADVANTAGE = 2


@dataclass(frozen=True)
class MushroomMonsterSnapshot:
    """保存攻击范围和额外追怪范围内的怪物检测快照。"""

    detected_at: float
    direction: Optional[str]
    chase_count: int
    attackable_count: int
    chase_left_count: int
    chase_right_count: int
    attackable_left_count: int
    attackable_right_count: int
    nearest_dx: Optional[float]
    age_ms: Optional[float]
    fresh: bool
    health_bar_attackable_left_count: int = 0
    health_bar_attackable_right_count: int = 0
    attack_confirmation_pending: bool = False


@dataclass(frozen=True)
class MushroomActionIntent:
    """描述本轮唯一的水平、垂直和攻击动作。"""

    horizontal: str
    vertical: str
    action: str
    source: str
    target_direction: Optional[str]
    target_age_ms: Optional[float]
    chase_count: int
    attackable_count: int
    chase_left_count: int
    chase_right_count: int
    attackable_left_count: int
    attackable_right_count: int
    nearest_dx: Optional[float]
    created_at: float
    health_bar_attackable_left_count: int = 0
    health_bar_attackable_right_count: int = 0


@dataclass
class MushroomCombatState:
    """保存蘑菇路线共用的战斗、追怪和按键协调状态。"""

    patrol_direction: str = "left"
    applied_direction: Optional[str] = None
    phase: str = "startup"
    combat_active: bool = False
    combat_started_at: float = 0.0
    combat_attack_count: int = 0
    last_attack_direction: int = 0
    last_facing_correction_at: float = 0.0
    last_facing_reassert_at: float = 0.0
    same_direction_attack_streak: int = 0
    last_target_side_recheck_attack_count: int = 0
    last_facing_log_signature: Optional[tuple] = None
    locked_attack_direction: Optional[str] = None
    side_switch_pending_direction: Optional[str] = None
    side_switch_pending_count: int = 0
    side_switch_pending_frame_at: float = 0.0
    last_side_switch_at: float = 0.0
    route_resume_pending_at: float = 0.0
    near_chase_pending_at: float = 0.0
    near_chase_pending_direction: Optional[str] = None
    chase_active: bool = False
    chase_direction: Optional[str] = None
    chase_direction_nearest_dx: Optional[float] = None
    chase_switch_pending_direction: Optional[str] = None
    chase_switch_pending_count: int = 0
    chase_switch_pending_frame_at: float = 0.0
    last_intent_signature: Optional[tuple] = None
    on_upper_platform: bool = False


def _clear_chase_switch_pending(state):
    """清理远距离追怪换边候选，避免旧检测帧影响下一轮追怪。"""
    state.chase_switch_pending_direction = None
    state.chase_switch_pending_count = 0
    state.chase_switch_pending_frame_at = 0.0


def _stable_chase_direction(state, snapshot, now):
    """两侧远怪同时存在或检测短暂跳边时，保持当前追怪方向。"""
    requested = snapshot.direction
    current = state.chase_direction if state.chase_active else None
    if requested not in ("left", "right") or current not in ("left", "right"):
        _clear_chase_switch_pending(state)
        return requested
    if requested == current:
        _clear_chase_switch_pending(state)
        return current

    left_count = max(0, int(getattr(snapshot, "chase_left_count", 0)))
    right_count = max(0, int(getattr(snapshot, "chase_right_count", 0)))
    current_count = left_count if current == "left" else right_count
    requested_count = right_count if requested == "right" else left_count
    both_sides_visible = left_count > 0 and right_count > 0
    meaningful_count_advantage = (
        requested_count
        >= current_count + V2_CHASE_DIRECTION_COUNT_ADVANTAGE
    )
    if both_sides_visible and not meaningful_count_advantage:
        # 最近模板中心在左右两边小幅变化时，不能让人物逐帧原地转身。
        _clear_chase_switch_pending(state)
        return current

    frame_at = float(getattr(snapshot, "detected_at", now) or now)
    if state.chase_switch_pending_direction != requested:
        state.chase_switch_pending_direction = requested
        state.chase_switch_pending_count = 1
        state.chase_switch_pending_frame_at = frame_at
        return current
    if frame_at > state.chase_switch_pending_frame_at + 1e-9:
        state.chase_switch_pending_count += 1
        state.chase_switch_pending_frame_at = frame_at
    if (
        state.chase_switch_pending_count
        < V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS
    ):
        return current
    _clear_chase_switch_pending(state)
    return requested


def _set_route_phase(runtime, state, phase, position=None, **details):
    """仅在路线阶段变化时记录一次关键日志。"""
    if state.phase == phase:
        return
    previous_phase = state.phase
    state.phase = phase
    runtime.trace_event(
        "mushroom_v2_phase_changed",
        previous_phase=previous_phase,
        phase=phase,
        position=position,
        patrol_direction=state.patrol_direction,
        applied_direction=state.applied_direction,
        climb_armed=getattr(state, "climb_armed", False),
        on_upper_platform=getattr(state, "on_upper_platform", False),
        **details,
    )


def _wait_for_runtime_ready(runtime, timeout=8.0):
    """等待怪物检测首帧和小地图人物坐标同时就绪。"""
    deadline = time.monotonic() + max(0.1, float(timeout))
    runtime.trace_event("mushroom_v2_waiting_for_runtime")
    while not runtime.已请求停止():
        position = runtime.读取人物位置()
        detector_ready = runtime.怪物检测就绪事件.is_set()
        if detector_ready and position is not None:
            runtime.trace_event("mushroom_v2_runtime_ready", position=position)
            return True
        if time.monotonic() >= deadline:
            runtime.trace_event(
                "mushroom_v2_runtime_timeout",
                detector_ready=detector_ready,
                position=position,
            )
            print("[蘑菇] 等待检测器或小地图人物坐标超时，本次停止。")
            return False
        if not runtime.可中断等待(0.05, interval=0.01):
            return False
    return False


def _release_route_keys(runtime, state, reason=None):
    """释放持续移动、向上和跳跃键，并清除已应用方向。"""
    runtime.释放水平移动键(reason=reason or state.phase)
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("c")
    state.applied_direction = None


def _tap_key(runtime, key, hold_seconds=0.04):
    """轻点一个按键，并保证结束时释放该键。"""
    runtime.pydirectinput.keyDown(key)
    completed = runtime.可中断等待(hold_seconds, interval=0.01)
    runtime.pydirectinput.keyUp(key)
    return completed


def _patrol_phase_name(state):
    """按人物所在平台返回路线阶段名。"""
    return "upper_patrol" if getattr(state, "on_upper_platform", False) else "patrol"


def _start_combat(runtime, state, position):
    """进入 V2 战斗阶段并只在入口处释放一次路线移动键。"""
    state.combat_active = True
    state.combat_started_at = time.monotonic()
    state.combat_attack_count = 0
    state.last_attack_direction = 0
    state.last_facing_correction_at = 0.0
    state.last_facing_reassert_at = 0.0
    state.same_direction_attack_streak = 0
    state.last_target_side_recheck_attack_count = 0
    state.last_facing_log_signature = None
    _clear_side_switch_pending(state)
    state.last_side_switch_at = 0.0
    _set_route_phase(runtime, state, "combat", position=position)
    _release_route_keys(runtime, state, reason="combat")
    _last_seen, target_direction, nearby = runtime.读取战斗感知()
    runtime.trace_event(
        "mushroom_v2_combat_started",
        nearby=nearby,
        target_direction=target_direction,
        resume_direction=state.patrol_direction,
    )


def _finish_combat(runtime, state):
    """结束 V2 战斗阶段并记录本轮攻击数量，不直接恢复移动。"""
    if not state.combat_active:
        return
    runtime.释放攻击键()
    runtime.trace_event(
        "mushroom_v2_combat_finished",
        duration_ms=round(
            (time.monotonic() - state.combat_started_at) * 1000,
            3,
        ),
        attacks=state.combat_attack_count,
        resume_direction=state.patrol_direction,
    )
    state.combat_active = False
    state.combat_started_at = 0.0
    state.combat_attack_count = 0
    state.last_attack_direction = 0
    state.last_facing_correction_at = 0.0
    state.last_facing_reassert_at = 0.0
    state.same_direction_attack_streak = 0
    state.last_target_side_recheck_attack_count = 0
    state.last_facing_log_signature = None
    _clear_side_switch_pending(state)
    state.last_side_switch_at = 0.0


def _clear_side_switch_pending(state):
    """清理模板换边确认，避免旧战斗或旧截图污染下一轮决策。"""
    state.side_switch_pending_direction = None
    state.side_switch_pending_count = 0
    state.side_switch_pending_frame_at = 0.0


def _reset_attack_direction_lock(runtime, state, reason):
    """没有可追目标时释放同侧攻击锁，并记录释放原因。"""
    _clear_side_switch_pending(state)
    state.last_side_switch_at = 0.0
    if state.locked_attack_direction not in ("left", "right"):
        return
    previous_direction = state.locked_attack_direction
    state.locked_attack_direction = None
    runtime.trace_event(
        "mushroom_v2_target_side_lock_released",
        previous_direction=previous_direction,
        reason=reason,
    )


def _resolve_attack_direction(runtime, state, intent, queued_direction):
    """锁定攻击侧，并在原侧清空或连续三次攻击后按最新目标重新评估。"""
    if queued_direction == 3:
        return 3

    health_bar_left_count = max(
        0, int(getattr(intent, "health_bar_attackable_left_count", 0))
    )
    health_bar_right_count = max(
        0, int(getattr(intent, "health_bar_attackable_right_count", 0))
    )
    if health_bar_left_count + health_bar_right_count > 0:
        # 血条是活怪的强证据：不能因为正面模板残影而延迟处理背后血条怪。
        left_count = health_bar_left_count
        right_count = health_bar_right_count
        count_source = "health_bar_attack_range"
    elif intent.attackable_left_count + intent.attackable_right_count > 0:
        left_count = intent.attackable_left_count
        right_count = intent.attackable_right_count
        count_source = "attack_range"
    else:
        # 近身补刀带位于严格攻击范围之外，此时使用额外追怪区的左右数量。
        left_count = intent.chase_left_count
        right_count = intent.chase_right_count
        count_source = "chase_range"

    if left_count + right_count <= 0:
        _clear_side_switch_pending(state)
        return 0

    snapshot_direction = intent.target_direction
    if count_source.startswith("health_bar_"):
        if left_count > right_count:
            snapshot_direction = "left"
        elif right_count > left_count:
            snapshot_direction = "right"
    if snapshot_direction not in ("left", "right"):
        # 目标方向偶发为空时不能继续盲用旧攻击队列；优先按本帧左右数量恢复
        # 方向。数量相同时才保留仍有目标的锁定侧或当前队列方向。
        if left_count > right_count:
            snapshot_direction = "left"
        elif right_count > left_count:
            snapshot_direction = "right"
        elif (
            state.locked_attack_direction == "left" and left_count > 0
        ) or (
            state.locked_attack_direction == "right" and right_count > 0
        ):
            snapshot_direction = state.locked_attack_direction
        elif queued_direction in (1, 2):
            snapshot_direction = "left" if queued_direction == 1 else "right"
        else:
            snapshot_direction = "left" if left_count > 0 else "right"

    if state.locked_attack_direction not in ("left", "right"):
        state.locked_attack_direction = snapshot_direction
        _clear_side_switch_pending(state)
        runtime.trace_event(
            "mushroom_v2_target_side_locked",
            direction=state.locked_attack_direction,
            reason="combat_started",
            count_source=count_source,
            left_count=left_count,
            right_count=right_count,
        )

    locked_direction = state.locked_attack_direction
    locked_count = left_count if locked_direction == "left" else right_count
    opposite_direction = "right" if locked_direction == "left" else "left"
    opposite_count = right_count if locked_direction == "left" else left_count
    switch_reason = None
    if (
        count_source.startswith("health_bar_")
        and snapshot_direction != locked_direction
        and (left_count if snapshot_direction == "left" else right_count) > 0
    ):
        # 血条明确给出另一侧活怪时，跳过模板确认和冷却，立即转身。
        switch_reason = "health_bar_target_direction"
    elif locked_count <= 0 and opposite_count > 0:
        switch_reason = "locked_side_empty"
    # 两侧同时有怪时保持当前侧连续攻击。不能每隔几刀按“最近怪/数量”重新
    # 选边，否则会出现左打一刀、右打一刀的机械抽动。只有当前侧确认清空后，
    # 才转身处理攻击范围内的背后怪；这不会启用背后智能追怪。
    if switch_reason is None:
        if state.side_switch_pending_direction is not None:
            runtime.trace_event(
                "combat_side_switch_cancelled",
                pending_direction=state.side_switch_pending_direction,
                pending_count=state.side_switch_pending_count,
                locked_direction=locked_direction,
                count_source=count_source,
                left_count=left_count,
                right_count=right_count,
                action="keep_current_side",
            )
        _clear_side_switch_pending(state)
    elif not count_source.startswith("health_bar_"):
        candidate_direction = opposite_direction
        now = time.monotonic()
        cooldown_remaining = (
            V2_TEMPLATE_SIDE_SWITCH_COOLDOWN_SECONDS
            - (now - state.last_side_switch_at)
        )
        if state.last_side_switch_at > 0 and cooldown_remaining > 0:
            pending_changed = (
                state.side_switch_pending_direction != candidate_direction
                or state.side_switch_pending_count != 0
            )
            state.side_switch_pending_direction = candidate_direction
            state.side_switch_pending_count = 0
            state.side_switch_pending_frame_at = 0.0
            if pending_changed:
                runtime.trace_event(
                    "combat_side_switch_pending",
                    pending_direction=candidate_direction,
                    pending_count=0,
                    confirmation_required=V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS,
                    cooldown_remaining_ms=round(cooldown_remaining * 1000.0, 3),
                    reason=switch_reason,
                    count_source=count_source,
                    left_count=left_count,
                    right_count=right_count,
                    action="keep_current_side_during_cooldown",
                )
            switch_reason = None
        else:
            snapshot_detected_at = (
                float(intent.created_at)
                - max(0.0, float(intent.target_age_ms or 0.0)) / 1000.0
            )
            snapshot_advanced = False
            if state.side_switch_pending_direction != candidate_direction:
                if state.side_switch_pending_direction is not None:
                    runtime.trace_event(
                        "combat_side_switch_cancelled",
                        pending_direction=state.side_switch_pending_direction,
                        pending_count=state.side_switch_pending_count,
                        locked_direction=locked_direction,
                        count_source=count_source,
                        left_count=left_count,
                        right_count=right_count,
                        action="replace_with_new_side_candidate",
                    )
                state.side_switch_pending_direction = candidate_direction
                state.side_switch_pending_count = 1
                state.side_switch_pending_frame_at = snapshot_detected_at
                snapshot_advanced = True
            elif (
                snapshot_detected_at
                > state.side_switch_pending_frame_at + 0.001
            ):
                state.side_switch_pending_count += 1
                state.side_switch_pending_frame_at = snapshot_detected_at
                snapshot_advanced = True

            if (
                state.side_switch_pending_count
                < V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS
            ):
                if snapshot_advanced:
                    runtime.trace_event(
                        "combat_side_switch_pending",
                        pending_direction=candidate_direction,
                        pending_count=state.side_switch_pending_count,
                        confirmation_required=V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS,
                        snapshot_detected_at=round(snapshot_detected_at, 6),
                        reason=switch_reason,
                        count_source=count_source,
                        left_count=left_count,
                        right_count=right_count,
                        action="wait_for_next_distinct_snapshot",
                    )
                switch_reason = None

    if switch_reason is not None:
        previous_direction = locked_direction
        locked_direction = (
            snapshot_direction
            if switch_reason == "health_bar_target_direction"
            else opposite_direction
        )
        state.locked_attack_direction = locked_direction
        _clear_side_switch_pending(state)
        state.last_side_switch_at = time.monotonic()
        runtime.trace_event(
            "mushroom_v2_target_side_switched",
            previous_direction=previous_direction,
            direction=locked_direction,
            reason=switch_reason,
            count_source=count_source,
            left_count=left_count,
            right_count=right_count,
            attacks_since_combat_start=state.combat_attack_count,
        )

    resolved_direction = 1 if locked_direction == "left" else 2
    if queued_direction != resolved_direction or snapshot_direction != locked_direction:
        runtime.trace_event(
            "mushroom_v2_attack_direction_resolved",
            queued_direction=queued_direction,
            snapshot_direction=snapshot_direction,
            locked_direction=locked_direction,
            resolved_direction=resolved_direction,
            switch_reason=switch_reason,
            count_source=count_source,
            left_count=left_count,
            right_count=right_count,
            action="keep_current_side" if switch_reason is None else "switch_side",
        )
    return resolved_direction


def _correct_attack_direction_before_press(runtime, state, attack_direction):
    """按键前复核最新怪物快照，纠正空侧方向或取消已经失效的攻击。"""
    if attack_direction not in (1, 2, 3):
        return 0

    latest = _read_monster_snapshot(runtime)
    if not latest.fresh or latest.attackable_count <= 0:
        runtime.释放攻击键()
        state.last_attack_direction = 0
        state.same_direction_attack_streak = 0
        _reset_attack_direction_lock(
            runtime,
            state,
            reason="latest_attack_range_empty",
        )
        runtime.trace_event(
            "mushroom_v2_empty_attack_cancelled",
            requested_direction=attack_direction,
            snapshot_fresh=latest.fresh,
            target_age_ms=latest.age_ms,
            attackable_count=latest.attackable_count,
            chase_count=latest.chase_count,
            action="stop_attacking_empty_position",
        )
        return 0

    if attack_direction == 3:
        return 3

    desired_direction = "left" if attack_direction == 1 else "right"
    desired_count = (
        latest.attackable_left_count
        if desired_direction == "left"
        else latest.attackable_right_count
    )
    opposite_direction = "right" if desired_direction == "left" else "left"
    opposite_count = (
        latest.attackable_right_count
        if desired_direction == "left"
        else latest.attackable_left_count
    )
    if desired_count > 0:
        return attack_direction
    if opposite_count <= 0:
        runtime.释放攻击键()
        state.last_attack_direction = 0
        state.same_direction_attack_streak = 0
        _reset_attack_direction_lock(
            runtime,
            state,
            reason="both_attack_sides_empty",
        )
        runtime.trace_event(
            "mushroom_v2_empty_attack_cancelled",
            requested_direction=desired_direction,
            target_age_ms=latest.age_ms,
            attackable_left_count=latest.attackable_left_count,
            attackable_right_count=latest.attackable_right_count,
            action="stop_attacking_empty_position",
        )
        return 0

    health_bar_count = (
        latest.health_bar_attackable_left_count
        + latest.health_bar_attackable_right_count
    )
    if (
        health_bar_count <= 0
        and state.side_switch_pending_direction == opposite_direction
        and state.side_switch_pending_count
        < V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS
    ):
        # 方向解析已发现模板只剩背后侧，但尚未由另一张检测图确认。此时不能
        # 通过按键前复核绕过防抖直接转身；先停手，既避免空打也避免左右抽动。
        runtime.释放攻击键()
        state.last_attack_direction = 0
        state.same_direction_attack_streak = 0
        runtime.trace_event(
            "combat_side_switch_attack_held",
            locked_direction=state.locked_attack_direction,
            pending_direction=opposite_direction,
            pending_count=state.side_switch_pending_count,
            confirmation_required=V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS,
            snapshot_detected_at=round(float(latest.detected_at or 0.0), 6),
            attackable_left_count=latest.attackable_left_count,
            attackable_right_count=latest.attackable_right_count,
            action="wait_without_attacking_empty_side",
        )
        return 0

    previous_direction = state.locked_attack_direction or desired_direction
    state.locked_attack_direction = opposite_direction
    corrected_direction = 1 if opposite_direction == "left" else 2
    runtime.释放攻击键()
    runtime.trace_event(
        "mushroom_v2_empty_side_corrected",
        previous_direction=previous_direction,
        direction=opposite_direction,
        target_age_ms=latest.age_ms,
        attackable_left_count=latest.attackable_left_count,
        attackable_right_count=latest.attackable_right_count,
        reason="requested_side_empty_opposite_side_alive",
        action="turn_and_attack_opposite_side",
    )
    return corrected_direction


def _execute_attack_intent(runtime, state, attack_direction):
    """执行一次攻击，并用最新可靠画面朝向修正受击造成的意外转身。"""
    if attack_direction not in (1, 2, 3):
        return False

    if attack_direction in (1, 2):
        face_direction = "left" if attack_direction == 1 else "right"
        if attack_direction == state.last_attack_direction:
            state.same_direction_attack_streak += 1
        else:
            state.same_direction_attack_streak = 1
        (
            facing_detected_at,
            detected_direction,
            reliable_direction,
            left_confidence,
            right_confidence,
            confidence_margin,
            facing_match_ms,
            black_flicker_suspected,
            facing_frame_reliable,
        ) = runtime.读取人物朝向感知()
        now = time.monotonic()
        facing_age_ms = (
            max(0.0, (now - facing_detected_at) * 1000)
            if facing_detected_at > 0
            else None
        )
        facing_snapshot_fresh = (
            facing_age_ms is not None
            and facing_age_ms <= V2_FACING_SNAPSHOT_TTL_SECONDS * 1000
        )
        # 自己刚纠正过方向后，忽略纠正动作开始前采集的旧帧；否则路线循环会
        # 对着同一张旧截图重复轻点方向键，产生新的顿挫。
        facing_snapshot_after_correction = (
            facing_detected_at > state.last_facing_correction_at
        )
        visual_direction = (
            detected_direction
            if (
                facing_frame_reliable
                and detected_direction in ("left", "right")
                and facing_snapshot_fresh
                and facing_snapshot_after_correction
            )
            else None
        )
        direction_mismatch = (
            visual_direction is not None and visual_direction != face_direction
        )
        direction_unknown_and_changed = (
            visual_direction is None
            and attack_direction != state.last_attack_direction
        )
        direction_reassert_required = (
            not direction_mismatch
            and state.same_direction_attack_streak >= V2_FACING_REASSERT_ATTACKS
            and now - state.last_facing_reassert_at
            >= V2_FACING_REASSERT_COOLDOWN_SECONDS
        )
        attack_streak_before_correction = state.same_direction_attack_streak
        if (
            direction_mismatch
            or direction_unknown_and_changed
            or direction_reassert_required
        ):
            runtime.释放攻击键()
            correction_started_at = time.monotonic()
            if not _tap_key(
                runtime,
                face_direction,
                hold_seconds=V2_DIRECTION_SETTLE_SECONDS,
            ):
                return False
            state.last_facing_correction_at = correction_started_at
            if direction_reassert_required:
                state.last_facing_reassert_at = correction_started_at
                state.same_direction_attack_streak = 0
                event_name = "mushroom_v2_facing_reasserted"
            elif direction_mismatch:
                event_name = "mushroom_v2_facing_corrected"
            else:
                event_name = "mushroom_v2_facing_fallback"
            log_signature = (
                event_name,
                visual_direction,
                face_direction,
                bool(black_flicker_suspected),
            )
            if (
                direction_reassert_required
                or log_signature != state.last_facing_log_signature
            ):
                runtime.trace_event(
                    event_name,
                    detected_direction=detected_direction,
                    reliable_direction=reliable_direction,
                    desired_direction=face_direction,
                    left_confidence=round(float(left_confidence), 4),
                    right_confidence=round(float(right_confidence), 4),
                    confidence_margin=round(float(confidence_margin), 4),
                    age_ms=(round(facing_age_ms, 3) if facing_age_ms is not None else None),
                    match_ms=round(float(facing_match_ms), 3),
                    black_flicker_suspected=bool(black_flicker_suspected),
                    same_direction_attack_streak=attack_streak_before_correction,
                    reassert_after_attacks=V2_FACING_REASSERT_ATTACKS,
                    action=(
                        "reassert_target_direction"
                        if direction_reassert_required
                        else (
                            "turn_to_match_target"
                            if direction_mismatch
                            else "fallback_to_attack_direction"
                        )
                    ),
                )
                state.last_facing_log_signature = log_signature
        elif visual_direction == face_direction:
            log_signature = (
                "mushroom_v2_facing_verified",
                visual_direction,
                face_direction,
                False,
            )
            if log_signature != state.last_facing_log_signature:
                runtime.trace_event(
                    "mushroom_v2_facing_verified",
                    detected_direction=visual_direction,
                    desired_direction=face_direction,
                    left_confidence=round(float(left_confidence), 4),
                    right_confidence=round(float(right_confidence), 4),
                    confidence_margin=round(float(confidence_margin), 4),
                    age_ms=round(facing_age_ms, 3),
                    action="attack_without_turn",
                )
                state.last_facing_log_signature = log_signature
        state.last_attack_direction = attack_direction
    elif attack_direction != state.last_attack_direction:
        state.last_attack_direction = attack_direction

    state.combat_attack_count += 1
    if attack_direction in (1, 2):
        face_direction = "left" if attack_direction == 1 else "right"
        runtime.trace_event(
            "attack_key",
            attack_type="single",
            direction=face_direction,
            flow="mushroom_v2",
            combat_attack_sequence=state.combat_attack_count,
            position=runtime.读取人物位置(),
            patrol_direction=state.patrol_direction,
        )
        attack_key = runtime.单体按键
    else:
        runtime.trace_event(
            "attack_key",
            attack_type="group",
            direction="area",
            flow="mushroom_v2",
            combat_attack_sequence=state.combat_attack_count,
            position=runtime.读取人物位置(),
            patrol_direction=state.patrol_direction,
        )
        attack_key = runtime.群攻按键

    runtime.pydirectinput.keyDown(attack_key)
    completed = runtime.可中断等待(
        V2_ATTACK_KEY_HOLD_SECONDS,
        interval=0.01,
    )
    runtime.pydirectinput.keyUp(attack_key)
    if not completed:
        return False

    runtime.标记攻击完成(
        recheck_seconds=V2_ATTACK_RECHECK_SECONDS,
        move_lock_seconds=V2_ATTACK_MOVE_LOCK_SECONDS,
    )
    return True


def _process_combat_frame(runtime, state, intent, position):
    """消费一次攻击许可，并用最新目标快照和同侧锁决定实际朝向。"""
    if not state.combat_active:
        _start_combat(runtime, state, position)
    queued_direction = runtime.领取攻击意图(wait_seconds=0.0)
    if queued_direction in (1, 2, 3):
        attack_direction = _resolve_attack_direction(
            runtime,
            state,
            intent,
            queued_direction,
        )
        attack_direction = _correct_attack_direction_before_press(
            runtime,
            state,
            attack_direction,
        )
        return _execute_attack_intent(runtime, state, attack_direction)
    return False


def _read_monster_snapshot(runtime):
    """读取V2追怪快照，并丢弃超过有效期的陈旧检测结果。"""
    raw_snapshot = tuple(runtime.读取追怪感知())
    (
        detected_at,
        direction,
        chase_count,
        attackable_count,
        nearest_dx,
        chase_left_count,
        chase_right_count,
        attackable_left_count,
        attackable_right_count,
    ) = raw_snapshot[:9]
    health_bar_attackable_left_count = (
        raw_snapshot[14] if len(raw_snapshot) > 14 else 0
    )
    health_bar_attackable_right_count = (
        raw_snapshot[15] if len(raw_snapshot) > 15 else 0
    )
    attack_confirmation_pending = (
        bool(raw_snapshot[17]) if len(raw_snapshot) > 17 else False
    )
    now = time.monotonic()
    age_ms = (
        max(0.0, (now - detected_at) * 1000)
        if detected_at and detected_at > 0
        else None
    )
    fresh = (
        age_ms is not None
        and age_ms <= V2_TARGET_SNAPSHOT_TTL_SECONDS * 1000
    )
    if not fresh:
        direction = None
        chase_count = 0
        attackable_count = 0
        nearest_dx = None
        chase_left_count = 0
        chase_right_count = 0
        attackable_left_count = 0
        attackable_right_count = 0
        health_bar_attackable_left_count = 0
        health_bar_attackable_right_count = 0
        attack_confirmation_pending = False
    return MushroomMonsterSnapshot(
        detected_at=float(detected_at or 0.0),
        direction=(direction if direction in ("left", "right") else None),
        chase_count=max(0, int(chase_count)),
        attackable_count=max(0, int(attackable_count)),
        chase_left_count=max(0, int(chase_left_count)),
        chase_right_count=max(0, int(chase_right_count)),
        attackable_left_count=max(0, int(attackable_left_count)),
        attackable_right_count=max(0, int(attackable_right_count)),
        nearest_dx=(float(nearest_dx) if nearest_dx is not None else None),
        age_ms=(round(age_ms, 3) if age_ms is not None else None),
        fresh=fresh,
        health_bar_attackable_left_count=max(
            0, int(health_bar_attackable_left_count)
        ),
        health_bar_attackable_right_count=max(
            0, int(health_bar_attackable_right_count)
        ),
        attack_confirmation_pending=attack_confirmation_pending,
    )


def _build_action_intent(state, snapshot):
    """按攻击、追怪、路线优先级生成本轮唯一动作意图。"""
    now = time.monotonic()
    if snapshot.attack_confirmation_pending:
        # 攻击后的第一张静止模板图可能只是死亡残影。等待下一张不同截图确认
        # 存活时必须停手并保持原地，不能因为 attackable_count 暂时清零就切成
        # 追怪或恢复 JSON 路线，否则会同时出现空打和一步走停。
        _clear_chase_switch_pending(state)
        state.route_resume_pending_at = 0.0
        state.near_chase_pending_at = 0.0
        state.near_chase_pending_direction = None
        return MushroomActionIntent(
            horizontal="stop",
            vertical="keep",
            action="none",
            source="combat_hold",
            target_direction=snapshot.direction,
            target_age_ms=snapshot.age_ms,
            chase_count=snapshot.chase_count,
            attackable_count=0,
            chase_left_count=snapshot.chase_left_count,
            chase_right_count=snapshot.chase_right_count,
            attackable_left_count=0,
            attackable_right_count=0,
            nearest_dx=snapshot.nearest_dx,
            created_at=now,
        )
    if snapshot.attackable_count > 0:
        _clear_chase_switch_pending(state)
        state.route_resume_pending_at = 0.0
        state.near_chase_pending_at = 0.0
        state.near_chase_pending_direction = None
        return MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction=snapshot.direction,
            target_age_ms=snapshot.age_ms,
            chase_count=snapshot.chase_count,
            attackable_count=snapshot.attackable_count,
            chase_left_count=snapshot.chase_left_count,
            chase_right_count=snapshot.chase_right_count,
            attackable_left_count=snapshot.attackable_left_count,
            attackable_right_count=snapshot.attackable_right_count,
            nearest_dx=snapshot.nearest_dx,
            created_at=now,
            health_bar_attackable_left_count=(
                snapshot.health_bar_attackable_left_count
            ),
            health_bar_attackable_right_count=(
                snapshot.health_bar_attackable_right_count
            ),
        )
    if snapshot.chase_count > 0 and snapshot.direction is not None:
        state.route_resume_pending_at = 0.0
        chase_direction = _stable_chase_direction(state, snapshot, now)
        should_debounce_near_chase = (
            not state.chase_active
            and (
                state.combat_active
                or state.locked_attack_direction in ("left", "right")
            )
            and snapshot.nearest_dx is not None
            and snapshot.nearest_dx <= V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE
        )
        if should_debounce_near_chase:
            if state.near_chase_pending_direction != chase_direction:
                state.near_chase_pending_at = now
                state.near_chase_pending_direction = chase_direction
            elif state.near_chase_pending_at <= 0:
                state.near_chase_pending_at = now
            if now - state.near_chase_pending_at < V2_NEAR_CHASE_DEBOUNCE_SECONDS:
                return MushroomActionIntent(
                    horizontal="stop",
                    vertical="keep",
                    action="none",
                    source="chase_hold",
                    target_direction=state.locked_attack_direction or chase_direction,
                    target_age_ms=snapshot.age_ms,
                    chase_count=snapshot.chase_count,
                    attackable_count=0,
                    chase_left_count=snapshot.chase_left_count,
                    chase_right_count=snapshot.chase_right_count,
                    attackable_left_count=0,
                    attackable_right_count=0,
                    nearest_dx=snapshot.nearest_dx,
                    created_at=now,
                )
        state.near_chase_pending_at = 0.0
        state.near_chase_pending_direction = None
        return MushroomActionIntent(
            horizontal=chase_direction,
            vertical="keep",
            action="none",
            source="chase",
            target_direction=chase_direction,
            target_age_ms=snapshot.age_ms,
            chase_count=snapshot.chase_count,
            attackable_count=0,
            chase_left_count=snapshot.chase_left_count,
            chase_right_count=snapshot.chase_right_count,
            attackable_left_count=0,
            attackable_right_count=0,
            nearest_dx=snapshot.nearest_dx,
            created_at=now,
        )
    recently_engaged = (
        state.combat_active
        or state.chase_active
        or state.locked_attack_direction in ("left", "right")
    )
    if recently_engaged:
        if state.route_resume_pending_at <= 0:
            state.route_resume_pending_at = now
        if now - state.route_resume_pending_at < V2_ROUTE_RESUME_DEBOUNCE_SECONDS:
            return MushroomActionIntent(
                horizontal="stop",
                vertical="keep",
                action="none",
                source="combat_hold",
                target_direction=state.locked_attack_direction,
                target_age_ms=snapshot.age_ms,
                chase_count=0,
                attackable_count=0,
                chase_left_count=0,
                chase_right_count=0,
                attackable_left_count=0,
                attackable_right_count=0,
                nearest_dx=None,
                created_at=now,
            )
    state.route_resume_pending_at = 0.0
    state.near_chase_pending_at = 0.0
    state.near_chase_pending_direction = None
    _clear_chase_switch_pending(state)
    return MushroomActionIntent(
        horizontal=state.patrol_direction,
        vertical="keep",
        action="none",
        source="route",
        target_direction=None,
        target_age_ms=snapshot.age_ms,
        chase_count=0,
        attackable_count=0,
        chase_left_count=0,
        chase_right_count=0,
        attackable_left_count=0,
        attackable_right_count=0,
        nearest_dx=None,
        created_at=now,
    )


def _intent_signature(intent):
    """返回用于过滤重复决策日志的动作意图特征。"""
    return (
        intent.source,
        intent.horizontal,
        intent.vertical,
        intent.action,
        intent.target_direction,
    )


def _trace_action_decision(runtime, state, intent, position):
    """仅在动作特征变化时记录一次完整决策，避免每20ms重复写日志。"""
    signature = _intent_signature(intent)
    if signature == state.last_intent_signature:
        return False
    previous_signature = state.last_intent_signature
    state.last_intent_signature = signature
    runtime.trace_event(
        "mushroom_v2_action_decision",
        previous_signature=previous_signature,
        source=intent.source,
        horizontal=intent.horizontal,
        vertical=intent.vertical,
        action=intent.action,
        target_direction=intent.target_direction,
        target_age_ms=intent.target_age_ms,
        chase_count=intent.chase_count,
        attackable_count=intent.attackable_count,
        nearest_dx=intent.nearest_dx,
        position=position,
        patrol_direction=state.patrol_direction,
        on_upper_platform=state.on_upper_platform,
    )
    return True


def _update_chase_activity(runtime, state, intent, position):
    """维护V2追怪开始、转向和结束状态，并输出低频关键日志。"""
    if intent.source != "chase":
        if not state.chase_active:
            return
        runtime.trace_event(
            "mushroom_v2_chase_stopped",
            position=position,
            previous_direction=state.chase_direction,
            next_source=intent.source,
            patrol_direction=state.patrol_direction,
        )
        state.chase_active = False
        state.chase_direction = None
        state.chase_direction_nearest_dx = None
        _clear_chase_switch_pending(state)
        return

    if not state.chase_active:
        state.chase_active = True
        state.chase_direction = intent.horizontal
        state.chase_direction_nearest_dx = intent.nearest_dx
        runtime.trace_event(
            "mushroom_v2_chase_started",
            position=position,
            direction=intent.horizontal,
            nearest_dx=intent.nearest_dx,
            target_age_ms=intent.target_age_ms,
            resume_direction=state.patrol_direction,
        )
        return
    if state.chase_direction == intent.horizontal:
        state.chase_direction_nearest_dx = intent.nearest_dx
        return
    previous_direction = state.chase_direction
    state.chase_direction = intent.horizontal
    state.chase_direction_nearest_dx = intent.nearest_dx
    runtime.trace_event(
        "mushroom_v2_chase_direction_changed",
        position=position,
        previous_direction=previous_direction,
        direction=intent.horizontal,
        nearest_dx=intent.nearest_dx,
    )


def _clear_combat_for_movement(runtime, state):
    """追怪或路线接管时清除旧攻击意图，并立即解除V2的公共战斗标志。"""
    had_combat_state = state.combat_active
    if state.combat_active:
        _finish_combat(runtime, state)
    if had_combat_state:
        # 只在真正退出战斗时清理一次，避免正常路线循环与检测线程竞争，
        # 把刚发布的新攻击意图误删后又多等待一帧YOLO。
        runtime.清除攻击意图()
    if runtime.zant == 1:
        runtime.zant = 0


def _apply_horizontal_intent(runtime, state, intent):
    """只在最终水平方向变化时切换按键，追怪不会改写巡逻方向。"""
    if intent.horizontal not in ("left", "right"):
        if state.applied_direction is not None:
            _release_route_keys(runtime, state, reason=intent.source)
        return
    if state.applied_direction == intent.horizontal:
        return
    runtime.切换持续移动(intent.horizontal, reason=intent.source)
    state.applied_direction = intent.horizontal


def _apply_action_intent(runtime, state, intent, position, decision_changed):
    """统一应用本轮动作，并记录检测到按键执行之间的实际延迟。"""
    apply_started_at = time.monotonic()
    _update_chase_activity(runtime, state, intent, position)
    attack_executed = False

    if intent.source == "combat":
        _set_route_phase(runtime, state, "combat", position=position)
        # 首次进入战斗由_start_combat统一释放路线键；持续战斗期间只有检测到
        # 残留移动方向时才额外清理，避免同一轮重复发送keyUp。
        if state.combat_active and state.applied_direction is not None:
            _release_route_keys(runtime, state, reason="combat")
        attack_executed = _process_combat_frame(runtime, state, intent, position)
    elif intent.source == "combat_hold":
        _set_route_phase(runtime, state, "combat_hold", position=position)
        if state.applied_direction is not None:
            _release_route_keys(runtime, state, reason="combat_hold")
    elif intent.source == "chase_hold":
        _set_route_phase(
            runtime,
            state,
            "chase_hold",
            position=position,
            nearest_dx=intent.nearest_dx,
            target_direction=intent.target_direction,
        )
        if state.applied_direction is not None:
            _release_route_keys(runtime, state, reason="chase_hold")
    elif intent.source == "chase":
        _clear_combat_for_movement(runtime, state)
        _set_route_phase(runtime, state, "chase", position=position)
        _apply_horizontal_intent(runtime, state, intent)
    else:
        _clear_combat_for_movement(runtime, state)
        _reset_attack_direction_lock(runtime, state, reason="no_visible_target")
        _set_route_phase(
            runtime,
            state,
            _patrol_phase_name(state),
            position=position,
        )
        _apply_horizontal_intent(runtime, state, intent)

    if decision_changed or attack_executed:
        runtime.trace_event(
            "mushroom_v2_action_applied",
            source=intent.source,
            horizontal=intent.horizontal,
            vertical=intent.vertical,
            action=intent.action,
            attack_executed=attack_executed,
            target_age_ms=intent.target_age_ms,
            decision_to_apply_ms=round(
                (apply_started_at - intent.created_at) * 1000,
                3,
            ),
            apply_duration_ms=round(
                (time.monotonic() - apply_started_at) * 1000,
                3,
            ),
            position=runtime.读取人物位置(),
            patrol_direction=state.patrol_direction,
            applied_direction=state.applied_direction,
        )


# 以下名称是路线模块统一使用的公开接口。保留上方旧私有函数，避免历史地图
# 或外部脚本在升级后立即失效；新路线只应调用这些不带下划线的名称。
RouteCombatState = MushroomCombatState
RouteMonsterSnapshot = MushroomMonsterSnapshot
RouteActionIntent = MushroomActionIntent


def set_route_phase(runtime, state, phase, position=None, **details):
    """更新公共路线阶段，并只在阶段变化时记录关键日志。"""
    return _set_route_phase(runtime, state, phase, position=position, **details)


def wait_for_runtime_ready(runtime, timeout=8.0):
    """等待人物定位和怪物检测首帧就绪，供所有自定义路线统一启动。"""
    return _wait_for_runtime_ready(runtime, timeout=timeout)


def release_route_keys(runtime, state, reason=None):
    """释放路线持续键并同步清理公共战斗状态中的已应用方向。"""
    return _release_route_keys(runtime, state, reason=reason)


def finish_combat(runtime, state):
    """结束当前公共战斗会话并清理攻击方向锁和残留按键。"""
    return _finish_combat(runtime, state)


def reset_attack_direction_lock(runtime, state, reason):
    """在路线重新接管时清除旧目标方向，避免继续朝死亡怪物方向攻击。"""
    return _reset_attack_direction_lock(runtime, state, reason=reason)


def read_monster_snapshot(runtime):
    """读取统一怪物快照，并过滤超过有效期的旧检测和死亡残影结果。"""
    return _read_monster_snapshot(runtime)


def build_action_intent(state, snapshot):
    """按照攻击、近身等待、追怪和恢复路线的统一优先级生成动作意图。"""
    return _build_action_intent(state, snapshot)


def trace_action_decision(runtime, state, intent, position):
    """记录变化后的公共战斗决策，并过滤每帧完全相同的重复日志。"""
    return _trace_action_decision(runtime, state, intent, position)


def clear_combat_for_movement(runtime, state):
    """当自定义路线重新移动时退出旧战斗并清除已经消费过的攻击意图。"""
    return _clear_combat_for_movement(runtime, state)


def apply_action_intent(runtime, state, intent, position, decision_changed):
    """统一执行攻击、方向锁、追怪、停顿抗抖或路线移动动作。"""
    return _apply_action_intent(
        runtime,
        state,
        intent,
        position,
        decision_changed,
    )


__all__ = (
    "MushroomActionIntent",
    "MushroomCombatState",
    "MushroomMonsterSnapshot",
    "RouteActionIntent",
    "RouteCombatState",
    "RouteMonsterSnapshot",
    "V2_ATTACK_KEY_HOLD_SECONDS",
    "V2_ATTACK_MOVE_LOCK_SECONDS",
    "V2_ATTACK_RECHECK_SECONDS",
    "V2_DIRECTION_SETTLE_SECONDS",
    "V2_FACING_REASSERT_ATTACKS",
    "V2_FACING_REASSERT_COOLDOWN_SECONDS",
    "V2_FACING_SNAPSHOT_TTL_SECONDS",
    "V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE",
    "V2_NEAR_CHASE_DEBOUNCE_SECONDS",
    "V2_CHASE_DIRECTION_COUNT_ADVANTAGE",
    "V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS",
    "V2_ROUTE_RESUME_DEBOUNCE_SECONDS",
    "V2_TARGET_SNAPSHOT_TTL_SECONDS",
    "apply_action_intent",
    "build_action_intent",
    "clear_combat_for_movement",
    "finish_combat",
    "read_monster_snapshot",
    "release_route_keys",
    "reset_attack_direction_lock",
    "set_route_phase",
    "trace_action_decision",
    "wait_for_runtime_ready",
)

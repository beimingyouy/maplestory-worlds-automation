"""蘑菇V2专用的绳子定位、攀爬、脱困和定时休息逻辑。"""

import time

from ..public.combat_actions import release_route_keys, set_route_phase


# 最新轨迹显示人物在小地图 X=160 已经到达实际最右侧；旧值169永远不会触发回头。
RIGHT_BOUNDARY_X = 158

# 绳子存在两个有效起跳侧：X=28～30、Y≈147 向右跳，X=32～34、Y≈147
# 向左跳。X=31只用于两侧过渡，不直接起跳。
ROPE_MIN_X = 28
ROPE_MAX_X = 34
ROPE_MIN_Y = 138
ROPE_MAX_Y = 149
ROPE_RIGHT_JUMP_MIN_X = 28
ROPE_RIGHT_JUMP_MAX_X = 30
ROPE_RIGHT_JUMP_MIN_Y = 145
ROPE_RIGHT_JUMP_MAX_Y = 149
ROPE_LEFT_JUMP_MIN_X = 32
ROPE_LEFT_JUMP_MAX_X = 34
ROPE_LEFT_JUMP_MIN_Y = 145
ROPE_LEFT_JUMP_MAX_Y = 149

# 一次经过绳区只尝试一次；人物向右离开绳区后才允许下一轮重新尝试。
ROPE_REARM_X = 62
ROPE_RETRY_SECONDS = 1.0

# 挂绳探测使用完整的一段连续轨迹，不能只看一次跳起后的瞬时坐标。
ROPE_ATTACH_ATTEMPTS = 8
ROPE_ATTACH_PROBE_SECONDS = 0.72
ROPE_ATTACH_SAMPLE_SECONDS = 0.10
ROPE_ATTACH_MIN_BEST_ASCENT = 8
ROPE_ATTACH_MIN_FINAL_ASCENT = 6
ROPE_ATTACH_MAX_HORIZONTAL_DRIFT = 12
# 失败重试允许在X=28～34之间重新对位，最终起跳方向由上述两个坐标区决定。
ROPE_ATTACH_MIN_X = 28
ROPE_ATTACH_CENTER_X = 31
ROPE_ATTACH_MAX_X = 34
ROPE_ATTACH_HORIZONTAL_HOLD_SECONDS = 0.18
ROPE_JUMP_KEY_HOLD_SECONDS = 0.04
ROPE_SLOPE_ALIGN_TIMEOUT_SECONDS = 3.0
ROPE_SLOPE_SAMPLE_SECONDS = 0.08

# 确认挂绳后持续向上，达到上层高度并短暂稳定后才判定成功。
ROPE_CLIMB_TIMEOUT_SECONDS = 5.0
ROPE_CLIMB_NO_PROGRESS_SECONDS = 1.0
ROPE_CLIMB_MIN_TOTAL_ASCENT = 12
# 最新日志实测人物从绳脚起跳后会稳定在 Y=117；这是实际绳顶，不再按
# 绳脚坐标机械减30，否则已经到顶仍会被误判为 rope_top_not_reached。
ROPE_TOP_TARGET_Y = 117
ROPE_TOP_SETTLE_TIMEOUT_SECONDS = 2.5
ROPE_TOP_EXIT_ATTEMPTS = 6
ROPE_TOP_EXIT_HOLD_SECONDS = 0.35
ROPE_TOP_EXIT_MIN_HORIZONTAL_MOVE = 2
ROPE_TOP_RECLIMB_SECONDS = 0.30
ROPE_FALL_DROP_THRESHOLD_Y = 8
ROPE_RECOVERY_SETTLE_TIMEOUT_SECONDS = 1.2
ROPE_RECOVERY_STABLE_SAMPLES = 3

# 起跳后若人物停在X=28～34、Y=132～144，说明可能已挂住绳子中下段，
# 也可能被树体卡住。连续静止后主动恢复，避免巡逻键一直按住但坐标不再变化。
ROPE_SUSPENDED_MIN_X = 28
ROPE_SUSPENDED_MAX_X = 34
ROPE_SUSPENDED_MIN_Y = 132
ROPE_SUSPENDED_MAX_Y = 144
ROPE_SUSPENDED_STALL_SECONDS = 1.2
ROPE_SUSPENDED_RECOVERY_COOLDOWN_SECONDS = 2.0
ROPE_SUSPENDED_DOWN_HOLD_SECONDS = 1.0
ROPE_SUSPENDED_JUMP_HOLD_SECONDS = 0.05
ROPE_SUSPENDED_JUMP_MOVE_SECONDS = 0.35
ROPE_SUSPENDED_SETTLE_SECONDS = 0.65

# 默认每连续刷图20分钟，在下一次爬绳时停到绳子中段休息1分钟；页面配置
# 会覆盖这两个默认值。休息后继续爬到Y=117，被打落则保留剩余时间重爬。
ROPE_REST_INTERVAL_SECONDS = 20 * 60
ROPE_REST_DURATION_SECONDS = 60
ROPE_REST_MIDDLE_Y = 132
ROPE_REST_FALL_THRESHOLD_Y = 8
ROPE_REST_HORIZONTAL_TOLERANCE_X = 8
ROPE_REST_SAMPLE_SECONDS = 0.5

# 爬绳成功后先向右刷上层，到右边界再向左刷回；回到绳脚附近才退出上层阶段。
UPPER_PLATFORM_ENTER_MAX_Y = 130
UPPER_PLATFORM_EXIT_MIN_Y = 142


def _rope_rest_interval_seconds(runtime):
    """返回页面配置的绳中休息周期秒数，并兼容旧运行入口。"""
    return max(
        0.0,
        float(
            getattr(
                runtime,
                "绳子休息间隔分钟",
                ROPE_REST_INTERVAL_SECONDS / 60.0,
            )
        )
        * 60.0,
    )


def _rope_rest_duration_seconds(runtime):
    """返回页面配置的单次绳中休息秒数，并兼容旧运行入口。"""
    return max(
        0.0,
        float(
            getattr(
                runtime,
                "绳子休息时长分钟",
                ROPE_REST_DURATION_SECONDS / 60.0,
            )
        )
        * 60.0,
    )


def _rope_jump_direction(position):
    """按人物所在的左右绳脚坐标返回应使用的水平起跳方向。"""
    if position is None:
        return None
    x, y = position
    if (
        ROPE_RIGHT_JUMP_MIN_X <= x <= ROPE_RIGHT_JUMP_MAX_X
        and ROPE_RIGHT_JUMP_MIN_Y <= y <= ROPE_RIGHT_JUMP_MAX_Y
    ):
        return "right"
    if (
        ROPE_LEFT_JUMP_MIN_X <= x <= ROPE_LEFT_JUMP_MAX_X
        and ROPE_LEFT_JUMP_MIN_Y <= y <= ROPE_LEFT_JUMP_MAX_Y
    ):
        return "left"
    return None


def _is_in_rope_zone(position):
    """返回人物是否位于左低右高两个有效绳脚区域之一。"""
    return _rope_jump_direction(position) in ("left", "right")


def _is_suspended_near_rope(position):
    """判断人物是否停在绳子或树体的中下部悬挂区域。"""
    if position is None:
        return False
    x, y = position
    return (
        ROPE_SUSPENDED_MIN_X <= x <= ROPE_SUSPENDED_MAX_X
        and ROPE_SUSPENDED_MIN_Y <= y <= ROPE_SUSPENDED_MAX_Y
    )


def _reset_suspended_tracking(state):
    """清空主循环用于判断绳边连续静止的坐标和计时。"""
    state.suspended_last_position = None
    state.suspended_since = 0.0


def _update_suspended_tracking(state, position):
    """更新绳边静止计时，并返回是否已经达到强制恢复条件。"""
    if state.climb_armed or not _is_suspended_near_rope(position):
        _reset_suspended_tracking(state)
        return False

    now = time.monotonic()
    previous = state.suspended_last_position
    if (
        previous is None
        or abs(position[0] - previous[0]) > 1
        or abs(position[1] - previous[1]) > 1
    ):
        state.suspended_last_position = position
        state.suspended_since = now
        return False

    state.suspended_last_position = position
    if state.suspended_since <= 0.0:
        state.suspended_since = now
        return False
    return (
        now - state.suspended_since >= ROPE_SUSPENDED_STALL_SECONDS
        and now - state.last_suspended_recovery_at
        >= ROPE_SUSPENDED_RECOVERY_COOLDOWN_SECONDS
    )


def _recover_from_suspended_rope(runtime, state, position, reason):
    """人物卡在绳中时先向下脱离，失败后向右跳离并返回最新坐标。"""
    if not _is_suspended_near_rope(position):
        return True, position

    state.last_suspended_recovery_at = time.monotonic()
    _reset_suspended_tracking(state)
    set_route_phase(
        runtime,
        state,
        "rope_suspended_recovery",
        position=position,
        reason=reason,
    )
    release_route_keys(runtime, state, reason="rope_suspended_recovery")
    runtime.释放攻击键()
    runtime.pydirectinput.keyUp("down")
    runtime.trace_event(
        "mushroom_v2_suspended_recovery_started",
        position=position,
        reason=reason,
    )

    runtime.pydirectinput.keyDown("down")
    try:
        runtime.可中断等待(
            ROPE_SUSPENDED_DOWN_HOLD_SECONDS,
            interval=0.02,
        )
    finally:
        runtime.pydirectinput.keyUp("down")

    if runtime.已请求停止():
        return False, runtime.读取人物位置()
    runtime.可中断等待(0.18, interval=0.02)
    down_position = runtime.读取人物位置()
    runtime.trace_event(
        "mushroom_v2_suspended_recovery_down",
        before_position=position,
        position=down_position,
    )
    if not _is_suspended_near_rope(down_position):
        runtime.trace_event(
            "mushroom_v2_suspended_recovery_completed",
            before_position=position,
            final_position=down_position,
            recovery_action="down",
        )
        return True, down_position

    # 向下仍卡住时，使用不带“上”的右+C离开树体；随后重新读取落点，
    # 由爬绳流程重新对位，不能在悬挂状态下继续按左右巡逻键。
    runtime.pydirectinput.keyDown("right")
    runtime.pydirectinput.keyDown("c")
    try:
        if runtime.可中断等待(
            ROPE_SUSPENDED_JUMP_HOLD_SECONDS,
            interval=0.01,
        ):
            runtime.pydirectinput.keyUp("c")
            runtime.可中断等待(
                ROPE_SUSPENDED_JUMP_MOVE_SECONDS,
                interval=0.02,
            )
    finally:
        runtime.pydirectinput.keyUp("c")
        runtime.pydirectinput.keyUp("right")

    runtime.可中断等待(
        ROPE_SUSPENDED_SETTLE_SECONDS,
        interval=0.02,
    )
    final_position = runtime.读取人物位置()
    runtime.trace_event(
        "mushroom_v2_suspended_recovery_jump_off",
        before_position=down_position,
        final_position=final_position,
        action="right+c",
    )
    recovered = not _is_suspended_near_rope(final_position)
    runtime.trace_event(
        (
            "mushroom_v2_suspended_recovery_completed"
            if recovered
            else "mushroom_v2_suspended_recovery_failed"
        ),
        before_position=position,
        final_position=final_position,
        recovery_action="down_then_right+c",
    )
    return recovered, final_position


def _update_climb_arming(runtime, state, position):
    """人物向右离开绳区后重新允许下一圈爬绳，防止同一区域连续触发。"""
    if state.climb_armed or position is None:
        return
    # 上层向右经过 X=62 时不能重新启用爬绳；必须先从上层掉回底层，
    # 再沿底层向右离开绳区，下一圈向左回来时才允许重新爬绳。
    if state.on_upper_platform:
        return
    if position[0] < ROPE_REARM_X:
        return
    state.climb_armed = True
    runtime.trace_event(
        "mushroom_v2_climb_rearmed",
        position=position,
        rearm_x=ROPE_REARM_X,
        patrol_direction=state.patrol_direction,
    )


def _record_climb_position(runtime, state, position, **details):
    """把爬绳阶段的坐标写入人物轨迹，供实测日志复盘。"""
    if position is None:
        return
    runtime.记录人物位置(
        position,
        interval=0.08,
        route="mushroom_v2",
        phase="climb",
        patrol_direction=state.patrol_direction,
        applied_direction=None,
        climb_armed=state.climb_armed,
        on_upper_platform=state.on_upper_platform,
        rope_zone=_is_in_rope_zone(position),
        **details,
    )


def _update_platform_level(runtime, state, position):
    """根据爬绳结果和人物Y坐标维护V2是否仍在上层平台。"""
    if not state.on_upper_platform or position is None:
        return
    if position[1] < UPPER_PLATFORM_EXIT_MIN_Y:
        return
    state.on_upper_platform = False
    runtime.trace_event(
        "mushroom_v2_upper_platform_exited",
        position=position,
        patrol_direction=state.patrol_direction,
        reason="returned_to_bottom",
    )


def _enter_upper_platform(runtime, state, position):
    """爬绳成功后进入上层刷图阶段，并从绳子位置开始向右刷平台。"""
    if position is None or position[1] > UPPER_PLATFORM_ENTER_MAX_Y:
        return False
    state.on_upper_platform = True
    state.patrol_direction = "right"
    state.applied_direction = None
    runtime.trace_event(
        "mushroom_v2_upper_platform_entered",
        position=position,
        direction="right",
        right_boundary_x=RIGHT_BOUNDARY_X,
    )
    return True


def _patrol_phase_name(state):
    """根据人物所在层返回用于日志展示的巡逻阶段名。"""
    return "upper_patrol" if state.on_upper_platform else "patrol"


def _walk_to_rope_slope_start(runtime, state, attempt):
    """失败重试时走到X=28～30或32～34的有效绳脚，并保留真实坐标。"""
    before_position = runtime.读取人物位置()
    if before_position is None:
        return False, None
    if _rope_jump_direction(before_position) in ("left", "right"):
        return True, before_position

    before_x = before_position[0]
    if before_x < ROPE_ATTACH_MIN_X:
        align_direction = "right"
    elif before_x > ROPE_ATTACH_MAX_X:
        align_direction = "left"
    elif before_x == ROPE_ATTACH_CENTER_X:
        # X=31处于两个起跳区中间，向左一步进入X=30的右跳区。
        align_direction = "left"
    elif before_x <= ROPE_RIGHT_JUMP_MAX_X:
        # 横坐标已在左侧但Y尚未稳定时，向右经过斜坡后重新寻找有效区。
        align_direction = "right"
    else:
        align_direction = "left"
    opposite_direction = "left" if align_direction == "right" else "right"
    runtime.pydirectinput.keyUp(opposite_direction)
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("c")
    runtime.pydirectinput.keyDown(align_direction)
    runtime.trace_event(
        "mushroom_v2_rope_slope_align_started",
        attempt=attempt,
        before_position=before_position,
        target_condition=(
            "28<=x<=30 and 145<=y<=149, or "
            "32<=x<=34 and 145<=y<=149"
        ),
        movement="{}_only".format(align_direction),
    )

    deadline = time.monotonic() + ROPE_SLOPE_ALIGN_TIMEOUT_SECONDS
    final_position = before_position
    completed = False
    try:
        while time.monotonic() < deadline and not runtime.已请求停止():
            if not runtime.可中断等待(
                ROPE_SLOPE_SAMPLE_SECONDS,
                interval=0.02,
            ):
                break
            current_position = runtime.读取人物位置()
            if current_position is None:
                continue
            final_position = current_position
            _record_climb_position(
                runtime,
                state,
                current_position,
                climb_stage="slope_align",
                climb_attempt=attempt,
                climb_direction=align_direction,
            )
            if _rope_jump_direction(current_position) in ("left", "right"):
                completed = True
                break
    finally:
        runtime.pydirectinput.keyUp(align_direction)

    runtime.trace_event(
        (
            "mushroom_v2_rope_slope_align_completed"
            if completed
            else "mushroom_v2_rope_slope_align_failed"
        ),
        attempt=attempt,
        before_position=before_position,
        final_position=final_position,
        target_min_x=ROPE_ATTACH_MIN_X,
        target_max_x=ROPE_ATTACH_MAX_X,
        target_y=147,
        jump_direction=_rope_jump_direction(final_position),
    )
    return completed, final_position


def _probe_rope_attachment(runtime, state, attempt):
    """按X=28～30向右、X=32～34向左的规则起跳并确认是否挂绳。"""
    before_position = runtime.读取人物位置()
    if before_position is None:
        return False, None
    horizontal_direction = _rope_jump_direction(before_position)
    if horizontal_direction not in ("left", "right"):
        runtime.trace_event(
            "mushroom_v2_rope_probe_skipped",
            attempt=attempt,
            position=before_position,
            reason="slope_not_aligned",
            required_condition=(
                "28<=x<=30,y~=147 => right; "
                "32<=x<=34,y~=147 => left"
            ),
        )
        return False, before_position

    runtime.trace_event(
        "mushroom_v2_rope_jump_started",
        attempt=attempt,
        position=before_position,
        horizontal_direction=horizontal_direction,
        action="{}+c+up".format(horizontal_direction),
        rope_center=(ROPE_ATTACH_CENTER_X, 147),
    )
    runtime.pydirectinput.keyDown(horizontal_direction)
    runtime.pydirectinput.keyDown("c")
    if not runtime.可中断等待(ROPE_JUMP_KEY_HOLD_SECONDS, interval=0.01):
        runtime.pydirectinput.keyUp("c")
        runtime.pydirectinput.keyUp(horizontal_direction)
        return False, runtime.读取人物位置()
    runtime.pydirectinput.keyUp("c")
    runtime.pydirectinput.keyDown("up")

    # 水平方向只在起跳初段短暂保持，使人物向绳子中心靠近；之后只按上键，
    # 避免已经挂住后又被水平键从绳子上拉下来。
    if not runtime.可中断等待(
        ROPE_ATTACH_HORIZONTAL_HOLD_SECONDS,
        interval=0.02,
    ):
        runtime.pydirectinput.keyUp(horizontal_direction)
        runtime.pydirectinput.keyUp("up")
        return False, runtime.读取人物位置()
    runtime.pydirectinput.keyUp(horizontal_direction)

    samples = []
    deadline = time.monotonic() + ROPE_ATTACH_PROBE_SECONDS
    while time.monotonic() < deadline and not runtime.已请求停止():
        if not runtime.可中断等待(
            ROPE_ATTACH_SAMPLE_SECONDS,
            interval=0.02,
        ):
            runtime.pydirectinput.keyUp("up")
            return False, runtime.读取人物位置()
        current_position = runtime.读取人物位置()
        if current_position is None:
            continue
        samples.append(current_position)
        _record_climb_position(
            runtime,
            state,
            current_position,
            climb_stage="attach_probe",
            climb_attempt=attempt,
            probe_sample=len(samples),
        )

    final_position = samples[-1] if samples else runtime.读取人物位置()
    if final_position is None:
        runtime.pydirectinput.keyUp("up")
        return False, None

    best_y = min((position[1] for position in samples), default=final_position[1])
    best_ascent = before_position[1] - best_y
    final_ascent = before_position[1] - final_position[1]
    horizontal_drift = abs(final_position[0] - before_position[0])
    recent_ascent = 0
    if len(samples) >= 3:
        recent_ascent = samples[-3][1] - samples[-1][1]

    recent_samples = samples[-3:]
    suspended_stable = (
        _is_suspended_near_rope(final_position)
        and len(recent_samples) >= 3
        and max(position[1] for position in recent_samples)
        - min(position[1] for position in recent_samples)
        <= 1
        and max(position[0] for position in recent_samples)
        - min(position[0] for position in recent_samples)
        <= 1
    )

    attached = (
        best_ascent >= ROPE_ATTACH_MIN_BEST_ASCENT
        and final_ascent >= ROPE_ATTACH_MIN_FINAL_ASCENT
        and horizontal_drift <= ROPE_ATTACH_MAX_HORIZONTAL_DRIFT
        and (
            recent_ascent >= 1
            or final_ascent >= 10
            or suspended_stable
        )
    )
    runtime.trace_event(
        "mushroom_v2_climb_probe_result",
        attempt=attempt,
        before_position=before_position,
        final_position=final_position,
        sample_count=len(samples),
        best_ascent=best_ascent,
        final_ascent=final_ascent,
        recent_ascent=recent_ascent,
        horizontal_drift=horizontal_drift,
        horizontal_direction=horizontal_direction,
        suspended_stable=suspended_stable,
        target_x=(
            ROPE_RIGHT_JUMP_MAX_X
            if horizontal_direction == "right"
            else ROPE_LEFT_JUMP_MIN_X
        ),
        attached=attached,
    )
    if not attached:
        runtime.pydirectinput.keyUp("up")
    return attached, final_position


def _wait_for_fall_position(runtime, state, detected_position, stage):
    """人物被击落后等待坐标短暂稳定，并返回下一次爬绳使用的真实落点。"""
    deadline = time.monotonic() + ROPE_RECOVERY_SETTLE_TIMEOUT_SECONDS
    last_position = detected_position
    stable_samples = 0
    sample_index = 0

    while time.monotonic() < deadline and not runtime.已请求停止():
        if not runtime.可中断等待(
            ROPE_ATTACH_SAMPLE_SECONDS,
            interval=0.02,
        ):
            break
        current_position = runtime.读取人物位置()
        if current_position is None:
            continue
        sample_index += 1
        _record_climb_position(
            runtime,
            state,
            current_position,
            climb_stage="fall_recovery",
            fall_stage=stage,
            recovery_sample=sample_index,
        )
        if (
            last_position is not None
            and abs(current_position[0] - last_position[0]) <= 1
            and abs(current_position[1] - last_position[1]) <= 1
        ):
            stable_samples += 1
        else:
            stable_samples = 0
        last_position = current_position
        if (
            stable_samples >= ROPE_RECOVERY_STABLE_SAMPLES
            and current_position[1] >= ROPE_MIN_Y
        ):
            break

    runtime.trace_event(
        "mushroom_v2_climb_fall_position_acquired",
        detected_position=detected_position,
        recovery_position=last_position,
        fall_stage=stage,
        stable_samples=stable_samples,
    )
    return last_position


def _is_climb_fall(position, best_y):
    """根据人物相对已到达最高点的明显下落判断本次爬绳是否被打断。"""
    return (
        position is not None
        and position[1] >= best_y + ROPE_FALL_DROP_THRESHOLD_Y
        and position[1] >= ROPE_MIN_Y
    )


def _exit_rope_to_upper_platform(runtime, state, best_y):
    """到达绳顶后向右离绳，并用水平位移与平台高度共同确认落脚成功。"""
    for exit_attempt in range(1, ROPE_TOP_EXIT_ATTEMPTS + 1):
        if runtime.已请求停止():
            return False, "stop_requested", runtime.读取人物位置()

        runtime.pydirectinput.keyUp("up")
        before_position = runtime.读取人物位置()
        runtime.trace_event(
            "mushroom_v2_rope_exit_started",
            exit_attempt=exit_attempt,
            before_position=before_position,
            direction="right",
        )
        runtime.pydirectinput.keyDown("right")
        completed = runtime.可中断等待(
            ROPE_TOP_EXIT_HOLD_SECONDS,
            interval=0.02,
        )
        runtime.pydirectinput.keyUp("right")
        if not completed:
            return False, "exit_interrupted", runtime.读取人物位置()

        after_position = runtime.读取人物位置()
        _record_climb_position(
            runtime,
            state,
            after_position,
            climb_stage="rope_exit",
            exit_attempt=exit_attempt,
        )
        moved_x = (
            after_position[0] - before_position[0]
            if before_position is not None and after_position is not None
            else None
        )
        verified = (
            after_position is not None
            and moved_x is not None
            and moved_x >= ROPE_TOP_EXIT_MIN_HORIZONTAL_MOVE
            and after_position[1] <= UPPER_PLATFORM_ENTER_MAX_Y
        )
        if verified:
            runtime.trace_event(
                "mushroom_v2_rope_exit_verified",
                exit_attempt=exit_attempt,
                before_position=before_position,
                final_position=after_position,
                moved_x=moved_x,
            )
            return True, "platform_verified", after_position

        if _is_climb_fall(after_position, best_y):
            runtime.trace_event(
                "mushroom_v2_climb_knocked_down",
                fall_stage="rope_exit",
                exit_attempt=exit_attempt,
                best_y=best_y,
                detected_position=after_position,
            )
            recovery_position = _wait_for_fall_position(
                runtime,
                state,
                after_position,
                "rope_exit",
            )
            return False, "knocked_down", recovery_position

        runtime.trace_event(
            "mushroom_v2_rope_exit_failed",
            exit_attempt=exit_attempt,
            before_position=before_position,
            final_position=after_position,
            moved_x=moved_x,
            reason="still_on_rope",
        )
        # 仍在绳顶附近时再次向上顶住一小段时间，然后重新尝试向右离绳。
        runtime.pydirectinput.keyDown("up")
        if not runtime.可中断等待(
            ROPE_TOP_RECLIMB_SECONDS,
            interval=0.02,
        ):
            runtime.pydirectinput.keyUp("up")
            return False, "reclimb_interrupted", runtime.读取人物位置()

    runtime.pydirectinput.keyUp("up")
    return False, "rope_exit_not_verified", runtime.读取人物位置()


def _update_rope_rest_schedule(runtime, state, now=None):
    """运行满配置周期后，标记下一次爬绳需要在中段休息。"""
    current_time = time.monotonic() if now is None else now
    interval_seconds = _rope_rest_interval_seconds(runtime)
    duration_seconds = _rope_rest_duration_seconds(runtime)
    if interval_seconds <= 0 or duration_seconds <= 0:
        state.next_rope_rest_at = 0.0
        state.rope_rest_pending = False
        state.rope_rest_remaining_seconds = 0.0
        return False
    if state.next_rope_rest_at <= 0:
        state.next_rope_rest_at = current_time + interval_seconds
        return False
    if state.rope_rest_pending or current_time < state.next_rope_rest_at:
        return False

    state.rope_rest_pending = True
    state.rope_rest_remaining_seconds = duration_seconds
    runtime.trace_event(
        "mushroom_v2_rope_rest_scheduled",
        rest_count=state.rope_rest_count + 1,
        interval_minutes=round(interval_seconds / 60, 2),
        duration_seconds=duration_seconds,
    )
    return True


def _should_pause_at_rope_middle(state, position):
    """返回本轮定时休息是否已到达绳子中段的目标高度。"""
    return (
        state.rope_rest_pending
        and position is not None
        and position[1] <= ROPE_REST_MIDDLE_Y
    )


def _rest_at_rope_middle(runtime, state, position):
    """在绳子中段可中断地休息剩余时间，完成后允许继续向上爬。"""
    set_route_phase(runtime, state, "rope_rest", position=position)
    release_route_keys(runtime, state, reason="rope_rest")
    runtime.清除攻击意图()
    runtime.释放攻击键()

    duration_seconds = _rope_rest_duration_seconds(runtime)
    remaining_seconds = state.rope_rest_remaining_seconds
    if remaining_seconds <= 0:
        remaining_seconds = duration_seconds
    rest_position = position
    runtime.trace_event(
        "mushroom_v2_rope_rest_started",
        rest_count=state.rope_rest_count + 1,
        position=position,
        target_y=ROPE_REST_MIDDLE_Y,
        remaining_seconds=round(remaining_seconds, 2),
        duration_seconds=duration_seconds,
        resumed=remaining_seconds < duration_seconds,
    )

    while remaining_seconds > 0 and not runtime.已请求停止():
        sample_started_at = time.monotonic()
        completed_wait = runtime.可中断等待(
            min(ROPE_REST_SAMPLE_SECONDS, remaining_seconds),
            interval=0.05,
        )
        remaining_seconds = max(
            0.0,
            remaining_seconds - (time.monotonic() - sample_started_at),
        )
        state.rope_rest_remaining_seconds = remaining_seconds
        if not completed_wait:
            runtime.trace_event(
                "mushroom_v2_rope_rest_interrupted",
                rest_count=state.rope_rest_count + 1,
                position=runtime.读取人物位置(),
                remaining_seconds=round(remaining_seconds, 2),
                reason="stop_requested",
            )
            return False, "rope_rest_stop_requested", runtime.读取人物位置()

        current_position = runtime.读取人物位置()
        if current_position is None:
            continue
        _record_climb_position(
            runtime,
            state,
            current_position,
            climb_stage="rope_rest",
            rest_remaining_seconds=round(remaining_seconds, 2),
        )
        fell_below_middle = (
            current_position[1]
            >= ROPE_REST_MIDDLE_Y + ROPE_REST_FALL_THRESHOLD_Y
        )
        moved_away_from_rope = (
            rest_position is not None
            and abs(current_position[0] - rest_position[0])
            > ROPE_REST_HORIZONTAL_TOLERANCE_X
        )
        if fell_below_middle or moved_away_from_rope:
            runtime.trace_event(
                "mushroom_v2_rope_rest_interrupted",
                rest_count=state.rope_rest_count + 1,
                position=current_position,
                rest_position=rest_position,
                remaining_seconds=round(remaining_seconds, 2),
                reason=(
                    "knocked_down"
                    if fell_below_middle
                    else "moved_away_from_rope"
                ),
            )
            return False, "rope_rest_knocked_down", current_position

    if runtime.已请求停止():
        return False, "rope_rest_stop_requested", runtime.读取人物位置()

    state.rope_rest_pending = False
    state.rope_rest_remaining_seconds = 0.0
    state.rope_rest_count += 1
    interval_seconds = _rope_rest_interval_seconds(runtime)
    state.next_rope_rest_at = time.monotonic() + interval_seconds
    final_position = runtime.读取人物位置()
    runtime.trace_event(
        "mushroom_v2_rope_rest_completed",
        rest_count=state.rope_rest_count,
        position=final_position,
        rested_seconds=duration_seconds,
        next_rest_in_seconds=interval_seconds,
    )
    set_route_phase(runtime, state, "climb", position=final_position)
    return True, "rope_rest_completed", final_position


def _finish_verified_climb(runtime, state, entry_position, attached_position):
    """持续爬到实测绳顶Y<=117，并在向右离绳站上平台后返回成功。"""
    best_y = attached_position[1]
    last_progress_at = time.monotonic()
    deadline = time.monotonic() + ROPE_CLIMB_TIMEOUT_SECONDS
    reached_rope_top = False

    # 探测成功时上键仍保持按下；这里继续按住并持续读取真实坐标。
    while time.monotonic() < deadline and not runtime.已请求停止():
        if not runtime.可中断等待(
            ROPE_ATTACH_SAMPLE_SECONDS,
            interval=0.02,
        ):
            runtime.pydirectinput.keyUp("up")
            return False, "ascent_interrupted", runtime.读取人物位置()
        current_position = runtime.读取人物位置()
        if current_position is None:
            continue
        _update_rope_rest_schedule(runtime, state)
        _record_climb_position(
            runtime,
            state,
            current_position,
            climb_stage="ascending",
        )
        if current_position[1] < best_y:
            best_y = current_position[1]
            last_progress_at = time.monotonic()

        if _should_pause_at_rope_middle(state, current_position):
            rest_completed, rest_reason, rest_final_position = (
                _rest_at_rope_middle(runtime, state, current_position)
            )
            if not rest_completed:
                runtime.pydirectinput.keyUp("up")
                if rest_reason == "rope_rest_knocked_down":
                    rest_final_position = _wait_for_fall_position(
                        runtime,
                        state,
                        rest_final_position,
                        "rope_rest",
                    )
                return False, rest_reason, rest_final_position

            # 休息结束后重新按住上键，并重置爬升超时和进度计时；休息的60秒
            # 不计入正常5秒爬绳超时，随后继续爬到Y=117再进入上层路线。
            runtime.pydirectinput.keyDown("up")
            current_position = rest_final_position or runtime.读取人物位置()
            if current_position is not None:
                best_y = min(best_y, current_position[1])
            last_progress_at = time.monotonic()
            deadline = time.monotonic() + ROPE_CLIMB_TIMEOUT_SECONDS
            continue

        if _is_climb_fall(current_position, best_y):
            runtime.pydirectinput.keyUp("up")
            runtime.trace_event(
                "mushroom_v2_climb_knocked_down",
                fall_stage="ascending",
                entry_position=entry_position,
                attached_position=attached_position,
                best_y=best_y,
                detected_position=current_position,
            )
            recovery_position = _wait_for_fall_position(
                runtime,
                state,
                current_position,
                "ascending",
            )
            return False, "knocked_down", recovery_position

        total_ascent = entry_position[1] - current_position[1]
        if (
            total_ascent >= ROPE_CLIMB_MIN_TOTAL_ASCENT
            and current_position[1] <= ROPE_TOP_TARGET_Y
        ):
            reached_rope_top = True
            break

        if (
            time.monotonic() - last_progress_at >= ROPE_CLIMB_NO_PROGRESS_SECONDS
            and total_ascent < ROPE_ATTACH_MIN_FINAL_ASCENT
        ):
            break

    if not reached_rope_top:
        runtime.trace_event(
            "mushroom_v2_climb_ascent_failed",
            entry_position=entry_position,
            final_position=runtime.读取人物位置(),
            best_y=best_y,
        )
        runtime.pydirectinput.keyUp("up")
        return False, "rope_top_not_reached", runtime.读取人物位置()

    # 首次到达目标高度后继续读取坐标；只有仍在绳顶高度才进入离绳阶段。
    settle_deadline = time.monotonic() + ROPE_TOP_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < settle_deadline and not runtime.已请求停止():
        current_position = runtime.读取人物位置()
        if current_position is not None and current_position[1] <= ROPE_TOP_TARGET_Y:
            break
        if not runtime.可中断等待(
            ROPE_ATTACH_SAMPLE_SECONDS,
            interval=0.02,
        ):
            runtime.pydirectinput.keyUp("up")
            return False, "top_settle_interrupted", runtime.读取人物位置()

    top_position = runtime.读取人物位置()
    if top_position is None or top_position[1] > ROPE_TOP_TARGET_Y:
        runtime.pydirectinput.keyUp("up")
        return False, "rope_top_not_stable", top_position
    runtime.trace_event(
        "mushroom_v2_rope_top_reached",
        entry_position=entry_position,
        attached_position=attached_position,
        top_position=top_position,
        best_y=best_y,
        target_y=ROPE_TOP_TARGET_Y,
    )

    verified, reason, final_position = _exit_rope_to_upper_platform(
        runtime,
        state,
        best_y,
    )
    runtime.trace_event(
        "mushroom_v2_climb_verified",
        entry_position=entry_position,
        attached_position=attached_position,
        final_position=final_position,
        best_y=best_y,
        verified=verified,
        reason=reason,
    )
    return verified, reason, final_position


def _try_climb_rope(runtime, state, entry_position):
    """在本次经过绳区时尝试一次反馈式爬绳，并返回真实成功状态。"""
    set_route_phase(runtime, state, "climb", position=entry_position)
    release_route_keys(runtime, state, reason="climb")
    state.climb_armed = False
    state.last_climb_at = time.monotonic()
    runtime.trace_event(
        "mushroom_v2_climb_started",
        position=entry_position,
        patrol_direction=state.patrol_direction,
        max_attempts=ROPE_ATTACH_ATTEMPTS,
    )

    climbed = False
    failure_reason = "attach_not_confirmed"
    try:
        current_entry_position = entry_position
        for attempt in range(1, ROPE_ATTACH_ATTEMPTS + 1):
            if runtime.已请求停止():
                failure_reason = "stop_requested"
                break
            attempt_position = runtime.读取人物位置()
            if _is_suspended_near_rope(attempt_position):
                recovered, recovery_position = _recover_from_suspended_rope(
                    runtime,
                    state,
                    attempt_position,
                    reason="retry_started_while_suspended",
                )
                if not recovered:
                    failure_reason = "suspended_recovery_failed"
                    break
                if recovery_position is not None:
                    current_entry_position = recovery_position
                set_route_phase(
                    runtime,
                    state,
                    "climb",
                    position=recovery_position,
                    reason="suspended_recovery_completed",
                )
            aligned, aligned_position = _walk_to_rope_slope_start(
                runtime,
                state,
                attempt,
            )
            if not aligned:
                failure_reason = "slope_alignment_failed"
                break
            if aligned_position is not None:
                current_entry_position = aligned_position
            attached, probe_position = _probe_rope_attachment(runtime, state, attempt)
            if attached and probe_position is not None:
                runtime.trace_event(
                    "mushroom_v2_rope_attached",
                    attempt=attempt,
                    entry_position=current_entry_position,
                    attached_position=probe_position,
                )
                climbed, failure_reason, recovery_position = _finish_verified_climb(
                    runtime,
                    state,
                    current_entry_position,
                    probe_position,
                )
                if climbed:
                    break
                if recovery_position is not None:
                    current_entry_position = recovery_position
                runtime.trace_event(
                    "mushroom_v2_climb_retry_from_position",
                    attempt=attempt,
                    reason=failure_reason,
                    retry_position=current_entry_position,
                    remaining_attempts=ROPE_ATTACH_ATTEMPTS - attempt,
                )
                # 被打落或未完成离绳时，重新读取坐标并走回左右任一有效绳脚，
                # 再按28～30向右、32～34向左的规则起跳，不沿用旧入口坐标。
                continue
            failure_reason = "attach_not_confirmed"
            if runtime.已请求停止():
                break

            if _is_suspended_near_rope(probe_position):
                recovered, recovery_position = _recover_from_suspended_rope(
                    runtime,
                    state,
                    probe_position,
                    reason="attach_probe_stalled",
                )
                if not recovered:
                    failure_reason = "suspended_recovery_failed"
                    break
                if recovery_position is not None:
                    current_entry_position = recovery_position
                set_route_phase(
                    runtime,
                    state,
                    "climb",
                    position=recovery_position,
                    reason="suspended_recovery_completed",
                )

        final_position = runtime.读取人物位置()
        if not climbed and _is_suspended_near_rope(final_position):
            recovered, final_position = _recover_from_suspended_rope(
                runtime,
                state,
                final_position,
                reason="climb_failed_while_suspended",
            )
            if recovered:
                state.climb_armed = True
                state.last_climb_at = 0.0
            else:
                failure_reason = "suspended_recovery_failed"
        if (
            not climbed
            and final_position is not None
            and _is_in_rope_zone(final_position)
        ):
            # 所有内部尝试均失败但人物仍在绳区时，允许主循环立即重新爬，
            # 避免恢复巡逻后向左走出绳区而错过本轮上层平台。
            state.climb_armed = True
            state.last_climb_at = 0.0
            runtime.trace_event(
                "mushroom_v2_climb_recovery_rearmed",
                position=final_position,
                reason=failure_reason,
            )
        runtime.trace_event(
            "mushroom_v2_climb_completed" if climbed else "mushroom_v2_climb_failed",
            entry_position=entry_position,
            final_position=final_position,
            reason=(None if climbed else failure_reason),
            climb_armed=state.climb_armed,
        )
        return climbed
    finally:
        runtime.pydirectinput.keyUp("left")
        runtime.pydirectinput.keyUp("right")
        runtime.pydirectinput.keyUp("up")
        runtime.pydirectinput.keyUp("c")
        state.applied_direction = None

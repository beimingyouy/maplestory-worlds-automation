"""自定义路线的边界计算和公共丝滑战斗调度入口。"""

from .public import combat_actions as combat_logic


CUSTOM_ROUTE_LOOP_SECONDS = 0.02


def next_custom_route_direction(
    current_x: int,
    left_x: int,
    right_x: int,
    current_direction: str,
    tolerance: int = 5,
) -> str:
    """根据黄色点 X 坐标决定是否在左右边界切换方向。"""
    if current_direction == "left":
        return "right" if current_x <= left_x + tolerance else "left"
    if current_direction == "right":
        return "left" if current_x >= right_x - tolerance else "right"
    raise ValueError("无效的自定义路线方向：{}".format(current_direction))


def run_custom_boundary_route(runtime, left_position, right_position):
    """使用V2公共识别、目标锁和攻击抗抖运行左右边界自定义路线。"""
    left_x = int(left_position[0])
    right_x = int(right_position[0])
    state = combat_logic.RouteCombatState(patrol_direction="left")
    runtime.zant = 0
    runtime.trace_event(
        "custom_route_started",
        left_position=left_position,
        right_position=right_position,
        combat_pipeline="common_v2_optimized",
        route_loop_ms=round(CUSTOM_ROUTE_LOOP_SECONDS * 1000, 1),
    )
    print(
        "[自定义路线] 左边={}，右边={}；已启用V2公共识别、追怪和攻击优化。".format(
            left_position,
            right_position,
        )
    )
    if not combat_logic.wait_for_runtime_ready(runtime):
        return
    combat_logic.set_route_phase(
        runtime,
        state,
        "patrol",
        position=runtime.读取人物位置(),
        route="custom_boundary",
    )
    try:
        while not runtime.已请求停止():
            if not runtime.可中断等待(
                CUSTOM_ROUTE_LOOP_SECONDS,
                interval=0.01,
            ):
                break
            position = runtime.读取人物位置()
            if position is not None:
                runtime.记录人物位置(
                    position,
                    interval=0.2,
                    route="custom_boundary",
                    phase=state.phase,
                    patrol_direction=state.patrol_direction,
                    applied_direction=state.applied_direction,
                )

            if runtime.zant == 2:
                combat_logic.set_route_phase(
                    runtime,
                    state,
                    "combat_wait",
                    position=position,
                )
                combat_logic.release_route_keys(
                    runtime,
                    state,
                    reason="combat_wait",
                )
                runtime.等待战斗恢复()
                continue
            if runtime.攻击移动仍锁定():
                combat_logic.set_route_phase(
                    runtime,
                    state,
                    "move_lock",
                    position=position,
                )
                combat_logic.release_route_keys(
                    runtime,
                    state,
                    reason="move_lock",
                )
                continue
            if position is None:
                combat_logic.set_route_phase(runtime, state, "position_missing")
                combat_logic.release_route_keys(
                    runtime,
                    state,
                    reason="position_missing",
                )
                continue

            next_direction = next_custom_route_direction(
                int(position[0]),
                left_x,
                right_x,
                state.patrol_direction,
            )
            if next_direction != state.patrol_direction:
                previous_direction = state.patrol_direction
                state.patrol_direction = next_direction
                runtime.trace_event(
                    "custom_route_boundary",
                    position=position,
                    previous_direction=previous_direction,
                    direction=next_direction,
                )

            snapshot = combat_logic.read_monster_snapshot(runtime)
            intent = combat_logic.build_action_intent(state, snapshot)
            decision_changed = combat_logic.trace_action_decision(
                runtime,
                state,
                intent,
                position,
            )
            combat_logic.apply_action_intent(
                runtime,
                state,
                intent,
                position,
                decision_changed,
            )
    finally:
        combat_logic.finish_combat(runtime, state)
        combat_logic.release_route_keys(runtime, state, reason="route_stopped")
        runtime.释放攻击键()
        runtime.trace_event("custom_route_stopped")

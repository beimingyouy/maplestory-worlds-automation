"""地图统一使用的战斗动作公共接口。

AI决策实现位于 ``combat_strategy``；本模块只暴露地图真正需要的稳定入口，
后续调整攻击按键、目标锁或追怪策略时，不需要修改各地图文件。
"""

from .combat_strategy import (
    RouteActionIntent,
    RouteCombatState,
    RouteMonsterSnapshot,
    V2_ATTACK_KEY_HOLD_SECONDS,
    V2_ATTACK_MOVE_LOCK_SECONDS,
    V2_ATTACK_RECHECK_SECONDS,
    V2_DIRECTION_SETTLE_SECONDS,
    V2_FACING_REASSERT_ATTACKS,
    V2_FACING_REASSERT_COOLDOWN_SECONDS,
    V2_FACING_SNAPSHOT_TTL_SECONDS,
    V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE,
    V2_NEAR_CHASE_DEBOUNCE_SECONDS,
    V2_ROUTE_RESUME_DEBOUNCE_SECONDS,
    V2_TARGET_SNAPSHOT_TTL_SECONDS,
    apply_action_intent,
    build_action_intent,
    clear_combat_for_movement,
    finish_combat,
    read_monster_snapshot,
    release_route_keys,
    reset_attack_direction_lock,
    set_route_phase,
    trace_action_decision,
    wait_for_runtime_ready,
)


__all__ = (
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




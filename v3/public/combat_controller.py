"""所有地图、录制路线和自定义路线共用的完整战斗控制模块。

路线模块只维护人物路线状态，并调用本模块生成和执行战斗意图。怪物感知、
目标快照、攻击优先级、目标方向锁、追怪抗抖、死亡残影等待和无怪恢复路线
都集中在本文件；后续战斗优化只修改这里，不再把规则散落到路线主循环。
"""

import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional


V2_ATTACK_RECHECK_SECONDS = 0.0
V2_ATTACK_MOVE_LOCK_SECONDS = 0.0
V2_ATTACK_KEY_HOLD_SECONDS = 0.055
V2_DIRECTION_SETTLE_SECONDS = 0.02
V2_FACING_SNAPSHOT_TTL_SECONDS = 0.45
# 可靠画面已经确认人物朝向正确时无需周期性松开攻击键再轻点方向键。
# 只有朝向暂时无法可靠识别时才保留低频兜底，避免连续战斗每约 0.6 秒
# 被人为切断一次；发现真实朝向错误、目标确认换边时仍会立即转身。
V2_FACING_REASSERT_ATTACKS = 6
V2_FACING_REASSERT_COOLDOWN_SECONDS = 0.90
V2_TARGET_SIDE_RECHECK_ATTACKS = 3
V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS = 3
# 当前锁定侧已经明确没有怪、另一侧在配置攻击范围内有怪时立即回头补刀。
# 这不是向背后智能追怪：只有attackable计数参与，超出配置范围仍不追。
# 两侧同时有怪时继续清当前侧，只有当前侧归零才切换，因此不会左右抽搐。
V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS = 1
V2_TEMPLATE_SIDE_SWITCH_COOLDOWN_SECONDS = 0.30
# 自定义模板较多或虚拟机负载较高时，一张完整识别帧实际可能需要
# 0.8~1.9 秒。有效期必须覆盖一轮正常检测，否则同一只怪会在两帧之间
# 被误判为消失，形成“追怪/攻击 -> 走路线 -> 回头再打”的控制权抖动。
# 明确的连续无目标帧仍由检测线程主动清空快照，因此这里加长不会阻止
# 正常清怪后及时恢复路线；检测线程完全停滞时则最多保留本时长。
V2_TARGET_SNAPSHOT_TTL_SECONDS = 2.2
# 连续空帧已经至少确认0.28秒，足以覆盖旧版0.26秒的战后稳定窗口。
# 不再额外叠加0.40秒等待，否则每次清怪都会形成一次可见的原地停顿。
V2_ROUTE_RESUME_DEBOUNCE_SECONDS = 0.26
V2_NEAR_CHASE_DEBOUNCE_SECONDS = 0.15
V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE = 340.0
V2_FORWARD_CHASE_EXTRA_RANGE = 200.0
# 追怪过程中模板可能在左右远怪之间来回跳动。换边必须经过连续画面和稳定
# 时间双重确认；如果新侧明显更近则缩短确认。进入攻击范围或出现血条的目标
# 仍由更高优先级分支立即处理，不受远距离追怪迟滞影响。
V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS = 4
V2_CHASE_DIRECTION_SWITCH_WINDOW_SECONDS = 0.80
V2_CHASE_DIRECTION_SWITCH_STABLE_SECONDS = 0.32
V2_CHASE_DIRECTION_SWITCH_CLOSER_CONFIRMATIONS = 2
V2_CHASE_DIRECTION_SWITCH_CLOSER_STABLE_SECONDS = 0.10
V2_CHASE_DIRECTION_SWITCH_CLOSER_MARGIN_X = 70.0
# 追怪可能被一次攻击、目标短漏帧或路线交接暂时结束。短时间重新开始追怪时
# 仍沿用上一方向作为换边基准，避免通过“结束追怪→重新追怪”绕过上面的防抖。
V2_CHASE_DIRECTION_MEMORY_SECONDS = 0.90
V2_CLOSE_GROUP_FALLBACK_RANGE = 50.0

# 怪物检测线程与路线线程共享的感知规则也放在本模块。这里沿用迁移前的参数，
# 本轮结构调整不改变任何识别、清怪或攻击时序。
V2_TARGET_LOSS_CONFIRMATION_FRAMES = 3
V2_TARGET_LOSS_MIN_SECONDS = 0.28
V2_POST_ATTACK_ALIVE_CONFIRMATION_FRAMES = 2
V2_POST_ATTACK_MOVEMENT_THRESHOLD = 12.0
V2_POST_ATTACK_DEATH_ANIMATION_WINDOW_SECONDS = 0.60
# 闪烁次数本身不能证明目标是死亡残影：五官小模板在正常受击动画中也会频繁
# 丢帧。本兜底只在“至少实际成功攻击 12 次、并发生 8 次丢失后恢复”后释放，
# 而且成功攻击次数只在攻击键真正执行完成后累计，不能再被路线循环空转放大。
V2_FLICKERING_TARGET_RELEASE_ATTACKS = 12
V2_FLICKERING_TARGET_MIN_RECOVERIES = 8
V2_FLICKERING_TARGET_SUPPRESS_SECONDS = 0.80


class CombatPerceptionStore:
    """线程安全地保存检测线程发布、路线线程消费的完整怪物快照。

    ``legacy_engine`` 只负责截图和模板匹配，不再直接维护十余个追怪全局变量。
    保留元组读取格式是为了让现有地图在迁移期间无需同步重写。
    """

    def __init__(
        self,
        *,
        trace: Callable[..., None],
        recent_attack_completed_at: Callable[[], float],
    ):
        self._trace = trace
        self._recent_attack_completed_at = recent_attack_completed_at
        self._combat_lock = threading.Lock()
        self._chase_lock = threading.Lock()
        self.reset(configured_group_over_count=2)

    def reset(self, *, configured_group_over_count=2):
        with self._combat_lock:
            self.last_nearby_at = 0.0
            self.last_nearby_direction = None
            self.nearby_count = 0
        with self._chase_lock:
            self.last_chase_at = 0.0
            self.last_chase_direction = None
            self.chase_count = 0
            self.attackable_count = 0
            self.nearest_distance = None
            self.chase_left_count = 0
            self.chase_right_count = 0
            self.attackable_left_count = 0
            self.attackable_right_count = 0
            self.health_bar_chase_left_count = 0
            self.health_bar_chase_right_count = 0
            self.health_bar_attackable_left_count = 0
            self.health_bar_attackable_right_count = 0
            self.target_loss_frames = 0
            self.target_loss_pending = False
            self.target_loss_started_at = 0.0
            self.automatic_group_count = 0
            self.close_group_count = 0
            self.configured_group_over_count = max(
                0, int(configured_group_over_count)
            )
            self.attack_confirmation_pending = False
            self.target_flicker_recoveries = 0
            self.target_stable_frames = 0

    def publish_nearby(
        self,
        total_nearby,
        left_count,
        right_count,
        preferred_direction=None,
        *,
        detected_at=None,
    ):
        total_nearby = int(total_nearby)
        left_count = int(left_count)
        right_count = int(right_count)
        frame_detected_at = float(detected_at or time.monotonic())
        with self._combat_lock:
            self.nearby_count = total_nearby
            if total_nearby <= 0:
                return
            self.last_nearby_at = frame_detected_at
            if preferred_direction in ("left", "right"):
                self.last_nearby_direction = preferred_direction
            elif left_count > right_count:
                self.last_nearby_direction = "left"
            elif right_count > left_count:
                self.last_nearby_direction = "right"

    def read_nearby(self):
        with self._combat_lock:
            return (
                self.last_nearby_at,
                self.last_nearby_direction,
                self.nearby_count,
            )

    def publish_chase(
        self,
        attackable_count,
        chase_count,
        chase_left_count,
        chase_right_count,
        attackable_left_count=0,
        attackable_right_count=0,
        preferred_direction=None,
        nearest_distance=None,
        detected_at=None,
        automatic_group_count=0,
        close_group_count=0,
        configured_group_over_count=None,
        health_bar_chase_left_count=0,
        health_bar_chase_right_count=0,
        health_bar_attackable_left_count=0,
        health_bar_attackable_right_count=0,
        attack_confirmation_pending=False,
    ):
        """发布一张完整目标快照，并在唯一入口完成目标丢失确认。"""
        frame_detected_at = float(detected_at or time.monotonic())
        if frame_detected_at <= float(self._recent_attack_completed_at() or 0.0):
            return False

        attackable_count = int(attackable_count)
        chase_count = int(chase_count)
        chase_left_count = int(chase_left_count)
        chase_right_count = int(chase_right_count)
        attackable_left_count = int(attackable_left_count)
        attackable_right_count = int(attackable_right_count)
        health_bar_chase_left_count = max(0, int(health_bar_chase_left_count))
        health_bar_chase_right_count = max(0, int(health_bar_chase_right_count))
        health_bar_attackable_left_count = max(
            0, int(health_bar_attackable_left_count)
        )
        health_bar_attackable_right_count = max(
            0, int(health_bar_attackable_right_count)
        )

        with self._chase_lock:
            configured_count = max(
                0,
                int(
                    self.configured_group_over_count
                    if configured_group_over_count is None
                    else configured_group_over_count
                ),
            )
            attack_confirmation_pending = bool(attack_confirmation_pending)
            if attack_confirmation_pending:
                self.target_loss_frames = 0
                self.target_loss_pending = False
                self.target_loss_started_at = 0.0
            elif chase_count > 0:
                if self.target_loss_frames > 0:
                    self.target_flicker_recoveries += 1
                    self.target_stable_frames = 0
                    self._trace(
                        "combat_target_loss_cancelled",
                        missing_frames=self.target_loss_frames,
                        confirmation_required=V2_TARGET_LOSS_CONFIRMATION_FRAMES,
                        recovered_direction=preferred_direction,
                        recovered_targets=chase_count,
                        action="keep_combat_control",
                    )
                self.target_loss_frames = 0
                self.target_loss_pending = False
                self.target_loss_started_at = 0.0
                self.target_stable_frames += 1
                if self.target_stable_frames >= 24:
                    self.target_flicker_recoveries = max(
                        0, self.target_flicker_recoveries - 1
                    )
                    self.target_stable_frames = 0
            elif self.chase_count > 0:
                self.target_loss_frames += 1
                if self.target_loss_started_at <= 0:
                    self.target_loss_started_at = frame_detected_at
                missing_seconds = max(
                    0.0, frame_detected_at - self.target_loss_started_at
                )
                if (
                    self.target_loss_frames < V2_TARGET_LOSS_CONFIRMATION_FRAMES
                    or missing_seconds < V2_TARGET_LOSS_MIN_SECONDS
                ):
                    self.last_chase_at = max(
                        float(self.last_chase_at), frame_detected_at
                    )
                    self.target_loss_pending = True
                    self._trace(
                        "combat_target_loss_pending",
                        missing_frame=self.target_loss_frames,
                        confirmation_required=V2_TARGET_LOSS_CONFIRMATION_FRAMES,
                        missing_duration_ms=round(missing_seconds * 1000.0, 1),
                        minimum_duration_ms=round(
                            V2_TARGET_LOSS_MIN_SECONDS * 1000.0, 1
                        ),
                        previous_direction=self.last_chase_direction,
                        previous_targets=self.chase_count,
                        action="keep_previous_snapshot",
                    )
                    return False
                self._trace(
                    "combat_target_loss_confirmed",
                    missing_frames=self.target_loss_frames,
                    confirmation_required=V2_TARGET_LOSS_CONFIRMATION_FRAMES,
                    missing_duration_ms=round(missing_seconds * 1000.0, 1),
                    minimum_duration_ms=round(
                        V2_TARGET_LOSS_MIN_SECONDS * 1000.0, 1
                    ),
                    previous_direction=self.last_chase_direction,
                    action="publish_empty_snapshot",
                )
                self.target_loss_frames = 0
                self.target_loss_pending = False
                self.target_loss_started_at = 0.0
                self.target_flicker_recoveries = 0
                self.target_stable_frames = 0
            else:
                self.target_loss_frames = 0
                self.target_loss_pending = False
                self.target_loss_started_at = 0.0
                self.target_flicker_recoveries = 0
                self.target_stable_frames = 0

            self.attackable_count = attackable_count
            self.chase_count = chase_count
            self.chase_left_count = chase_left_count
            self.chase_right_count = chase_right_count
            self.attackable_left_count = attackable_left_count
            self.attackable_right_count = attackable_right_count
            self.health_bar_chase_left_count = health_bar_chase_left_count
            self.health_bar_chase_right_count = health_bar_chase_right_count
            self.health_bar_attackable_left_count = health_bar_attackable_left_count
            self.health_bar_attackable_right_count = health_bar_attackable_right_count
            self.attack_confirmation_pending = attack_confirmation_pending
            self.automatic_group_count = max(0, int(automatic_group_count))
            self.close_group_count = max(0, int(close_group_count))
            self.configured_group_over_count = configured_count
            self.nearest_distance = (
                float(nearest_distance) if nearest_distance is not None else None
            )
            if chase_count <= 0:
                self.last_chase_direction = None
                return True
            self.last_chase_at = frame_detected_at
            if preferred_direction in ("left", "right"):
                self.last_chase_direction = preferred_direction
            elif chase_left_count > chase_right_count:
                self.last_chase_direction = "left"
            elif chase_right_count > chase_left_count:
                self.last_chase_direction = "right"
            return True

    def keep_chase_fresh(self, detected_at=None):
        frame_detected_at = float(detected_at or time.monotonic())
        with self._chase_lock:
            if self.chase_count <= 0:
                return False
            self.last_chase_at = max(float(self.last_chase_at), frame_detected_at)
            return True

    def read_chase(self):
        with self._chase_lock:
            return (
                self.last_chase_at,
                self.last_chase_direction,
                self.chase_count,
                self.attackable_count,
                self.nearest_distance,
                self.chase_left_count,
                self.chase_right_count,
                self.attackable_left_count,
                self.attackable_right_count,
                self.automatic_group_count,
                self.close_group_count,
                self.configured_group_over_count,
                self.health_bar_chase_left_count,
                self.health_bar_chase_right_count,
                self.health_bar_attackable_left_count,
                self.health_bar_attackable_right_count,
                self.target_loss_pending,
                self.attack_confirmation_pending,
                self.target_flicker_recoveries,
            )


@dataclass(frozen=True)
class CombatTargetEvaluation:
    """一帧怪物中心相对人物位置的完整分层结果。"""

    automatic_group_count: int
    close_group_count: int
    attackable_count: int
    attackable_left_count: int
    attackable_right_count: int
    attack_direction: Optional[str]
    nearest_candidate_x: Optional[float]
    nearest_candidate_y: Optional[float]
    visible_count: int
    visible_left_count: int
    visible_right_count: int
    visible_direction: Optional[str]
    chase_count: int
    chase_left_count: int
    chase_right_count: int
    chase_direction: Optional[str]
    chase_nearest_distance: Optional[float]
    health_bar_attackable_count: int
    health_bar_attackable_left_count: int
    health_bar_attackable_right_count: int
    health_bar_attack_direction: Optional[str]
    health_bar_chase_count: int
    health_bar_chase_left_count: int
    health_bar_chase_right_count: int
    health_bar_chase_direction: Optional[str]


class PostAttackTargetGuard:
    """区分受击后仍存活的怪物与死亡动画、目标切换和人物坐标抖动。"""

    def __init__(
        self,
        *,
        trace: Callable[..., None],
        alive_confirmation_frames=V2_POST_ATTACK_ALIVE_CONFIRMATION_FRAMES,
        movement_threshold=V2_POST_ATTACK_MOVEMENT_THRESHOLD,
        death_animation_window_seconds=(
            V2_POST_ATTACK_DEATH_ANIMATION_WINDOW_SECONDS
        ),
    ):
        self._trace = trace
        self.alive_confirmation_frames = max(1, int(alive_confirmation_frames))
        self.movement_threshold = max(0.0, float(movement_threshold))
        self.death_animation_window_seconds = max(
            0.0, float(death_animation_window_seconds)
        )
        self.reset()

    def reset(self):
        self.last_confirmed_attack_at = 0.0
        self.post_attack_near_frames = 0
        self.current_attack_alive_confirmed = False
        self.latest_target_direction = None
        self.latest_target_distance = None
        self.latest_target_count = 0
        self.baseline_direction = None
        self.baseline_distance = None
        self.baseline_count = 0
        self.confirmation_pending = False

    def filter_attackable_count(
        self,
        *,
        frame_detected_at,
        recent_attack_completed_at,
        decision_count,
        decision_direction,
        chase_nearest_distance,
        health_bar_attackable_count,
        health_bar_chase_count,
        health_bar_green_max_ratio,
        close_range_promoted,
    ):
        """返回本帧允许发布的攻击数量，行为与迁移前保持一致。"""
        frame_detected_at = float(frame_detected_at)
        recent_attack_completed_at = float(recent_attack_completed_at or 0.0)
        decision_count = max(0, int(decision_count))
        publish_count = decision_count
        self.confirmation_pending = False
        if recent_attack_completed_at > self.last_confirmed_attack_at:
            self.baseline_direction = self.latest_target_direction
            self.baseline_distance = self.latest_target_distance
            self.baseline_count = self.latest_target_count
            self.last_confirmed_attack_at = recent_attack_completed_at
            self.post_attack_near_frames = 0
            self.current_attack_alive_confirmed = False

        elapsed = frame_detected_at - recent_attack_completed_at
        if (
            recent_attack_completed_at > 0
            and 0 < elapsed <= self.death_animation_window_seconds
        ):
            distance_delta = (
                abs(float(chase_nearest_distance) - self.baseline_distance)
                if chase_nearest_distance is not None
                and self.baseline_distance is not None
                else 0.0
            )
            health_bar_alive = health_bar_attackable_count > 0 or (
                close_range_promoted and health_bar_chase_count > 0
            )
            if decision_count > 0 and health_bar_alive:
                self.post_attack_near_frames = self.alive_confirmation_frames
                if not self.current_attack_alive_confirmed:
                    self.current_attack_alive_confirmed = True
                    self._trace(
                        "monster_health_bar_alive_confirmed",
                        baseline_direction=self.baseline_direction,
                        current_direction=decision_direction,
                        baseline_distance=self.baseline_distance,
                        current_distance=(
                            round(float(chase_nearest_distance), 2)
                            if chase_nearest_distance is not None
                            else None
                        ),
                        distance_delta=round(distance_delta, 2),
                        baseline_count=self.baseline_count,
                        current_count=decision_count,
                        health_bar_attackable=health_bar_attackable_count,
                        health_bar_chase_targets=health_bar_chase_count,
                        health_bar_green_max_ratio=(
                            round(float(health_bar_green_max_ratio), 4)
                            if health_bar_green_max_ratio is not None
                            else None
                        ),
                        movement_threshold=self.movement_threshold,
                        action="fast_reattack",
                    )
            elif decision_count > 0:
                # 攻击动画会让人物模板横向漂移，多个目标之间的最近点也会切换；
                # 两者都会让相对距离瞬间变化几十像素。没有血条时不能再把这种
                # 变化当作活怪运动，否则会在死亡动画尚未结束时每0.1~0.2秒空打。
                # 整个死亡动画窗口都保持停手；窗口结束后仍存在的模板按新目标处理。
                if self.post_attack_near_frames <= 0:
                    self._trace(
                        "post_attack_target_confirmation",
                        detected_targets=decision_count,
                        confirmation_frame=1,
                        confirmation_required=self.alive_confirmation_frames,
                        frame_after_attack_ms=round(elapsed * 1000, 3),
                        confirmation_window_ms=round(
                            self.death_animation_window_seconds * 1000, 1
                        ),
                        baseline_direction=self.baseline_direction,
                        current_direction=decision_direction,
                        baseline_distance=self.baseline_distance,
                        current_distance=(
                            round(float(chase_nearest_distance), 2)
                            if chase_nearest_distance is not None
                            else None
                        ),
                        distance_delta=round(distance_delta, 2),
                        health_bar_evidence=False,
                        reason="wait_out_death_animation_without_health_bar",
                        action="hold_without_attack",
                    )
                self.post_attack_near_frames = 1
                publish_count = 0
                self.confirmation_pending = True
            else:
                self.post_attack_near_frames = 0
        elif elapsed > self.death_animation_window_seconds:
            self.post_attack_near_frames = self.alive_confirmation_frames

        if decision_count > 0 and (
            recent_attack_completed_at <= 0
            or frame_detected_at > recent_attack_completed_at
        ):
            self.latest_target_direction = decision_direction
            self.latest_target_distance = chase_nearest_distance
            self.latest_target_count = decision_count
        return publish_count


@dataclass(frozen=True)
class CombatFrameDecision:
    """检测线程本帧对旧战斗通道的唯一决定。"""

    lost_frames: int
    combat_active: bool
    clear_attack_intent: bool
    attack_direction: int = 0
    attack_reason: Optional[str] = None
    attack_monster_count: Optional[int] = None
    attack_horizontal_range: Optional[float] = None
    configured_group_over_count: Optional[int] = None


def decide_combat_frame(
    *,
    total_nearby,
    left_count,
    right_count,
    lost_frames,
    combat_active,
    visible_nearby=0,
    hold_visible_combat=True,
    preferred_direction=None,
    last_direction=None,
    recent_target=False,
    attack_movement_locked=False,
    allow_group_attack=False,
    automatic_group_count=0,
    close_group_count=0,
    configured_group_over_count=0,
    target_lost_confirmation_frames=3,
    automatic_group_range=150.0,
    close_group_range=50.0,
):
    """把近怪、漏帧、群攻和攻击方向统一折叠为一个无副作用决定。"""
    total_nearby = max(0, int(total_nearby))
    left_count = max(0, int(left_count))
    right_count = max(0, int(right_count))
    visible_nearby = max(0, int(visible_nearby))
    lost_frames = max(0, int(lost_frames))
    confirmation_frames = max(1, int(target_lost_confirmation_frames))
    automatic_group_count = max(0, int(automatic_group_count))
    close_group_count = max(0, int(close_group_count))
    group_over_count = max(0, int(configured_group_over_count))

    if total_nearby <= 0:
        if visible_nearby > 0 and hold_visible_combat:
            return CombatFrameDecision(0, True, False)
        if visible_nearby > 0:
            return CombatFrameDecision(0, False, True)
        if not combat_active:
            return CombatFrameDecision(0, False, False)
        lost_frames += 1
        if (
            lost_frames < confirmation_frames
            or recent_target
            or attack_movement_locked
        ):
            return CombatFrameDecision(
                min(lost_frames, confirmation_frames - 1),
                True,
                False,
            )
        return CombatFrameDecision(0, False, True)

    if close_group_count > 0:
        return CombatFrameDecision(
            0,
            True,
            False,
            attack_direction=3,
            attack_reason="auto_group_close_x_range",
            attack_monster_count=close_group_count,
            attack_horizontal_range=float(close_group_range),
            configured_group_over_count=0,
        )
    if automatic_group_count > group_over_count:
        return CombatFrameDecision(
            0,
            True,
            False,
            attack_direction=3,
            attack_reason="auto_group_x_range",
            attack_monster_count=automatic_group_count,
            attack_horizontal_range=float(automatic_group_range),
            configured_group_over_count=group_over_count,
        )
    if allow_group_attack and total_nearby >= 2:
        return CombatFrameDecision(
            0,
            True,
            False,
            attack_direction=3,
            attack_reason="group_attack_mode",
            attack_monster_count=total_nearby,
        )
    if preferred_direction == "right":
        attack_direction = 2
    elif preferred_direction == "left":
        attack_direction = 1
    elif right_count > left_count:
        attack_direction = 2
    elif left_count > right_count:
        attack_direction = 1
    else:
        attack_direction = 2 if last_direction == "right" else 1
    return CombatFrameDecision(
        0,
        True,
        False,
        attack_direction=attack_direction,
    )


def _count_horizontal_targets(character_x, centers, horizontal_range):
    character_x = float(character_x)
    horizontal_range = max(0.0, float(horizontal_range))
    return sum(
        1
        for center_x, _center_y in centers
        if abs(float(center_x) - character_x) <= horizontal_range
    )


def evaluate_detected_targets(
    *,
    character_x,
    character_attack_y,
    centers,
    health_bar_alive_indices,
    attack_range_x,
    attack_range_y,
    chase_extra_x,
    evaluate_target,
    forward_direction=None,
    forward_chase_range=None,
    forward_only_chase=False,
    automatic_group_range=150.0,
    close_group_range=50.0,
):
    """把原始识图中心统一分成攻击、追怪、可见和血条优先四层目标。

    攻击范围仍按页面配置对人物左右两侧生效；智能追怪只允许沿录制路线的稳定
    前进方向，默认最远为“配置攻击范围 + 200px”。人物为了攻击身后怪物而临时转身时，
    不会改变这个基准，因此后方怪一旦离开配置攻击范围就立即交还路线。

    ``chase_extra_x`` 仅为旧调用保留。传入有效前进方向时不再按“攻击范围+
    extra”扩张，避免页面攻击距离变化后追怪距离也跟着失控。
    """
    centers = tuple(centers)
    health_bar_alive_indices = set(health_bar_alive_indices)
    forward_direction = (
        forward_direction if forward_direction in ("left", "right") else None
    )
    forward_chase_range = max(
        0.0,
        float(
            float(attack_range_x) + V2_FORWARD_CHASE_EXTRA_RANGE
            if forward_chase_range is None
            else forward_chase_range
        ),
    )
    legacy_chase_range = max(
        0.0,
        float(attack_range_x) + float(chase_extra_x),
    )
    automatic_group_count = _count_horizontal_targets(
        character_x, centers, automatic_group_range
    )
    close_group_count = _count_horizontal_targets(
        character_x, centers, close_group_range
    )
    attack_left = attack_right = 0
    visible_count = visible_left = visible_right = 0
    chase_count = chase_left = chase_right = 0
    health_attack_count = health_attack_left = health_attack_right = 0
    health_chase_count = health_chase_left = health_chase_right = 0
    attack_direction = visible_direction = chase_direction = None
    health_attack_direction = health_chase_direction = None
    nearest_candidate_x = float("inf")
    nearest_candidate_y = None
    nearest_attack_x = float("inf")
    nearest_visible_x = float("inf")
    nearest_chase_x = float("inf")
    nearest_health_attack_x = float("inf")
    nearest_health_chase_x = float("inf")

    for index, (center_x, center_y) in enumerate(centers):
        health_alive = index in health_bar_alive_indices
        horizontal, vertical, attackable = evaluate_target(
            character_x,
            character_attack_y,
            (center_x, center_y),
            attack_range_x,
            attack_range_y,
        )
        side = "left" if character_x > center_x else "right"
        if horizontal < nearest_candidate_x:
            nearest_candidate_x = horizontal
            nearest_candidate_y = vertical
        if horizontal < attack_range_x + 80 and vertical < attack_range_y + 30:
            visible_count += 1
            if side == "left":
                visible_left += 1
            else:
                visible_right += 1
            if horizontal < nearest_visible_x:
                nearest_visible_x = horizontal
                visible_direction = side
        if forward_only_chase:
            chase_allowed = (
                forward_direction is not None
                and side == forward_direction
                and horizontal <= forward_chase_range
                and vertical < attack_range_y
            )
        elif forward_direction is not None:
            chase_allowed = (
                side == forward_direction
                and horizontal <= forward_chase_range
                and vertical < attack_range_y
            )
        else:
            chase_allowed = (
                horizontal < legacy_chase_range and vertical < attack_range_y
            )
        if chase_allowed:
            chase_count += 1
            if side == "left":
                chase_left += 1
            else:
                chase_right += 1
            if horizontal < nearest_chase_x:
                nearest_chase_x = horizontal
                chase_direction = side
            if health_alive:
                health_chase_count += 1
                if side == "left":
                    health_chase_left += 1
                else:
                    health_chase_right += 1
                if horizontal < nearest_health_chase_x:
                    nearest_health_chase_x = horizontal
                    health_chase_direction = side
        if not attackable:
            continue
        if side == "left":
            attack_left += 1
        else:
            attack_right += 1
        if horizontal < nearest_attack_x:
            nearest_attack_x = horizontal
            attack_direction = side
        if health_alive:
            health_attack_count += 1
            if side == "left":
                health_attack_left += 1
            else:
                health_attack_right += 1
            if horizontal < nearest_health_attack_x:
                nearest_health_attack_x = horizontal
                health_attack_direction = side

    return CombatTargetEvaluation(
        automatic_group_count=automatic_group_count,
        close_group_count=close_group_count,
        attackable_count=attack_left + attack_right,
        attackable_left_count=attack_left,
        attackable_right_count=attack_right,
        attack_direction=attack_direction,
        nearest_candidate_x=(
            nearest_candidate_x if nearest_candidate_x != float("inf") else None
        ),
        nearest_candidate_y=nearest_candidate_y,
        visible_count=visible_count,
        visible_left_count=visible_left,
        visible_right_count=visible_right,
        visible_direction=visible_direction,
        chase_count=chase_count,
        chase_left_count=chase_left,
        chase_right_count=chase_right,
        chase_direction=chase_direction,
        chase_nearest_distance=(
            nearest_chase_x if nearest_chase_x != float("inf") else None
        ),
        health_bar_attackable_count=health_attack_count,
        health_bar_attackable_left_count=health_attack_left,
        health_bar_attackable_right_count=health_attack_right,
        health_bar_attack_direction=health_attack_direction,
        health_bar_chase_count=health_chase_count,
        health_bar_chase_left_count=health_chase_left,
        health_bar_chase_right_count=health_chase_right,
        health_bar_chase_direction=health_chase_direction,
    )


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
    automatic_group_count: int = 0
    close_group_count: int = 0
    configured_group_over_count: int = 0
    health_bar_chase_left_count: int = 0
    health_bar_chase_right_count: int = 0
    health_bar_attackable_left_count: int = 0
    health_bar_attackable_right_count: int = 0
    target_loss_pending: bool = False
    attack_confirmation_pending: bool = False
    target_flicker_recoveries: int = 0


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
    automatic_group_count: int = 0
    close_group_count: int = 0
    configured_group_over_count: int = 0
    health_bar_chase_left_count: int = 0
    health_bar_chase_right_count: int = 0
    health_bar_attackable_left_count: int = 0
    health_bar_attackable_right_count: int = 0
    target_loss_pending: bool = False
    attack_confirmation_pending: bool = False
    target_flicker_recoveries: int = 0


@dataclass
class MushroomCombatState:
    """保存蘑菇路线共用的战斗、追怪和按键协调状态。"""

    patrol_direction: str = "left"
    applied_direction: Optional[str] = None
    phase: str = "startup"
    combat_active: bool = False
    combat_started_at: float = 0.0
    combat_attack_count: int = 0
    last_attack_completed_at: float = 0.0
    combat_suppressed_until: float = 0.0
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
    side_switch_confirmation_required: int = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
    last_side_switch_at: float = 0.0
    route_resume_pending_at: float = 0.0
    # 普通模板短漏帧只允许沿锁定侧补一拍，避免确认目标已经消失后连续空打。
    # 有攻击范围血条证据时不受此限制，因为血条能更可靠地证明目标仍然存活。
    target_loss_attack_used: bool = False
    near_chase_pending_at: float = 0.0
    near_chase_pending_direction: Optional[str] = None
    chase_active: bool = False
    chase_direction: Optional[str] = None
    chase_direction_nearest_dx: Optional[float] = None
    recent_chase_direction: Optional[str] = None
    recent_chase_nearest_dx: Optional[float] = None
    recent_chase_at: float = 0.0
    chase_switch_pending_direction: Optional[str] = None
    chase_switch_pending_detected_at: float = 0.0
    chase_switch_pending_started_at: float = 0.0
    chase_switch_pending_count: int = 0
    last_intent_signature: Optional[tuple] = None
    on_upper_platform: bool = False
    flickering_target_attack_streak: int = 0
    # 录制路线前进方向的攻击范围内一旦发现怪物，就锁在原地完成战斗。
    # 短暂漏帧和死亡残影确认期间保持本锁；只有确认该方向攻击范围清空后，
    # 才允许智能追怪或JSON路线重新移动。
    forward_attack_lock_active: bool = False
    forward_attack_lock_direction: Optional[str] = None
    last_attack_hold_snapshot_at: float = 0.0


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
    state.last_attack_completed_at = 0.0
    state.target_loss_attack_used = False
    state.last_attack_direction = 0
    state.last_facing_correction_at = 0.0
    state.last_facing_reassert_at = 0.0
    state.same_direction_attack_streak = 0
    state.last_target_side_recheck_attack_count = 0
    state.last_facing_log_signature = None
    state.side_switch_pending_direction = None
    state.side_switch_pending_count = 0
    state.side_switch_pending_frame_at = 0.0
    state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
    state.last_side_switch_at = 0.0
    state.flickering_target_attack_streak = 0
    state.last_attack_hold_snapshot_at = 0.0
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
    state.last_attack_completed_at = 0.0
    state.target_loss_attack_used = False
    state.last_attack_direction = 0
    state.last_facing_correction_at = 0.0
    state.last_facing_reassert_at = 0.0
    state.same_direction_attack_streak = 0
    state.last_target_side_recheck_attack_count = 0
    state.last_facing_log_signature = None
    state.side_switch_pending_direction = None
    state.side_switch_pending_count = 0
    state.side_switch_pending_frame_at = 0.0
    state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
    state.last_side_switch_at = 0.0
    state.flickering_target_attack_streak = 0
    state.forward_attack_lock_active = False
    state.forward_attack_lock_direction = None
    state.last_attack_hold_snapshot_at = 0.0


def _reset_attack_direction_lock(runtime, state, reason):
    """没有可追目标时释放同侧攻击锁，并记录释放原因。"""
    state.side_switch_pending_direction = None
    state.side_switch_pending_count = 0
    state.side_switch_pending_frame_at = 0.0
    state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
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

    health_bar_left_count = max(0, int(intent.health_bar_attackable_left_count))
    health_bar_right_count = max(0, int(intent.health_bar_attackable_right_count))
    if health_bar_left_count + health_bar_right_count > 0:
        # 血条比模板命中更能证明怪物仍然存活。只要任一侧存在血条目标，方向
        # 锁就只按血条侧计算；背后血条怪不会被正面的死亡残影延迟三次攻击。
        left_count = health_bar_left_count
        right_count = health_bar_right_count
        count_source = "health_bar_attack_range"
    elif intent.attackable_left_count + intent.attackable_right_count > 0:
        left_count = intent.attackable_left_count
        right_count = intent.attackable_right_count
        count_source = "attack_range"
    else:
        # 近身补刀带位于严格攻击范围之外，此时使用额外追怪区的左右数量。
        health_bar_left_count = max(0, int(intent.health_bar_chase_left_count))
        health_bar_right_count = max(0, int(intent.health_bar_chase_right_count))
        if health_bar_left_count + health_bar_right_count > 0:
            left_count = health_bar_left_count
            right_count = health_bar_right_count
            count_source = "health_bar_chase_range"
        else:
            left_count = intent.chase_left_count
            right_count = intent.chase_right_count
            count_source = "chase_range"

    if left_count + right_count <= 0:
        return 0

    snapshot_direction = intent.target_direction
    if count_source.startswith("health_bar_"):
        if left_count > right_count:
            snapshot_direction = "left"
        elif right_count > left_count:
            snapshot_direction = "right"
        elif snapshot_direction not in ("left", "right"):
            snapshot_direction = "left" if left_count > 0 else "right"
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
    snapshot_count = left_count if snapshot_direction == "left" else right_count
    side_recheck_due = (
        state.combat_attack_count >= V2_TARGET_SIDE_RECHECK_ATTACKS
        and state.combat_attack_count - state.last_target_side_recheck_attack_count
        >= V2_TARGET_SIDE_RECHECK_ATTACKS
    )
    switch_reason = None
    if locked_count <= 0 and opposite_count > 0:
        switch_reason = "locked_side_empty"
    elif (
        state.side_switch_pending_direction in ("left", "right")
        and state.side_switch_pending_direction != locked_direction
        and locked_count <= 0
        and opposite_count > 0
    ):
        # 当前侧已经清空后，后续不同截图继续确认同一侧。两侧同时有怪时不因
        # “另一侧数量更多”中断当前连击；清完当前侧后再快速回头，避免左右抽搐。
        switch_reason = "pending_side_confirmation"
    if side_recheck_due:
        state.last_target_side_recheck_attack_count = state.combat_attack_count
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
        state.side_switch_pending_direction = None
        state.side_switch_pending_count = 0
        state.side_switch_pending_frame_at = 0.0
        state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
    elif not count_source.startswith("health_bar_"):
        candidate_direction = (
            snapshot_direction
            if switch_reason == "persistent_target_recheck"
            else (
                state.side_switch_pending_direction
                if switch_reason == "pending_side_confirmation"
                else opposite_direction
            )
        )
        now = time.monotonic()
        confirmation_required = (
            V2_TEMPLATE_EMPTY_SIDE_SWITCH_CONFIRMATIONS
            if switch_reason == "locked_side_empty"
            else max(
                1,
                int(state.side_switch_confirmation_required),
            )
        )
        cooldown_remaining = (
            V2_TEMPLATE_SIDE_SWITCH_COOLDOWN_SECONDS
            - (now - state.last_side_switch_at)
        )
        if state.last_side_switch_at > 0 and cooldown_remaining > 0:
            pending_changed = (
                state.side_switch_pending_direction != candidate_direction
                or state.side_switch_pending_count != 0
            )
            if pending_changed:
                state.side_switch_pending_direction = candidate_direction
                state.side_switch_pending_count = 0
                state.side_switch_pending_frame_at = 0.0
                state.side_switch_confirmation_required = confirmation_required
                runtime.trace_event(
                    "combat_side_switch_pending",
                    pending_direction=candidate_direction,
                    pending_count=0,
                    confirmation_required=confirmation_required,
                    cooldown_remaining_ms=round(cooldown_remaining * 1000, 3),
                    reason=switch_reason,
                    count_source=count_source,
                    left_count=left_count,
                    right_count=right_count,
                    action="keep_current_side_during_cooldown",
                )
            switch_reason = None
        else:
            snapshot_detected_at = (
                intent.created_at
                - max(0.0, float(intent.target_age_ms or 0.0)) / 1000.0
            )
            snapshot_advanced = False
            if (
                state.side_switch_pending_direction == candidate_direction
                and snapshot_detected_at
                > state.side_switch_pending_frame_at + 0.001
            ):
                state.side_switch_pending_count += 1
                state.side_switch_pending_frame_at = snapshot_detected_at
                snapshot_advanced = True
            elif state.side_switch_pending_direction != candidate_direction:
                state.side_switch_pending_direction = candidate_direction
                state.side_switch_pending_count = 1
                state.side_switch_pending_frame_at = snapshot_detected_at
                state.side_switch_confirmation_required = confirmation_required
                snapshot_advanced = True
            confirmation_required = max(
                1,
                int(state.side_switch_confirmation_required),
            )
            if (
                state.side_switch_pending_count
                < confirmation_required
            ):
                # 路线线程可能在检测线程发布下一张画面之前循环数十次。同一张
                # snapshot 只能累计、打印和触发一次等待，不能把一次换边拉长成
                # 0.9~2秒的重复停手。
                if snapshot_advanced:
                    runtime.trace_event(
                        "combat_side_switch_pending",
                        pending_direction=candidate_direction,
                        pending_count=state.side_switch_pending_count,
                        confirmation_required=confirmation_required,
                        snapshot_detected_at=round(snapshot_detected_at, 6),
                        cooldown_remaining_ms=0.0,
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
            if switch_reason == "persistent_target_recheck"
            else (
                state.side_switch_pending_direction
                if switch_reason == "pending_side_confirmation"
                else opposite_direction
            )
        )
        state.locked_attack_direction = locked_direction
        state.side_switch_pending_direction = None
        state.side_switch_pending_count = 0
        state.side_switch_pending_frame_at = 0.0
        state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
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
    if not latest.fresh or (
        latest.attackable_count <= 0
        and not latest.target_loss_pending
    ):
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

    if latest.target_loss_pending and latest.attackable_count <= 0:
        pending_health_bar_count = (
            latest.health_bar_attackable_left_count
            + latest.health_bar_attackable_right_count
        )
        if pending_health_bar_count > 0:
            # 血条是怪物仍然存活的强证据。即使五官模板暂时被技能遮挡，也可
            # 继续攻击；方向会在下方按血条左右计数再次校正。
            pass
        else:
            # 普通模板进入连续空帧确认后不再沿旧方向补打一拍。当前攻击意图
            # 可能产生于上一张有怪截图，真正按键前必须取消，等待新画面确认：
            # 重新识别到怪物后再攻击，确认消失后则直接恢复路线。
            runtime.释放攻击键()
            state.last_attack_direction = 0
            state.same_direction_attack_streak = 0
            runtime.trace_event(
                "combat_target_loss_attack_held",
                requested_direction=attack_direction,
                target_age_ms=latest.age_ms,
                target_loss_pending=True,
                health_bar_attackable=0,
                action="wait_without_empty_attack",
            )
            state.last_attack_hold_snapshot_at = max(
                state.last_attack_hold_snapshot_at,
                float(latest.detected_at or 0.0),
            )
            return 0

    if attack_direction == 3:
        return 3

    desired_direction = "left" if attack_direction == 1 else "right"
    health_bar_count = (
        latest.health_bar_attackable_left_count
        + latest.health_bar_attackable_right_count
    )
    if health_bar_count > 0:
        desired_count = (
            latest.health_bar_attackable_left_count
            if desired_direction == "left"
            else latest.health_bar_attackable_right_count
        )
    else:
        desired_count = (
            latest.attackable_left_count
            if desired_direction == "left"
            else latest.attackable_right_count
        )
    opposite_direction = "right" if desired_direction == "left" else "left"
    if health_bar_count > 0:
        opposite_count = (
            latest.health_bar_attackable_right_count
            if desired_direction == "left"
            else latest.health_bar_attackable_left_count
        )
    else:
        opposite_count = (
            latest.attackable_right_count
            if desired_direction == "left"
            else latest.attackable_left_count
        )
    # 按键前的最新截图若只有一侧存在可攻击怪物，该截图优先级高于旧锁定侧、
    # 路线方向和等待中的换边确认。这里直接原子更新锁定方向，防止检测已经是
    # “左0右1”，执行线程仍沿上一场战斗的 left 队列向空侧攻击。
    if desired_count <= 0 and opposite_count > 0:
        previous_direction = state.locked_attack_direction or desired_direction
        state.locked_attack_direction = opposite_direction
        state.side_switch_pending_direction = None
        state.side_switch_pending_count = 0
        state.side_switch_pending_frame_at = 0.0
        state.side_switch_confirmation_required = V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS
        state.last_side_switch_at = time.monotonic()
        corrected_direction = 1 if opposite_direction == "left" else 2
        runtime.释放攻击键()
        runtime.trace_event(
            "mushroom_v2_latest_snapshot_side_corrected",
            previous_direction=previous_direction,
            direction=opposite_direction,
            target_age_ms=latest.age_ms,
            snapshot_detected_at=round(float(latest.detected_at or 0.0), 6),
            attackable_left_count=latest.attackable_left_count,
            attackable_right_count=latest.attackable_right_count,
            health_bar_attackable_left_count=(
                latest.health_bar_attackable_left_count
            ),
            health_bar_attackable_right_count=(
                latest.health_bar_attackable_right_count
            ),
            reason="latest_snapshot_has_only_opposite_side",
            action="turn_and_attack_latest_alive_side",
        )
        return corrected_direction
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

    if (
        health_bar_count <= 0
        and state.side_switch_pending_direction == opposite_direction
        and state.side_switch_pending_count
        < max(1, int(state.side_switch_confirmation_required))
    ):
        # 主方向解析已经发现模板从一侧闪到另一侧，但尚未达到连续确认次数。
        # 保留连续多帧换边确认以防左右抽搐，但旧锁定侧已经没有目标，所以确认
        # 期间只停手等待，不再朝空侧补刀。下一张一致画面会继续累计并完成换边；
        # 血条目标不走此分支，背后血条怪仍会立即转身攻击。
        runtime.释放攻击键()
        state.last_attack_direction = 0
        state.same_direction_attack_streak = 0
        snapshot_is_new_hold = (
            float(latest.detected_at or 0.0)
            > state.last_attack_hold_snapshot_at + 0.001
        )
        if snapshot_is_new_hold:
            runtime.trace_event(
                "combat_side_switch_attack_held",
                locked_direction=state.locked_attack_direction,
                pending_direction=opposite_direction,
                pending_count=state.side_switch_pending_count,
                confirmation_required=max(
                    1, int(state.side_switch_confirmation_required)
                ),
                snapshot_detected_at=round(float(latest.detected_at or 0.0), 6),
                attackable_left_count=latest.attackable_left_count,
                attackable_right_count=latest.attackable_right_count,
                action="wait_without_attacking_empty_side",
            )
        state.last_attack_hold_snapshot_at = max(
            state.last_attack_hold_snapshot_at,
            float(latest.detected_at or 0.0),
        )
        return 0

    return 0


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
            and visual_direction is None
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
    state.last_attack_completed_at = time.monotonic()
    return True


def _process_combat_frame(runtime, state, intent, position):
    """消费一次攻击许可，并用最新目标快照和同侧锁决定实际朝向。"""
    if not state.combat_active:
        _start_combat(runtime, state, position)
    health_bar_evidence = (
        intent.health_bar_attackable_left_count
        + intent.health_bar_attackable_right_count
    ) > 0
    if health_bar_evidence or intent.target_flicker_recoveries <= 0:
        state.flickering_target_attack_streak = 0
    elif (
        intent.target_flicker_recoveries >= V2_FLICKERING_TARGET_MIN_RECOVERIES
        and state.flickering_target_attack_streak
        >= V2_FLICKERING_TARGET_RELEASE_ATTACKS
    ):
        now = time.monotonic()
        state.combat_suppressed_until = max(
            state.combat_suppressed_until,
            now + V2_FLICKERING_TARGET_SUPPRESS_SECONDS,
        )
        suppress_runtime_combat = getattr(runtime, "暂时忽略战斗", None)
        if callable(suppress_runtime_combat):
            suppress_runtime_combat(
                V2_FLICKERING_TARGET_SUPPRESS_SECONDS,
                reason="flickering_template_without_health_bar",
            )
        runtime.trace_event(
            "combat_flickering_target_released",
            position=position,
            attacks=state.flickering_target_attack_streak,
            successful_attacks=state.flickering_target_attack_streak,
            recoveries=intent.target_flicker_recoveries,
            health_bar_evidence=False,
            suppress_ms=round(
                V2_FLICKERING_TARGET_SUPPRESS_SECONDS * 1000.0, 1
            ),
            action="resume_route_and_recheck_later",
        )
        state.flickering_target_attack_streak = 0
        runtime.释放攻击键()
        return False
    queued_direction = runtime.领取攻击意图(wait_seconds=0.0)
    if queued_direction not in (1, 2, 3):
        # 录制路线根据公共怪物快照决定是否停止移动，而检测线程发布的
        # ``攻击`` 是一个会过期、会在冷却期被清空的一次性队列。人物从
        # 保留平台掉到被忽略平台后，返程动作也会被公共快照暂停；若此时
        # 队列恰好丢失，就会形成“既不返程也不攻击”的永久原地锁。
        #
        # 这里把同一份新鲜快照作为第二攻击许可。只允许比最近一次成功
        # 攻击更新的截图触发，避免路线循环对同一帧连续补发多次按键。
        snapshot_detected_at = (
            intent.created_at - max(0.0, float(intent.target_age_ms)) / 1000.0
            if intent.target_age_ms is not None
            else 0.0
        )
        snapshot_is_new = (
            intent.source == "combat"
            and intent.action == "attack"
            and intent.attackable_count > 0
            and snapshot_detected_at
            > max(
                state.last_attack_completed_at,
                state.last_attack_hold_snapshot_at,
            )
        )
        if snapshot_is_new:
            close_group_range = max(
                0.0,
                float(
                    getattr(
                        runtime,
                        "CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE",
                        V2_CLOSE_GROUP_FALLBACK_RANGE,
                    )
                ),
            )
            configured_group_over_count = max(
                0,
                int(intent.configured_group_over_count),
            )
            close_group_count = max(0, int(intent.close_group_count))
            automatic_group_count = max(
                0,
                int(intent.automatic_group_count),
            )
            if close_group_count > 0 or (
                close_group_count == 0
                and intent.nearest_dx is not None
                and intent.nearest_dx <= close_group_range
            ):
                queued_direction = 3
                fallback_reason = "close_target_group_attack"
            elif automatic_group_count > configured_group_over_count:
                queued_direction = 3
                fallback_reason = "configured_monster_count_group_attack"
            elif intent.target_direction == "left":
                queued_direction = 1
                fallback_reason = "fresh_snapshot_target_direction"
            elif intent.target_direction == "right":
                queued_direction = 2
                fallback_reason = "fresh_snapshot_target_direction"
            elif intent.attackable_left_count > intent.attackable_right_count:
                queued_direction = 1
                fallback_reason = "fresh_snapshot_side_count"
            elif intent.attackable_right_count > intent.attackable_left_count:
                queued_direction = 2
                fallback_reason = "fresh_snapshot_side_count"
            else:
                queued_direction = 0
                fallback_reason = "fresh_snapshot_direction_unknown"
            runtime.trace_event(
                "mushroom_v2_attack_queue_fallback",
                reason=fallback_reason,
                resolved_direction=queued_direction,
                target_direction=intent.target_direction,
                target_age_ms=intent.target_age_ms,
                attackable_count=intent.attackable_count,
                attackable_left_count=intent.attackable_left_count,
                attackable_right_count=intent.attackable_right_count,
                nearest_dx=intent.nearest_dx,
                automatic_group_count=automatic_group_count,
                close_group_count=close_group_count,
                configured_group_over_count=configured_group_over_count,
                last_attack_completed_at=state.last_attack_completed_at,
                snapshot_detected_at=snapshot_detected_at,
                position=position,
                action=(
                    "attack_from_fresh_snapshot"
                    if queued_direction in (1, 2, 3)
                    else "wait_for_direction"
                ),
            )
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
        attack_executed = _execute_attack_intent(runtime, state, attack_direction)
        if attack_executed:
            if health_bar_evidence or intent.target_flicker_recoveries <= 0:
                state.flickering_target_attack_streak = 0
            else:
                state.flickering_target_attack_streak += 1
        return attack_executed
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
    automatic_group_count = (
        raw_snapshot[9] if len(raw_snapshot) > 9 else 0
    )
    close_group_count = raw_snapshot[10] if len(raw_snapshot) > 10 else 0
    configured_group_over_count = (
        raw_snapshot[11]
        if len(raw_snapshot) > 11
        else getattr(runtime, "自动群攻大于数量", 0)
    )
    health_bar_chase_left_count = raw_snapshot[12] if len(raw_snapshot) > 12 else 0
    health_bar_chase_right_count = raw_snapshot[13] if len(raw_snapshot) > 13 else 0
    health_bar_attackable_left_count = (
        raw_snapshot[14] if len(raw_snapshot) > 14 else 0
    )
    health_bar_attackable_right_count = (
        raw_snapshot[15] if len(raw_snapshot) > 15 else 0
    )
    target_loss_pending = bool(raw_snapshot[16]) if len(raw_snapshot) > 16 else False
    attack_confirmation_pending = (
        bool(raw_snapshot[17]) if len(raw_snapshot) > 17 else False
    )
    target_flicker_recoveries = raw_snapshot[18] if len(raw_snapshot) > 18 else 0
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
        automatic_group_count = 0
        close_group_count = 0
        health_bar_chase_left_count = 0
        health_bar_chase_right_count = 0
        health_bar_attackable_left_count = 0
        health_bar_attackable_right_count = 0
        target_loss_pending = False
        attack_confirmation_pending = False
        target_flicker_recoveries = 0
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
        automatic_group_count=max(0, int(automatic_group_count)),
        close_group_count=max(0, int(close_group_count)),
        configured_group_over_count=max(
            0,
            int(configured_group_over_count),
        ),
        health_bar_chase_left_count=max(0, int(health_bar_chase_left_count)),
        health_bar_chase_right_count=max(0, int(health_bar_chase_right_count)),
        health_bar_attackable_left_count=max(
            0, int(health_bar_attackable_left_count)
        ),
        health_bar_attackable_right_count=max(
            0, int(health_bar_attackable_right_count)
        ),
        target_loss_pending=target_loss_pending,
        attack_confirmation_pending=attack_confirmation_pending,
        target_flicker_recoveries=max(0, int(target_flicker_recoveries)),
    )


def _build_action_intent(state, snapshot):
    """按攻击、追怪、路线优先级生成本轮唯一动作意图。"""
    now = time.monotonic()
    route_forward_direction = (
        state.patrol_direction
        if state.patrol_direction in ("left", "right")
        else None
    )
    forward_attackable_count = (
        snapshot.attackable_left_count
        if route_forward_direction == "left"
        else (
            snapshot.attackable_right_count
            if route_forward_direction == "right"
            else 0
        )
    )
    if forward_attackable_count > 0:
        state.forward_attack_lock_active = True
        state.forward_attack_lock_direction = route_forward_direction
        # 前方攻击范围持续有怪时，不允许旧的路线恢复计时继续累计。
        state.route_resume_pending_at = 0.0
    elif (
        state.forward_attack_lock_active
        and not snapshot.target_loss_pending
        and not snapshot.attack_confirmation_pending
    ):
        # 只有检测线程完成连续空帧/攻击后残影确认，才解除原地战斗锁。
        state.forward_attack_lock_active = False
        state.forward_attack_lock_direction = None
    if now < state.combat_suppressed_until:
        # 录制路线发现持续误识别或攻击通道没有进展后，会短暂屏蔽当前战斗
        # 快照。检测线程仍可继续刷新画面，但在屏蔽期内必须把控制权交还路线，
        # 否则同一个静态模板会在刚释放 zant 后立即再次锁住人物。
        state.route_resume_pending_at = 0.0
        state.near_chase_pending_at = 0.0
        state.near_chase_pending_direction = None
        state.chase_switch_pending_direction = None
        state.chase_switch_pending_detected_at = 0.0
        state.chase_switch_pending_started_at = 0.0
        state.chase_switch_pending_count = 0
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
    if snapshot.attack_confirmation_pending:
        # 攻击后仍命中同一静态模板时，检测线程正在区分真怪与死亡残影。
        # 这一短窗口必须让攻击队列和路线备用攻击许可同时停下，也不能追着
        # 尚未确认存活的模板移动；只保持当前位置等待下一张新画面。
        return MushroomActionIntent(
            horizontal="stop",
            vertical="keep",
            action="none",
            source="combat_hold",
            target_direction=state.locked_attack_direction or snapshot.direction,
            target_age_ms=snapshot.age_ms,
            chase_count=0,
            attackable_count=0,
            chase_left_count=0,
            chase_right_count=0,
            attackable_left_count=0,
            attackable_right_count=0,
            nearest_dx=None,
            created_at=now,
            attack_confirmation_pending=True,
            target_flicker_recoveries=snapshot.target_flicker_recoveries,
        )
    if snapshot.target_loss_pending:
        # 检测线程正在确认连续空帧。普通模板不再沿锁定侧补打一拍；只有攻击
        # 范围血条能证明目标尚未死亡时才继续攻击，避免死亡后朝空气补刀。
        # 远处追怪则沿上一帧方向继续移动，避免每次模板短漏检都先松开方向键，
        # 形成“追一下、停一下”的机械节奏。录制路线会在平台边缘阻止向外追怪。
        if state.route_resume_pending_at <= 0:
            state.route_resume_pending_at = now
        pending_direction = state.locked_attack_direction or snapshot.direction
        pending_health_bar_count = (
            snapshot.health_bar_attackable_left_count
            + snapshot.health_bar_attackable_right_count
        )
        if (
            state.combat_active
            and pending_direction in ("left", "right")
            and pending_health_bar_count > 0
        ):
            return MushroomActionIntent(
                horizontal="stop",
                vertical="stop",
                action="attack",
                source="combat",
                target_direction=(
                    pending_direction
                ),
                target_age_ms=snapshot.age_ms,
                chase_count=snapshot.chase_count,
                attackable_count=max(1, snapshot.attackable_count),
                chase_left_count=snapshot.chase_left_count,
                chase_right_count=snapshot.chase_right_count,
                attackable_left_count=snapshot.attackable_left_count,
                attackable_right_count=snapshot.attackable_right_count,
                nearest_dx=snapshot.nearest_dx,
                created_at=now,
                automatic_group_count=snapshot.automatic_group_count,
                close_group_count=snapshot.close_group_count,
                configured_group_over_count=snapshot.configured_group_over_count,
                health_bar_chase_left_count=snapshot.health_bar_chase_left_count,
                health_bar_chase_right_count=snapshot.health_bar_chase_right_count,
                health_bar_attackable_left_count=(
                    snapshot.health_bar_attackable_left_count
                ),
                health_bar_attackable_right_count=(
                    snapshot.health_bar_attackable_right_count
                ),
                target_loss_pending=True,
                target_flicker_recoveries=snapshot.target_flicker_recoveries,
            )
        if state.forward_attack_lock_active:
            # 前方攻击范围刚才仍有怪时，一两张模板空帧不能把动作切换成追怪，
            # 否则人物会在攻击间隙向前迈步。保持原地，等待怪物重新出现或完成
            # 连续空帧清怪确认；确认清空后的下一轮才交还路线。
            return MushroomActionIntent(
                horizontal="stop",
                vertical="keep",
                action="none",
                source="combat_hold",
                target_direction=(
                    state.forward_attack_lock_direction
                    or state.locked_attack_direction
                    or snapshot.direction
                ),
                target_age_ms=snapshot.age_ms,
                chase_count=0,
                attackable_count=0,
                chase_left_count=0,
                chase_right_count=0,
                attackable_left_count=0,
                attackable_right_count=0,
                nearest_dx=None,
                created_at=now,
                target_loss_pending=True,
                target_flicker_recoveries=snapshot.target_flicker_recoveries,
            )
        chase_direction = state.chase_direction or snapshot.direction
        if (
            (state.chase_active or snapshot.chase_count > 0)
            and chase_direction in ("left", "right")
        ):
            return MushroomActionIntent(
                horizontal=chase_direction,
                vertical="keep",
                action="none",
                source="chase",
                target_direction=chase_direction,
                target_age_ms=snapshot.age_ms,
                chase_count=max(1, snapshot.chase_count),
                attackable_count=0,
                chase_left_count=snapshot.chase_left_count,
                chase_right_count=snapshot.chase_right_count,
                attackable_left_count=0,
                attackable_right_count=0,
                nearest_dx=snapshot.nearest_dx,
                created_at=now,
                health_bar_chase_left_count=snapshot.health_bar_chase_left_count,
                health_bar_chase_right_count=snapshot.health_bar_chase_right_count,
                target_loss_pending=True,
                target_flicker_recoveries=snapshot.target_flicker_recoveries,
            )
        return MushroomActionIntent(
            horizontal="stop",
            vertical="keep",
            action="none",
            source="combat_hold",
            target_direction=state.locked_attack_direction or snapshot.direction,
            target_age_ms=snapshot.age_ms,
            chase_count=0,
            attackable_count=0,
            chase_left_count=0,
            chase_right_count=0,
            attackable_left_count=0,
            attackable_right_count=0,
            nearest_dx=None,
            created_at=now,
            target_loss_pending=True,
            target_flicker_recoveries=snapshot.target_flicker_recoveries,
        )
    if snapshot.attackable_count > 0:
        state.target_loss_attack_used = False
        state.route_resume_pending_at = 0.0
        state.near_chase_pending_at = 0.0
        state.near_chase_pending_direction = None
        state.chase_switch_pending_direction = None
        state.chase_switch_pending_detected_at = 0.0
        state.chase_switch_pending_started_at = 0.0
        state.chase_switch_pending_count = 0
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
            automatic_group_count=snapshot.automatic_group_count,
            close_group_count=snapshot.close_group_count,
            configured_group_over_count=snapshot.configured_group_over_count,
            health_bar_chase_left_count=snapshot.health_bar_chase_left_count,
            health_bar_chase_right_count=snapshot.health_bar_chase_right_count,
            health_bar_attackable_left_count=(
                snapshot.health_bar_attackable_left_count
            ),
            health_bar_attackable_right_count=(
                snapshot.health_bar_attackable_right_count
            ),
            target_flicker_recoveries=snapshot.target_flicker_recoveries,
        )
    if snapshot.chase_count > 0 and snapshot.direction is not None:
        state.target_loss_attack_used = False
        state.route_resume_pending_at = 0.0
        chase_direction = snapshot.direction
        effective_chase_distance = snapshot.nearest_dx
        remembered_chase_direction = None
        remembered_chase_distance = None
        if state.chase_active and state.chase_direction in ("left", "right"):
            remembered_chase_direction = state.chase_direction
            remembered_chase_distance = state.chase_direction_nearest_dx
        elif (
            state.recent_chase_direction in ("left", "right")
            and state.recent_chase_at > 0
            and now - state.recent_chase_at <= V2_CHASE_DIRECTION_MEMORY_SECONDS
        ):
            remembered_chase_direction = state.recent_chase_direction
            remembered_chase_distance = state.recent_chase_nearest_dx
        chase_direction_changed = (
            remembered_chase_direction in ("left", "right")
            and snapshot.direction != remembered_chase_direction
        )
        health_bar_chase_count = (
            snapshot.health_bar_chase_left_count
            + snapshot.health_bar_chase_right_count
        )
        if chase_direction_changed and health_bar_chase_count <= 0:
            current_direction_distance = remembered_chase_distance
            new_direction_is_clearly_closer = (
                snapshot.nearest_dx is not None
                and current_direction_distance is not None
                and float(snapshot.nearest_dx)
                + V2_CHASE_DIRECTION_SWITCH_CLOSER_MARGIN_X
                <= float(current_direction_distance)
            )
            required_confirmations = (
                V2_CHASE_DIRECTION_SWITCH_CLOSER_CONFIRMATIONS
                if new_direction_is_clearly_closer
                else V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS
            )
            required_stable_seconds = (
                V2_CHASE_DIRECTION_SWITCH_CLOSER_STABLE_SECONDS
                if new_direction_is_clearly_closer
                else V2_CHASE_DIRECTION_SWITCH_STABLE_SECONDS
            )
            pending_matches = (
                state.chase_switch_pending_direction == snapshot.direction
                and now - state.chase_switch_pending_started_at
                <= V2_CHASE_DIRECTION_SWITCH_WINDOW_SECONDS
            )
            if not pending_matches:
                state.chase_switch_pending_direction = snapshot.direction
                state.chase_switch_pending_detected_at = 0.0
                state.chase_switch_pending_started_at = now
                state.chase_switch_pending_count = 0
            if snapshot.detected_at > state.chase_switch_pending_detected_at:
                state.chase_switch_pending_detected_at = snapshot.detected_at
                state.chase_switch_pending_count += 1
            if (
                state.chase_switch_pending_count
                < required_confirmations
                or now - state.chase_switch_pending_started_at
                < required_stable_seconds
            ):
                chase_direction = remembered_chase_direction
                effective_chase_distance = remembered_chase_distance
            else:
                state.chase_direction_nearest_dx = snapshot.nearest_dx
                state.recent_chase_direction = snapshot.direction
                state.recent_chase_nearest_dx = snapshot.nearest_dx
                state.recent_chase_at = now
                state.chase_switch_pending_direction = None
                state.chase_switch_pending_detected_at = 0.0
                state.chase_switch_pending_started_at = 0.0
                state.chase_switch_pending_count = 0
        else:
            if (
                remembered_chase_direction in ("left", "right")
                and snapshot.direction == remembered_chase_direction
                and snapshot.nearest_dx is not None
            ):
                state.chase_direction_nearest_dx = snapshot.nearest_dx
                state.recent_chase_direction = snapshot.direction
                state.recent_chase_nearest_dx = snapshot.nearest_dx
                state.recent_chase_at = now
            state.chase_switch_pending_direction = None
            state.chase_switch_pending_detected_at = 0.0
            state.chase_switch_pending_started_at = 0.0
            state.chase_switch_pending_count = 0
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
                    nearest_dx=effective_chase_distance,
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
            nearest_dx=effective_chase_distance,
            created_at=now,
            health_bar_chase_left_count=snapshot.health_bar_chase_left_count,
            health_bar_chase_right_count=snapshot.health_bar_chase_right_count,
            health_bar_attackable_left_count=(
                snapshot.health_bar_attackable_left_count
            ),
            health_bar_attackable_right_count=(
                snapshot.health_bar_attackable_right_count
            ),
            target_flicker_recoveries=snapshot.target_flicker_recoveries,
        )
    state.chase_switch_pending_direction = None
    state.chase_switch_pending_detected_at = 0.0
    state.chase_switch_pending_started_at = 0.0
    state.chase_switch_pending_count = 0
    recently_engaged = (
        state.combat_active
        or state.chase_active
        or state.locked_attack_direction in ("left", "right")
    )
    state.target_loss_attack_used = False
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
        if state.chase_direction in ("left", "right"):
            state.recent_chase_direction = state.chase_direction
            state.recent_chase_nearest_dx = state.chase_direction_nearest_dx
            state.recent_chase_at = time.monotonic()
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
        return

    if not state.chase_active:
        state.chase_active = True
        state.chase_direction = intent.horizontal
        state.chase_direction_nearest_dx = intent.nearest_dx
        state.recent_chase_direction = intent.horizontal
        state.recent_chase_nearest_dx = intent.nearest_dx
        state.recent_chase_at = time.monotonic()
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
        return
    previous_direction = state.chase_direction
    state.chase_direction = intent.horizontal
    state.chase_direction_nearest_dx = intent.nearest_dx
    state.recent_chase_direction = intent.horizontal
    state.recent_chase_nearest_dx = intent.nearest_dx
    state.recent_chase_at = time.monotonic()
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
    "CombatPerceptionStore",
    "PostAttackTargetGuard",
    "CombatTargetEvaluation",
    "CombatFrameDecision",
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
    "V2_FORWARD_CHASE_EXTRA_RANGE",
    "V2_FLICKERING_TARGET_MIN_RECOVERIES",
    "V2_FLICKERING_TARGET_RELEASE_ATTACKS",
    "V2_FLICKERING_TARGET_SUPPRESS_SECONDS",
    "V2_POST_ATTACK_ALIVE_CONFIRMATION_FRAMES",
    "V2_POST_ATTACK_DEATH_ANIMATION_WINDOW_SECONDS",
    "V2_POST_ATTACK_MOVEMENT_THRESHOLD",
    "V2_CLOSE_GROUP_FALLBACK_RANGE",
    "V2_NEAR_CHASE_DEBOUNCE_MAX_DISTANCE",
    "V2_NEAR_CHASE_DEBOUNCE_SECONDS",
    "V2_ROUTE_RESUME_DEBOUNCE_SECONDS",
    "V2_TARGET_SNAPSHOT_TTL_SECONDS",
    "V2_CHASE_DIRECTION_MEMORY_SECONDS",
    "V2_CHASE_DIRECTION_SWITCH_CONFIRMATIONS",
    "V2_CHASE_DIRECTION_SWITCH_STABLE_SECONDS",
    "V2_TEMPLATE_SIDE_SWITCH_CONFIRMATIONS",
    "V2_TARGET_LOSS_CONFIRMATION_FRAMES",
    "V2_TARGET_LOSS_MIN_SECONDS",
    "apply_action_intent",
    "build_action_intent",
    "clear_combat_for_movement",
    "finish_combat",
    "decide_combat_frame",
    "evaluate_detected_targets",
    "read_monster_snapshot",
    "release_route_keys",
    "reset_attack_direction_lock",
    "set_route_phase",
    "trace_action_decision",
    "wait_for_runtime_ready",
)

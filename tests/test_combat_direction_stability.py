import time
import unittest
from types import SimpleNamespace

from v3.public import combat_strategy


def _intent(
    *,
    direction,
    detected_at,
    left=0,
    right=0,
    health_left=0,
    health_right=0,
):
    now = time.monotonic()
    return combat_strategy.MushroomActionIntent(
        horizontal="stop",
        vertical="stop",
        action="attack",
        source="combat",
        target_direction=direction,
        target_age_ms=max(0.0, (now - detected_at) * 1000),
        chase_count=left + right,
        attackable_count=left + right,
        chase_left_count=left,
        chase_right_count=right,
        attackable_left_count=left,
        attackable_right_count=right,
        nearest_dx=30.0,
        created_at=now,
        health_bar_attackable_left_count=health_left,
        health_bar_attackable_right_count=health_right,
    )


class CombatDirectionStabilityTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.runtime = SimpleNamespace(
            trace_event=lambda name, **details: self.events.append(
                (name, details)
            )
        )

    def test_template_side_switch_requires_two_distinct_detection_frames(self):
        """同一张模板截图被路线循环重复读取时，不能累计换边确认。"""
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="left"
        )
        first_frame = time.monotonic() - 0.10
        first = _intent(direction="right", detected_at=first_frame, right=1)

        self.assertEqual(
            1,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, first, 2
            ),
        )
        self.assertEqual(1, state.side_switch_pending_count)
        self.assertEqual(
            1,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, first, 2
            ),
        )
        self.assertEqual(1, state.side_switch_pending_count)

        second = _intent(
            direction="right",
            detected_at=first_frame + 0.05,
            right=1,
        )
        self.assertEqual(
            2,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, second, 2
            ),
        )
        self.assertEqual("right", state.locked_attack_direction)

    def test_single_template_side_flicker_is_cancelled(self):
        """一帧闪到背后、下一帧回到原侧时应取消换边，不产生转身抽搐。"""
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="left"
        )
        first_frame = time.monotonic() - 0.10
        combat_strategy._resolve_attack_direction(
            self.runtime,
            state,
            _intent(direction="right", detected_at=first_frame, right=1),
            2,
        )
        resolved = combat_strategy._resolve_attack_direction(
            self.runtime,
            state,
            _intent(
                direction="left",
                detected_at=first_frame + 0.05,
                left=1,
            ),
            1,
        )
        self.assertEqual(1, resolved)
        self.assertEqual("left", state.locked_attack_direction)
        self.assertEqual(0, state.side_switch_pending_count)

    def test_health_bar_side_switch_remains_immediate(self):
        """背后绿色血条是活怪强证据，应跳过模板防抖立即转身。"""
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="right"
        )
        intent = _intent(
            direction="left",
            detected_at=time.monotonic(),
            left=1,
            right=1,
            health_left=1,
        )
        resolved = combat_strategy._resolve_attack_direction(
            self.runtime, state, intent, 2
        )
        self.assertEqual(1, resolved)
        self.assertEqual("left", state.locked_attack_direction)

    def test_recent_template_switch_blocks_immediate_reverse(self):
        """模板刚换边后的冷却期内，不能被下一张反向模板立即拉回。"""
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="right",
            last_side_switch_at=time.monotonic(),
        )
        first_frame = time.monotonic() - 0.05
        reverse = _intent(
            direction="left",
            detected_at=first_frame,
            left=1,
        )

        self.assertEqual(
            2,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, reverse, 1
            ),
        )
        self.assertEqual("right", state.locked_attack_direction)
        self.assertEqual(0, state.side_switch_pending_count)

        state.last_side_switch_at = time.monotonic() - 1.0
        self.assertEqual(
            2,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, reverse, 1
            ),
        )
        self.assertEqual(1, state.side_switch_pending_count)

        second = _intent(
            direction="left",
            detected_at=first_frame + 0.05,
            left=1,
        )
        self.assertEqual(
            1,
            combat_strategy._resolve_attack_direction(
                self.runtime, state, second, 1
            ),
        )
        self.assertEqual("left", state.locked_attack_direction)

if __name__ == "__main__":
    unittest.main()

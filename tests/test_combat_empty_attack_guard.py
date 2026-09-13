import time
import unittest

from v3 import legacy_engine
from v3.public import combat_controller, combat_strategy


class CombatEmptyAttackGuardTests(unittest.TestCase):
    def test_static_post_attack_template_requires_another_frame(self):
        events = []
        guard = combat_controller.PostAttackTargetGuard(
            trace=lambda event, **fields: events.append((event, fields)),
        )

        self.assertEqual(
            1,
            guard.filter_attackable_count(
                frame_detected_at=10.0,
                recent_attack_completed_at=0.0,
                decision_count=1,
                decision_direction="right",
                chase_nearest_distance=120.0,
                health_bar_attackable_count=0,
                health_bar_chase_count=0,
                health_bar_green_max_ratio=None,
                close_range_promoted=False,
            ),
        )
        held_count = guard.filter_attackable_count(
            frame_detected_at=10.15,
            recent_attack_completed_at=10.10,
            decision_count=1,
            decision_direction="right",
            chase_nearest_distance=120.0,
            health_bar_attackable_count=0,
            health_bar_chase_count=0,
            health_bar_green_max_ratio=None,
            close_range_promoted=False,
        )

        self.assertEqual(0, held_count)
        self.assertTrue(guard.confirmation_pending)
        self.assertIn(
            "post_attack_target_confirmation",
            [event for event, _fields in events],
        )

    def test_health_bar_evidence_allows_immediate_reattack(self):
        guard = combat_controller.PostAttackTargetGuard(
            trace=lambda _event, **_fields: None,
        )
        guard.filter_attackable_count(
            frame_detected_at=20.0,
            recent_attack_completed_at=0.0,
            decision_count=1,
            decision_direction="left",
            chase_nearest_distance=90.0,
            health_bar_attackable_count=0,
            health_bar_chase_count=0,
            health_bar_green_max_ratio=None,
            close_range_promoted=False,
        )

        allowed_count = guard.filter_attackable_count(
            frame_detected_at=20.15,
            recent_attack_completed_at=20.10,
            decision_count=1,
            decision_direction="left",
            chase_nearest_distance=90.0,
            health_bar_attackable_count=1,
            health_bar_chase_count=1,
            health_bar_green_max_ratio=0.8,
            close_range_promoted=False,
        )

        self.assertEqual(1, allowed_count)
        self.assertFalse(guard.confirmation_pending)

    def test_target_motion_without_health_bar_waits_out_death_window(self):
        events = []
        guard = combat_controller.PostAttackTargetGuard(
            trace=lambda event, **fields: events.append((event, fields)),
        )
        guard.filter_attackable_count(
            frame_detected_at=30.0,
            recent_attack_completed_at=0.0,
            decision_count=1,
            decision_direction="right",
            chase_nearest_distance=180.0,
            health_bar_attackable_count=0,
            health_bar_chase_count=0,
            health_bar_green_max_ratio=None,
            close_range_promoted=False,
        )

        for frame_detected_at, direction, distance, count in (
            (30.15, "left", 40.0, 3),
            (30.40, "right", 220.0, 2),
            (30.65, "left", 70.0, 4),
        ):
            allowed_count = guard.filter_attackable_count(
                frame_detected_at=frame_detected_at,
                recent_attack_completed_at=30.10,
                decision_count=count,
                decision_direction=direction,
                chase_nearest_distance=distance,
                health_bar_attackable_count=0,
                health_bar_chase_count=0,
                health_bar_green_max_ratio=None,
                close_range_promoted=False,
            )
            self.assertEqual(0, allowed_count)
            self.assertTrue(guard.confirmation_pending)

        allowed_after_window = guard.filter_attackable_count(
            frame_detected_at=30.71,
            recent_attack_completed_at=30.10,
            decision_count=2,
            decision_direction="right",
            chase_nearest_distance=90.0,
            health_bar_attackable_count=0,
            health_bar_chase_count=0,
            health_bar_green_max_ratio=None,
            close_range_promoted=False,
        )

        self.assertEqual(2, allowed_after_window)
        self.assertFalse(guard.confirmation_pending)
        event_names = [event for event, _fields in events]
        self.assertNotIn("post_attack_target_motion_confirmed", event_names)
        self.assertEqual(1, event_names.count("post_attack_target_confirmation"))

    def test_confirmation_pending_holds_position_without_attacking(self):
        snapshot = combat_strategy.MushroomMonsterSnapshot(
            detected_at=time.monotonic(),
            direction="right",
            chase_count=1,
            attackable_count=0,
            chase_left_count=0,
            chase_right_count=1,
            attackable_left_count=0,
            attackable_right_count=0,
            nearest_dx=120.0,
            age_ms=10.0,
            fresh=True,
            attack_confirmation_pending=True,
        )

        intent = combat_strategy.build_action_intent(
            combat_strategy.MushroomCombatState(combat_active=True),
            snapshot,
        )

        self.assertEqual("combat_hold", intent.source)
        self.assertEqual("stop", intent.horizontal)
        self.assertEqual("none", intent.action)

    def test_legacy_snapshot_exposes_attack_confirmation_state(self):
        legacy_engine.重置战斗感知()
        try:
            legacy_engine.更新追怪感知(
                0,
                1,
                0,
                1,
                preferred_direction="right",
                nearest_distance=120.0,
                detected_at=time.monotonic(),
                attack_confirmation_pending=True,
            )

            raw_snapshot = legacy_engine.读取追怪感知()
            snapshot = combat_strategy.read_monster_snapshot(
                type(
                    "Runtime",
                    (),
                    {"读取追怪感知": lambda _self: raw_snapshot},
                )()
            )

            self.assertTrue(raw_snapshot[17])
            self.assertTrue(snapshot.attack_confirmation_pending)
            self.assertEqual(0, snapshot.attackable_count)
            self.assertEqual(1, snapshot.chase_count)
        finally:
            legacy_engine.重置战斗感知()


if __name__ == "__main__":
    unittest.main()

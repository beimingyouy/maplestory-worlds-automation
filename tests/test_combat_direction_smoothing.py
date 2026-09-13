import unittest
from types import SimpleNamespace
from unittest.mock import patch

from v3.public import combat_strategy
from v3.public import recorded_route_player


def _snapshot(
    *,
    detected_at,
    direction,
    chase_left=0,
    chase_right=0,
    attackable_left=0,
    attackable_right=0,
):
    return combat_strategy.MushroomMonsterSnapshot(
        detected_at=float(detected_at),
        direction=direction,
        chase_count=int(chase_left + chase_right),
        attackable_count=int(attackable_left + attackable_right),
        chase_left_count=int(chase_left),
        chase_right_count=int(chase_right),
        attackable_left_count=int(attackable_left),
        attackable_right_count=int(attackable_right),
        nearest_dx=350.0,
        age_ms=0.0,
        fresh=True,
    )


class CombatDirectionSmoothingTests(unittest.TestCase):
    def test_opposite_chase_direction_requires_two_distinct_snapshots(self):
        state = combat_strategy.MushroomCombatState(
            chase_active=True,
            chase_direction="left",
            applied_direction="left",
        )

        first = combat_strategy._build_action_intent(
            state,
            _snapshot(
                detected_at=100.0,
                direction="right",
                chase_right=1,
            ),
        )
        repeated_same_frame = combat_strategy._build_action_intent(
            state,
            _snapshot(
                detected_at=100.0,
                direction="right",
                chase_right=1,
            ),
        )
        confirmed = combat_strategy._build_action_intent(
            state,
            _snapshot(
                detected_at=100.05,
                direction="right",
                chase_right=1,
            ),
        )

        self.assertEqual("left", first.horizontal)
        self.assertEqual("left", repeated_same_frame.horizontal)
        self.assertEqual("right", confirmed.horizontal)

    def test_balanced_targets_on_both_sides_keep_current_chase_direction(self):
        state = combat_strategy.MushroomCombatState(
            chase_active=True,
            chase_direction="left",
            applied_direction="left",
        )

        for detected_at in (100.0, 100.05, 100.10):
            intent = combat_strategy._build_action_intent(
                state,
                _snapshot(
                    detected_at=detected_at,
                    direction="right",
                    chase_left=1,
                    chase_right=1,
                ),
            )
            self.assertEqual("chase", intent.source)
            self.assertEqual("left", intent.horizontal)

        self.assertIsNone(state.chase_switch_pending_direction)

    def test_attackable_opposite_target_preempts_chase_hysteresis(self):
        state = combat_strategy.MushroomCombatState(
            chase_active=True,
            chase_direction="left",
            chase_switch_pending_direction="right",
            chase_switch_pending_count=1,
            chase_switch_pending_frame_at=99.0,
        )

        intent = combat_strategy._build_action_intent(
            state,
            _snapshot(
                detected_at=100.0,
                direction="right",
                chase_left=1,
                chase_right=1,
                attackable_right=1,
            ),
        )

        self.assertEqual("combat", intent.source)
        self.assertEqual("right", intent.target_direction)
        self.assertIsNone(state.chase_switch_pending_direction)

    def test_visible_chase_target_starts_target_loss_tracking(self):
        runtime = SimpleNamespace(trace_event=lambda *_args, **_kwargs: None)
        state = recorded_route_player.RecordedRouteState()
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=0.0,
            chase_count=1,
            attackable_count=0,
        )
        intent = SimpleNamespace(
            source="chase",
            target_direction="left",
        )

        with (
            patch.object(
                recorded_route_player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "trace_action_decision",
                return_value=True,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "apply_action_intent",
            ) as apply_intent,
            patch.object(recorded_route_player, "_apply_vertical"),
        ):
            handled = recorded_route_player._apply_smart_seek_priority(
                runtime,
                state,
                (50, 100),
            )

        self.assertTrue(handled)
        self.assertTrue(state.smart_seek_target_active)
        self.assertEqual("left", state.last_monster_direction)
        apply_intent.assert_called_once()

    def test_target_loss_keeps_last_chase_direction_when_route_opposes(self):
        events = []
        movement = []
        runtime = SimpleNamespace(
            trace_event=lambda name, **fields: events.append((name, fields)),
            切换持续移动=lambda direction, reason=None: movement.append(
                (direction, reason)
            ),
        )
        state = recorded_route_player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target",
            last_monster_direction="left",
            route_command_direction="right",
        )
        state.combat.applied_direction = "right"
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=0.0,
            chase_count=0,
            attackable_count=0,
        )
        intent = SimpleNamespace(source="idle")

        with (
            patch.object(
                recorded_route_player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset,
        ):
            handled = recorded_route_player._apply_smart_seek_priority(
                runtime,
                state,
                (50, 100),
            )

        self.assertTrue(handled)
        self.assertEqual("target_loss_direction_coast", state.smart_seek_mode)
        self.assertEqual(
            [("left", "target_loss_chase_coast")],
            movement,
        )
        clear.assert_called_once()
        reset.assert_called_once()
        self.assertIn(
            "recorded_route_smart_seek_target_loss_route_continued",
            [name for name, _fields in events],
        )

    def test_route_resumes_after_direction_coast_without_waiting_full_grace(self):
        base = 100.0
        movement = []
        runtime = SimpleNamespace(
            trace_event=lambda *_args, **_kwargs: None,
            切换持续移动=lambda direction, reason=None: movement.append(
                (direction, reason)
            ),
        )
        state = recorded_route_player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target_loss_direction_coast",
            smart_seek_target_loss_started_at=(
                base
                - recorded_route_player.RECORDED_ROUTE_TARGET_LOSS_DIRECTION_COAST_SECONDS
                - 0.01
            ),
            last_monster_direction="left",
            route_command_direction="right",
        )
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=0.0,
            chase_count=0,
            attackable_count=0,
        )
        intent = SimpleNamespace(source="idle")

        with (
            patch.object(recorded_route_player.time, "monotonic", return_value=base),
            patch.object(
                recorded_route_player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset,
        ):
            handled = recorded_route_player._apply_smart_seek_priority(
                runtime,
                state,
                (50, 100),
            )

        self.assertFalse(handled)
        self.assertEqual("route_during_target_loss", state.smart_seek_mode)
        self.assertEqual([], movement)
        clear.assert_called_once()
        reset.assert_called_once()


if __name__ == "__main__":
    unittest.main()

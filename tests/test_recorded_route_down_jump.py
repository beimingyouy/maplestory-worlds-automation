import unittest
from unittest.mock import patch

from v3.public import recorded_route_player


def _point(x, y, action="none", segment_type="platform", platform_id="平台2"):
    return recorded_route_player.RecordedRoutePoint(
        x=x,
        y=y,
        horizontal="right" if segment_type == "platform" else "none",
        vertical="down" if segment_type == "down_jump" else "none",
        action=action,
        kind="jump" if action == "jump" else segment_type,
        segment_type=segment_type,
        platform_id=platform_id if segment_type == "platform" else None,
    )


def _variant():
    points = [
        _point(10, 100),
        _point(50, 100),
        _point(100, 100),
        _point(50, 100, segment_type="down_jump"),
        _point(50, 100, action="jump", segment_type="down_jump"),
    ]
    platform_range = recorded_route_player.RecordedPlatformRange(
        start_index=0,
        end_index=2,
        minimum_x=10,
        maximum_x=100,
        minimum_y=100,
        maximum_y=100,
        start_x=10,
        platform_number=2,
    )
    return recorded_route_player.RecordedRouteVariant(
        name="down_jump_test",
        probability=100,
        points=points,
        closed_loop=True,
        platform_patrol=False,
        platform_ranges=(platform_range,),
    )


class _Runtime:
    def __init__(self):
        self.events = []

    def trace_event(self, name, **fields):
        self.events.append((name, fields))


class RecordedRouteDownJumpTests(unittest.TestCase):
    def test_down_jump_is_blocked_until_both_platform_edges_are_visited(self):
        runtime = _Runtime()
        state = recorded_route_player.RecordedRouteState()
        variant = _variant()

        recorded_route_player._update_down_jump_patrol_progress(
            runtime,
            state,
            variant,
            (12, 100),
        )
        self.assertTrue(state.down_jump_patrol_seen_min_x)
        self.assertFalse(state.down_jump_patrol_seen_max_x)
        self.assertIsNone(
            recorded_route_player._select_jump_index(
                state,
                variant,
                (50, 100),
                2,
            )
        )

        recorded_route_player._update_down_jump_patrol_progress(
            runtime,
            state,
            variant,
            (96, 100),
        )
        self.assertTrue(state.down_jump_patrol_completed)
        with patch.object(recorded_route_player.random, "randint", return_value=0):
            self.assertEqual(
                4,
                recorded_route_player._select_jump_index(
                    state,
                    variant,
                    (50, 100),
                    2,
                ),
            )
        self.assertEqual(
            1,
            len(
                [
                    event
                    for event in runtime.events
                    if event[0] == "recorded_route_down_jump_patrol_completed"
                ]
            ),
        )

    def test_down_jump_random_offset_is_locked_for_current_attempt(self):
        state = recorded_route_player.RecordedRouteState()
        point = _point(80, 100, action="jump", segment_type="down_jump")
        with patch.object(
            recorded_route_player.random,
            "randint",
            side_effect=[-5, 3],
        ) as randint:
            self.assertEqual(
                75,
                recorded_route_player._down_jump_random_target_x(state, 4, point),
            )
            self.assertEqual(
                75,
                recorded_route_player._down_jump_random_target_x(state, 4, point),
            )
            self.assertEqual(
                83,
                recorded_route_player._down_jump_random_target_x(state, 9, point),
            )
        self.assertEqual(2, randint.call_count)

    def test_ignored_platform_replan_can_take_down_jump_immediately(self):
        state = recorded_route_player.RecordedRouteState(
            platform_replan_active=True,
            active_ignored_platform_number=2,
        )
        variant = _variant()

        self.assertTrue(
            recorded_route_player._down_jump_patrol_ready(
                state,
                variant,
                4,
            )
        )

    def test_early_down_jump_point_routes_to_unvisited_edge(self):
        runtime = _Runtime()
        state = recorded_route_player.RecordedRouteState()
        variant = _variant()
        recorded_route_player._update_down_jump_patrol_progress(
            runtime,
            state,
            variant,
            (12, 100),
        )
        with (
            patch.object(recorded_route_player.combat_logic, "clear_combat_for_movement"),
            patch.object(recorded_route_player.combat_logic, "reset_attack_direction_lock"),
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
            patch.object(recorded_route_player, "_apply_horizontal") as horizontal,
        ):
            handled = recorded_route_player._apply_down_jump_patrol_gate(
                runtime,
                state,
                variant,
                (20, 100),
                3,
            )
        self.assertTrue(handled)
        vertical.assert_called_once_with(runtime, state, "none")
        horizontal.assert_called_once_with(runtime, state, "right")


if __name__ == "__main__":
    unittest.main()

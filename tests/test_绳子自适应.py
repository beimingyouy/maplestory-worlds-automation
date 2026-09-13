import unittest
from unittest.mock import patch

from v3.public import recorded_route_player as player


class _Runtime:
    def __init__(self):
        self.events = []
        self.key_events = []

        class _Input:
            def __init__(inner_self, events):
                inner_self.events = events

            def keyDown(inner_self, key):
                inner_self.events.append(("down", key))

            def keyUp(inner_self, key):
                inner_self.events.append(("up", key))

        self.pydirectinput = _Input(self.key_events)

    def trace_event(self, event, **fields):
        self.events.append((event, fields))

    def 可中断等待(self, *_args, **_kwargs):
        return True


def _rope_point(horizontal="right"):
    return player.RecordedRoutePoint(
        x=52,
        y=152,
        horizontal=horizontal,
        vertical="up",
        action="jump",
        kind="jump",
        segment_type="rope_entry",
        rope_x=54,
        rope_top_y=128,
        rope_bottom_y=148,
    )


def _rope_variant(point, minimum_x=40, maximum_x=70):
    return player.RecordedRouteVariant(
        name="rope-runup",
        probability=100,
        points=[point],
        closed_loop=True,
        platform_patrol=False,
        platform_ranges=(
            player.RecordedPlatformRange(
                start_index=0,
                end_index=0,
                minimum_x=minimum_x,
                maximum_x=maximum_x,
                minimum_y=150,
                maximum_y=154,
                start_x=minimum_x,
                platform_number=1,
            ),
        ),
    )


class RopeEntryAdaptiveTests(unittest.TestCase):
    def test_rope_contact_represses_and_holds_up(self):
        state = player.RecordedRouteState(
            active_rope_direction="left",
            active_rope_x=54,
            active_rope_top_y=128,
            active_rope_bottom_y=148,
            active_rope_started_at=player.time.monotonic(),
            active_rope_best_y=152,
            active_rope_progress_at=player.time.monotonic(),
        )
        runtime = _Runtime()
        with (
            patch.object(player, "_apply_rope_rest", return_value=False),
            patch.object(player, "_start_rope_rest", return_value=False),
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_keep_rope_up_pressed"),
        ):
            handled = player._apply_active_rope(runtime, state, (54, 148))

        self.assertTrue(handled)
        self.assertTrue(state.active_rope_contacted)
        self.assertFalse(state.active_rope_confirmed)
        self.assertTrue(state.active_rope_contact_up_repressed)
        self.assertEqual([("up", "up"), ("down", "up")], runtime.key_events)
        event, fields = runtime.events[-1]
        self.assertEqual("recorded_route_rope_contact_latched", event)
        self.assertTrue(fields["up_repressed"])
        self.assertEqual(8.0, fields["up_release_ms"])

    def test_failures_change_offset_gradually(self):
        point = _rope_point("right")
        state = player.RecordedRouteState()
        runtime = _Runtime()
        profile_key = player._rope_entry_profile_key(point)
        player._rope_entry_profile(state, point)
        state.active_rope_direction = "left"
        state.active_rope_x = 54
        state.active_rope_top_y = 128
        state.active_rope_bottom_y = 148
        state.active_rope_profile_key = profile_key
        state.active_rope_attempt_offset_x = 3

        self.assertEqual(3, player._rope_entry_target_offset(state, point))
        first = player._learn_rope_entry_failure(
            runtime, state, (52, 152), "entry_commit_timeout"
        )
        first_horizontal_hold = (
            player._rope_entry_horizontal_after_jump_hold_seconds(
                state.rope_entry_profiles[profile_key]
            )
        )
        self.assertEqual(2, first["next_offset_x"])
        self.assertEqual(2, player._rope_entry_target_offset(state, point))

        state.active_rope_attempt_offset_x = 2
        second = player._learn_rope_entry_failure(
            runtime, state, (52, 152), "entry_commit_timeout"
        )
        second_horizontal_hold = (
            player._rope_entry_horizontal_after_jump_hold_seconds(
                state.rope_entry_profiles[profile_key]
            )
        )
        self.assertEqual(1, second["next_offset_x"])
        self.assertEqual(1, player._rope_entry_target_offset(state, point))
        self.assertGreater(second["commit_seconds"], first["commit_seconds"])
        self.assertGreater(second_horizontal_hold, first_horizontal_hold)
        self.assertGreaterEqual(
            player.ROUTE_ROPE_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS,
            0.03,
        )

    def test_confirmed_offset_is_reused(self):
        point = _rope_point("right")
        state = player.RecordedRouteState()
        runtime = _Runtime()
        profile_key = player._rope_entry_profile_key(point)
        player._rope_entry_profile(state, point)
        state.active_rope_direction = "left"
        state.active_rope_x = 54
        state.active_rope_top_y = 128
        state.active_rope_bottom_y = 148
        state.active_rope_profile_key = profile_key
        state.active_rope_attempt_offset_x = 3

        player._confirm_rope_entry_contact(runtime, state, (54, 145))
        self.assertEqual(0, state.rope_entry_profiles[profile_key].success_count)
        player._record_rope_entry_success(runtime, state, (54, 128))

        profile = state.rope_entry_profiles[profile_key]
        self.assertEqual(3, profile.preferred_offset_x)
        self.assertEqual(1, profile.success_count)
        self.assertEqual(1, profile.offset_attempts[3])
        self.assertEqual(1, profile.offset_successes[3])
        self.assertEqual(0, profile.consecutive_failures)
        self.assertEqual(3, player._rope_entry_target_offset(state, point))
        self.assertEqual(
            "recorded_route_rope_entry_learning_success",
            runtime.events[-1][0],
        )

    def test_runup_walks_past_rope_then_turns_before_jump(self):
        point = _rope_point("right")
        variant = _rope_variant(point, minimum_x=40, maximum_x=70)
        state = player.RecordedRouteState()
        runtime = _Runtime()

        direction = player._apply_rope_entry_runup(
            runtime, state, variant, point, 0, (52, 152), 54
        )
        self.assertEqual("right", direction)
        self.assertFalse(state.rope_entry_runup_ready)
        self.assertEqual(3, state.rope_entry_runup_target_offset_x)
        self.assertEqual(7, state.rope_entry_runup_staging_offset_x)

        direction = player._apply_rope_entry_runup(
            runtime, state, variant, point, 0, (61, 152), 54
        )
        self.assertEqual("left", direction)
        self.assertTrue(state.rope_entry_runup_ready)

        direction = player._apply_rope_entry_runup(
            runtime, state, variant, point, 0, (57, 152), 54
        )
        self.assertEqual("none", direction)
        self.assertEqual("left", player._rope_entry_jump_direction(57, 54, 3))
        self.assertEqual(0, player._select_jump_index(state, variant, (57, 152), 0))

    def test_runup_is_clamped_inside_platform_safe_edge(self):
        point = _rope_point("right")
        variant = _rope_variant(point, minimum_x=40, maximum_x=60)
        state = player.RecordedRouteState()
        runtime = _Runtime()

        direction = player._apply_rope_entry_runup(
            runtime, state, variant, point, 0, (52, 152), 54
        )
        self.assertEqual("right", direction)
        # 右边界60，预留3像素后最远只能到57；不会走到平台边缘或掉下去。
        self.assertEqual(3, state.rope_entry_runup_staging_offset_x)
        event, fields = runtime.events[-1]
        self.assertEqual("recorded_route_rope_runup_started", event)
        self.assertTrue(fields["runup_clamped"])
        self.assertEqual([40, 60], fields["platform_bounds"])

    def test_jump_waits_until_runup_has_turned_back(self):
        point = _rope_point("right")
        variant = _rope_variant(point)
        state = player.RecordedRouteState()

        self.assertIsNone(player._select_jump_index(state, variant, (57, 152), 0))
        state.rope_entry_runup_index = 0
        state.rope_entry_runup_target_offset_x = 3
        state.rope_entry_runup_staging_offset_x = 7
        state.rope_entry_runup_ready = True
        self.assertEqual(0, player._select_jump_index(state, variant, (57, 152), 0))

    def test_revisited_rope_entry_clears_previous_jump_lock(self):
        """掉回原平台后重新规划同一绳子入口时，必须允许再次发送跳跃。"""
        point = _rope_point("right")
        variant = _rope_variant(point)
        state = player.RecordedRouteState(
            last_jump_index=0,
            last_jump_at=player.time.monotonic() - 5.0,
        )
        runtime = _Runtime()

        rearmed = player._rearm_revisited_rope_entry(
            runtime,
            state,
            point,
            0,
            (52, 152),
        )

        self.assertTrue(rearmed)
        self.assertIsNone(state.last_jump_index)
        self.assertEqual(0.0, state.last_jump_at)
        self.assertEqual("recorded_route_rope_entry_rearmed", runtime.events[-1][0])

        player._apply_rope_entry_runup(
            runtime,
            state,
            variant,
            point,
            0,
            (52, 152),
            54,
        )
        player._apply_rope_entry_runup(
            runtime,
            state,
            variant,
            point,
            0,
            (61, 152),
            54,
        )
        self.assertEqual(0, player._select_jump_index(state, variant, (57, 152), 0))

    def test_rope_top_exit_requires_target_direction_and_real_platform_bounds(self):
        """绳顶X向反方向变化或仍在平台边界外时，不得误判已经离绳成功。"""
        point = _rope_point("right")
        variant = player.replace(
            _rope_variant(point, minimum_x=55, maximum_x=70),
            platform_ranges=(
                player.RecordedPlatformRange(
                    start_index=0,
                    end_index=0,
                    minimum_x=55,
                    maximum_x=70,
                    minimum_y=126,
                    maximum_y=130,
                    start_x=55,
                    platform_number=1,
                ),
            ),
        )
        state = player.RecordedRouteState(
            last_jump_index=0,
            last_jump_at=player.time.monotonic() - 1.0,
            rope_top_exit_pending=True,
            rope_top_exit_direction="right",
            rope_top_exit_route_index=0,
            rope_top_exit_rope_x=54,
            rope_top_exit_top_y=128,
            rope_top_exit_bottom_y=148,
            rope_top_exit_start_x=54,
            rope_top_exit_started_at=player.time.monotonic(),
        )
        runtime = _Runtime()

        with (
            patch.object(player.combat_logic, "clear_combat_for_movement"),
            patch.object(player, "_apply_horizontal") as horizontal,
            patch.object(player, "_apply_vertical"),
        ):
            opposite_move = player._apply_rope_top_exit(
                runtime,
                state,
                variant,
                (52, 128),
            )

            self.assertTrue(opposite_move)
            self.assertTrue(state.rope_top_exit_pending)
            self.assertEqual(0, state.last_jump_index)
            horizontal.assert_called_with(runtime, state, "right")
            self.assertFalse(
                any(
                    event == "recorded_route_rope_top_exit_completed"
                    for event, _fields in runtime.events
                )
            )

            completed = player._apply_rope_top_exit(
                runtime,
                state,
                variant,
                (57, 128),
            )

        self.assertFalse(completed)
        self.assertFalse(state.rope_top_exit_pending)
        self.assertIsNone(state.last_jump_index)
        self.assertEqual(0.0, state.last_jump_at)
        completion_events = [
            fields
            for event, fields in runtime.events
            if event == "recorded_route_rope_top_exit_completed"
        ]
        self.assertEqual(1, len(completion_events))
        self.assertEqual(3, completion_events[0]["directional_moved_x"])
        self.assertEqual([55, 70], completion_events[0]["platform_x_range"])


if __name__ == "__main__":
    unittest.main()

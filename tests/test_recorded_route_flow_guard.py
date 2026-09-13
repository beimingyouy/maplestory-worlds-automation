import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from v3.public import recorded_route_player as player


def _point(
    x,
    y,
    *,
    segment_type="platform",
    action="none",
    horizontal="right",
    vertical="none",
    platform_id=None,
    rope_x=None,
    rope_top_y=None,
    rope_bottom_y=None,
):
    return player.RecordedRoutePoint(
        x=x,
        y=y,
        horizontal=horizontal,
        vertical=vertical,
        action=action,
        kind=segment_type,
        segment_type=segment_type,
        platform_id=platform_id,
        rope_x=rope_x,
        rope_top_y=rope_top_y,
        rope_bottom_y=rope_bottom_y,
    )


def _two_platform_variant():
    points = [
        _point(10, 100, platform_id="平台1"),
        _point(50, 100, platform_id="平台1"),
        _point(
            52,
            100,
            segment_type="rope_entry",
            action="jump",
            horizontal="right",
            vertical="up",
            rope_x=54,
            rope_top_y=50,
            rope_bottom_y=100,
        ),
        _point(
            54,
            75,
            segment_type="rope",
            horizontal="none",
            vertical="up",
            rope_x=54,
            rope_top_y=50,
            rope_bottom_y=100,
        ),
        _point(54, 50, segment_type="rope_exit", horizontal="right"),
        _point(55, 50, platform_id="平台2"),
        _point(95, 50, platform_id="平台2"),
    ]
    return player.RecordedRouteVariant(
        name="flow_guard",
        probability=100,
        points=points,
        closed_loop=True,
        platform_patrol=False,
        platform_ranges=(
            player.RecordedPlatformRange(
                start_index=0,
                end_index=1,
                minimum_x=10,
                maximum_x=50,
                minimum_y=100,
                maximum_y=100,
                start_x=10,
                platform_number=1,
            ),
            player.RecordedPlatformRange(
                start_index=5,
                end_index=6,
                minimum_x=55,
                maximum_x=95,
                minimum_y=50,
                maximum_y=50,
                start_x=55,
                platform_number=2,
            ),
        ),
    )


class _Runtime:
    def __init__(self):
        self.events = []
        self.zant = 0
        self.pydirectinput = SimpleNamespace(
            keyDown=lambda _key: None,
            keyUp=lambda _key: None,
        )

    def trace_event(self, name, **fields):
        self.events.append((name, fields))

    def 释放攻击键(self):
        return None

    def 攻击移动仍锁定(self):
        return False

    def 可中断等待(self, _seconds, interval=0.01):
        return True


class RecordedRouteFlowGuardTests(unittest.TestCase):
    def test_final_route_handoff_rechecks_combat_before_movement(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(smart_seek_mode="patrol")
        variant = _two_platform_variant()
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=0.0,
            chase_count=0,
            attackable_count=1,
        )
        intent = SimpleNamespace(
            source="combat",
            target_direction="left",
        )

        with (
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                player.combat_logic,
                "trace_action_decision",
                return_value=True,
            ),
            patch.object(
                player.combat_logic,
                "apply_action_intent",
            ) as apply_intent,
            patch.object(player, "_apply_vertical") as vertical,
        ):
            handled = player._apply_final_route_combat_handoff_guard(
                runtime,
                state,
                variant,
                (50, 100),
            )

        self.assertTrue(handled)
        apply_intent.assert_called_once_with(
            runtime,
            state.combat,
            intent,
            (50, 100),
            True,
        )
        vertical.assert_called_once_with(runtime, state, "none")
        self.assertIn(
            "recorded_route_movement_preempted_by_latest_target",
            [name for name, _fields in runtime.events],
        )

    def test_target_recovery_resumes_combat_without_route_restart(self):
        runtime = _Runtime()
        base = 100.0
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target_hold",
            smart_seek_target_loss_started_at=base - 0.20,
        )
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=0.0,
            chase_count=0,
            attackable_count=1,
        )
        intent = SimpleNamespace(
            source="combat",
            target_direction="left",
        )

        with (
            patch.object(player.time, "monotonic", return_value=base),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                player.combat_logic,
                "trace_action_decision",
                return_value=False,
            ),
            patch.object(player.combat_logic, "apply_action_intent"),
            patch.object(player, "_apply_vertical"),
        ):
            handled = player._apply_smart_seek_priority(
                runtime,
                state,
                None,
                (50, 100),
            )

        self.assertTrue(handled)
        self.assertEqual(0.0, state.smart_seek_target_loss_started_at)
        self.assertEqual("target", state.smart_seek_mode)
        self.assertIn(
            "recorded_route_smart_seek_target_loss_recovered",
            [name for name, _fields in runtime.events],
        )

    def test_rope_entry_candidates_start_from_the_opposite_side_of_jump(self):
        left_jump = SimpleNamespace(horizontal="left")
        right_jump = SimpleNamespace(horizontal="right")

        self.assertEqual(
            (3, -3),
            player._rope_entry_candidate_offsets(left_jump),
        )
        self.assertEqual(
            (-3, 3),
            player._rope_entry_candidate_offsets(right_jump),
        )

    def test_connection_priority_stays_active_until_next_platform(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(active_platform_range_index=0)
        variant = _two_platform_variant()
        with (
            patch.object(player.combat_logic, "clear_combat_for_movement"),
            patch.object(player.combat_logic, "reset_attack_direction_lock"),
        ):
            started = player._update_connection_priority(
                runtime,
                state,
                variant,
                (50, 100),
                2,
            )
            climbing = player._update_connection_priority(
                runtime,
                state,
                variant,
                (54, 75),
                3,
            )
            completed = player._update_connection_priority(
                runtime,
                state,
                variant,
                (55, 50),
                5,
            )

        self.assertTrue(started)
        self.assertTrue(climbing)
        self.assertFalse(completed)
        self.assertFalse(state.connection_priority_active)
        self.assertEqual(
            [
                "recorded_route_connection_priority_started",
                "recorded_route_connection_priority_completed",
            ],
            [name for name, _fields in runtime.events],
        )

    def test_platform_boundary_handoff_locks_rope_entry_immediately(self):
        """平台边界切到绳入口时必须当场建立连接锁，不能下一帧回退平台点。"""
        runtime = _Runtime()
        state = player.RecordedRouteState(
            route_index=1,
            active_platform_range_index=0,
        )
        variant = _two_platform_variant()
        point = variant.points[1]
        with (
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_apply_vertical"),
            patch.object(player, "_apply_recorded_command") as apply_command,
            patch.object(player.combat_logic, "clear_combat_for_movement"),
            patch.object(player.combat_logic, "reset_attack_direction_lock"),
        ):
            handled = player._apply_platform_coordinate_boundary_guard(
                runtime,
                state,
                variant,
                (50, 100),
                1,
                point,
            )

        self.assertTrue(handled)
        self.assertTrue(state.connection_priority_active)
        self.assertEqual(2, state.connection_priority_route_index)
        self.assertEqual(2, state.route_index)
        apply_command.assert_called_once()
        boundary_event = next(
            fields
            for name, fields in runtime.events
            if name == "recorded_route_platform_boundary_connection"
        )
        self.assertTrue(boundary_event["connection_locked"])

    def test_collision_motion_does_not_hide_control_silence(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(route_index=1)
        state.last_control_intent_at = (
            time.monotonic() - player.RECORDED_ROUTE_CONTROL_SILENCE_SECONDS - 0.1
        )
        variant = _two_platform_variant()
        with patch.object(player, "_force_route_resume") as resume:
            handled = player._apply_route_liveness_watchdog(
                runtime,
                state,
                variant,
                # 即使人物被怪物碰撞到了别的坐标，没有攻击或路线控制意图仍应恢复。
                (35, 101),
            )

        self.assertTrue(handled)
        self.assertIsNone(state.route_index)
        resume.assert_called_once()
        self.assertEqual(
            "no_attack_or_route_control_intent",
            resume.call_args.kwargs["reason"],
        )

    def test_repeated_attacks_keep_combat_priority_until_targets_are_cleared(self):
        runtime = _Runtime()
        runtime.zant = 1
        state = player.RecordedRouteState(route_index=1)
        now = time.monotonic()
        state.last_control_intent_at = now
        state.last_control_intent_kind = "attack_completed"
        state.combat.combat_active = True
        state.combat.last_attack_completed_at = now
        state.last_route_progress_at = (
            now - player.RECORDED_ROUTE_COMBAT_STARVATION_SECONDS - 0.1
        )
        variant = _two_platform_variant()
        with patch.object(player, "_force_route_resume") as resume:
            handled = player._apply_route_liveness_watchdog(
                runtime,
                state,
                variant,
                (45, 100),
            )

        self.assertFalse(handled)
        resume.assert_not_called()
        self.assertEqual("attack_completed", state.last_control_intent_kind)
        self.assertGreaterEqual(state.last_route_progress_at, now)

    def test_combat_lock_has_no_absolute_timeout_while_attacks_progress(self):
        runtime = _Runtime()
        state = player.RecordedRouteState()
        now = time.monotonic()
        state.combat.combat_active = True
        state.combat.combat_attack_count = 12
        state.combat.last_attack_completed_at = now
        state.combat_lock_started_at = now - 30.0
        state.combat_lock_last_progress_at = now - 0.1
        state.combat_lock_last_attack_completed_at = now - 0.2
        snapshot = SimpleNamespace(age_ms=20.0, attackable_count=1, chase_count=1)
        intent = SimpleNamespace(source="combat", attackable_count=1)

        with patch.object(player, "_release_stalled_combat") as release:
            timed_out = player._combat_lock_timed_out(
                runtime,
                state,
                snapshot,
                intent,
                (45, 100),
            )

        self.assertFalse(timed_out)
        release.assert_not_called()

    def test_held_route_direction_without_expected_motion_jumps_inward(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(route_index=1)
        now = time.monotonic()
        state.last_control_intent_at = now
        state.route_command_direction = "right"
        state.route_command_observed_direction = "right"
        state.route_command_anchor_x = 50
        state.route_command_progress_at = (
            now - player.RECORDED_ROUTE_MOVE_NO_PROGRESS_SECONDS - 0.1
        )
        state.last_route_progress_at = now
        variant = _two_platform_variant()
        with (
            patch.object(player, "_force_route_resume"),
            patch.object(player, "_apply_vertical"),
            patch.object(player, "_apply_horizontal") as horizontal,
        ):
            handled = player._apply_route_liveness_watchdog(
                runtime,
                state,
                variant,
                (50, 100),
            )

        self.assertTrue(handled)
        # 人物位于平台1右侧，兜底必须向平台内部左跳，不能固定右跳掉下去。
        horizontal.assert_called_once_with(runtime, state, "left")
        self.assertEqual(1, state.route_recovery_stage)

    def test_transient_target_loss_keeps_route_moving_during_reacquire_window(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target",
            last_monster_direction="right",
            platform_direction="left",
        )
        state.combat.patrol_direction = "left"
        snapshot = SimpleNamespace(fresh=True, age_ms=0.0)
        intent = SimpleNamespace(source="idle")
        with (
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                player.combat_logic, "clear_combat_for_movement"
            ) as clear,
            patch.object(
                player.combat_logic, "reset_attack_direction_lock"
            ) as reset,
            patch.object(player, "_apply_horizontal") as horizontal,
            patch.object(player, "_apply_vertical") as vertical,
        ):
            handled = player._apply_smart_seek_priority(
                runtime,
                state,
                (50, 100),
            )

        self.assertFalse(handled)
        self.assertTrue(state.smart_seek_target_active)
        self.assertEqual("route_during_target_loss", state.smart_seek_mode)
        self.assertGreater(state.smart_seek_target_loss_started_at, 0.0)
        clear.assert_called_once()
        reset.assert_called_once()
        horizontal.assert_not_called()
        vertical.assert_not_called()
        self.assertIn(
            "recorded_route_smart_seek_target_loss_route_continued",
            [name for name, _fields in runtime.events],
        )
        self.assertFalse(player._route_resume_grace_active(state))
        self.assertEqual(0.0, state.loot_approach_until)
        self.assertIsNone(state.loot_approach_direction)
        self.assertEqual("left", state.platform_direction)
        self.assertEqual("left", state.combat.patrol_direction)

    def test_target_loss_grace_expiry_keeps_route_running_without_scan(self):
        runtime = _Runtime()
        base = 100.0
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target_hold",
            smart_seek_target_loss_started_at=(
                base - player.RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS
                - 0.01
            ),
        )
        snapshot = SimpleNamespace(fresh=True, age_ms=0.0, chase_count=0, attackable_count=0)
        intent = SimpleNamespace(source="idle")
        with (
            patch.object(player.time, "monotonic", return_value=base),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_apply_vertical"),
        ):
            handled = player._apply_smart_seek_priority(runtime, state, (50, 100))

        self.assertFalse(handled)
        clear.assert_called_once()
        reset.assert_called_once()
        self.assertFalse(state.smart_seek_target_active)
        self.assertEqual(0.0, state.smart_seek_target_loss_started_at)
        self.assertEqual(0.0, state.smart_seek_scan_until)
        self.assertIn(
            "recorded_route_smart_seek_target_loss_grace_expired",
            [name for name, _fields in runtime.events],
        )
        self.assertNotIn(
            "recorded_route_post_combat_scan_started",
            [name for name, _fields in runtime.events],
        )

    def test_new_target_loss_grace_includes_existing_snapshot_age(self):
        runtime = _Runtime()
        base = 100.0
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target",
        )
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=400.0,
            chase_count=0,
            attackable_count=0,
        )
        intent = SimpleNamespace(source="idle")

        with (
            patch.object(player.time, "monotonic", return_value=base),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_apply_vertical"),
        ):
            handled = player._apply_smart_seek_priority(runtime, state, (50, 100))

        self.assertFalse(handled)
        self.assertAlmostEqual(base - 0.40, state.smart_seek_target_loss_started_at)
        clear.assert_called_once()
        reset.assert_called_once()
        hold_events = [
            fields
            for name, fields in runtime.events
            if name == "recorded_route_smart_seek_target_loss_route_continued"
        ]
        self.assertEqual(1, len(hold_events))
        self.assertEqual(400.0, hold_events[0]["elapsed_loss_ms"])
        self.assertEqual(50.0, hold_events[0]["remaining_grace_ms"])

        with (
            patch.object(player.time, "monotonic", return_value=base + 0.06),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_apply_vertical"),
        ):
            handled = player._apply_smart_seek_priority(runtime, state, (50, 100))

        self.assertFalse(handled)
        clear.assert_called_once()
        reset.assert_called_once()
        self.assertFalse(state.smart_seek_target_active)
        self.assertEqual(0.0, state.smart_seek_target_loss_started_at)
        self.assertIn(
            "recorded_route_smart_seek_target_loss_grace_expired",
            [name for name, _fields in runtime.events],
        )

    def test_stale_combat_hold_does_not_reset_target_loss_grace(self):
        runtime = _Runtime()
        base = 100.0
        loss_started_at = base - 0.30
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target_hold",
            smart_seek_target_loss_started_at=loss_started_at,
        )
        snapshot = SimpleNamespace(
            fresh=False,
            age_ms=5000.0,
            chase_count=0,
            attackable_count=0,
        )
        intent = SimpleNamespace(
            source="combat_hold",
            target_direction="left",
        )

        with (
            patch.object(player.time, "monotonic", return_value=base),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(player.combat_logic, "apply_action_intent") as apply_intent,
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal") as horizontal,
            patch.object(player, "_apply_vertical") as vertical,
        ):
            handled = player._apply_smart_seek_priority(runtime, state, (50, 100))

        self.assertFalse(handled)
        self.assertEqual(loss_started_at, state.smart_seek_target_loss_started_at)
        self.assertEqual("route_during_target_loss", state.smart_seek_mode)
        apply_intent.assert_not_called()
        clear.assert_called_once()
        reset.assert_called_once()
        horizontal.assert_not_called()
        vertical.assert_not_called()
        self.assertNotIn(
            "recorded_route_smart_seek_target_loss_recovered",
            [name for name, _fields in runtime.events],
        )

        with (
            patch.object(player.time, "monotonic", return_value=base + 0.20),
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(player.combat_logic, "apply_action_intent") as apply_intent,
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal"),
            patch.object(player, "_apply_vertical"),
        ):
            handled = player._apply_smart_seek_priority(runtime, state, (50, 100))

        self.assertFalse(handled)
        apply_intent.assert_not_called()
        clear.assert_called_once()
        reset.assert_called_once()
        self.assertFalse(state.smart_seek_target_active)
        self.assertEqual(0.0, state.smart_seek_target_loss_started_at)
        self.assertIn(
            "recorded_route_smart_seek_target_loss_grace_expired",
            [name for name, _fields in runtime.events],
        )

    def test_combat_hold_without_real_target_does_not_stop_route(self):
        runtime = _Runtime()
        state = player.RecordedRouteState(
            smart_seek_target_active=True,
            smart_seek_mode="target",
            platform_direction="left",
        )
        state.combat.patrol_direction = "left"
        snapshot = SimpleNamespace(
            fresh=True,
            age_ms=120.0,
            chase_count=0,
            attackable_count=0,
        )
        intent = SimpleNamespace(source="combat_hold", target_direction="left")
        with (
            patch.object(
                player.combat_logic,
                "read_monster_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                player.combat_logic,
                "build_action_intent",
                return_value=intent,
            ),
            patch.object(
                player.combat_logic,
                "trace_action_decision",
                return_value=True,
            ),
            patch.object(player.combat_logic, "apply_action_intent") as apply_intent,
            patch.object(player.combat_logic, "clear_combat_for_movement") as clear,
            patch.object(player.combat_logic, "reset_attack_direction_lock") as reset,
            patch.object(player, "_apply_horizontal") as horizontal,
            patch.object(player, "_apply_vertical") as vertical,
        ):
            handled = player._apply_smart_seek_priority(
                runtime,
                state,
                (50, 100),
            )

        self.assertFalse(handled)
        apply_intent.assert_not_called()
        clear.assert_called_once()
        reset.assert_called_once()
        horizontal.assert_not_called()
        vertical.assert_not_called()
        self.assertFalse(player._route_resume_grace_active(state))
        self.assertEqual("left", state.platform_direction)
        self.assertEqual("left", state.combat.patrol_direction)

    def test_route_resume_grace_temporarily_blocks_new_combat(self):
        state = player.RecordedRouteState()
        state.route_resume_grace_until = time.monotonic() + 0.5
        self.assertTrue(player._route_resume_grace_active(state))
        state.route_resume_grace_until = time.monotonic() - 0.1
        self.assertFalse(player._route_resume_grace_active(state))


if __name__ == "__main__":
    unittest.main()

import time
import unittest
from unittest.mock import patch

from v3 import legacy_engine
from v3.public import combat_strategy


class _SnapshotRuntime:
    自动群攻大于数量 = 2

    def __init__(self, detected_at, chase_count=1, attackable_count=1):
        self.detected_at = detected_at
        self.chase_count = chase_count
        self.attackable_count = attackable_count

    def 读取追怪感知(self):
        return (
            self.detected_at,
            "right",
            self.chase_count,
            self.attackable_count,
            80.0,
            0,
            self.chase_count,
            0,
            self.attackable_count,
            self.attackable_count,
            0,
            2,
        )


class _TupleSnapshotRuntime:
    自动群攻大于数量 = 2

    def __init__(self, snapshot):
        self.snapshot = tuple(snapshot)

    def 读取追怪感知(self):
        return self.snapshot


class CombatSnapshotTimingTests(unittest.TestCase):
    def test_snapshot_survives_one_slow_template_detection_interval(self):
        runtime = _SnapshotRuntime(time.monotonic() - 1.8)

        snapshot = combat_strategy.read_monster_snapshot(runtime)

        self.assertTrue(snapshot.fresh)
        self.assertEqual(1, snapshot.chase_count)
        self.assertEqual(1, snapshot.attackable_count)

    def test_snapshot_still_expires_if_detection_thread_stops(self):
        runtime = _SnapshotRuntime(
            time.monotonic()
            - combat_strategy.V2_TARGET_SNAPSHOT_TTL_SECONDS
            - 0.2
        )

        snapshot = combat_strategy.read_monster_snapshot(runtime)

        self.assertFalse(snapshot.fresh)
        self.assertEqual(0, snapshot.chase_count)
        self.assertEqual(0, snapshot.attackable_count)

    def test_first_missing_frame_refreshes_existing_snapshot_without_changing_target(self):
        previous = legacy_engine.读取追怪感知()
        try:
            original_detected_at = time.monotonic() - 1.8
            legacy_engine.更新追怪感知(
                1,
                1,
                0,
                1,
                attackable_right_count=1,
                preferred_direction="right",
                nearest_distance=80.0,
                detected_at=original_detected_at,
            )

            with patch.object(legacy_engine.time, "monotonic", return_value=time.monotonic()):
                refreshed = legacy_engine.保持追怪感知有效()
            snapshot = legacy_engine.读取追怪感知()

            self.assertTrue(refreshed)
            self.assertGreater(snapshot[0], original_detected_at)
            self.assertEqual("right", snapshot[1])
            self.assertEqual(1, snapshot[2])
            self.assertEqual(1, snapshot[3])
        finally:
            legacy_engine.重置战斗感知()
            if previous[2] > 0:
                legacy_engine.更新追怪感知(
                    previous[3],
                    previous[2],
                    previous[5],
                    previous[6],
                    attackable_left_count=previous[7],
                    attackable_right_count=previous[8],
                    preferred_direction=previous[1],
                    nearest_distance=previous[4],
                    detected_at=previous[0],
                    automatic_group_count=previous[9],
                    close_group_count=previous[10],
                    configured_group_over_count=previous[11],
                )

    def test_public_snapshot_requires_three_empty_frames_before_route_resume(self):
        """所有检测入口都必须经过三张空帧确认，不能一漏检就清空目标。"""
        legacy_engine.重置战斗感知()
        try:
            base = time.monotonic()
            legacy_engine.更新追怪感知(
                1,
                1,
                1,
                0,
                attackable_left_count=1,
                preferred_direction="left",
                nearest_distance=40.0,
                detected_at=base,
            )
            for offset in (0.05, 0.10, 0.20):
                legacy_engine.更新追怪感知(0, 0, 0, 0, detected_at=base + offset)
                snapshot = legacy_engine.读取追怪感知()
                self.assertEqual(1, snapshot[2])
                self.assertEqual("left", snapshot[1])
                self.assertTrue(snapshot[16])

                action = combat_strategy.build_action_intent(
                    combat_strategy.MushroomCombatState(combat_active=True),
                    combat_strategy.read_monster_snapshot(
                        _TupleSnapshotRuntime(snapshot)
                    ),
                )
                self.assertEqual("combat", action.source)
                self.assertEqual("attack", action.action)

            legacy_engine.更新追怪感知(0, 0, 0, 0, detected_at=base + 0.35)
            snapshot = legacy_engine.读取追怪感知()
            self.assertEqual(0, snapshot[2])
            self.assertIsNone(snapshot[1])
            self.assertFalse(snapshot[16])
        finally:
            legacy_engine.重置战斗感知()

    def test_target_reappearing_cancels_pending_empty_frame_confirmation(self):
        """空帧确认中重新识别到怪物时，应保留战斗并重新从0开始计数。"""
        legacy_engine.重置战斗感知()
        try:
            base = time.monotonic()
            legacy_engine.更新追怪感知(
                1, 1, 0, 1,
                attackable_right_count=1,
                preferred_direction="right",
                detected_at=base,
            )
            legacy_engine.更新追怪感知(0, 0, 0, 0, detected_at=base + 0.05)
            legacy_engine.更新追怪感知(
                1, 1, 0, 1,
                attackable_right_count=1,
                preferred_direction="right",
                detected_at=base + 0.10,
            )
            legacy_engine.更新追怪感知(0, 0, 0, 0, detected_at=base + 0.15)
            snapshot = legacy_engine.读取追怪感知()
            self.assertEqual(1, snapshot[2])
            self.assertEqual("right", snapshot[1])
        finally:
            legacy_engine.重置战斗感知()


if __name__ == "__main__":
    unittest.main()

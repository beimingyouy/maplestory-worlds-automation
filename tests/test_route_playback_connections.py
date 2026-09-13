import unittest
import time

from v3.public.route_recording import _platform_sweep_points
from v3.public import recorded_route_player as route_player


def _platform_points(minimum_x=10, maximum_x=20, y=100):
    return [
        {
            "x": x,
            "y": y,
            "command": "right none none",
            "kind": "platform",
            "elapsed_ms": x,
            "segment_id": 1,
            "segment_type": "platform",
            "platform_id": "平台1",
        }
        for x in range(minimum_x, maximum_x + 1)
    ]


class RoutePlaybackConnectionTests(unittest.TestCase):
    def test_middle_landing_connects_to_full_sweep_and_next_transition(self):
        points = _platform_points()

        rebuilt = _platform_sweep_points(
            points,
            incoming_x=15,
            outgoing_x=17,
        )

        coordinates = [(int(point["x"]), int(point["y"])) for point in rebuilt]
        self.assertEqual((15, 100), coordinates[0])
        self.assertEqual((17, 100), coordinates[-1])
        self.assertEqual(10, min(x for x, _y in coordinates))
        self.assertEqual(20, max(x for x, _y in coordinates))
        self.assertLessEqual(
            max(
                abs(next_x - x) + abs(next_y - y)
                for (x, y), (next_x, next_y) in zip(
                    coordinates,
                    coordinates[1:],
                )
            ),
            1,
        )

    def test_down_jump_landing_switches_cursor_to_target_platform(self):
        points = [
            route_player.RecordedRoutePoint(
                10, 100, "right", "none", "none", "platform", "platform", "平台1"
            ),
            route_player.RecordedRoutePoint(
                15, 100, "none", "down", "jump", "down_jump", "down_jump"
            ),
            route_player.RecordedRoutePoint(
                15, 110, "none", "none", "none", "down_jump", "down_jump"
            ),
            # 下跳末点和目标平台首点允许同坐标；同距离时必须选择后面的
            # 平台点，才能立即完成平台切换，而不是继续卡在下跳段。
            route_player.RecordedRoutePoint(
                15, 110, "right", "none", "none", "platform", "platform", "平台2"
            ),
            route_player.RecordedRoutePoint(
                20, 110, "right", "none", "none", "platform", "platform", "平台2"
            ),
        ]
        variant = route_player.RecordedRouteVariant(
            name="down_jump_switch",
            probability=100,
            points=points,
            closed_loop=False,
            platform_patrol=False,
            platform_ranges=(
                route_player.RecordedPlatformRange(0, 0, 10, 10, 100, 100, 10, 1),
                route_player.RecordedPlatformRange(3, 4, 15, 20, 110, 110, 15, 2),
            ),
        )
        state = route_player.RecordedRouteState(
            route_index=1,
            active_platform_range_index=0,
            connection_priority_active=True,
            connection_priority_source_platform_index=0,
            connection_priority_route_index=1,
            connection_priority_started_at=time.monotonic(),
        )

        class Runtime:
            def __init__(self):
                self.events = []

            def trace_event(self, event, **fields):
                self.events.append((event, fields))

        runtime = Runtime()
        selected = route_player._select_route_index(
            runtime,
            state,
            variant,
            (15, 110),
        )

        self.assertEqual(3, selected)
        self.assertEqual("platform", variant.points[selected].segment_type)
        route_player._update_connection_priority(
            runtime,
            state,
            variant,
            (15, 110),
            selected,
        )
        self.assertFalse(state.connection_priority_active)
        self.assertTrue(
            any(
                event == "recorded_route_connection_priority_completed"
                for event, _fields in runtime.events
            )
        )

    def test_relocalization_stays_in_current_platform_until_connection(self):
        first_pass = [
            route_player.RecordedRoutePoint(
                x,
                100,
                "right",
                "none",
                "none",
                "platform",
                "platform",
                "平台1",
            )
            for x in range(61)
        ]
        transition = [
            route_player.RecordedRoutePoint(
                60,
                100,
                "none",
                "down",
                "jump",
                "down_jump",
                "down_jump",
            )
        ]
        duplicate_pass = [
            route_player.RecordedRoutePoint(
                x,
                100,
                "right",
                "none",
                "none",
                "platform",
                "platform",
                "平台1",
            )
            for x in range(61)
        ]
        points = first_pass + transition + duplicate_pass
        variant = route_player.RecordedRouteVariant(
            name="duplicate_platform_passes",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=(
                route_player.RecordedPlatformRange(0, 60, 0, 60, 100, 100, 0, 1),
                route_player.RecordedPlatformRange(62, 122, 0, 60, 100, 100, 0, 1),
            ),
        )
        state = route_player.RecordedRouteState(route_index=50)

        class Runtime:
            def __init__(self):
                self.events = []

            def trace_event(self, event, **fields):
                self.events.append((event, fields))

        runtime = Runtime()
        selected = route_player._select_route_index(
            runtime,
            state,
            variant,
            (0, 100),
        )

        self.assertEqual(0, selected)
        self.assertLessEqual(selected, 60)
        relocation = next(
            fields
            for event, fields in runtime.events
            if event == "recorded_route_relocalized"
        )
        self.assertEqual(0, relocation["platform_range_index"])
        self.assertEqual(
            "stay_on_current_platform_until_recorded_connection",
            relocation["reason"],
        )

    def test_uninitialized_cursor_prefers_platform_over_down_jump_tail(self):
        points = [
            route_player.RecordedRoutePoint(
                10, 100, "right", "none", "none", "platform", "platform", "平台1"
            ),
            route_player.RecordedRoutePoint(
                20, 100, "right", "none", "none", "platform", "platform", "平台1"
            ),
            route_player.RecordedRoutePoint(
                10, 100, "none", "none", "none", "down_jump", "down_jump"
            ),
        ]
        variant = route_player.RecordedRouteVariant(
            name="startup_overlap",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=(
                route_player.RecordedPlatformRange(0, 1, 10, 20, 100, 100, 10, 1),
            ),
        )
        state = route_player.RecordedRouteState(route_index=None)

        class Runtime:
            def trace_event(self, _event, **_fields):
                return None

        selected = route_player._select_route_index(
            Runtime(),
            state,
            variant,
            (10, 100),
        )

        self.assertEqual(0, selected)
        self.assertEqual("platform", variant.points[selected].segment_type)

    def test_short_rope_top_does_not_relock_rope_entry(self):
        points = [
            route_player.RecordedRoutePoint(
                65, 174, "right", "none", "none", "platform", "platform", "平台3"
            ),
            route_player.RecordedRoutePoint(
                65,
                174,
                "right",
                "up",
                "jump",
                "jump",
                "rope_entry",
                rope_x=67,
                rope_top_y=159,
                rope_bottom_y=171,
            ),
            route_player.RecordedRoutePoint(
                67, 160, "none", "up", "none", "rope", "rope"
            ),
            route_player.RecordedRoutePoint(
                67, 159, "right", "none", "none", "rope_exit", "rope_exit"
            ),
            route_player.RecordedRoutePoint(
                67, 159, "left", "none", "none", "platform", "platform", "平台2"
            ),
        ]
        variant = route_player.RecordedRouteVariant(
            name="short_rope_exit",
            probability=100,
            points=points,
            closed_loop=False,
            platform_patrol=False,
            platform_ranges=(
                route_player.RecordedPlatformRange(0, 0, 65, 65, 174, 174, 65, 3),
                route_player.RecordedPlatformRange(4, 4, 67, 67, 159, 159, 67, 2),
            ),
        )
        state = route_player.RecordedRouteState(
            route_index=3,
            connection_priority_active=True,
            connection_priority_source_platform_index=0,
            connection_priority_route_index=1,
            connection_priority_started_at=time.monotonic(),
        )

        class Runtime:
            def trace_event(self, _event, **_fields):
                return None

        selected = route_player._select_route_index(
            Runtime(),
            state,
            variant,
            (67, 159),
        )

        self.assertEqual(4, selected)
        self.assertEqual("platform", variant.points[selected].segment_type)

    def test_reverse_oriented_platform_still_starts_and_ends_at_connections(self):
        points = _platform_points()

        rebuilt = _platform_sweep_points(
            points,
            incoming_x=18,
            outgoing_x=12,
        )

        coordinates = [(int(point["x"]), int(point["y"])) for point in rebuilt]
        self.assertEqual((18, 100), coordinates[0])
        self.assertEqual((12, 100), coordinates[-1])
        self.assertEqual(10, min(x for x, _y in coordinates))
        self.assertEqual(20, max(x for x, _y in coordinates))
        self.assertLessEqual(
            max(
                abs(next_x - x) + abs(next_y - y)
                for (x, y), (next_x, next_y) in zip(
                    coordinates,
                    coordinates[1:],
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()

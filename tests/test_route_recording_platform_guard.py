import unittest

from v3.public.route_recording import (
    PLATFORM_RECORD_BASELINE_SAMPLE_COUNT,
    PLATFORM_RECORD_OFF_PLATFORM_FRAMES,
    VALID_SEGMENT_TYPES,
    V2_TRANSITION_SEGMENT_TYPES,
    _observe_platform_recording_position,
    _build_route_graph,
    _build_segment_summaries,
    _sanitize_recorded_platform_points,
    _validate_platform_geometry,
)


def _point(segment_id, segment_type, x, y, platform_id=None):
    return {
        "segment_id": segment_id,
        "segment_type": segment_type,
        "platform_id": platform_id,
        "x": x,
        "y": y,
        "command": "right none none",
        "kind": "platform" if segment_type == "platform" else "rope",
        "elapsed_ms": x,
    }


class RouteRecordingPlatformGuardTests(unittest.TestCase):
    def test_all_v2_transition_buttons_are_valid_recording_segments(self):
        self.assertTrue(V2_TRANSITION_SEGMENT_TYPES)
        self.assertTrue(V2_TRANSITION_SEGMENT_TYPES.issubset(VALID_SEGMENT_TYPES))

    def test_save_cleanup_removes_false_icon_and_other_floor_points(self):
        points = [
            _point(1, "platform", x, 159 + (x % 2), "平台2")
            for x in range(50, 70)
        ]
        points.extend(
            [
                _point(1, "platform", 286, 236, "平台2"),
                _point(1, "platform", 280, 237, "平台2"),
                _point(1, "platform", 270, 238, "平台2"),
            ]
        )
        points.append(_point(2, "rope", 67, 171))

        cleaned, report = _sanitize_recorded_platform_points(points)

        platform_points = [point for point in cleaned if point["segment_id"] == 1]
        self.assertEqual(20, len(platform_points))
        self.assertTrue(all(159 <= point["y"] <= 160 for point in platform_points))
        self.assertEqual(3, report[1]["removed_points"])
        self.assertEqual(1, len([point for point in cleaned if point["segment_id"] == 2]))

    def test_live_guard_ignores_instant_yellow_marker_teleport(self):
        state = {}
        for index in range(PLATFORM_RECORD_BASELINE_SAMPLE_COUNT):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(state, (50 + index, 174)),
            )

        self.assertEqual(
            "outlier",
            _observe_platform_recording_position(state, (228, 223)),
        )
        self.assertEqual(1, state["recognition_outliers"])
        self.assertEqual(
            "valid",
            _observe_platform_recording_position(state, (58, 174)),
        )

    def test_live_guard_ends_after_continuous_plausible_fall(self):
        state = {}
        for index in range(PLATFORM_RECORD_BASELINE_SAMPLE_COUNT):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(state, (80 + index, 159)),
            )

        for index in range(PLATFORM_RECORD_OFF_PLATFORM_FRAMES - 1):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(
                    state,
                    (88 + index, 166 + index),
                ),
            )
        self.assertEqual(
            "departed",
            _observe_platform_recording_position(
                state,
                (88 + PLATFORM_RECORD_OFF_PLATFORM_FRAMES, 170),
            ),
        )

    def test_upward_jump_does_not_end_platform_recording(self):
        state = {}
        for index in range(PLATFORM_RECORD_BASELINE_SAMPLE_COUNT):
            _observe_platform_recording_position(state, (100 + index, 180))
        for index in range(10):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(state, (108 + index, 172)),
            )
        self.assertEqual(0, state.get("off_platform_frames", 0))

    def test_live_guard_follows_long_downhill_slope(self):
        state = {}
        for index in range(PLATFORM_RECORD_BASELINE_SAMPLE_COUNT):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(state, (30 + index, 147)),
            )
        for offset in range(1, 26):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(
                    state,
                    (37 + offset, 147 + offset),
                ),
            )

        self.assertEqual(172, state["surface_y"])
        self.assertEqual(0, state.get("off_platform_frames", 0))
        self.assertGreater(state.get("slope_points", 0), 20)

    def test_live_guard_follows_uphill_and_slope_from_first_frame(self):
        state = {}
        for offset in range(30):
            self.assertEqual(
                "valid",
                _observe_platform_recording_position(
                    state,
                    (40 + offset, 180 - offset),
                ),
            )

        self.assertEqual(151, state["surface_y"])
        self.assertEqual(0, state.get("off_platform_frames", 0))
        self.assertGreater(state.get("slope_points", 0), 20)

    def test_save_cleanup_preserves_sloped_platform_points(self):
        points = [
            _point(1, "platform", 20 + offset, 140 + offset, "平台1")
            for offset in range(31)
        ]

        cleaned, report = _sanitize_recorded_platform_points(points)

        self.assertEqual(len(points), len(cleaned))
        self.assertEqual(0, report[1]["removed_points"])
        self.assertEqual(140, report[1]["surface_min_y"])
        self.assertEqual(170, report[1]["surface_max_y"])
        segments = _build_segment_summaries(cleaned)
        self.assertTrue(segments[0]["slope_compatible"])
        _validate_platform_geometry(segments)

    def test_rope_matching_uses_platform_local_y_near_rope_x(self):
        lower = [
            _point(1, "platform", offset, 100 + offset, "平台1")
            for offset in range(81)
        ]
        upper = [
            _point(2, "platform", x, 90, "平台2")
            for x in range(70, 91)
        ]
        rope = [
            _point(3, "rope", 80, y)
            for y in range(90, 181)
        ]
        points = lower + upper + rope
        segments = _build_segment_summaries(points)

        graph = _build_route_graph(segments, raw_points=points)

        self.assertEqual([], graph["unmatched_rope_ids"])
        self.assertEqual(1, len(graph["connections"]))
        self.assertGreaterEqual(graph["connections"][0]["lower_platform_y"], 178)
        self.assertEqual(90, graph["connections"][0]["upper_platform_y"])


if __name__ == "__main__":
    unittest.main()

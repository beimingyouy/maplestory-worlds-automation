import unittest

from v3.public import route_recording


class RouteRecordingWarningSaveTests(unittest.TestCase):
    def test_unmatched_rope_is_a_saved_warning_instead_of_validation_error(self):
        route_graph = {
            "unmatched_rope_ids": [3],
            "unmatched_transition_ids": [],
        }

        warnings = route_recording._route_graph_recording_warnings(
            route_graph,
            is_v2=False,
            rope_count=1,
            legacy_transition_count=0,
        )

        self.assertEqual(1, len(warnings))
        self.assertEqual("unmatched_rope_platforms", warnings[0]["code"])
        self.assertEqual([3], warnings[0]["segment_ids"])
        self.assertEqual(
            route_recording.ROPE_LOWER_PLATFORM_Y_TOLERANCE,
            warnings[0]["tolerance"]["lower_platform_y"],
        )

    def test_unmatched_return_connection_keeps_segment_ids_for_analysis(self):
        warnings = route_recording._route_graph_recording_warnings(
            {
                "unmatched_rope_ids": [],
                "unmatched_transition_ids": [7, 9],
            },
            is_v2=False,
            rope_count=0,
            legacy_transition_count=2,
        )

        self.assertEqual(1, len(warnings))
        self.assertEqual("unmatched_return_transition", warnings[0]["code"])
        self.assertEqual([7, 9], warnings[0]["segment_ids"])

    def test_v2_graph_uses_its_own_fusion_diagnostics(self):
        warnings = route_recording._route_graph_recording_warnings(
            {
                "unmatched_rope_ids": [3],
                "unmatched_transition_ids": [7],
            },
            is_v2=True,
            rope_count=1,
            legacy_transition_count=1,
        )

        self.assertEqual([], warnings)


if __name__ == "__main__":
    unittest.main()

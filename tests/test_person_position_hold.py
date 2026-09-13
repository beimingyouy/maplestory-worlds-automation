import unittest
from unittest.mock import patch

import cv2
import numpy as np

from v3 import person_locator
from v3.runtime_trace import RuntimeTrace


class PersonPositionHoldTests(unittest.TestCase):
    def test_recent_reliable_position_is_held_for_only_two_hundred_ms(self):
        rng = np.random.default_rng(20260815)
        template = rng.integers(0, 256, (12, 16, 3), dtype=np.uint8)
        monitor = {"top": 300, "left": 0, "width": 1000, "height": 300}
        tracker = person_locator.CharacterTracker(
            ("xueliang.png", template),
            monitor=monitor,
        )
        frame = np.zeros((300, 1000, 3), dtype=np.uint8)
        frame[50:62, 100:116] = template

        with patch.object(person_locator.time, "monotonic", return_value=100.0):
            confidence, screen_center, _local_center = tracker.locate(cv2, frame)

        self.assertGreaterEqual(confidence, tracker.threshold)
        self.assertEqual((108, 356), screen_center)
        self.assertEqual((108, 356), tracker.recent_screen_center(now=100.199))
        self.assertAlmostEqual(
            199.0,
            tracker.recent_screen_center_age_ms(now=100.199),
        )
        self.assertIsNone(tracker.recent_screen_center(now=100.201))

    def test_reset_discards_held_position_immediately(self):
        tracker = person_locator.CharacterTracker(None)
        tracker.last_screen_center = (500, 400)
        tracker.last_success_at = 100.0

        tracker.reset()

        self.assertIsNone(tracker.recent_screen_center(now=100.01))
        self.assertIsNone(tracker.recent_screen_center_age_ms(now=100.01))

    def test_compact_detection_log_marks_held_person_position(self):
        compact = RuntimeTrace._compact_fields(
            "detection_frame",
            {
                "person_position_held": True,
                "person_position_age_ms": 125.0,
                "person_mode": "local-miss 1/2",
                "targets": 2,
                "nearby": 1,
                "chase_targets": 2,
            },
        )

        self.assertTrue(compact["ph"])
        self.assertEqual(125.0, compact["pa"])
        self.assertEqual("local-miss 1/2", compact["pmode"])


if __name__ == "__main__":
    unittest.main()

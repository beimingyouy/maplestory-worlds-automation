import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from v3.public.server_error_guard import (
    detect_server_connection_error,
    load_server_error_template,
    save_server_error_screenshot,
)
from v3.automation_service import AutomationService


class ServerErrorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parents[1]
        cls.reference = load_server_error_template(cls.project_root)

    def test_detects_supplied_error_image_inside_full_window(self):
        frame = np.zeros((600, 900, 3), dtype=np.uint8)
        top, left = 180, 260
        height, width = self.reference.shape[:2]
        frame[top:top + height, left:left + width] = self.reference

        result = detect_server_connection_error(frame, self.reference)

        self.assertTrue(result.matched)
        self.assertGreaterEqual(result.message_confidence, 0.99)
        self.assertGreaterEqual(result.button_confidence, 0.99)
        self.assertEqual((left, top), result.popup_top_left)

    def test_brightness_change_still_matches(self):
        adjusted = cv2.convertScaleAbs(self.reference, alpha=0.94, beta=8)
        frame = np.zeros((500, 700, 3), dtype=np.uint8)
        height, width = adjusted.shape[:2]
        frame[120:120 + height, 200:200 + width] = adjusted

        result = detect_server_connection_error(frame, self.reference)

        self.assertTrue(result.matched)

    def test_unrelated_frame_does_not_match(self):
        generator = np.random.default_rng(20260815)
        frame = generator.integers(0, 256, (500, 700, 3), dtype=np.uint8)

        result = detect_server_connection_error(frame, self.reference)

        self.assertFalse(result.matched)

    def test_trigger_screenshot_is_saved_as_png(self):
        output_path = save_server_error_screenshot(
            self.reference,
            self.project_root,
        )
        try:
            self.assertTrue(output_path.is_file())
            restored = cv2.imread(str(output_path), cv2.IMREAD_COLOR)
            self.assertIsNotNone(restored)
            self.assertEqual(self.reference.shape, restored.shape)
        finally:
            output_path.unlink(missing_ok=True)

    def test_guard_stops_service_after_confirmed_match(self):
        frame = np.zeros((600, 900, 4), dtype=np.uint8)
        height, width = self.reference.shape[:2]
        frame[180:180 + height, 260:260 + width, :3] = self.reference
        frame[:, :, 3] = 255

        class Engine:
            stop_event2 = 1
            游戏窗口外框宽度 = 900
            游戏窗口外框高度 = 600
            游戏客户区屏幕左 = 0
            游戏客户区屏幕顶 = 0
            游戏客户区宽度 = 900
            游戏客户区高度 = 600
            np = np

            def get_base_dir(self):
                return str(ServerErrorGuardTests.project_root)

            def grab_screen(self, _monitor):
                return frame

        service = AutomationService()
        engine = Engine()
        service._engine = engine
        service.stop = Mock(side_effect=lambda: setattr(engine, "stop_event2", 0))
        expected_path = self.project_root / "logs" / "confirmed_server_error.png"

        with (
            patch("v3.automation_service.SERVER_ERROR_CONFIRMATION_FRAMES", 1),
            patch(
                "v3.automation_service.save_server_error_screenshot",
                return_value=expected_path,
            ) as save_screenshot,
        ):
            service._run_server_error_guard(engine)

        service.stop.assert_called_once_with()
        save_screenshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()

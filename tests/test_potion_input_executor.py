import unittest
from unittest.mock import patch

from v3 import legacy_engine


class _InputDriver:
    def __init__(self):
        self.events = []

    def keyDown(self, key):
        self.events.append(("down", key))

    def keyUp(self, key):
        self.events.append(("up", key))


class PotionInputExecutorTests(unittest.TestCase):
    def test_short_presses_are_serial_and_release_each_key(self):
        """执行器以 keyDown/keyUp 成对完成一瓶后才处理下一瓶。"""
        driver = _InputDriver()
        clock = iter((0.0, 0.0, 0.05, 0.10, 0.10, 0.10, 0.15, 0.20))

        with patch.object(legacy_engine.time, "monotonic", side_effect=clock):
            self.assertTrue(
                legacy_engine._执行药水按键(
                    "1",
                    input_driver=driver,
                    should_stop=lambda: False,
                    wait=lambda _seconds: None,
                )
            )
            self.assertTrue(
                legacy_engine._执行药水按键(
                    "2",
                    input_driver=driver,
                    should_stop=lambda: False,
                    wait=lambda _seconds: None,
                )
            )

        self.assertEqual(
            [("down", "1"), ("up", "1"), ("down", "2"), ("up", "2")],
            driver.events,
        )

    def test_potion_probe_queues_without_sleeping_in_detection_thread(self):
        """连续确认和冷却仍在调用线程，实际按键改为快速入队。"""
        legacy_engine.重置药水检测状态()
        try:
            with (
                patch.object(legacy_engine, "药水检测点位于客户区", return_value=True),
                patch.object(legacy_engine, "is_white_pixel", return_value=True),
                patch.object(legacy_engine, "_排队药水按键", return_value=True) as enqueue,
                patch.object(legacy_engine.time, "monotonic", return_value=100.0),
                patch.object(legacy_engine.time, "sleep") as sleep,
            ):
                # 第一帧只累计确认；第二帧才入队，保持原有两帧判定规则。
                self.assertFalse(
                    legacy_engine.尝试按下药水("health", "1", "红量", 10, 10)
                )
                self.assertTrue(
                    legacy_engine.尝试按下药水("health", "1", "红量", 10, 10)
                )

            enqueue.assert_called_once_with("1")
            sleep.assert_not_called()
        finally:
            legacy_engine.重置药水检测状态()

    def test_stopped_task_never_starts_a_late_key_press(self):
        driver = _InputDriver()

        pressed = legacy_engine._执行药水按键(
            "1",
            input_driver=driver,
            should_stop=lambda: True,
            wait=lambda _seconds: None,
        )

        self.assertFalse(pressed)
        self.assertEqual([("up", "1")], driver.events)


if __name__ == "__main__":
    unittest.main()

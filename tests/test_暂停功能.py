import threading
import time
from types import SimpleNamespace
import unittest

from v3.automation_service import AutomationService
from v3.config import AppConfig
from v3 import legacy_engine


class _AliveWorker:
    @staticmethod
    def is_alive():
        return True


class _InputRecorder:
    def __init__(self):
        self.down = []
        self.up = []
        self.pressed = []
        self.clicked = 0

    def keyDown(self, key, *args, **kwargs):
        self.down.append(key)

    def keyUp(self, key, *args, **kwargs):
        self.up.append(key)

    def press(self, key, *args, **kwargs):
        self.pressed.append(key)

    def click(self, *args, **kwargs):
        self.clicked += 1


class PauseFeatureTests(unittest.TestCase):
    def test_pause_aware_input_blocks_new_actions_but_allows_release(self):
        recorder = _InputRecorder()
        guarded = legacy_engine._PauseAwareDirectInput(recorder)
        previous_event = legacy_engine.用户运行许可事件
        try:
            legacy_engine.用户运行许可事件 = threading.Event()
            guarded.keyDown("right")
            guarded.press("c")
            guarded.click(10, 20)
            guarded.keyUp("right")
            self.assertEqual([], recorder.down)
            self.assertEqual([], recorder.pressed)
            self.assertEqual(0, recorder.clicked)
            self.assertEqual(["right"], recorder.up)

            legacy_engine.用户运行许可事件.set()
            guarded.keyDown("left")
            guarded.press("x")
            guarded.click(10, 20)
            self.assertEqual(["left"], recorder.down)
            self.assertEqual(["x"], recorder.pressed)
            self.assertEqual(1, recorder.clicked)
        finally:
            legacy_engine.用户运行许可事件 = previous_event

    def test_interruptible_wait_freezes_duration_while_paused(self):
        previous_event = legacy_engine.用户运行许可事件
        previous_stop = legacy_engine.stop_event
        previous_stop2 = legacy_engine.stop_event2
        try:
            permission = threading.Event()
            permission.set()
            legacy_engine.用户运行许可事件 = permission
            legacy_engine.stop_event = threading.Event()
            legacy_engine.stop_event2 = 1
            result = []
            started_at = time.monotonic()
            worker = threading.Thread(
                target=lambda: result.append(legacy_engine.可中断等待(0.08, interval=0.01))
            )
            worker.start()
            time.sleep(0.025)
            permission.clear()
            time.sleep(0.10)
            permission.set()
            worker.join(timeout=1.0)
            elapsed = time.monotonic() - started_at
            self.assertEqual([True], result)
            self.assertGreaterEqual(elapsed, 0.17)
        finally:
            legacy_engine.用户运行许可事件 = previous_event
            legacy_engine.stop_event = previous_stop
            legacy_engine.stop_event2 = previous_stop2

    def test_automation_pause_releases_keys_and_resume_relocates(self):
        service = AutomationService()
        service._worker = _AliveWorker()
        service._last_config = AppConfig(
            single_attack_key="x",
            group_attack_key="f",
            flash_key="c",
            scheduled_keys=(("q", 60.0),),
        )
        recorder = _InputRecorder()
        cleared = []
        relocation = threading.Event()
        engine = SimpleNamespace(
            用户运行许可事件=service._pause_event,
            zant=1,
            清除攻击意图=lambda: cleared.append(True),
            pydirectinput=recorder,
            人物全图重定位事件=relocation,
        )
        service._engine = engine
        service._running_route_label = "自定义录制路线"

        self.assertTrue(service.pause())
        self.assertTrue(service.is_paused)
        self.assertFalse(service._pause_event.is_set())
        self.assertEqual(0, engine.zant)
        self.assertIn("left", recorder.up)
        self.assertIn("right", recorder.up)
        self.assertIn("x", recorder.up)
        self.assertIn("f", recorder.up)
        self.assertIn("q", recorder.up)

        self.assertTrue(service.resume())
        self.assertFalse(service.is_paused)
        self.assertTrue(service._pause_event.is_set())
        self.assertTrue(relocation.is_set())
        self.assertGreaterEqual(len(cleared), 2)


if __name__ == "__main__":
    unittest.main()

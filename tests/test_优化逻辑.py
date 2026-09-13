"""针对 3.0 结构、注释覆盖和检测节奏优化的静态回归测试。"""

import ast
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from v3.public.monster_detection import (
    ATTACK_INTENT_TTL_SECONDS,
    COMBAT_RELEASE_GRACE_SECONDS,
    COMBAT_TARGET_LOST_CONFIRMATION_FRAMES,
    MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_X,
    MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET,
    MONSTER_TEMPLATE_CONFIDENCE,
    POST_ATTACK_RECHECK_SECONDS,
    YOLO_MAX_DETECTIONS,
    YOLO_MONSTER_CONFIDENCE,
    associate_monster_health_bars,
    detect_monster_health_bars,
    match_monster_templates,
    prepare_monster_template,
)
from v3.map.definitions import MAP_SPECS
from v3.person_locator import (
    CharacterTracker,
    PERSON_LOCAL_FAILURES_BEFORE_FULL,
    load_character_templates,
    require_character_templates,
)
from v3.runtime_trace import start_runtime_trace, stop_runtime_trace, trace_event


ROOT = Path(__file__).resolve().parent.parent


class V3OptimizationTests(unittest.TestCase):
    """验证本轮重构的重要约束不会被后续修改破坏。"""

    def test_all_production_functions_have_docstrings(self):
        """v3 生产包内的每个函数和方法都应有职责说明。"""
        missing = []
        for path in (ROOT / "v3").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if ast.get_docstring(node) is None:
                        missing.append("{}:{} {}".format(path, node.lineno, node.name))
        self.assertEqual([], missing)

    def test_map_definitions_live_in_dedicated_package(self):
        """地图元数据和路线实现都应集中在 v3/map。"""
        self.assertEqual(19, len(MAP_SPECS))
        registry = (ROOT / "v3" / "map_registry.py").read_text(encoding="utf-8")
        definitions = (ROOT / "v3" / "map" / "definitions.py").read_text(
            encoding="utf-8"
        )
        routes = (ROOT / "v3" / "map" / "routes.py").read_text(encoding="utf-8")
        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertIn("from .map.definitions import MAP_SPECS, MapSpec", registry)
        self.assertIn("MapSpec(\"蘑菇\"", definitions)
        route_functions = {
            node.name
            for node in ast.parse(routes).body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue({spec.handler for spec in MAP_SPECS}.issubset(route_functions))
        self.assertIn("def 安装地图路线", engine)
        self.assertNotIn("def 蘑菇地图1():", engine)

    def test_detection_threshold_and_attack_recheck_are_enabled(self):
        """正式与测试检测应共用置信度阈值，并启用攻击后复检窗口。"""
        self.assertGreaterEqual(YOLO_MONSTER_CONFIDENCE, 0.5)
        self.assertLessEqual(YOLO_MONSTER_CONFIDENCE, 0.85)
        self.assertGreater(POST_ATTACK_RECHECK_SECONDS, 0.0)
        self.assertLess(POST_ATTACK_RECHECK_SECONDS, 1.0)
        self.assertAlmostEqual(0.75, MONSTER_TEMPLATE_CONFIDENCE)
        self.assertGreater(MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET, 40.0)

        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        test_service = (ROOT / "v3" / "detection_test_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("conf=YOLO怪物置信度", engine)
        self.assertIn("conf=float(confidence)", test_service)
        self.assertIn("发布攻击意图", engine)
        self.assertIn("标记攻击完成", engine)
        self.assertIn("threshold=MONSTER_TEMPLATE_CONFIDENCE", engine)
        self.assertIn("threshold=MONSTER_TEMPLATE_CONFIDENCE", test_service)

    def test_character_tracker_uses_gray_local_search_then_full_relocation(self):
        """人物应先灰度局部搜索，并在连续两次失败后才执行全区域重定位。"""
        import cv2
        import numpy as np

        rng = np.random.default_rng(20260805)
        template = rng.integers(0, 256, (12, 16, 3), dtype=np.uint8)
        monitor = {"top": 300, "left": 0, "width": 1000, "height": 300}
        tracker = CharacterTracker(
            ("dengpao.png", template),
            monitor,
            radius_x=100,
            radius_y=70,
        )

        first = np.zeros((300, 1000, 3), dtype=np.uint8)
        first[50:62, 100:116] = template
        confidence, screen_center, _local_center = tracker.locate(cv2, first)
        self.assertGreaterEqual(confidence, 0.99)
        self.assertEqual((108, 356), screen_center)
        self.assertEqual("full", tracker.last_mode)
        self.assertEqual(2, tracker.template_gray.ndim)
        cached_gray_template = tracker.template_gray

        nearby = np.zeros_like(first)
        nearby[55:67, 110:126] = template
        confidence, screen_center, _local_center = tracker.locate(cv2, nearby)
        self.assertGreaterEqual(confidence, 0.99)
        self.assertEqual((118, 361), screen_center)
        self.assertEqual("local", tracker.last_mode)
        self.assertIs(cached_gray_template, tracker.template_gray)

        far_away = np.zeros_like(first)
        far_away[200:212, 800:816] = template
        confidence, screen_center, local_center = tracker.locate(cv2, far_away)
        self.assertLess(confidence, tracker.threshold)
        self.assertIsNone(screen_center)
        self.assertIsNone(local_center)
        self.assertEqual(
            "local-miss 1/{}".format(PERSON_LOCAL_FAILURES_BEFORE_FULL),
            tracker.last_mode,
        )

        confidence, screen_center, _local_center = tracker.locate(cv2, far_away)
        self.assertGreaterEqual(confidence, 0.99)
        self.assertEqual((808, 506), screen_center)
        self.assertEqual("full", tracker.last_mode)
        self.assertGreaterEqual(tracker.last_match_ms, 0.0)

    def test_detection_preview_reports_actual_match_time(self):
        """测试预览和正式日志都应展示人物匹配与怪物推理的实际毫秒耗时。"""
        service = (ROOT / "v3" / "detection_test_service.py").read_text(
            encoding="utf-8"
        )
        window = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertIn('"self_match_ms": character_tracker.last_match_ms', service)
        self.assertIn('"monster_match_ms": monster_match_ms', service)
        self.assertIn("人物匹配：{self_ms:.2f}ms", window)
        self.assertIn("人物匹配={:.2f}ms", engine)
        self.assertIn("YOLO={:.1f}ms，人物匹配={:.2f}ms", engine)
        self.assertIn("monster_ms=round(怪物检测耗时, 3)", engine)

    def test_screenshot_mode_throttles_full_window_capture_in_virtual_machine(self):
        """截图运行应低频抓完整窗口，其余帧只抓检测区以避免虚拟机低FPS。"""
        from v3 import legacy_engine as engine

        original_test_mode = engine.测试截图模式
        original_callback = engine.检测预览回调
        original_interval = engine.完整检测预览截图间隔
        original_grab_screen = engine.grab_screen
        captured_areas = []
        detection_monitor = {
            "top": 300,
            "left": 0,
            "width": 640,
            "height": 330,
        }

        def fake_grab_screen(area):
            captured_areas.append(dict(area))
            return engine.np.zeros(
                (int(area["height"]), int(area["width"]), 4),
                dtype=engine.np.uint8,
            )

        engine.测试截图模式 = True
        engine.检测预览回调 = lambda *_args, **_kwargs: None
        engine.完整检测预览截图间隔 = 60.0
        engine.grab_screen = fake_grab_screen
        if hasattr(engine._capture_local, "full_preview_captured_at"):
            del engine._capture_local.full_preview_captured_at
        try:
            first = engine.捕获检测与预览画面(detection_monitor)
            second = engine.捕获检测与预览画面(detection_monitor)

            self.assertIsNotNone(first[1])
            self.assertIsNone(second[1])
            self.assertGreater(captured_areas[0]["height"], 330)
            self.assertEqual(detection_monitor, captured_areas[1])
            self.assertIn("完整预览限频", second[4])
        finally:
            engine.测试截图模式 = original_test_mode
            engine.检测预览回调 = original_callback
            engine.完整检测预览截图间隔 = original_interval
            engine.grab_screen = original_grab_screen
            if hasattr(engine._capture_local, "full_preview_captured_at"):
                del engine._capture_local.full_preview_captured_at

    def test_normal_mode_uses_capture_window_alignment_without_enabling_preview(self):
        """正常模式也应复用截图模式的窗口贴齐流程，且不能改变预览开关。"""
        from v3 import legacy_engine as engine

        original_test_mode = engine.测试截图模式
        try:
            engine.测试截图模式 = False
            with (
                patch.object(
                    engine,
                    "find_window_by_title",
                    return_value=[101, 202],
                ) as find_windows,
                patch.object(engine, "将游戏窗口贴齐截图原点") as align,
                patch.object(
                    engine,
                    "更新游戏客户区尺寸",
                    return_value=(1280, 800),
                ) as update_client_size,
                patch.object(engine, "trace_event") as trace,
                patch("builtins.print"),
            ):
                self.assertTrue(engine.prepare_game_window(capture_mode=False))
                self.assertEqual(
                    [((101,), {}), ((202,), {})],
                    align.call_args_list,
                )
                self.assertEqual(
                    [((101,), {}), ((202,), {})],
                    update_client_size.call_args_list,
                )
                trace.assert_not_called()
                self.assertFalse(engine.测试截图模式)

                align.reset_mock()
                update_client_size.reset_mock()
                self.assertTrue(engine.prepare_game_window(capture_mode=True))
                align.assert_called_once_with(101)
                update_client_size.assert_called_once_with(101)

                detection_monitor = {
                    "top": 300,
                    "left": 0,
                    "width": 640,
                    "height": 330,
                }
                with patch.object(
                    engine,
                    "grab_screen",
                    return_value=engine.np.zeros((330, 640, 4), dtype=engine.np.uint8),
                ) as grab:
                    detection_img, preview_img, *_rest = engine.捕获检测与预览画面(
                        detection_monitor
                    )
                grab.assert_called_once_with(detection_monitor)
                self.assertIs(detection_img, preview_img)

                find_windows.return_value = []
                trace.reset_mock()
                self.assertFalse(engine.prepare_game_window(capture_mode=True))
                trace.assert_called_once()
                self.assertEqual(
                    {
                        "success": False,
                        "hwnd": None,
                        "preserve_resolution": True,
                    },
                    {
                        key: trace.call_args.kwargs[key]
                        for key in ("success", "hwnd", "preserve_resolution")
                    },
                )
        finally:
            engine.测试截图模式 = original_test_mode

    def test_yolo_and_detection_loops_avoid_extra_fixed_waits(self):
        """YOLO 应提前筛选怪物类别，检测循环按目标周期补时而非固定追加 sleep。"""
        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        service = (ROOT / "v3" / "detection_test_service.py").read_text(
            encoding="utf-8"
        )
        self.assertGreater(YOLO_MAX_DETECTIONS, 0)
        self.assertIn("classes=[0]", engine)
        self.assertIn("max_det=YOLO_MAX_DETECTIONS", engine)
        self.assertIn("classes=[0]", service)
        self.assertIn("等待下一检测帧", engine)
        self.assertIn("pydirectinput.PAUSE = PYDIRECTINPUT_PAUSE_SECONDS", engine)
        self.assertNotIn("[YOLO检测] 目标=", engine)

    def test_combat_requires_consecutive_lost_frames_before_moving(self):
        """单帧或双帧漏检不得解除战斗，第三帧确认后才恢复路线。"""
        from v3 import legacy_engine as engine

        engine.stop_event2 = 1
        engine.stop_event = None
        engine.重置攻击状态()
        engine.重置战斗感知()
        engine.zant = 0
        try:
            lost = engine.处理怪物检测结果(
                1, 1, 0, 0, preferred_direction="left"
            )
            self.assertEqual(0, lost)
            self.assertEqual(1, engine.zant)
            self.assertEqual(1, engine.领取攻击意图(wait_seconds=0))

            # 此用例只校验连续丢帧计数，因此先把战斗记忆窗口设为已过期。
            with engine.战斗感知锁:
                engine.最近近怪时间 = (
                    time.monotonic() - COMBAT_RELEASE_GRACE_SECONDS - 0.01
                )

            for expected in range(1, COMBAT_TARGET_LOST_CONFIRMATION_FRAMES):
                lost = engine.处理怪物检测结果(0, 0, 0, lost)
                self.assertEqual(expected, lost)
                self.assertEqual(1, engine.zant)

            lost = engine.处理怪物检测结果(0, 0, 0, lost)
            self.assertEqual(0, lost)
            self.assertEqual(0, engine.zant)
        finally:
            engine.重置攻击状态()
            engine.重置战斗感知()
            engine.stop_event2 = 0

    def test_auto_group_attack_uses_configurable_over_count_within_150_x(self):
        """人物 X 左右 150 像素内怪物数量严格大于配置值时自动群攻。"""
        from v3 import legacy_engine as engine

        previous_over_count = engine.自动群攻大于数量
        engine.stop_event2 = 1
        engine.stop_event = None
        engine.重置攻击状态()
        engine.重置战斗感知()
        engine.zant = 0
        try:
            engine.自动群攻大于数量 = 2
            count = engine.统计横向群攻怪物数量(
                850,
                ((850, 500), (700, 520), (1000, 480), (1001, 500)),
            )
            self.assertEqual(3, count)
            engine.处理怪物检测结果(
                1,
                1,
                0,
                0,
                automatic_group_count=count,
                preferred_direction="left",
            )
            self.assertEqual(3, engine.领取攻击意图(wait_seconds=0))

            engine.重置攻击状态()
            engine.zant = 0
            engine.自动群攻大于数量 = 3
            engine.处理怪物检测结果(
                1,
                1,
                0,
                0,
                automatic_group_count=3,
                preferred_direction="left",
            )
            self.assertEqual(1, engine.领取攻击意图(wait_seconds=0))

            engine.重置攻击状态()
            engine.zant = 0
            engine.处理怪物检测结果(
                1,
                1,
                0,
                0,
                automatic_group_count=4,
                preferred_direction="left",
            )
            self.assertEqual(3, engine.领取攻击意图(wait_seconds=0))
        finally:
            engine.自动群攻大于数量 = previous_over_count
            engine.重置攻击状态()
            engine.重置战斗感知()
            engine.stop_event2 = 0

    def test_monster_within_50_x_forces_group_attack(self):
        """任意可攻击怪物进入人物 X 正负 50 像素时应直接使用群攻。"""
        from v3 import legacy_engine as engine

        engine.stop_event2 = 1
        engine.stop_event = None
        engine.重置攻击状态()
        engine.重置战斗感知()
        engine.zant = 0
        try:
            close_count = engine.统计指定横向范围怪物数量(
                850,
                ((800, 500), (900, 500), (901, 500)),
                engine.CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE,
            )
            self.assertEqual(2, close_count)
            engine.处理怪物检测结果(
                1,
                1,
                0,
                0,
                automatic_group_count=1,
                close_group_count=1,
                preferred_direction="left",
            )
            self.assertEqual(3, engine.领取攻击意图(wait_seconds=0))

            engine.重置攻击状态()
            engine.zant = 0
            engine.处理怪物检测结果(
                1,
                1,
                0,
                0,
                automatic_group_count=1,
                close_group_count=0,
                preferred_direction="left",
            )
            self.assertEqual(1, engine.领取攻击意图(wait_seconds=0))
        finally:
            engine.重置攻击状态()
            engine.重置战斗感知()
            engine.stop_event2 = 0

    def test_auto_group_attack_over_count_is_saved_in_runtime_settings(self):
        """自动群攻大于数量应校验、保存、回填并注入运行引擎。"""
        from v3.config import AppConfig

        config = AppConfig(auto_group_attack_over_count=4).validate()
        self.assertEqual(4, config.auto_group_attack_over_count)
        self.assertEqual(
            4,
            AppConfig.from_dict(config.to_dict()).auto_group_attack_over_count,
        )
        with self.assertRaises(ValueError):
            AppConfig(auto_group_attack_over_count=100).validate()

        window = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        automation = (ROOT / "v3" / "automation_service.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("人物X左右150像素内识别到的怪物数量严格大于此值", window)
        self.assertIn("self.auto_group_attack_over_count.value()", window)
        self.assertIn(
            "self.auto_group_attack_over_count.valueChanged.connect(",
            window,
        )
        self.assertIn("update_auto_group_attack_over_count", automation)
        self.assertIn(
            "engine.自动群攻大于数量 = int(config.auto_group_attack_over_count)",
            automation,
        )

    def test_auto_group_attack_threshold_can_be_updated_while_running(self):
        """页面运行中修改阈值时，应同步引擎变量和当前公共快照。"""
        from v3.automation_service import AutomationService
        from v3.config import AppConfig

        class AliveWorker:
            @staticmethod
            def is_alive():
                return True

        perception_lock = threading.Lock()
        engine = SimpleNamespace(
            自动群攻大于数量=2,
            当前自动群攻大于数量=2,
            追怪感知锁=perception_lock,
        )
        service = AutomationService()
        service._worker = AliveWorker()
        service._engine = engine
        service._last_config = AppConfig(auto_group_attack_over_count=2)

        changed = service.update_auto_group_attack_over_count(4)

        self.assertTrue(changed)
        self.assertEqual(4, engine.自动群攻大于数量)
        self.assertEqual(4, engine.当前自动群攻大于数量)
        self.assertEqual(4, service.last_config.auto_group_attack_over_count)

    def test_packaged_route_falls_back_from_missing_absolute_path(self):
        """换电脑后旧绝对路线失效时，应按文件名使用安装目录中的JSON。"""
        from v3.public.route_recording import resolve_mushroom_v3_route_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packaged_route = root / "v3" / "map" / "recordings" / "木棉.json"
            packaged_route.parent.mkdir(parents=True)
            packaged_route.write_text("{}", encoding="utf-8")
            missing_old_path = root / "old_install" / "木棉.json"

            resolved = resolve_mushroom_v3_route_path(
                root,
                str(missing_old_path.resolve()),
            )

            self.assertEqual(packaged_route, resolved)

    def test_one_click_build_includes_editable_routes_and_monster_atlas(self):
        """一键打包应同时校验并复制内外两份路线JSON和怪物图鉴。"""
        spec = (ROOT / "QQ炫舞3.0.spec").read_text(encoding="utf-8")
        build = (ROOT / "tools" / "build_release.ps1").read_text(
            encoding="utf-8"
        )
        entry = (ROOT / "正式版3.0.py").read_text(encoding="utf-8")

        self.assertIn('(str(project_root / "img"), "img")', spec)
        self.assertIn('"v3" / "map" / "recordings"', spec)
        self.assertIn('$monsterLibrarySource', build)
        self.assertIn('$routeRecordingsSource', build)
        self.assertIn('"img\\monsters"', build)
        self.assertIn('"v3\\map\\recordings"', build)
        self.assertIn('"_internal\\img\\monsters"', build)
        self.assertIn('"_internal\\v3\\map\\recordings"', build)
        self.assertIn('Start-Process', build)
        self.assertIn('"内部怪物图鉴"', entry)
        self.assertIn('"外部路线JSON"', entry)

    def test_route_recording_page_has_visible_new_route_entry(self):
        """路线录制页顶部应显示完整的新建和加载入口，不能缩成难以发现的短按钮。"""
        window = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")

        self.assertIn('QPushButton("新建路线录制")', window)
        self.assertIn('QPushButton("加载路线 JSON")', window)
        self.assertIn("route_record_layout.addLayout(route_file_actions)", window)
        self.assertIn(
            'self.settings_tabs.addTab(recording_page, "路线录制")',
            window,
        )

    def test_route_recording_page_has_visible_platform_actions(self):
        """新增和删除平台应使用完整名称并单独成行，避免被平台下拉框挤掉。"""
        window = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")

        self.assertIn('QPushButton("新增平台")', window)
        self.assertIn('QPushButton("删除当前平台")', window)
        self.assertIn("platform_action_row.addWidget", window)
        self.assertIn("route_record_layout.addLayout(platform_action_row)", window)

    def test_group_attack_intent_uses_configured_f_key(self):
        """群攻意图应执行运行设置注入的群攻键 F。"""
        from v3.public import combat_strategy

        key_events = []

        class FakeInput:
            """记录群攻键的按下和释放。"""

            @staticmethod
            def keyDown(key):
                """记录按下。"""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """记录释放。"""
                key_events.append(("up", key))

        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            单体按键="a",
            群攻按键="f",
            trace_event=lambda *_args, **_kwargs: None,
            读取人物位置=lambda: (850, 500),
            可中断等待=lambda *_args, **_kwargs: True,
            标记攻击完成=lambda **_kwargs: None,
        )

        completed = combat_strategy._execute_attack_intent(
            runtime,
            combat_strategy.MushroomCombatState(),
            3,
        )

        self.assertTrue(completed)
        self.assertIn(("down", "f"), key_events)
        self.assertIn(("up", "f"), key_events)

    def test_recorded_route_attacks_from_fresh_snapshot_when_queue_is_empty(self):
        """忽略平台返程途中即使攻击队列丢失，也应按新鲜快照攻击而非原地锁死。"""
        from v3.public import combat_strategy

        key_events = []
        trace_events = []
        now = time.monotonic()

        class FakeInput:
            """记录攻击按键，验证公共战斗层实际执行了输入。"""

            @staticmethod
            def keyDown(key):
                """记录按下。"""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """记录释放。"""
                key_events.append(("up", key))

        completed = []
        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            单体按键="a",
            群攻按键="f",
            领取攻击意图=lambda **_kwargs: 0,
            读取追怪感知=lambda: (
                now,
                "right",
                1,
                1,
                80.0,
                0,
                1,
                0,
                1,
            ),
            读取人物朝向感知=lambda: (
                now,
                "right",
                "right",
                0.1,
                0.95,
                0.85,
                1.0,
                False,
                True,
            ),
            释放攻击键=lambda: None,
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda name, **details: trace_events.append(
                (name, details)
            ),
            读取人物位置=lambda: (49, 171),
            标记攻击完成=lambda **kwargs: completed.append(kwargs),
        )
        state = combat_strategy.MushroomCombatState(combat_active=True)
        intent = combat_strategy.MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction="right",
            target_age_ms=0.0,
            chase_count=1,
            attackable_count=1,
            chase_left_count=0,
            chase_right_count=1,
            attackable_left_count=0,
            attackable_right_count=1,
            nearest_dx=80.0,
            created_at=now,
        )

        attacked = combat_strategy._process_combat_frame(
            runtime,
            state,
            intent,
            (49, 171),
        )

        self.assertTrue(attacked)

    def test_health_bar_behind_character_switches_attack_side_immediately(self):
        """背后出现血条活怪时，应越过正面模板残影立即转身攻击。"""
        from v3.public import combat_strategy

        runtime = SimpleNamespace(trace_event=lambda *_args, **_kwargs: None)
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="right",
            combat_attack_count=1,
        )
        intent = combat_strategy.MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction="left",
            target_age_ms=0.0,
            chase_count=2,
            attackable_count=2,
            chase_left_count=1,
            chase_right_count=1,
            attackable_left_count=1,
            attackable_right_count=1,
            nearest_dx=40.0,
            created_at=time.monotonic(),
            health_bar_attackable_left_count=1,
            health_bar_attackable_right_count=0,
        )

        resolved = combat_strategy._resolve_attack_direction(
            runtime,
            state,
            intent,
            2,
        )

        self.assertEqual(1, resolved)
        self.assertEqual("left", state.locked_attack_direction)
        self.assertIn(("down", "a"), key_events)
        self.assertIn(("up", "a"), key_events)
        self.assertEqual(1, len(completed))
        self.assertTrue(
            any(
                name == "mushroom_v2_attack_queue_fallback"
                and details.get("resolved_direction") == 2
                for name, details in trace_events
            )
        )
        self.assertTrue(
            any(name == "attack_key" for name, _details in trace_events)
        )

    def test_attack_queue_fallback_does_not_repeat_the_same_snapshot(self):
        """已完成攻击后，同一张旧快照不得在路线循环中再次补发攻击。"""
        from v3.public import combat_strategy

        now = time.monotonic()
        runtime = SimpleNamespace(
            领取攻击意图=lambda **_kwargs: 0,
            trace_event=lambda *_args, **_kwargs: None,
        )
        state = combat_strategy.MushroomCombatState(
            combat_active=True,
            last_attack_completed_at=now + 0.01,
        )
        intent = combat_strategy.MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction="left",
            target_age_ms=0.0,
            chase_count=1,
            attackable_count=1,
            chase_left_count=1,
            chase_right_count=0,
            attackable_left_count=1,
            attackable_right_count=0,
            nearest_dx=80.0,
            created_at=now,
        )

        attacked = combat_strategy._process_combat_frame(
            runtime,
            state,
            intent,
            (49, 171),
        )

        self.assertFalse(attacked)

    def test_close_monster_uses_group_key_during_queue_fallback(self):
        """攻击队列丢失时，人物正负50像素内的怪仍应使用群攻键。"""
        from v3.public import combat_strategy

        key_events = []
        now = time.monotonic()

        class FakeInput:
            """记录近身群攻按键。"""

            @staticmethod
            def keyDown(key):
                """记录按下。"""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """记录释放。"""
                key_events.append(("up", key))

        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            单体按键="a",
            群攻按键="f",
            CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE=50.0,
            领取攻击意图=lambda **_kwargs: 0,
            读取追怪感知=lambda: (
                now,
                "left",
                1,
                1,
                35.0,
                1,
                0,
                1,
                0,
            ),
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda *_args, **_kwargs: None,
            读取人物位置=lambda: (49, 171),
            标记攻击完成=lambda **_kwargs: None,
        )
        state = combat_strategy.MushroomCombatState(combat_active=True)
        intent = combat_strategy.MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction="left",
            target_age_ms=0.0,
            chase_count=1,
            attackable_count=1,
            chase_left_count=1,
            chase_right_count=0,
            attackable_left_count=1,
            attackable_right_count=0,
            nearest_dx=35.0,
            created_at=now,
        )

        attacked = combat_strategy._process_combat_frame(
            runtime,
            state,
            intent,
            (49, 171),
        )

        self.assertTrue(attacked)
        self.assertIn(("down", "f"), key_events)
        self.assertIn(("up", "f"), key_events)

    def test_configured_monster_count_uses_group_key_during_queue_fallback(self):
        """攻击队列丢失时，公共快照仍应按页面配置的150像素怪物数量群攻。"""
        from v3.public import combat_strategy

        key_events = []
        trace_events = []
        now = time.monotonic()

        class FakeInput:
            @staticmethod
            def keyDown(key):
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                key_events.append(("up", key))

        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            单体按键="a",
            群攻按键="f",
            CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE=50.0,
            领取攻击意图=lambda **_kwargs: 0,
            读取追怪感知=lambda: (
                now,
                "right",
                3,
                1,
                80.0,
                1,
                2,
                0,
                1,
                3,
                0,
                2,
            ),
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda name, **details: trace_events.append(
                (name, details)
            ),
            读取人物位置=lambda: (49, 171),
            标记攻击完成=lambda **_kwargs: None,
        )
        state = combat_strategy.MushroomCombatState(combat_active=True)
        intent = combat_strategy.MushroomActionIntent(
            horizontal="stop",
            vertical="stop",
            action="attack",
            source="combat",
            target_direction="right",
            target_age_ms=0.0,
            chase_count=3,
            attackable_count=1,
            chase_left_count=1,
            chase_right_count=2,
            attackable_left_count=0,
            attackable_right_count=1,
            nearest_dx=80.0,
            created_at=now,
            automatic_group_count=3,
            close_group_count=0,
            configured_group_over_count=2,
        )

        attacked = combat_strategy._process_combat_frame(
            runtime,
            state,
            intent,
            (49, 171),
        )

        self.assertTrue(attacked)
        self.assertIn(("down", "f"), key_events)
        self.assertIn(("up", "f"), key_events)
        self.assertTrue(
            any(
                name == "mushroom_v2_attack_queue_fallback"
                and details.get("reason")
                == "configured_monster_count_group_attack"
                and details.get("automatic_group_count") == 3
                and details.get("configured_group_over_count") == 2
                for name, details in trace_events
            )
        )

    def test_group_attack_counts_are_published_in_public_snapshot(self):
        """模板和YOLO共用的追怪快照必须保存页面阈值及两档群攻数量。"""
        from v3 import legacy_engine as engine
        from v3.public import combat_strategy

        previous_over_count = engine.自动群攻大于数量
        engine.自动群攻大于数量 = 2
        engine.重置攻击状态()
        engine.重置战斗感知()
        try:
            detected_at = time.monotonic()
            engine.更新追怪感知(
                1,
                3,
                1,
                2,
                attackable_right_count=1,
                preferred_direction="right",
                nearest_distance=80.0,
                detected_at=detected_at,
                automatic_group_count=3,
                close_group_count=0,
                configured_group_over_count=2,
            )

            snapshot = combat_strategy._read_monster_snapshot(engine)
            intent = combat_strategy._build_action_intent(
                combat_strategy.MushroomCombatState(),
                snapshot,
            )

            self.assertEqual(3, snapshot.automatic_group_count)
            self.assertEqual(0, snapshot.close_group_count)
            self.assertEqual(2, snapshot.configured_group_over_count)
            self.assertEqual(3, intent.automatic_group_count)
            self.assertEqual(2, intent.configured_group_over_count)
        finally:
            engine.自动群攻大于数量 = previous_over_count
            engine.重置攻击状态()
            engine.重置战斗感知()

    def test_yolo_confidence_is_configurable_in_ui_and_both_runtimes(self):
        """页面配置的 YOLO 阈值应同时进入正式检测和测试预览。"""
        config = (ROOT / "v3" / "config.py").read_text(encoding="utf-8")
        window = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        service = (ROOT / "v3" / "detection_test_service.py").read_text(
            encoding="utf-8"
        )
        automation = (ROOT / "v3" / "automation_service.py").read_text(
            encoding="utf-8"
        )
        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertIn("yolo_confidence: float = 0.65", config)
        self.assertIn('form.addRow("YOLO 怪物置信度"', window)
        self.assertIn("confidence=config.yolo_confidence", service)
        self.assertIn("engine.YOLO怪物置信度 = float(config.yolo_confidence)", automation)
        self.assertIn("conf=YOLO怪物置信度", engine)

    def test_runtime_trace_records_detection_attack_and_movement_events(self):
        """运行轨迹应使用后台 JSON Lines 文件保存关键诊断事件。"""
        with tempfile.TemporaryDirectory() as directory:
            path = start_runtime_trace(Path(directory), {"map": "蘑菇"})
            trace_event("detection_frame", targets=1, nearby=1, monster_ms=12.3)
            trace_event("attack_key", direction="left")
            trace_event("movement_started", direction="right")
            stop_runtime_trace()
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        events = [record["event"] for record in records]
        self.assertEqual("session_start", events[0])
        self.assertIn("detection_frame", events)
        self.assertIn("attack_key", events)
        self.assertIn("movement_started", events)
        self.assertEqual("session_stop", events[-1])

        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        routes = (ROOT / "v3" / "map" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('"minimap_position"', engine)
        self.assertIn("25 <= r_pos[0] <= 50", routes)
        self.assertNotIn("路线 == 2\n                and 28 <= r_pos[0]", routes)

    def test_mushroom_v2_uses_dedicated_smooth_combat_and_rope_flow(self):
        """蘑菇 V2 应独立协调检测就绪、连续攻击、巡逻和爬绳。"""
        from v3.map.mushroom_v2 import (
            V2_ATTACK_RECHECK_SECONDS,
            run_mushroom_v2,
        )

        source = (ROOT / "v3" / "map" / "mushroom_v2.py").read_text(
            encoding="utf-8"
        )
        definitions = (ROOT / "v3" / "map" / "definitions.py").read_text(
            encoding="utf-8"
        )
        self.assertTrue(callable(run_mushroom_v2))
        self.assertLess(V2_ATTACK_RECHECK_SECONDS, POST_ATTACK_RECHECK_SECONDS)
        self.assertIn('MapSpec("蘑菇V2", "蘑菇地图V2", resource_name="蘑菇")', definitions)
        self.assertIn('flow="mushroom_v2"', source)
        self.assertIn("mushroom_v2_rope_attached", source)
        self.assertIn("怪物检测就绪事件", source)

    def test_attack_intent_is_atomic_expiring_and_cooldown_protected(self):
        """攻击意图应原子消费、自动过期，并在死亡动画复检期拒绝重新发布。"""
        import time

        from v3 import legacy_engine as engine

        engine.stop_event2 = 1
        engine.stop_event = None
        engine.重置攻击状态()
        try:
            self.assertTrue(engine.发布攻击意图(1))
            self.assertEqual(1, engine.领取攻击意图(wait_seconds=0))
            self.assertEqual(0, engine.领取攻击意图(wait_seconds=0))

            self.assertTrue(engine.发布攻击意图(2))
            engine.攻击意图生成时间 = time.monotonic() - ATTACK_INTENT_TTL_SECONDS - 0.01
            self.assertEqual(0, engine.领取攻击意图(wait_seconds=0))

            engine.发布攻击意图(3)
            engine.标记攻击完成()
            self.assertFalse(engine.发布攻击意图(3))
            self.assertEqual(0, engine.领取攻击意图(wait_seconds=0))
        finally:
            engine.重置攻击状态()
            engine.stop_event2 = 0

        routes = (ROOT / "v3" / "map" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn("if 攻击 ==", routes)

    def test_unreachable_legacy_pause_loops_are_removed(self):
        """旧版从未赋值的 111 系列暂停状态不应重新出现。"""
        engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*if\s+zant\s*==\s*111+", engine, re.M))

    def test_template_matching_keeps_only_local_peaks(self):
        """模板检测应先筛局部峰值，避免遍历大量相邻重复命中。"""
        source = (
            ROOT / "v3" / "public" / "monster_detection.py"
        ).read_text(encoding="utf-8")
        self.assertIn("cv2.dilate", source)
        self.assertIn("local_maxima", source)

    def test_prepared_monster_template_keeps_color_confirmation(self):
        """灰度预筛后的模板仍应返回彩色模板的准确位置和置信度。"""
        import cv2
        import numpy as np

        rng = np.random.default_rng(20260808)
        template = rng.integers(0, 256, (16, 19, 3), dtype=np.uint8)
        frame = rng.integers(0, 64, (140, 600, 3), dtype=np.uint8)
        frame[44:60, 213:232] = template
        prepared = prepare_monster_template(cv2, template)

        matches = match_monster_templates(cv2, np, frame, [prepared])

        self.assertEqual(2, prepared.gray.ndim)
        self.assertEqual(1, len(matches))
        self.assertEqual((213, 44, 232, 60), matches[0].local_box)
        self.assertGreaterEqual(matches[0].confidence, 0.999)

    def test_template_matching_accepts_moderate_animation_difference(self):
        """轻微动作和画面差异降到0.80以下时，仍应通过统一模板阈值。"""
        import cv2
        import numpy as np

        rng = np.random.default_rng(20260810)
        template = rng.integers(0, 256, (18, 22, 3), dtype=np.uint8)
        noise = rng.integers(0, 256, template.shape, dtype=np.uint8)
        changed = np.clip(
            template.astype(np.float32) * 0.55
            + noise.astype(np.float32) * 0.45,
            0,
            255,
        ).astype(np.uint8)
        frame = rng.integers(0, 48, (100, 180, 3), dtype=np.uint8)
        frame[35:53, 80:102] = changed

        matches = match_monster_templates(
            cv2,
            np,
            frame,
            [prepare_monster_template(cv2, template)],
        )

        self.assertEqual(1, len(matches))
        self.assertGreaterEqual(matches[0].confidence, MONSTER_TEMPLATE_CONFIDENCE)
        self.assertLess(matches[0].confidence, 0.80)

    def test_cross_template_matches_for_one_monster_are_deduplicated(self):
        """同一怪物的不同局部模板应合并，不能把一只怪物计成多个目标。"""
        import cv2
        import numpy as np

        rng = np.random.default_rng(20260809)
        sprite = rng.integers(0, 256, (42, 42, 3), dtype=np.uint8)
        frame = rng.integers(0, 64, (140, 600, 3), dtype=np.uint8)
        frame[50:92, 240:282] = sprite
        templates = [sprite[2:16, 2:16], sprite[24:38, 24:38]]

        matches = match_monster_templates(cv2, np, frame, templates)

        self.assertGreaterEqual(MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_X, 30)
        self.assertEqual(1, len(matches))

    def test_template_detector_reuses_last_position_after_full_miss(self):
        """人物全图重定位失败后也应复用旧ROI，避免怪物模板退回全区扫描。"""
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        detector = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "detection_thread"
        )
        detector_source = ast.get_source_segment(source, detector)
        self.assertIn("character_tracker.last_local_center is not None", detector_source)
        self.assertNotIn('startswith("local-miss")', detector_source)

    def test_health_bar_detection_links_and_infers_alive_monsters(self):
        """绿色血条应关联模板目标，并能在模板漏检时补充活怪中心。"""
        import cv2
        import numpy as np

        frame = np.zeros((80, 160, 3), dtype=np.uint8)
        bar_x, bar_y = 30, 20
        frame[bar_y, bar_x + 1:bar_x + 51] = (255, 255, 255)
        frame[bar_y + 7, bar_x + 1:bar_x + 51] = (255, 255, 255)
        frame[bar_y:bar_y + 8, bar_x + 50] = (255, 255, 255)
        frame[bar_y + 2:bar_y + 6, bar_x + 3:bar_x + 30] = (0, 243, 0)

        health_bars = detect_monster_health_bars(cv2, np, frame)
        linked = associate_monster_health_bars([(57.0, 64.0)], health_bars, 40.0)
        inferred = associate_monster_health_bars([], health_bars, 40.0)

        self.assertEqual(1, len(health_bars))
        self.assertEqual(1, linked.linked_count)
        self.assertEqual(0, linked.inferred_count)
        self.assertEqual(frozenset({0}), linked.alive_center_indices)
        self.assertEqual(0, inferred.linked_count)
        self.assertEqual(1, inferred.inferred_count)
        self.assertAlmostEqual(64.0, inferred.centers[0][1])

    def test_health_bar_detection_accepts_dim_capture_colors(self):
        """截图稍暗时，血条绿色和灰白边框仍应被识别。"""
        import cv2
        import numpy as np

        frame = np.zeros((80, 160, 3), dtype=np.uint8)
        bar_x, bar_y = 30, 20
        frame[bar_y, bar_x + 1:bar_x + 51] = (165, 165, 165)
        frame[bar_y + 7, bar_x + 1:bar_x + 51] = (165, 165, 165)
        frame[bar_y:bar_y + 8, bar_x + 50] = (165, 165, 165)
        frame[bar_y + 2:bar_y + 6, bar_x + 3:bar_x + 28] = (25, 205, 25)

        health_bars = detect_monster_health_bars(cv2, np, frame)

        self.assertEqual(1, len(health_bars))

    def test_custom_templates_always_enable_health_bar_fallback(self):
        """普通地图被自定义模板覆盖时，也必须启用血条补目标。"""
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(
            source.count("启用怪物血条检测 = bool(monster_templates)"),
            2,
        )
        self.assertGreaterEqual(source.count("if 启用怪物血条检测:"), 3)

    def test_template_detector_uses_mushroom_health_bar_pipeline(self):
        """自定义录制路线的模板线程应复用蘑菇V3血条检测和关联流程。"""
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        detector = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "detection_thread"
        )
        detector_source = ast.get_source_segment(source, detector)
        self.assertIn("detect_and_associate_monster_health_bars", detector_source)
        self.assertIn("health_bar_targets=血条目标数量", detector_source)
        self.assertGreaterEqual(
            source.count("detect_and_associate_monster_health_bars("),
            2,
        )

    def test_template_search_radius_covers_smart_chase_range(self):
        """模板ROI应覆盖攻击距离和智能追怪余量，避免后方远怪根本不进识别区。"""
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        detectors = {
            node.name: ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"detection_thread", "detection_threadyolo"}
        }
        self.assertEqual({"detection_thread", "detection_threadyolo"}, set(detectors))
        for detector_source in detectors.values():
            self.assertIn(
                "search_range + 智能追怪额外横向范围",
                detector_source,
            )
            self.assertIn(
                "horizontal_radius=模板搜索横向半径",
                detector_source,
            )

    def test_persistent_monster_rechecks_and_switches_attack_side(self):
        """连续攻击三次目标仍在时，应按最新最近怪物方向重新锁边。"""
        from v3.public import combat_strategy

        events = []
        runtime = SimpleNamespace(
            trace_event=lambda name, **details: events.append((name, details))
        )
        state = combat_strategy.MushroomCombatState(
            locked_attack_direction="left",
            combat_attack_count=3,
            last_target_side_recheck_attack_count=0,
        )
        intent = combat_strategy.MushroomActionIntent(
            horizontal="none",
            vertical="none",
            action="attack",
            source="combat",
            target_direction="right",
            target_age_ms=0.0,
            chase_count=2,
            attackable_count=2,
            chase_left_count=1,
            chase_right_count=1,
            attackable_left_count=1,
            attackable_right_count=1,
            nearest_dx=30.0,
            created_at=time.monotonic(),
        )

        resolved = combat_strategy._resolve_attack_direction(runtime, state, intent, 1)

        self.assertEqual(2, resolved)
        self.assertEqual("right", state.locked_attack_direction)
        self.assertTrue(
            any(
                name == "mushroom_v2_target_side_switched"
                and details.get("reason") == "persistent_target_recheck"
                for name, details in events
            )
        )

    def test_same_direction_attack_reasserts_facing_every_three_hits(self):
        """即使视觉朝向一致，连续三次同向攻击也应先轻点方向键再攻击。"""
        from v3.public import combat_strategy

        key_events = []

        class FakeInput:
            """记录方向键和攻击键的按下释放顺序。"""

            @staticmethod
            def keyDown(key):
                """记录按下。"""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """记录释放。"""
                key_events.append(("up", key))

        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            单体按键="x",
            群攻按键="z",
            读取人物朝向感知=lambda: (
                time.monotonic(),
                "right",
                "right",
                0.1,
                0.95,
                0.85,
                1.0,
                False,
                True,
            ),
            释放攻击键=lambda: key_events.append(("release", "attack")),
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda *_args, **_kwargs: None,
            读取人物位置=lambda: (100, 50),
            标记攻击完成=lambda **_kwargs: None,
        )
        state = combat_strategy.MushroomCombatState(
            last_attack_direction=2,
            same_direction_attack_streak=2,
        )

        completed = combat_strategy._execute_attack_intent(runtime, state, 2)

        self.assertTrue(completed)
        self.assertIn(("down", "right"), key_events)
        self.assertIn(("down", "x"), key_events)
        self.assertLess(
            key_events.index(("down", "right")),
            key_events.index(("down", "x")),
        )

    def test_recorded_route_smart_seek_prioritizes_monsters_then_resumes_route(self):
        """有怪时追击，确认无怪后立即恢复JSON路线且不覆盖原方向。"""
        from v3.public import recorded_route_player

        trace_events = []
        runtime = SimpleNamespace(
            trace_event=lambda name, **fields: trace_events.append((name, fields))
        )
        state = recorded_route_player.RecordedRouteState()
        chase_snapshot = SimpleNamespace(fresh=True, age_ms=0.0)
        chase_intent = SimpleNamespace(source="chase", target_direction="right")
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "read_monster_snapshot",
                return_value=chase_snapshot,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "build_action_intent",
                return_value=chase_intent,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "trace_action_decision",
                return_value=True,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "apply_action_intent",
            ) as apply_intent,
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear_chase,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset_chase,
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
        ):
            chase_handled = recorded_route_player._apply_smart_seek_priority(
                runtime,
                state,
                (100, 50),
            )
        self.assertTrue(chase_handled)
        apply_intent.assert_called_once()
        clear_chase.assert_not_called()
        reset_chase.assert_not_called()
        vertical.assert_not_called()

        state.smart_seek_target_active = True
        state.smart_seek_mode = "target"
        state.last_monster_direction = "right"
        state.platform_direction = "left"
        state.combat.patrol_direction = "left"
        idle_snapshot = SimpleNamespace(fresh=True, age_ms=0.0)
        idle_intent = SimpleNamespace(source="idle")
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "read_monster_snapshot",
                return_value=idle_snapshot,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "build_action_intent",
                return_value=idle_intent,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear_combat,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset_lock,
            patch.object(recorded_route_player, "_apply_horizontal") as horizontal,
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
        ):
            route_handled = recorded_route_player._apply_smart_seek_priority(
                runtime,
                state,
                (100, 50),
            )
        self.assertFalse(route_handled)
        clear_combat.assert_called_once()
        reset_lock.assert_called_once()
        horizontal.assert_not_called()
        vertical.assert_not_called()
        self.assertEqual("right", state.last_monster_direction)
        self.assertIsNone(state.loot_approach_direction)
        self.assertEqual("left", state.platform_direction)
        self.assertEqual("left", state.combat.patrol_direction)
        self.assertEqual(0.0, state.route_resume_grace_until)
        self.assertTrue(
            any(
                name == "recorded_route_combat_route_resumed"
                and fields.get("action") == "resume_json_route_after_no_target"
                and fields.get("route_grace_ms") == 0.0
                for name, fields in trace_events
            )
        )

    def test_post_combat_loot_approach_prefers_last_monster_direction(self):
        """恢复巡逻时应先在安全平台范围内朝怪物死亡方向移动拾取。"""
        from v3.public import recorded_route_player

        state = recorded_route_player.RecordedRouteState(
            loot_approach_direction="right",
            loot_approach_until=time.monotonic() + 1.0,
        )
        variant = SimpleNamespace(
            platform_patrol=True,
            platform_min_x=10,
            platform_max_x=100,
            platform_ranges=(),
        )
        runtime = SimpleNamespace(trace_event=lambda *_args, **_kwargs: None)
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear_combat,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset_lock,
            patch.object(recorded_route_player, "_apply_horizontal") as horizontal,
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
        ):
            handled = recorded_route_player._apply_post_combat_loot_approach(
                runtime,
                state,
                variant,
                (50, 100),
            )
        self.assertTrue(handled)
        clear_combat.assert_called_once()
        reset_lock.assert_called_once()
        horizontal.assert_called_once_with(runtime, state, "right")
        vertical.assert_called_once_with(runtime, state, "none")

        state.loot_approach_direction = "right"
        state.loot_approach_until = time.monotonic() + 1.0
        blocked = recorded_route_player._apply_post_combat_loot_approach(
            runtime,
            state,
            variant,
            (99, 100),
        )
        self.assertFalse(blocked)
        self.assertIsNone(state.loot_approach_direction)

    def test_existing_route_can_store_and_reload_recorded_rest_point(self):
        """已有JSON追加休息点时应保留原路线，并自动推断下方起跳平台Y。"""
        from v3.public.route_recording import (
            RouteRecorderService,
            read_route_rest_point,
            save_route_rest_point,
        )

        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "已有路线.json"
            original_points = [
                {
                    "x": 60,
                    "y": 145,
                    "command": "right none none",
                    "segment_type": "platform",
                },
                {
                    "x": 90,
                    "y": 145,
                    "command": "left none none",
                    "segment_type": "platform",
                },
            ]
            route_path.write_text(
                json.dumps({"points": original_points}, ensure_ascii=False),
                encoding="utf-8",
            )

            saved = save_route_rest_point(
                route_path,
                (80, 120),
                interval_minutes=12.5,
                duration_minutes=3.0,
            )
            loaded = read_route_rest_point(route_path)
            route_data = json.loads(route_path.read_text(encoding="utf-8"))

            self.assertEqual(80, saved["x"])
            self.assertEqual(120, saved["y"])
            self.assertEqual(145, saved["approach_y"])
            self.assertEqual(12.5, saved["interval_minutes"])
            self.assertEqual(3.0, saved["duration_minutes"])
            self.assertEqual(saved, loaded)
            self.assertEqual(original_points, route_data["points"])

            recorder = RouteRecorderService(Path(directory))
            recorder.set_route_path(route_path)
            self.assertEqual(saved, recorder._rest_point)

            from v3.public import recorded_route_player

            runtime = SimpleNamespace(
                PROJECT_ROOT=directory,
                自定义录制路线文件=str(route_path),
                get_base_dir=lambda: directory,
            )
            plan = recorded_route_player._load_recorded_route(runtime)
            self.assertIsNotNone(plan.rest_point)
            self.assertEqual(80, plan.rest_point.x)
            self.assertEqual(120, plan.rest_point.y)
            self.assertEqual(145, plan.rest_point.approach_y)
            self.assertEqual(12.5, plan.rest_point.interval_minutes)
            self.assertEqual(3.0, plan.rest_point.duration_minutes)

            state = recorded_route_player.RecordedRouteState(
                recorded_rest_interval_seconds=12.5 * 60.0,
                recorded_rest_duration_seconds=3.0 * 60.0,
            )
            settings = recorded_route_player._rope_rest_settings(
                SimpleNamespace(
                    绳子休息间隔分钟=99.0,
                    绳子休息时长分钟=88.0,
                ),
                state,
            )
            self.assertEqual((750.0, 180.0), settings)

    def test_recorded_rest_point_walks_to_x_jumps_and_starts_rest(self):
        """定时休息应等待对应平台、对齐X、跳跃并在录制X/Y处开始休息。"""
        from v3.public import recorded_route_player

        key_events = []

        class FakeInput:
            """记录休息点跳跃键。"""

            @staticmethod
            def keyDown(key):
                """记录按下。"""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """记录释放。"""
                key_events.append(("up", key))

        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            绳子休息间隔分钟=20.0,
            绳子休息时长分钟=1.0,
            zant=0,
            清除攻击意图=lambda: None,
            释放攻击键=lambda: None,
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda *_args, **_kwargs: None,
            人物全图重定位事件=threading.Event(),
        )
        state = recorded_route_player.RecordedRouteState(
            rope_rest_pending=True,
            rope_rest_remaining_seconds=60.0,
            recorded_rest_point_enabled=True,
        )
        rest_point = recorded_route_player.RecordedRestPoint(
            x=80,
            y=120,
            approach_y=145,
        )
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ),
            patch.object(recorded_route_player, "_apply_horizontal") as horizontal,
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_open_button",
                return_value=True,
            ),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_shop_open_button",
                return_value=True,
            ),
        ):
            wrong_platform = recorded_route_player._apply_recorded_rest_point(
                runtime,
                state,
                rest_point,
                (70, 170),
            )
            self.assertFalse(wrong_platform)

            approaching = recorded_route_player._apply_recorded_rest_point(
                runtime,
                state,
                rest_point,
                (70, 145),
            )
            self.assertTrue(approaching)
            horizontal.assert_called_with(runtime, state, "right")

            jumped = recorded_route_player._apply_recorded_rest_point(
                runtime,
                state,
                rest_point,
                # 持续移动可能直接跨过精确 X；只要仍在有效落点窗口内就应
                # 立即释放水平键并起跳，不能在休息点下方左右折返。
                (76, 145),
            )
            self.assertTrue(jumped)
            self.assertEqual("jumping", state.recorded_rest_phase)
            self.assertIn(("down", "c"), key_events)
            self.assertIn(("up", "c"), key_events)

            resting = recorded_route_player._apply_recorded_rest_point(
                runtime,
                state,
                rest_point,
                (84, 120),
            )
            self.assertTrue(resting)
            self.assertEqual("resting", state.recorded_rest_phase)
            self.assertGreater(state.rope_rest_until, time.monotonic())

            state.rope_rest_until = time.monotonic() - 0.01
            completed = recorded_route_player._apply_recorded_rest_point(
                runtime,
                state,
                rest_point,
                (84, 120),
            )
            self.assertTrue(completed)
            self.assertFalse(state.rope_rest_pending)
            self.assertIsNone(state.recorded_rest_phase)
            self.assertTrue(runtime.人物全图重定位事件.is_set())
        vertical.assert_called()

    def test_rest_entry_skips_bell_when_maple_is_already_visible(self):
        """The visible maple icon should be clicked directly without touching the bell."""
        from v3.public import recorded_route_player

        state = recorded_route_player.RecordedRouteState(
            rope_rest_pending=True,
            recorded_rest_interval_seconds=1200.0,
            recorded_rest_duration_seconds=60.0,
        )
        rest_point = recorded_route_player.RecordedRestPoint(x=80, y=120)
        runtime = SimpleNamespace(
            trace_event=lambda *_args, **_kwargs: None,
            recorded_rest_arrival_callback=None,
        )
        with (
            patch.object(recorded_route_player, "_release_rope_rest_keys"),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_open_button",
                return_value=True,
            ) as maple,
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_bell_button",
            ) as bell,
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_shop_open_button",
                return_value=True,
            ) as shop,
            patch.object(recorded_route_player, "_notify_rope_rest_schedule"),
        ):
            handled = recorded_route_player._begin_recorded_rest(
                runtime,
                state,
                rest_point,
                (80, 120),
                time.monotonic(),
            )

        self.assertTrue(handled)
        maple.assert_called_once()
        bell.assert_not_called()
        shop.assert_called_once()
        self.assertTrue(state.recorded_rest_member_panel_opened)
        self.assertEqual("resting", state.recorded_rest_phase)

    def test_maple_icon_click_uses_the_calibrated_screen_coordinate(self):
        """Maple verification should click the exact center calibrated from the full screenshot."""
        import cv2
        import numpy as np

        from v3.public import recorded_route_player

        recorded_route_player._recorded_rest_open_button_template = None
        template = recorded_route_player._load_recorded_rest_open_button_template(
            SimpleNamespace(cv2=cv2, np=np)
        )
        frame = np.zeros((800, 1368, 3), dtype=np.uint8)
        center_x, center_y = recorded_route_player.RECORDED_REST_MAPLE_BUTTON_POSITION
        height, width = template.shape[:2]
        left = center_x - width // 2
        top = center_y - height // 2
        frame[top:top + height, left:left + width] = template
        clicks = []
        runtime = SimpleNamespace(
            cv2=cv2,
            np=np,
            游戏窗口外框宽度=1368,
            游戏窗口外框高度=800,
            grab_screen=lambda _area: frame,
            可中断等待=lambda *_args, **_kwargs: True,
            pydirectinput=SimpleNamespace(
                click=lambda x, y: clicks.append((x, y)),
            ),
            trace_event=lambda *_args, **_kwargs: None,
        )

        clicked = recorded_route_player._click_recorded_rest_open_button(
            runtime,
            recorded_route_player.RecordedRouteState(),
            (80, 120),
        )

        self.assertTrue(clicked)
        self.assertEqual([(154, 447)], clicks)

    def test_rest_entry_uses_bell_then_maple_then_shop(self):
        """When maple is absent, rest entry should click bell, retry maple, then open shop."""
        from v3.public import recorded_route_player

        state = recorded_route_player.RecordedRouteState(
            rope_rest_pending=True,
            recorded_rest_interval_seconds=1200.0,
            recorded_rest_duration_seconds=60.0,
        )
        rest_point = recorded_route_player.RecordedRestPoint(x=80, y=120)
        runtime = SimpleNamespace(
            trace_event=lambda *_args, **_kwargs: None,
            recorded_rest_arrival_callback=None,
        )
        calls = []

        def click_maple(*_args):
            """Record both maple searches around the bell click."""
            calls.append("maple")
            return len(calls) > 2

        def click_bell(*_args):
            """Record the bell click between the two maple searches."""
            calls.append("bell")
            return True

        def click_shop(*_args):
            """Record the final shop-open click."""
            calls.append("shop")
            return True

        with (
            patch.object(recorded_route_player, "_release_rope_rest_keys"),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_open_button",
                side_effect=click_maple,
            ),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_bell_button",
                side_effect=click_bell,
            ),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_shop_open_button",
                side_effect=click_shop,
            ),
            patch.object(recorded_route_player, "_notify_rope_rest_schedule"),
        ):
            handled = recorded_route_player._begin_recorded_rest(
                runtime,
                state,
                rest_point,
                (80, 120),
                time.monotonic(),
            )

        self.assertTrue(handled)
        self.assertEqual(["maple", "bell", "maple", "shop"], calls)
        self.assertEqual("resting", state.recorded_rest_phase)

    def test_manual_rest_test_presses_escape_and_stays_parked(self):
        """A successful manual test should close the UI with Esc and never resume the route."""
        from v3.public import recorded_route_player

        key_events = []

        class FakeInput:
            """Record the Esc press used after the successful test."""

            @staticmethod
            def keyDown(key):
                """Record key-down events."""
                key_events.append(("down", key))

            @staticmethod
            def keyUp(key):
                """Record key-up events."""
                key_events.append(("up", key))

        statuses = []
        state = recorded_route_player.RecordedRouteState(
            rope_rest_pending=True,
            rope_rest_test_active=True,
            recorded_rest_on_rope=True,
            recorded_rest_interval_seconds=1200.0,
            recorded_rest_duration_seconds=60.0,
        )
        rest_point = recorded_route_player.RecordedRestPoint(x=80, y=120)
        runtime = SimpleNamespace(
            pydirectinput=FakeInput(),
            可中断等待=lambda *_args, **_kwargs: True,
            trace_event=lambda *_args, **_kwargs: None,
            recorded_rest_arrival_callback=None,
        )
        with (
            patch.object(recorded_route_player, "_release_rope_rest_keys"),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_open_button",
                return_value=True,
            ),
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_bell_button",
            ) as bell,
            patch.object(
                recorded_route_player,
                "_click_recorded_rest_shop_open_button",
                return_value=True,
            ),
            patch.object(
                recorded_route_player,
                "_notify_rope_rest_schedule",
                side_effect=lambda _runtime, _state, status, _remaining=0: statuses.append(status),
            ),
        ):
            handled = recorded_route_player._begin_recorded_rest(
                runtime,
                state,
                rest_point,
                (80, 120),
                time.monotonic(),
            )

        self.assertTrue(handled)
        bell.assert_not_called()
        self.assertIn(("down", "esc"), key_events)
        self.assertIn(("up", "esc"), key_events)
        self.assertTrue(state.rest_point_test_parked)
        self.assertTrue(state.rope_rest_test_active)
        self.assertFalse(state.rope_rest_pending)
        self.assertFalse(state.rope_resting)
        self.assertEqual("test_parked", state.recorded_rest_phase)
        self.assertEqual("test_parked", statuses[-1])

    def test_missing_rest_point_uses_first_valid_rope_middle(self):
        """A route without an explicit rest point should select one stable rope midpoint."""
        from v3.public import recorded_route_player

        def point(rope_x, top_y, bottom_y):
            """Build one rope entry for fallback selection."""
            return recorded_route_player.RecordedRoutePoint(
                x=int(rope_x or 0),
                y=160,
                horizontal="right",
                vertical="up",
                action="jump",
                kind="jump",
                segment_type="rope_entry",
                rope_x=rope_x,
                rope_top_y=top_y,
                rope_bottom_y=bottom_y,
            )

        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=[
                point(40, 90, 90),
                point(80, 140, 60),
                point(120, 40, 160),
            ],
            closed_loop=True,
            platform_patrol=False,
        )
        rest_point, geometry = (
            recorded_route_player._derive_rope_middle_rest_point([variant])
        )

        self.assertIsNotNone(rest_point)
        self.assertEqual((80, 100), (rest_point.x, rest_point.y))
        self.assertIsNone(rest_point.approach_y)
        self.assertEqual((80, 60, 140), geometry)

        invalid_variant = recorded_route_player.RecordedRouteVariant(
            name="invalid",
            probability=100,
            points=[point(40, None, 100), point(50, 70, 70)],
            closed_loop=False,
            platform_patrol=False,
        )
        self.assertEqual(
            (None, None),
            recorded_route_player._derive_rope_middle_rest_point(
                [invalid_variant]
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "rope-fallback.json"
            route_path.write_text(
                json.dumps(
                    {
                        "points": [
                            {
                                "x": 78,
                                "y": 145,
                                "command": "right up jump",
                                "segment_type": "rope_entry",
                                "rope_x": 80,
                                "rope_top_y": 140,
                                "rope_bottom_y": 60,
                            },
                            {
                                "x": 80,
                                "y": 100,
                                "command": "none up none",
                                "segment_type": "rope",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            runtime = SimpleNamespace(
                PROJECT_ROOT=directory,
                自定义录制路线文件=str(route_path),
                get_base_dir=lambda: directory,
            )
            plan = recorded_route_player._load_recorded_route(runtime)

        self.assertIsNone(plan.rest_point)
        self.assertEqual(
            (80, 100),
            (plan.fallback_rest_point.x, plan.fallback_rest_point.y),
        )
        self.assertEqual((80, 60, 140), plan.fallback_rest_rope_geometry)

    def test_explicit_rest_point_wins_over_rope_middle_fallback(self):
        """Loading a route must not replace an explicit rest point with a rope midpoint."""
        from v3.public import recorded_route_player

        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "explicit-rest.json"
            route_path.write_text(
                json.dumps(
                    {
                        "rest_point": {"x": 25, "y": 35, "approach_y": 80},
                        "points": [
                            {
                                "x": 78,
                                "y": 145,
                                "command": "right up jump",
                                "segment_type": "rope_entry",
                                "rope_x": 80,
                                "rope_top_y": 60,
                                "rope_bottom_y": 140,
                            },
                            {
                                "x": 80,
                                "y": 120,
                                "command": "none up none",
                                "segment_type": "rope",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runtime = SimpleNamespace(
                PROJECT_ROOT=directory,
                自定义录制路线文件=str(route_path),
                get_base_dir=lambda: directory,
            )

            plan = recorded_route_player._load_recorded_route(runtime)

        self.assertEqual((25, 35), (plan.rest_point.x, plan.rest_point.y))
        self.assertIsNone(plan.fallback_rest_point)
        self.assertIsNone(plan.fallback_rest_rope_geometry)

    def test_rope_middle_fallback_climbs_rests_and_resumes(self):
        """The fallback should climb to its selected midpoint without using platform jump flow."""
        from v3.public import recorded_route_player

        runtime = SimpleNamespace(
            绳子休息间隔分钟=20.0,
            绳子休息时长分钟=1.0,
            recorded_rest_schedule_callback=None,
            trace_event=lambda *_args, **_kwargs: None,
        )
        state = recorded_route_player.RecordedRouteState(
            rope_rest_pending=True,
            rope_rest_remaining_seconds=30.0,
            rope_rest_fallback_enabled=True,
            recorded_rest_point=recorded_route_player.RecordedRestPoint(
                x=80,
                y=100,
            ),
            recorded_rest_rope_x=80,
            recorded_rest_rope_top_y=60,
            recorded_rest_rope_bottom_y=140,
            active_rope_direction="right",
            active_rope_x=80,
            active_rope_top_y=60,
            active_rope_bottom_y=140,
        )

        with (
            patch.object(recorded_route_player, "_apply_horizontal") as horizontal,
            patch.object(recorded_route_player, "_apply_vertical") as vertical,
            patch.object(recorded_route_player, "_release_rope_rest_keys") as release,
        ):
            wrong_rope_state = recorded_route_player.replace(
                state,
                active_rope_x=120,
            )
            self.assertFalse(
                recorded_route_player._start_rope_rest(
                    runtime,
                    wrong_rope_state,
                    (120, 100),
                )
            )

            self.assertTrue(
                recorded_route_player._start_rope_rest(runtime, state, (80, 130))
            )
            vertical.assert_called_with(runtime, state, "up")
            self.assertFalse(state.rope_resting)

            vertical.reset_mock()
            self.assertTrue(
                recorded_route_player._start_rope_rest(runtime, state, (80, 90))
            )
            vertical.assert_called_with(runtime, state, "down")
            self.assertFalse(state.rope_resting)

            vertical.reset_mock()
            self.assertTrue(
                recorded_route_player._start_rope_rest(runtime, state, (80, 100))
            )
            self.assertTrue(state.rope_resting)
            self.assertEqual(100, state.rope_rest_target_y)
            release.assert_called()

            state.rope_rest_until = time.monotonic() - 0.01
            self.assertTrue(
                recorded_route_player._apply_rope_rest(runtime, state, (80, 100))
            )
            self.assertFalse(state.rope_rest_pending)
            self.assertFalse(state.rope_resting)
            self.assertIsNone(state.rope_rest_target_y)
            vertical.assert_called_with(runtime, state, "up")

    def test_ignored_platform_numbers_accept_slash_separated_multiple_values(self):
        """Runtime settings should normalize slash-separated platform numbers."""
        from v3.config import AppConfig

        config = AppConfig(ignored_platform_numbers="3/1/2/2").validate()
        self.assertEqual((1, 2, 3), config.ignored_platform_numbers)

        restored = AppConfig.from_dict(
            {"ignored_platform_numbers": [5, 2, 5]}
        )
        self.assertEqual((2, 5), restored.ignored_platform_numbers)

        with self.assertRaises(ValueError):
            AppConfig(ignored_platform_numbers="1/a/3").validate()

    def test_ignored_platform_follows_transition_without_combat_or_sweeping(self):
        """An ignored platform should use its next JSON transition immediately."""
        from v3.public import recorded_route_player

        def point(x, platform_id=None, segment_type="platform", action="none"):
            """Build a small route point with an optional platform label."""
            return recorded_route_player.RecordedRoutePoint(
                x=x,
                y=100,
                horizontal="right",
                vertical="none",
                action=action,
                kind="walk",
                segment_type=segment_type,
                platform_id=platform_id,
            )

        points = [
            point(10, "平台1"),
            point(20, "平台1"),
            point(30, segment_type="walk_off_right"),
            point(40, "平台2"),
            point(50, "平台2"),
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=points,
            closed_loop=False,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        self.assertEqual(
            (1, 2),
            tuple(item.platform_number for item in variant.platform_ranges),
        )

        events = []
        cleared = []
        released = []
        runtime = SimpleNamespace(
            zant=2,
            trace_event=lambda name, **details: events.append((name, details)),
            清除攻击意图=lambda: cleared.append(True),
            释放攻击键=lambda: released.append(True),
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1, 3),
        )
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear_combat,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ) as reset_lock,
            patch.object(recorded_route_player, "_apply_recorded_command") as command,
        ):
            handled = recorded_route_player._apply_ignored_platform_route(
                runtime,
                state,
                variant,
                (15, 100),
                preferred_route_index=0,
            )

            self.assertTrue(handled)
            self.assertEqual(0, runtime.zant)
            self.assertEqual(1, state.active_ignored_platform_number)
            self.assertEqual(2, state.ignored_platform_exit_index)
            command.assert_called_once_with(runtime, state, variant, (15, 100), 2)
            clear_combat.assert_called_once()
            reset_lock.assert_called_once()
            self.assertTrue(cleared)
            self.assertTrue(released)
            self.assertEqual("recorded_route_platform_ignored", events[0][0])

            command.reset_mock()
            not_ignored = recorded_route_player._apply_ignored_platform_route(
                runtime,
                state,
                variant,
                (45, 100),
                preferred_route_index=3,
            )
            self.assertFalse(not_ignored)
            command.assert_not_called()

    def test_off_route_platform_replans_through_intermediate_platform_to_target(self):
        """只刷平台3时，掉到平台1应规划平台1→平台2→平台3并保持原始目标。"""
        from v3.public import recorded_route_player

        def point(x, y, platform_id=None, segment_type="platform"):
            """构造带独立高度的平台和连接点。"""
            return recorded_route_player.RecordedRoutePoint(
                x=x,
                y=y,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type=segment_type,
                platform_id=platform_id,
            )

        points = [
            point(10, 140, "平台1"),
            point(20, 140, "平台1"),
            point(30, 130, segment_type="walk_off_right"),
            point(40, 110, "平台2"),
            point(50, 110, "平台2"),
            point(60, 100, segment_type="walk_off_right"),
            point(70, 80, "平台3"),
            point(80, 80, "平台3"),
            point(90, 100, segment_type="walk_off_right"),
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        events = []
        runtime = SimpleNamespace(
            zant=1,
            trace_event=lambda name, **details: events.append((name, details)),
            清除攻击意图=lambda: None,
            释放攻击键=lambda: None,
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1, 2),
            single_active_platform_number=3,
            single_active_platform_patrol_enabled=True,
        )
        with (
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ),
            patch.object(recorded_route_player, "_apply_recorded_command") as command,
        ):
            self.assertTrue(
                recorded_route_player._apply_ignored_platform_route(
                    runtime,
                    state,
                    variant,
                    (15, 140),
                    preferred_route_index=0,
                )
            )
            self.assertTrue(state.platform_replan_active)
            self.assertEqual(1, state.platform_replan_source_number)
            self.assertEqual(3, state.platform_replan_target_number)
            self.assertEqual(2, state.platform_replan_exit_index)
            command.assert_called_with(runtime, state, variant, (15, 140), 2)

            command.reset_mock()
            self.assertTrue(
                recorded_route_player._apply_ignored_platform_route(
                    runtime,
                    state,
                    variant,
                    (45, 110),
                    preferred_route_index=3,
                )
            )
            self.assertEqual(1, state.platform_replan_source_number)
            self.assertEqual(3, state.platform_replan_target_number)
            self.assertEqual(5, state.platform_replan_exit_index)
            command.assert_called_with(runtime, state, variant, (45, 110), 5)

            command.reset_mock()
            self.assertFalse(
                recorded_route_player._apply_ignored_platform_route(
                    runtime,
                    state,
                    variant,
                    (75, 80),
                    preferred_route_index=6,
                )
            )
            command.assert_not_called()

        self.assertFalse(state.platform_replan_active)
        self.assertEqual(6, state.route_index)
        self.assertTrue(
            any(
                name == "recorded_route_platform_replanned"
                and details.get("current_platform_number") == 1
                and details.get("target_platform_number") == 3
                for name, details in events
            )
        )
        self.assertTrue(
            any(
                name == "recorded_route_platform_replan_progress"
                and details.get("current_platform_number") == 2
                for name, details in events
            )
        )
        self.assertTrue(
            any(
                name == "recorded_route_platform_replan_completed"
                and details.get("arrived_platform_number") == 3
                for name, details in events
            )
        )

    def test_platform_replan_keeps_route_control_between_platforms(self):
        """返程经过不属于任何平台的连接段时，应继续JSON而不是交给战斗巡逻。"""
        from v3.public import recorded_route_player

        point = recorded_route_player.RecordedRoutePoint(
            x=30,
            y=120,
            horizontal="right",
            vertical="none",
            action="none",
            kind="walk",
            segment_type="walk_off_right",
        )
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=[point],
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=(
                recorded_route_player.RecordedPlatformRange(
                    start_index=0,
                    end_index=0,
                    minimum_x=10,
                    maximum_x=20,
                    minimum_y=140,
                    maximum_y=140,
                    start_x=10,
                    platform_number=1,
                ),
                recorded_route_player.RecordedPlatformRange(
                    start_index=0,
                    end_index=0,
                    minimum_x=70,
                    maximum_x=80,
                    minimum_y=80,
                    maximum_y=80,
                    start_x=70,
                    platform_number=3,
                ),
            ),
        )
        runtime = SimpleNamespace(trace_event=lambda *_args, **_kwargs: None)
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1, 2),
            platform_replan_active=True,
            platform_replan_source_number=1,
            platform_replan_target_number=3,
            platform_replan_exit_index=0,
            route_index=0,
        )
        with (
            patch.object(
                recorded_route_player,
                "_current_platform_range_index",
                return_value=None,
            ),
            patch.object(
                recorded_route_player,
                "_select_route_index",
                return_value=0,
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ) as clear_combat,
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ),
            patch.object(recorded_route_player, "_apply_recorded_command") as command,
        ):
            handled = recorded_route_player._apply_ignored_platform_route(
                runtime,
                state,
                variant,
                (35, 120),
                preferred_route_index=0,
            )

        self.assertTrue(handled)
        clear_combat.assert_called_once()
        command.assert_called_once_with(runtime, state, variant, (35, 120), 0)
        self.assertTrue(state.platform_replan_active)

    def test_single_platform_left_after_filter_stays_on_local_patrol(self):
        """过滤后只剩一个平台时，正常刷图应固定该平台而不是轮换。"""
        from v3.public import recorded_route_player

        def point(x, platform_id=None, segment_type="platform"):
            return recorded_route_player.RecordedRoutePoint(
                x=x,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type=segment_type,
                platform_id=platform_id,
            )

        points = [
            point(10, "平台1"),
            point(20, "平台1"),
            point(30, segment_type="walk_off_right"),
            point(40, "平台2"),
            point(60, "平台2"),
            point(70, segment_type="walk_off_right"),
            point(80, "平台3"),
            point(90, "平台3"),
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        selection = recorded_route_player._single_unignored_platform_patrol_variant(
            variant,
            (1, 3),
        )
        self.assertIsNotNone(selection)
        active_number, patrol_variant = selection
        self.assertEqual(2, active_number)
        self.assertTrue(patrol_variant.platform_patrol)
        self.assertEqual(40, patrol_variant.platform_min_x)
        self.assertEqual(60, patrol_variant.platform_max_x)

        runtime = SimpleNamespace()
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1, 3),
            route_index=3,
        )
        with (
            patch.object(recorded_route_player, "_apply_platform_patrol") as patrol,
            patch.object(recorded_route_player, "_maybe_apply_platform_ai_action") as ai,
        ):
            handled = recorded_route_player._apply_single_unignored_platform_patrol(
                runtime,
                state,
                variant,
                (50, 100),
            )
        self.assertTrue(handled)
        self.assertTrue(state.single_active_platform_patrol_enabled)
        self.assertEqual(2, state.single_active_platform_number)
        patrol.assert_called_once()
        ai.assert_called_once()

    def test_single_filtered_platform_only_leaves_for_rest_route(self):
        """休息点到期时应暂停固定巡逻，让JSON路线可以前往其他平台。"""
        from v3.public import recorded_route_player

        points = [
            recorded_route_player.RecordedRoutePoint(
                x=x,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="platform",
                platform_id=platform_id,
            )
            for x, platform_id in (
                (10, "平台1"),
                (20, "平台1"),
                (40, "平台2"),
                (60, "平台2"),
            )
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1,),
            route_index=2,
            rope_rest_pending=True,
        )
        with patch.object(recorded_route_player, "_apply_platform_patrol") as patrol:
            handled = recorded_route_player._apply_single_unignored_platform_patrol(
                SimpleNamespace(),
                state,
                variant,
                (50, 100),
            )
        self.assertFalse(handled)
        patrol.assert_not_called()

    def test_falling_to_filtered_platform_routes_back_to_single_active_platform(self):
        """意外掉到过滤平台时应沿连接继续走，直到返回唯一有效平台。"""
        from v3.public import recorded_route_player

        points = [
            recorded_route_player.RecordedRoutePoint(
                x=10,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="platform",
                platform_id="平台1",
            ),
            recorded_route_player.RecordedRoutePoint(
                x=20,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="platform",
                platform_id="平台1",
            ),
            recorded_route_player.RecordedRoutePoint(
                x=30,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="walk_off_right",
            ),
            recorded_route_player.RecordedRoutePoint(
                x=40,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="platform",
                platform_id="平台2",
            ),
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="main",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        events = []
        runtime = SimpleNamespace(
            zant=0,
            trace_event=lambda name, **details: events.append((name, details)),
            清除攻击意图=lambda: None,
            释放攻击键=lambda: None,
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1,),
            single_active_platform_number=2,
            single_active_platform_patrol_enabled=True,
        )
        with (
            patch.object(recorded_route_player.combat_logic, "clear_combat_for_movement"),
            patch.object(recorded_route_player.combat_logic, "reset_attack_direction_lock"),
            patch.object(recorded_route_player, "_apply_recorded_command") as command,
        ):
            handled = recorded_route_player._apply_ignored_platform_route(
                runtime,
                state,
                variant,
                (15, 100),
                preferred_route_index=0,
            )
        self.assertTrue(handled)
        command.assert_called_once()
        self.assertEqual(
            "return_to_single_active_platform",
            events[0][1]["action"],
        )
        self.assertEqual(2, events[0][1]["single_active_platform_number"])

    def test_single_platform_return_watchdog_reasserts_stale_move_key(self):
        """返程方向状态未变但实际按键丢失时，应强制重新发送移动键。"""
        from v3.public import recorded_route_player

        points = [
            recorded_route_player.RecordedRoutePoint(
                x=10,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="platform",
                platform_id="平台1",
            ),
            recorded_route_player.RecordedRoutePoint(
                x=30,
                y=100,
                horizontal="right",
                vertical="none",
                action="none",
                kind="walk",
                segment_type="walk_off_right",
            ),
        ]
        variant = recorded_route_player.RecordedRouteVariant(
            name="return",
            probability=100,
            points=points,
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges(points),
        )
        events = []
        key_events = []
        move_events = []
        relocations = []
        pydirectinput = SimpleNamespace(
            keyDown=lambda key: key_events.append(("down", key)),
            keyUp=lambda key: key_events.append(("up", key)),
        )
        runtime = SimpleNamespace(
            zant=2,
            trace_event=lambda name, **details: events.append((name, details)),
            清除攻击意图=lambda: None,
            释放攻击键=lambda: None,
            释放水平移动键=lambda reason=None: move_events.append(("release", reason)),
            切换持续移动=lambda direction, reason=None: move_events.append(
                ("move", direction, reason)
            ),
            请求人物重新定位=lambda reason=None: relocations.append(reason),
            可中断等待=lambda *_args, **_kwargs: True,
            pydirectinput=pydirectinput,
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1,),
            single_active_platform_number=2,
            single_active_platform_patrol_enabled=True,
        )

        self.assertFalse(
            recorded_route_player._apply_single_platform_return_watchdog(
                runtime, state, variant, (15, 100), 1, 1
            )
        )
        state.single_platform_return_progress_at = (
            time.monotonic()
            - recorded_route_player.ROUTE_SINGLE_PLATFORM_RETURN_STALL_SECONDS
            - 0.1
        )
        with (
            patch.object(recorded_route_player.combat_logic, "clear_combat_for_movement"),
            patch.object(recorded_route_player.combat_logic, "reset_attack_direction_lock"),
        ):
            handled = recorded_route_player._apply_single_platform_return_watchdog(
                runtime, state, variant, (15, 100), 1, 1
            )

        self.assertTrue(handled)
        self.assertEqual(0, runtime.zant)
        self.assertIn(("move", "right", "single_platform_return_watchdog"), move_events)
        self.assertFalse(any(key == "c" for _action, key in key_events))
        self.assertEqual(
            "reassert_return_direction",
            events[-1][1]["action"],
        )

    def test_single_platform_return_watchdog_jumps_then_relocalizes(self):
        """重新发送移动键仍无位移时，第二级跳跃，第三极刷新定位。"""
        from v3.public import recorded_route_player

        point = recorded_route_player.RecordedRoutePoint(
            x=30,
            y=100,
            horizontal="right",
            vertical="none",
            action="none",
            kind="walk",
            segment_type="walk_off_right",
            platform_id="平台1",
        )
        variant = recorded_route_player.RecordedRouteVariant(
            name="return",
            probability=100,
            points=[point],
            closed_loop=True,
            platform_patrol=False,
            platform_ranges=recorded_route_player._recorded_platform_ranges([point]),
        )
        key_events = []
        relocations = []
        runtime = SimpleNamespace(
            zant=0,
            trace_event=lambda *_args, **_kwargs: None,
            清除攻击意图=lambda: None,
            释放攻击键=lambda: None,
            释放水平移动键=lambda reason=None: None,
            切换持续移动=lambda direction, reason=None: None,
            请求人物重新定位=lambda reason=None: relocations.append(reason),
            可中断等待=lambda *_args, **_kwargs: True,
            pydirectinput=SimpleNamespace(
                keyDown=lambda key: key_events.append(("down", key)),
                keyUp=lambda key: key_events.append(("up", key)),
            ),
        )
        state = recorded_route_player.RecordedRouteState(
            single_active_platform_number=2,
            single_active_platform_patrol_enabled=True,
            single_platform_return_watch_platform_number=1,
            single_platform_return_watch_target_number=2,
            single_platform_return_watch_x=15,
            single_platform_return_watch_y=100,
            single_platform_return_recovery_stage=1,
        )
        with (
            patch.object(recorded_route_player.combat_logic, "clear_combat_for_movement"),
            patch.object(recorded_route_player.combat_logic, "reset_attack_direction_lock"),
        ):
            for expected_stage in (2, 3):
                state.single_platform_return_progress_at = (
                    time.monotonic()
                    - recorded_route_player.ROUTE_SINGLE_PLATFORM_RETURN_STALL_SECONDS
                    - 0.1
                )
                self.assertTrue(
                    recorded_route_player._apply_single_platform_return_watchdog(
                        runtime, state, variant, (15, 100), 0, 1
                    )
                )
                self.assertEqual(expected_stage, state.single_platform_return_recovery_stage)

        self.assertGreaterEqual(key_events.count(("down", "c")), 2)
        self.assertGreaterEqual(key_events.count(("up", "c")), 2)
        self.assertEqual(["single_platform_return_stalled"], relocations)

    def test_single_platform_return_watchdog_resets_after_horizontal_progress(self):
        """返程X轴恢复变化后，应清除升级阶段，避免正常移动时误跳。"""
        from v3.public import recorded_route_player

        point = recorded_route_player.RecordedRoutePoint(
            x=30,
            y=100,
            horizontal="right",
            vertical="none",
            action="none",
            kind="walk",
            segment_type="walk_off_right",
            platform_id="平台1",
        )
        variant = recorded_route_player.RecordedRouteVariant(
            name="return",
            probability=100,
            points=[point],
            closed_loop=True,
            platform_patrol=False,
        )
        state = recorded_route_player.RecordedRouteState(
            single_active_platform_number=2,
            single_active_platform_patrol_enabled=True,
            single_platform_return_watch_platform_number=1,
            single_platform_return_watch_target_number=2,
            single_platform_return_watch_x=15,
            single_platform_return_watch_y=100,
            single_platform_return_progress_at=time.monotonic() - 5,
            single_platform_return_recovery_stage=2,
        )
        runtime = SimpleNamespace()

        self.assertFalse(
            recorded_route_player._apply_single_platform_return_watchdog(
                runtime, state, variant, (16, 100), 0, 1
            )
        )
        self.assertEqual(16, state.single_platform_return_watch_x)
        self.assertEqual(0, state.single_platform_return_recovery_stage)

    def test_ignored_platform_input_is_present_in_runtime_settings(self):
        """The runtime settings page should expose and persist the multi-value input."""
        source = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('setPlaceholderText("例如：1/2/3")', source)
        self.assertIn('form.addWidget(QLabel("忽略平台")', source)
        self.assertIn(
            "ignored_platform_numbers=self.ignored_platform_numbers_input.text()",
            source,
        )
        self.assertIn(
            '"/".join(str(number) for number in config.ignored_platform_numbers)',
            source,
        )

    def test_rest_point_test_clears_ignored_platform_route_cursor(self):
        """Rest-point testing should relocalize after an ignored platform advanced the cursor."""
        from v3.public import recorded_route_player

        relocation_event = threading.Event()
        runtime = SimpleNamespace(
            休息点测试进行中=False,
            zant=2,
            人物全图重定位事件=relocation_event,
            重置攻击状态=lambda: None,
            重置战斗感知=lambda: None,
            释放攻击键=lambda: None,
            trace_event=lambda *_args, **_kwargs: None,
        )
        state = recorded_route_player.RecordedRouteState(
            ignored_platform_numbers=(1, 3),
            active_ignored_platform_number=3,
            ignored_platform_exit_index=88,
            route_index=88,
            last_relocalized_index=87,
            platform_coverage_target_x=120,
            platform_coverage_route_index=86,
            platform_direction="right",
            smart_seek_target_active=True,
            smart_seek_scan_until=999.0,
            smart_seek_mode="monster",
            loot_approach_direction="left",
            loot_approach_until=999.0,
        )

        with (
            patch.object(
                recorded_route_player.combat_logic,
                "clear_combat_for_movement",
            ),
            patch.object(
                recorded_route_player.combat_logic,
                "reset_attack_direction_lock",
            ),
        ):
            recorded_route_player._enter_rest_point_test_control(runtime, state)

        self.assertTrue(runtime.休息点测试进行中)
        self.assertEqual(0, runtime.zant)
        self.assertIsNone(state.active_ignored_platform_number)
        self.assertIsNone(state.ignored_platform_exit_index)
        self.assertIsNone(state.route_index)
        self.assertIsNone(state.last_relocalized_index)
        self.assertIsNone(state.platform_coverage_target_x)
        self.assertIsNone(state.platform_coverage_route_index)
        self.assertIsNone(state.platform_direction)
        self.assertFalse(state.smart_seek_target_active)
        self.assertIsNone(state.smart_seek_mode)
        self.assertIsNone(state.loot_approach_direction)
        self.assertTrue(relocation_event.is_set())

    def test_route_summary_repaints_after_the_preview_tab_is_visible(self):
        """The first route summary display must not scale against a hidden tab's stale size."""
        source = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        show_summary_source = ast.get_source_segment(
            source,
            methods["_show_route_summary"],
        )
        self.assertLess(
            show_summary_source.index("setCurrentWidget"),
            show_summary_source.index("_load_last_route_preview"),
        )
        self.assertIn("_schedule_route_preview_render", show_summary_source)
        self.assertIn("currentChanged.connect(self._on_monitor_tab_changed)", source)
        render_source = ast.get_source_segment(
            source,
            methods["_render_route_preview"],
        )
        self.assertIn("contentsRect().size()", render_source)
        resize_source = ast.get_source_segment(source, methods["resizeEvent"])
        self.assertIn("_schedule_route_preview_render(40)", resize_source)

    def test_rest_point_recording_button_is_available_in_route_editor(self):
        """路线编辑页应提供向已有JSON写入休息点的独立按钮。"""
        source = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn('QPushButton("录制休息点")', source)
        self.assertIn("save_route_rest_point", source)
        self.assertIn("_record_route_rest_point", source)
        self.assertIn("interval_minutes=self.rope_rest_interval.value()", source)
        self.assertIn("duration_minutes=self.rope_rest_duration.value()", source)

    def test_user_facing_templates_drive_left_and_right_observations(self):
        """用户提供的左右人物小图应优先于内嵌模板并正确识别两种面向。"""
        import cv2
        import numpy as np

        from v3.facing_direction import CharacterFacingTracker

        tracker = CharacterFacingTracker(cv2, np)
        self.assertIn("facing_left.png", tracker.template_source)
        self.assertEqual((22, 28), tracker.left_template.shape)
        self.assertEqual((24, 29), tracker.right_template.shape)
        monitor = {"left": 0, "top": 0, "width": 240, "height": 180}

        left_frame = np.zeros((180, 240, 3), dtype=np.uint8)
        left_bgr = cv2.cvtColor(tracker.left_template, cv2.COLOR_GRAY2BGR)
        left_frame[80:102, 100:128] = left_bgr
        left = tracker.observe(cv2, np, left_frame, monitor, (114, 91))
        self.assertTrue(left.reliable)
        self.assertEqual("left", left.detected_direction)

        right_frame = np.zeros((180, 240, 3), dtype=np.uint8)
        right_bgr = cv2.cvtColor(tracker.right_template, cv2.COLOR_GRAY2BGR)
        right_frame[78:102, 100:129] = right_bgr
        right = tracker.observe(cv2, np, right_frame, monitor, (114, 90))
        self.assertTrue(right.reliable)
        self.assertEqual("right", right.detected_direction)

    def test_runtime_trace_compacts_facing_and_detection_logs(self):
        """高频识别日志应保留诊断关键值并删除重复的大字段。"""
        from v3.runtime_trace import RuntimeTrace

        detection = RuntimeTrace._compact_fields(
            "detection_frame",
            {
                "map": "自定义录制路线",
                "targets": 2,
                "facing_direction": "left",
                "frame_ms": 12.3,
                "template_search_left": 999,
                "template_search_width": 840,
                "unused_large_details": list(range(100)),
            },
        )
        self.assertEqual("left", detection["facing_direction"])
        self.assertEqual(2, detection["targets"])
        self.assertNotIn("template_search_left", detection)
        self.assertNotIn("unused_large_details", detection)

        facing = RuntimeTrace._compact_fields(
            "character_facing_frame",
            {
                "detected_direction": "right",
                "left_confidence": 0.1,
                "right_confidence": 0.98,
                "template_source": "files:facing_left.png|facing_right.png",
                "full_frame_pixels": list(range(100)),
            },
        )
        self.assertEqual("right", facing["detected_direction"])
        self.assertIn("template_source", facing)
        self.assertNotIn("full_frame_pixels", facing)

        diagnostics = (
            ROOT / "v3" / "detection_diagnostics.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("[模板诊断][目标#", diagnostics)
        self.assertIn("[模板诊断] 帧{}", diagnostics)

    def test_game_focus_guard_is_common_and_uses_five_second_grace(self):
        """所有地图共用焦点保护，失焦时暂停输入并在五秒后尝试恢复。"""
        from v3 import automation_service
        from v3 import legacy_engine as engine

        source = (ROOT / "v3" / "automation_service.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(5.0, automation_service.GAME_FOCUS_LOST_GRACE_SECONDS)
        self.assertIn('"game_focus_guard"', source)
        self.assertIn("GetForegroundWindow", source)
        self.assertIn("_restore_game_window_focus", source)
        self.assertIn("focus_event.clear()", source)
        self.assertIn("engine.人物全图重定位事件.set()", source)

        engine.stop_event2 = 1
        engine.stop_event = threading.Event()
        engine.游戏窗口焦点事件.clear()
        timer = threading.Timer(0.03, engine.游戏窗口焦点事件.set)
        started_at = time.monotonic()
        timer.start()
        try:
            self.assertTrue(engine.可中断等待(0.0, interval=0.01))
            self.assertGreaterEqual(time.monotonic() - started_at, 0.02)
        finally:
            timer.cancel()
            engine.游戏窗口焦点事件.set()
            engine.stop_event2 = 0

    def test_game_focus_restore_activates_existing_window(self):
        """焦点恢复应先还原并激活窗口，成功后无需点击游戏客户区。"""
        from v3.automation_service import AutomationService

        class FakeWin32Gui:
            """模拟可被正常激活的游戏窗口。"""

            def __init__(self):
                """初始化前台句柄。"""
                self.foreground = 999
                self.shown = []

            def ShowWindow(self, hwnd, mode):
                """记录窗口还原。"""
                self.shown.append((hwnd, mode))

            def SetForegroundWindow(self, hwnd):
                """模拟成功激活。"""
                self.foreground = hwnd

            def GetForegroundWindow(self):
                """返回当前前台窗口。"""
                return self.foreground

        clicks = []
        gui = FakeWin32Gui()
        runtime = SimpleNamespace(
            win32gui=gui,
            pydirectinput=SimpleNamespace(
                click=lambda x, y: clicks.append((x, y))
            ),
        )

        restored = AutomationService._restore_game_window_focus(runtime, 123)

        self.assertTrue(restored)
        self.assertEqual(123, gui.foreground)
        self.assertTrue(gui.shown)
        self.assertEqual([], clicks)

    def test_missing_character_templates_stop_startup(self):
        """灯泡和血量模板都缺失时应在启动阶段给出明确错误。"""
        class MissingCv2:
            """模拟所有图片都读取失败的 OpenCV 接口。"""

            @staticmethod
            def imread(_path):
                """始终返回空结果。"""
                return None

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                require_character_templates(MissingCv2, Path(directory))

    def test_legacy_health_template_name_is_supported(self):
        """现有 renwuxueliang.png 应按 xueliang.png 类型参与人物定位。"""
        marker = object()

        class LegacyHealthCv2:
            """只模拟旧血量模板文件读取成功。"""

            @staticmethod
            def imread(path):
                """旧资源名匹配时返回测试对象。"""
                return marker if str(path).endswith("renwuxueliang.png") else None

        templates = load_character_templates(LegacyHealthCv2, Path("unused"))
        self.assertEqual([("xueliang.png", marker)], templates)


if __name__ == "__main__":
    unittest.main()

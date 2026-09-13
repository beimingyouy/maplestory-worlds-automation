import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

from v3.config import AppConfig, ConfigStore
from v3.custom_route import next_custom_route_direction
from v3.public.monster_detection import (
    custom_template_directory,
    effective_detector,
    ensure_custom_template_directory,
)
from v3.map_registry import MAP_SPECS, get_map_spec


ROOT = Path(__file__).resolve().parent.parent


def _is_name_equal(test, name, value):
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == name
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == value
    )


def _call_name(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


class _MapActionNormalizer(ast.NodeTransformer):
    """把 3.0 已抽取的公共代码归一化，只比较各地图特有路线。"""

    @staticmethod
    def _marker(name):
        return ast.Expr(value=ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=[], keywords=[]))

    def visit_If(self, node):
        if _is_name_equal(node.test, "zant", 1):
            has_old_block = any(
                isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "baocfangxiang" for target in item.targets)
                for item in node.body
            )
            has_helper = any(
                isinstance(item, ast.Expr) and _call_name(item.value) == "处理战斗暂停"
                for item in node.body
            )
            if has_old_block or has_helper:
                return self._marker("公共战斗逻辑")

        if _is_name_equal(node.test, "zant", 2):
            return self._marker("公共暂停等待")

        old_stop = _is_name_equal(node.test, "stop_event2", 0)
        new_stop = _call_name(node.test) == "已请求停止"
        if old_stop or new_stop:
            return self._marker("公共停止判断")
        return self.generic_visit(node)

    def visit_With(self, node):
        if (
            len(node.items) == 1
            and isinstance(node.items[0].context_expr, ast.Name)
            and node.items[0].context_expr.id == "lock"
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Assign)
        ):
            assignment = node.body[0]
            if (
                isinstance(assignment.targets[0], ast.Name)
                and assignment.targets[0].id == "r_pos"
                and isinstance(assignment.value, ast.Name)
                and assignment.value.id == "renwu_pos"
            ):
                return self._marker("公共坐标读取")
        return self.generic_visit(node)

    def visit_Assign(self, node):
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "r_pos"
            and _call_name(node.value) == "读取人物位置"
        ):
            return self._marker("公共坐标读取")
        return self.generic_visit(node)


class MapRegistryTests(unittest.TestCase):
    def test_all_legacy_maps_are_registered_once(self):
        names = [spec.name for spec in MAP_SPECS]
        self.assertEqual(19, len(names))
        self.assertEqual(len(names), len(set(names)))
        mushroom_v2 = get_map_spec("蘑菇V2")
        self.assertEqual("蘑菇地图V2", mushroom_v2.handler)
        self.assertEqual("蘑菇", mushroom_v2.detection_resource_name)
        self.assertEqual("蘑菇地图1半图", get_map_spec("蘑菇半层").handler)
        backup = get_map_spec("蘑菇半图上层备用")
        self.assertEqual("蘑菇地图1半图上", backup.handler)
        self.assertEqual("template", backup.detector)

    def test_registry_handlers_exist_in_map_routes(self):
        source = (ROOT / "v3" / "map" / "routes.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        missing = [spec.handler for spec in MAP_SPECS if spec.handler not in functions]
        self.assertEqual([], missing)

    def test_map_specific_routes_match_version_1_9(self):
        if not (ROOT / "正式版1.9.py").is_file():
            self.skipTest("当前项目快照未包含正式版1.9.py，无法执行源码逐段对比")
        original_source = (ROOT / "正式版1.9.py").read_text(encoding="utf-8")
        v3_source = (ROOT / "v3" / "map" / "routes.py").read_text(encoding="utf-8")

        def action_nodes(source):
            tree = ast.parse(source)
            # 蘑菇 V2 是 3.0 新增流程，不参与 1.9 原路线逐段对比。
            wanted = {
                spec.handler for spec in MAP_SPECS if spec.name != "蘑菇V2"
            }
            result = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in wanted:
                    normalized = _MapActionNormalizer().visit(copy.deepcopy(node))
                    result[node.name] = ast.dump(normalized, include_attributes=False)
            return result

        self.assertEqual(action_nodes(original_source), action_nodes(v3_source))

    def test_common_runtime_logic_is_extracted(self):
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        route_source = (ROOT / "v3" / "map" / "routes.py").read_text(
            encoding="utf-8"
        )
        route_tree = ast.parse(route_source)
        route_functions = {
            node.name: node
            for node in route_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        helpers = {
            "已请求停止",
            "可中断等待",
            "读取人物位置",
            "释放水平移动键",
            "切换持续移动",
            "释放攻击键",
            "轻点方向",
            "等待战斗恢复",
            "处理战斗暂停",
        }
        self.assertTrue(helpers.issubset(functions))

        handlers = {spec.handler for spec in MAP_SPECS}
        calls = []
        for name in handlers:
            calls.extend(
                _call_name(node)
                for node in ast.walk(route_functions[name])
                if isinstance(node, ast.Call)
            )
        self.assertEqual(17, calls.count("处理战斗暂停"))
        self.assertGreaterEqual(calls.count("读取人物位置"), 17)
        self.assertEqual(17, calls.count("等待战斗恢复"))
        self.assertGreaterEqual(calls.count("已请求停止"), 17)

        pause_helper = functions["等待战斗恢复"]
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and _call_name(node) == "可中断等待"
            for node in ast.walk(pause_helper)
        ))
        # 检测就绪事件和可配置攻击复检使兼容引擎少量增长，
        # 新的蘑菇 V2 主体仍必须保持在 map 独立模块中。
        self.assertLess(len(source.splitlines()), 1900)

        custom_route = route_functions["自定义路线"]
        custom_calls = {
            _call_name(node)
            for node in ast.walk(custom_route)
            if isinstance(node, ast.Call)
        }
        self.assertTrue({
            "处理战斗暂停",
            "等待战斗恢复",
            "读取人物位置",
            "next_custom_route_direction",
            "切换持续移动",
            "释放水平移动键",
        }.issubset(custom_calls))

        self.assertNotIn("MainWindow", functions)
        wheel_solver = functions["jielun"]
        busy_loops = [
            node
            for node in ast.walk(wheel_solver)
            if isinstance(node, ast.While)
            and isinstance(node.test, ast.Constant)
            and node.test.value is True
        ]
        self.assertEqual([], busy_loops)

    def test_detection_loop_helpers_are_reused(self):
        source = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("load_monster_templates", functions)
        self.assertNotIn("find_red_marker_center", functions)

        template_detector = functions["detection_thread"]
        nested_functions = [
            node.name
            for node in ast.walk(template_detector)
            if isinstance(node, ast.FunctionDef) and node is not template_detector
        ]
        self.assertEqual([], nested_functions)

        helper_calls = [
            _call_name(node)
            for node in ast.walk(template_detector)
            if isinstance(node, ast.Call)
        ]
        self.assertIn("load_monster_templates", helper_calls)
        self.assertIn("require_character_templates", helper_calls)
        self.assertIn("select_character_template", helper_calls)
        self.assertIn("CharacterTracker", helper_calls)
        self.assertIn("locate", [
            node.func.attr
            for node in ast.walk(template_detector)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ])
        self.assertIn("character_attack_reference_y", helper_calls)
        self.assertIn("evaluate_attack_target", helper_calls)
        self.assertIn("match_monster_templates", helper_calls)
        self.assertEqual(1, helper_calls.count("检查并补充红蓝"))
        self.assertNotIn("发布检测预览", helper_calls)

        renwu_assignments = [
            node
            for node in ast.walk(template_detector)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "renwu_pos"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ]
        self.assertEqual([], renwu_assignments)

        monitors = []
        for node in ast.walk(template_detector):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "monitor"
                for target in node.targets
            ):
                continue
            monitors.append({
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
            })
        self.assertIn(
            {"top": 300, "left": 0, "width": 1280, "height": 330},
            monitors,
        )
        detector_source = ast.get_source_segment(source, template_detector)
        self.assertIn("monitor['left'], monitor['top']", detector_source)
        self.assertIn('dituming == "蘑菇半图上层备用" and didd == 1', detector_source)
        self.assertEqual(
            ["名字", "search_range", "vertical_range", "template_directory"],
            [argument.arg for argument in template_detector.args.args],
        )
        yolo_detector = functions["detection_threadyolo"]
        self.assertEqual(
            ["名字", "search_range", "vertical_range", "template_directory"],
            [argument.arg for argument in yolo_detector.args.args],
        )
        yolo_source = ast.get_source_segment(source, yolo_detector)
        self.assertIn("if monster_templates", yolo_source)
        self.assertIn("model(", yolo_source)
        yolo_calls = [
            _call_name(node)
            for node in ast.walk(yolo_detector)
            if isinstance(node, ast.Call)
        ]
        self.assertEqual(1, yolo_calls.count("检查并补充红蓝"))
        self.assertNotIn("发布检测预览", yolo_calls)

        coordinate_helper = functions["获取红蓝检测坐标"]
        coordinate_source = ast.get_source_segment(source, coordinate_helper)
        self.assertIn("512 + int(didd红) / 100 * 100", coordinate_source)
        self.assertIn("623 + int(didd蓝) / 100 * 100", coordinate_source)
        self.assertIn("检测y = 826", coordinate_source)

        potion_helper = functions["检查并补充红蓝"]
        potion_source = ast.get_source_segment(source, potion_helper)
        self.assertIn("获取红蓝检测坐标", potion_source)
        potion_calls = [
            _call_name(node)
            for node in ast.walk(potion_helper)
            if isinstance(node, ast.Call)
        ]
        self.assertEqual(2, potion_calls.count("is_white_pixel"))
        self.assertNotIn("renwuzhongxin_template_img", detector_source)

        loader_source = ast.get_source_segment(source, functions["load_monster_templates"])
        self.assertIn("cv2.imdecode", loader_source)
        self.assertIn("np.fromfile", loader_source)

        self.assertNotIn("发布检测预览", functions)
        self.assertNotIn("DETECTION_PREVIEW_CALLBACK", source)

    def test_registered_map_resources_exist(self):
        missing = []
        for spec in MAP_SPECS:
            if spec.detector == "yolo":
                resource = ROOT / "moxing" / "{}.pt".format(
                    spec.detection_resource_name
                )
            else:
                resource = ROOT / "img" / spec.detection_resource_name
                if spec.name.endswith("备用"):
                    # 备用模板图允许用户后续自行补充，不阻止其他正式地图回归。
                    continue
            if not resource.exists():
                missing.append(str(resource))
        self.assertTrue((ROOT / "moxing" / "血.pt").exists())
        self.assertEqual([], missing)


class ConfigTests(unittest.TestCase):
    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = ConfigStore(path)
            expected = AppConfig(
                map_name="火野猪1",
                attack_height=95,
                yolo_confidence=0.75,
                wheel_detection_enabled=True,
                use_custom_templates=True,
                custom_mode=True,
                custom_left_position=(60, 192),
                custom_right_position=(126, 192),
                group_attack=True,
                red_percent=42,
            )
            store.save(expected)
            actual = store.load()
            self.assertEqual(expected.to_dict(), actual.to_dict())
            json.loads(path.read_text(encoding="utf-8"))

    def test_invalid_map_is_rejected(self):
        with self.assertRaises(ValueError):
            AppConfig(map_name="不存在的地图").validate()

    def test_old_config_uses_default_attack_height(self):
        config = AppConfig.from_dict({"attack_distance": 340})
        self.assertEqual(340, config.attack_distance)
        self.assertEqual(70, config.attack_height)

    def test_wheel_detection_is_disabled_by_default_and_survives_round_trip(self):
        self.assertFalse(AppConfig().wheel_detection_enabled)
        self.assertFalse(AppConfig.from_dict({}).wheel_detection_enabled)
        restored = AppConfig.from_dict(
            AppConfig(wheel_detection_enabled=True).to_dict()
        )
        self.assertTrue(restored.wheel_detection_enabled)

    def test_wheel_detection_switch_is_wired_to_both_detection_pipelines(self):
        window_source = (ROOT / "v3" / "main_window.py").read_text(
            encoding="utf-8"
        )
        service_source = (ROOT / "v3" / "automation_service.py").read_text(
            encoding="utf-8"
        )
        engine_source = (ROOT / "v3" / "legacy_engine.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('QCheckBox("启用轮检测")', window_source)
        self.assertIn(
            "wheel_detection_enabled=self.wheel_detection_enabled.isChecked()",
            window_source,
        )
        self.assertIn(
            "self.wheel_detection_enabled.setChecked(config.wheel_detection_enabled)",
            window_source,
        )
        self.assertIn(
            "engine.启用轮检测 = bool(config.wheel_detection_enabled)",
            service_source,
        )
        self.assertIn("启用轮检测 = False", engine_source)
        self.assertIn(
            "lun_template = cv2.imread(luntup) if 启用轮检测 else None",
            engine_source,
        )
        self.assertIn(
            "if 启用轮检测 and lun_template is not None:",
            engine_source,
        )
        self.assertIn(
            "if 启用轮检测 and 发现轮 == 0:",
            engine_source,
        )
        self.assertIn(
            "if 启用轮检测 and lun_template is not None and luntishi is not None:",
            engine_source,
        )

    def test_invalid_attack_height_is_rejected(self):
        with self.assertRaises(ValueError):
            AppConfig(attack_height=5).validate()

    def test_invalid_yolo_confidence_is_rejected(self):
        """界面可调的 YOLO 阈值必须限制在安全范围内。"""
        with self.assertRaises(ValueError):
            AppConfig(yolo_confidence=0.2).validate()
        with self.assertRaises(ValueError):
            AppConfig(yolo_confidence=0.99).validate()

    def test_custom_template_mode_overrides_yolo_only_when_enabled(self):
        self.assertEqual("yolo", effective_detector("yolo", False))
        self.assertEqual("template", effective_detector("yolo", True))
        self.assertEqual("template", effective_detector("template", False))
        self.assertEqual("template", effective_detector("template", True))

    def test_custom_template_directory_is_fixed(self):
        directory = custom_template_directory(ROOT)
        self.assertEqual(ROOT / "img" / "自定义", directory)
        self.assertTrue((directory / "图片放这里.txt").is_file())

    def test_onefile_build_seeds_bundled_custom_templates_once(self):
        import sys
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as bundled, tempfile.TemporaryDirectory() as output:
            bundled_templates = Path(bundled) / "img" / "自定义"
            bundled_templates.mkdir(parents=True)
            (bundled_templates / "guai1.png").write_bytes(b"bundled")
            (bundled_templates / "图片放这里.txt").write_text("说明", encoding="utf-8")

            with patch.object(sys, "_MEIPASS", bundled, create=True):
                target = ensure_custom_template_directory(output)
                self.assertEqual(b"bundled", (target / "guai1.png").read_bytes())
                self.assertTrue((target / "图片放这里.txt").is_file())

                (target / "guai1.png").write_bytes(b"user")
                ensure_custom_template_directory(output)
                self.assertEqual(b"user", (target / "guai1.png").read_bytes())

    def test_custom_mode_requires_recorded_boundaries(self):
        with self.assertRaises(ValueError):
            AppConfig(custom_mode=True).validate()
        with self.assertRaises(ValueError):
            AppConfig(
                custom_mode=True,
                custom_left_position=(126, 192),
                custom_right_position=(60, 192),
            ).validate()

    def test_custom_route_turns_at_recorded_boundaries(self):
        self.assertEqual("left", next_custom_route_direction(80, 60, 126, "left"))
        self.assertEqual("right", next_custom_route_direction(65, 60, 126, "left"))
        self.assertEqual("right", next_custom_route_direction(100, 60, 126, "right"))
        self.assertEqual("left", next_custom_route_direction(121, 60, 126, "right"))

    def test_custom_mode_overrides_selected_map_route(self):
        source = (ROOT / "v3" / "automation_service.py").read_text(encoding="utf-8")
        self.assertIn('get_map_spec("蘑菇半层上层") if config.custom_mode', source)
        self.assertIn("engine.自定义路线(", source)
        self.assertIn("config.custom_mode or config.use_custom_templates", source)


class DetectionTestModeTests(unittest.TestCase):
    def test_entry_exposes_editable_test_mode_switch(self):
        source = (ROOT / "正式版3.0.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        switches = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "TEST_MODE"
                for target in node.targets
            )
        ]
        self.assertEqual(1, len(switches))
        self.assertIsInstance(switches[0].value, ast.Constant)
        self.assertIsInstance(switches[0].value.value, bool)
        self.assertIn("--test", source)
        self.assertIn("--package-self-test", source)
        self.assertIn("v3.legacy_engine", source)

        self.assertIn("from v3.app import main", source)

    def test_test_mode_receives_actual_detection_preview(self):
        service_source = (ROOT / "v3" / "detection_test_service.py").read_text(encoding="utf-8")
        window_source = (ROOT / "v3" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("frame_ready = pyqtSignal(object, object)", service_source)
        self.assertIn("self.frame_ready.emit(rgb_frame.copy(), info)", service_source)
        self.assertIn("self.service.frame_ready.connect(self.update_test_preview)", window_source)
        self.assertIn("人物匹配：{self_ms:.2f}ms", window_source)
        self.assertIn('"MONSTER {} {:.2f}"', service_source)
        self.assertIn('"SELF {}"', service_source)

    def test_v3_runtime_has_no_removed_preview_callback(self):
        v3_service = (ROOT / "v3" / "automation_service.py").read_text(encoding="utf-8")
        v3_engine = (ROOT / "v3" / "legacy_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("DETECTION_PREVIEW_CALLBACK", v3_service)
        self.assertNotIn("DETECTION_PREVIEW_CALLBACK", v3_engine)

    def test_pyinstaller_spec_collects_dynamic_engine_and_resources(self):
        if not (ROOT / "QQ炫舞3.0.spec").is_file():
            self.skipTest("当前项目快照未包含 PyInstaller 打包脚本")
        spec = (ROOT / "QQ炫舞3.0.spec").read_text(encoding="utf-8")
        build_script = (ROOT / "打包3.0.bat").read_text(encoding="utf-8")
        self.assertIn('PACKAGE_NAME = "v31" if APP_VERSION == "3.1" else "v3"', spec)
        self.assertIn("collect_submodules(PACKAGE_NAME)", spec)
        self.assertIn('"{}.legacy_engine".format(PACKAGE_NAME)', spec)
        self.assertIn('("img", "moxing", "resres")', spec)
        self.assertIn('"tensorflow"', spec)
        self.assertIn("excludes=excludes", spec)
        self.assertIn("tools\\package_v3.py", build_script)
        self.assertTrue(all(byte < 128 for byte in build_script.encode("utf-8")))

        onefile_spec = (ROOT / "QQ炫舞3.0单文件.spec").read_text(encoding="utf-8")
        onefile_script = (ROOT / "3.0单文件打包.bat").read_text(encoding="utf-8")
        self.assertIn('PACKAGE_NAME = "v31" if APP_VERSION == "3.1" else "v3"', onefile_spec)
        self.assertIn("collect_submodules(PACKAGE_NAME)", onefile_spec)
        self.assertIn('"{}.legacy_engine".format(PACKAGE_NAME)', onefile_spec)
        self.assertIn("a.binaries", onefile_spec)
        self.assertIn("a.datas", onefile_spec)
        self.assertIn("console=False", onefile_spec)
        self.assertNotIn("COLLECT(", onefile_spec)
        self.assertIn("tools\\package_v3.py", onefile_script)
        self.assertTrue(all(byte < 128 for byte in onefile_script.encode("utf-8")))

        package_helper = (ROOT / "tools" / "package_v3.py").read_text(encoding="utf-8")
        self.assertIn('"--package-self-test"', package_helper)
        self.assertIn('build_with_pyinstaller("QQ炫舞3.0.spec", app_version)', package_helper)
        self.assertIn('build_with_pyinstaller("QQ炫舞3.0单文件.spec", app_version)', package_helper)
        self.assertIn('package_directory("3.1")', package_helper)
        self.assertIn('package_onefile("3.1")', package_helper)
        self.assertIn("shutil.copytree", package_helper)

    def test_app_selects_dedicated_test_service(self):
        source = (ROOT / "v3" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        self.assertIn("test_mode", [argument.arg for argument in main_function.args.args])
        called_names = {
            node.func.id
            for node in ast.walk(main_function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("DetectionTestService", called_names)
        self.assertIn("AutomationService", called_names)

    def test_detection_test_service_has_no_input_actions(self):
        source = (ROOT / "v3" / "detection_test_service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn("pydirectinput", source)
        self.assertNotIn("find_red_marker_center", source)
        forbidden_calls = {"keyDown", "keyUp", "press", "click", "moveTo"}
        attribute_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden_calls.isdisjoint(attribute_calls))
        self.assertIn("frame_ready", source)

        capture_source = (
            ROOT / "v3" / "public" / "minimap_tracking.py"
        ).read_text(encoding="utf-8")
        for forbidden in ("pydirectinput", "keyDown", "keyUp", "press(", "click("):
            self.assertNotIn(forbidden, capture_source)

    def test_minimap_coordinate_capture_returns_absolute_yellow_position(self):
        import cv2
        import numpy as np

        from v3.public.minimap_tracking import capture_yellow_position

        class FakeEngine:
            pass

        engine = FakeEngine()
        engine.np = np
        engine.cv2 = cv2
        frame = np.zeros((190, 270, 4), dtype=np.uint8)
        frame[49:52, 39:42] = (0, 255, 255, 255)
        engine.grab_screen = lambda _monitor: frame
        self.assertEqual((60, 160), capture_yellow_position(engine))

    def test_self_position_template_is_selected_once_then_fixed(self):
        import cv2
        import numpy as np

        from v3.detection_test_service import (
            SELF_MATCH_THRESHOLD,
            DetectionTestService,
        )
        from v3.person_locator import CharacterTracker, PERSON_MONITOR

        self.assertEqual(0.80, SELF_MATCH_THRESHOLD)

        rng = np.random.default_rng(20260804)
        lamp = rng.integers(0, 256, (12, 16, 3), dtype=np.uint8)
        health = rng.integers(0, 256, (10, 14, 3), dtype=np.uint8)
        templates = [("dengpao.png", lamp), ("xueliang.png", health)]

        class FakeStopEvent:
            @staticmethod
            def is_set():
                return False

            @staticmethod
            def wait(_timeout):
                return False

        class FakeEngine:
            def __init__(self, frames):
                self.frames = frames
                self.index = 0

            def grab_screen(self, _monitor):
                frame = self.frames[min(self.index, len(self.frames) - 1)]
                self.index += 1
                return cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[330:342, 100:116] = lamp
        frame[390:400, 300:314] = health

        selected = DetectionTestService._select_person_template(
            cv2,
            np,
            FakeEngine([frame]),
            {"top": 10, "left": 0, "width": 1280, "height": 720},
            templates,
            FakeStopEvent(),
        )
        self.assertEqual("dengpao.png", selected[0])
        tracker = CharacterTracker(selected, PERSON_MONITOR)
        screen_position, preview_position = DetectionTestService._find_self_position(
            cv2, frame[290:620, 0:1280], tracker
        )
        self.assertEqual((108, 336), preview_position)
        self.assertEqual((108, 346), screen_position)

        fallback_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        fallback_frame[390:400, 300:314] = health
        selected = DetectionTestService._select_person_template(
            cv2,
            np,
            FakeEngine([fallback_frame, fallback_frame, fallback_frame]),
            {"top": 10, "left": 0, "width": 1280, "height": 720},
            templates,
            FakeStopEvent(),
        )
        self.assertEqual("xueliang.png", selected[0])
        tracker = CharacterTracker(selected, PERSON_MONITOR)
        screen_position, preview_position = DetectionTestService._find_self_position(
            cv2, fallback_frame[290:620, 0:1280], tracker
        )
        self.assertEqual((307, 395), preview_position)
        self.assertEqual((307, 405), screen_position)

        # 即使后续画面同时出现灯泡，也必须继续使用启动时选定的血量模板。
        screen_position, preview_position = DetectionTestService._find_self_position(
            cv2, frame[290:620, 0:1280], tracker
        )
        self.assertEqual((307, 395), preview_position)
        self.assertEqual((307, 405), screen_position)

    def test_template_monster_boxes_receive_preview_y_offset(self):
        import cv2
        import numpy as np

        from v3.detection_test_service import DetectionTestService

        rng = np.random.default_rng(20260805)
        template = rng.integers(0, 256, (14, 18, 3), dtype=np.uint8)
        detection_region = np.zeros((330, 1280, 3), dtype=np.uint8)
        detection_region[40:54, 120:138] = template
        boxes = DetectionTestService._detect_templates(
            cv2,
            np,
            detection_region,
            [template],
            y_offset=290,
        )
        matching = [box for box in boxes if box[:4] == (120, 330, 138, 344)]
        self.assertEqual(1, len(matching))
        self.assertGreaterEqual(matching[0][4], 0.99)

    def test_character_attack_offsets_match_version_1_9(self):
        from v3.person_locator import (
            character_attack_reference_y,
            evaluate_attack_target,
        )

        self.assertEqual(537, character_attack_reference_y("xueliang.png", 577))
        self.assertEqual(647, character_attack_reference_y("dengpao.png", 577))
        dx, dy, accepted = evaluate_attack_target(779, 537, (788, 512.5), 300)
        self.assertEqual(9, dx)
        self.assertEqual(24.5, dy)
        self.assertTrue(accepted)
        _, _, accepted = evaluate_attack_target(
            779, 537, (788, 620), 300, vertical_range=95
        )
        self.assertTrue(accepted)
        _, _, accepted = evaluate_attack_target(
            779, 537, (788, 640), 300, vertical_range=95
        )
        self.assertFalse(accepted)


if __name__ == "__main__":
    unittest.main()

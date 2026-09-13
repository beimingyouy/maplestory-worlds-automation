"""QQ炫舞 3.0 启动入口。"""

import importlib
import sys
import traceback
from pathlib import Path

from v3.app import main


# 兼容旧启动方式：True会让页面首次默认勾选截图测试模式，自动化仍正常运行。
# 日常使用可保持False，直接在主页面“运行模式”中切换并保存。
TEST_MODE = False


def package_self_test() -> int:
    """打包后安全检查动态运行引擎和资源；不创建窗口，也不执行任何按键。"""
    output_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    report_path = output_dir / "打包自检结果.txt"
    try:
        engine = importlib.import_module("v3.legacy_engine")
        resource_root = Path(engine.get_base_dir())
        missing = [
            name
            for name in ("img", "moxing", "resres")
            if not (resource_root / name).exists()
        ]
        if missing:
            raise FileNotFoundError("缺少资源目录：{}".format(", ".join(missing)))
        image_extensions = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
        internal_monster_directory = resource_root / "img" / "monsters"
        external_monster_directory = output_dir / "img" / "monsters"
        internal_route_directory = resource_root / "v3" / "map" / "recordings"
        external_route_directory = output_dir / "v3" / "map" / "recordings"
        internal_monster_count = sum(
            1
            for path in internal_monster_directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in image_extensions
        )
        external_monster_count = sum(
            1
            for path in external_monster_directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in image_extensions
        )
        internal_route_count = sum(
            1 for path in internal_route_directory.rglob("*.json") if path.is_file()
        )
        external_route_count = sum(
            1 for path in external_route_directory.rglob("*.json") if path.is_file()
        )
        resource_counts = {
            "内部怪物图鉴": internal_monster_count,
            "外部怪物图鉴": external_monster_count,
            "内部路线JSON": internal_route_count,
            "外部路线JSON": external_route_count,
        }
        incomplete = [
            name for name, count in resource_counts.items() if count <= 0
        ]
        if incomplete:
            raise FileNotFoundError(
                "打包资源为空：{}".format("、".join(incomplete))
            )
        report_path.write_text(
            (
                "打包自检通过\nlegacy_engine={}\n资源根目录={}\n"
                "内部怪物图鉴={}\n外部怪物图鉴={}\n"
                "内部路线JSON={}\n外部路线JSON={}\n"
            ).format(
                Path(engine.__file__).resolve(),
                resource_root,
                internal_monster_count,
                external_monster_count,
                internal_route_count,
                external_route_count,
            ),
            encoding="utf-8",
        )
        return 0
    except Exception:
        report_path.write_text(
            "打包自检失败\n{}".format(traceback.format_exc()),
            encoding="utf-8",
        )
        return 1


if __name__ == "__main__":
    if "--package-self-test" in sys.argv:
        raise SystemExit(package_self_test())
    # 也可以使用：python .\正式版3.0.py --test
    command_line_test = "--test" in sys.argv
    if command_line_test:
        # QApplication 不需要看到程序自定义参数，提前移除可避免 Qt 参数警告。
        sys.argv.remove("--test")
    raise SystemExit(main(test_mode=TEST_MODE or command_line_test))

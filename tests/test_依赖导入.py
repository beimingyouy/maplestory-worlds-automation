"""验证项目声明的第三方依赖和全部 v3 模块能够真实导入。"""

import importlib
import os
import pkgutil
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault(
    "YOLO_CONFIG_DIR",
    str(ROOT / ".v3_runtime" / "ultralytics"),
)
os.environ.setdefault(
    "MPLCONFIGDIR",
    str(ROOT / ".v3_runtime" / "matplotlib"),
)


class DependencyImportTests(unittest.TestCase):
    """检查 Python 包已安装且项目模块没有遗漏依赖。"""

    def test_direct_third_party_imports(self):
        """导入源码直接使用的全部第三方顶层模块。"""
        modules = (
            "PyQt5",
            "mss",
            "numpy",
            "cv2",
            "torch",
            "torchvision",
            "ultralytics",
            "pydirectinput",
            "keyboard",
            "win32gui",
            "win32con",
            "requests",
            "PIL",
        )
        for name in modules:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_all_v3_modules_import(self):
        """递归导入 v3 包，捕获内部模块新增但 requirements 漏配的依赖。"""
        names = ["v3"]
        names.extend(
            info.name
            for info in pkgutil.walk_packages(
                [str(ROOT / "v3")],
                prefix="v3.",
            )
        )
        for name in sorted(names):
            with self.subTest(module=name):
                importlib.import_module(name)


if __name__ == "__main__":
    unittest.main()

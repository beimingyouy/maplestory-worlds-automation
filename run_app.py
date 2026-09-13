"""启动 3.0 主程序的 ASCII 入口。

批处理文件只使用 ASCII 命令，中文文件名统一由本文件以 UTF-8 方式处理，
避免 Windows CMD 在 UTF-8/GBK 之间切换时把中文路径拆成错误命令。

用法：
    .venv\\Scripts\\python.exe run_app.py [--test]
等价于：
    python 正式版3.0.py [--test]
"""

import pathlib
import runpy
import sys

TARGET_NAME = "\u6b63\u5f0f\u72483.0.py"


def main() -> None:
    """把入口文件当作 __main__ 执行，并保留命令行参数。"""
    target = pathlib.Path(__file__).resolve().parent / TARGET_NAME
    if not target.is_file():
        raise SystemExit("entry not found: {}".format(target))
    sys.argv = [str(target)] + sys.argv[1:]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()

# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH)

# 构建脚本（tools/build_release.ps1）会先生成随机构建身份（名称/图标/版权），
# 再通过以下环境变量传入。手动执行 pyinstaller 时若未设置，则回退到默认名称，
# 且不带图标与版本资源。
_package_name = os.environ.get("V3_PACKAGE_NAME") or "QQ炫舞3.0"
_icon_path = os.environ.get("V3_ICON_PATH") or None
_version_file_path = os.environ.get("V3_VERSION_FILE") or None
if _icon_path and not Path(_icon_path).is_file():
    _icon_path = None
if _version_file_path and not Path(_version_file_path).is_file():
    _version_file_path = None

ultralytics_datas, ultralytics_binaries, ultralytics_hiddenimports = collect_all(
    "ultralytics",
    filter_submodules=lambda name: not name.startswith("ultralytics.trackers"),
)
v3_hiddenimports = collect_submodules("v3")

datas = [
    (str(project_root / "img"), "img"),
    (str(project_root / "moxing"), "moxing"),
    (str(project_root / "resres"), "resres"),
    (str(project_root / "v3" / "map" / "recordings"), "v3/map/recordings"),
]
datas += ultralytics_datas

hiddenimports = sorted(
    set(
        v3_hiddenimports
        + ultralytics_hiddenimports
        + [
            "v3.legacy_engine",
            "v3.map.routes",
            "v3.public.recorded_route_player",
            "win32gui",
            "win32con",
            "win32api",
        ]
    )
)

# Ultralytics 支持很多本程序不用的导出和云平台后端。排除它们能明显缩小目录，
# 但保留 torch、torchvision、OpenCV、SciPy、Pillow 和 matplotlib 等推理依赖。
excludes = [
    "tensorflow",
    "tensorboard",
    "paddle",
    "paddlepaddle",
    "onnxruntime",
    "onnxruntime_gpu",
    "openvino",
    "tensorrt",
    "coremltools",
    "jax",
    "flax",
    "keras",
    "triton",
    "ray",
    "wandb",
    "comet_ml",
    "mlflow",
    "clearml",
    "neptune",
    "dvclive",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "ultralytics.trackers",
]

a = Analysis(
    [str(project_root / "正式版3.0.py")],
    pathex=[str(project_root)],
    binaries=ultralytics_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_package_name,
    icon=_icon_path,
    version=_version_file_path,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=_package_name,
)

# -*- coding: utf-8 -*-
"""运行时品牌显示名。

打包脚本（tools/build_release.ps1）每次构建都会先生成随机构建身份
（EXE 名称 / 图标 / 版权资源，三者互相一致），再交给 PyInstaller。
打包环境下这里直接读取自身 EXE 的 ``ProductName`` 版本资源作为显示名，
保证窗口标题、任务栏名称与 EXE 文件名完全一致；源码运行或读取失败时
回退到默认名称。
"""

import ctypes
import sys

DEFAULT_DISPLAY_NAME = "QQ炫舞 3.0"


def _read_exe_product_name(exe_path):
    """读取 Windows PE 版本资源中的 ProductName，失败返回 None。"""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
        if not size or size <= 0:
            return None
        data = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(
            exe_path, 0, size, data
        ):
            return None
        value = ctypes.c_void_p()
        length = ctypes.c_uint()
        # tools/build_randomize.py 写入的是英文(US)+Unicode 资源块。
        sub_block = "\\StringFileInfo\\040904b0\\ProductName"
        if not ctypes.windll.version.VerQueryValueW(
            data, sub_block, ctypes.byref(value), ctypes.byref(length)
        ):
            return None
        if not value.value or length.value <= 1:
            return None
        raw = ctypes.wstring_at(value.value, length.value)
        name = raw.rstrip("\x00").strip()
        return name or None
    except Exception:
        return None


def display_name():
    """当前程序对外的显示名（窗口标题 / 任务栏 / 日志文案）。"""
    if getattr(sys, "frozen", False):
        name = _read_exe_product_name(sys.executable)
        if name:
            return name
    return DEFAULT_DISPLAY_NAME

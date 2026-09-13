"""识别服务器连接错误画面并保存终止截图。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np


SERVER_ERROR_TEMPLATE_RELATIVE_PATH = Path(
    "img/system/server_connection_error.png"
)
SERVER_ERROR_MESSAGE_CROP = (90, 45, 292, 100)
SERVER_ERROR_BUTTON_CROP = (135, 140, 216, 180)
SERVER_ERROR_MESSAGE_THRESHOLD = 0.86
SERVER_ERROR_BUTTON_THRESHOLD = 0.86
SERVER_ERROR_GEOMETRY_TOLERANCE = 8


@dataclass(frozen=True)
class ServerErrorMatch:
    """一次服务器错误画面双模板检测结果。"""

    matched: bool
    message_confidence: float
    button_confidence: float
    message_location: Optional[Tuple[int, int]] = None
    button_location: Optional[Tuple[int, int]] = None
    popup_top_left: Optional[Tuple[int, int]] = None


def normalize_bgr_frame(frame) -> np.ndarray:
    """把MSS、灰度或BGR截图统一转换为连续BGR数组。"""
    array = np.asarray(frame)
    if array.ndim == 2:
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if array.ndim != 3:
        raise ValueError("服务器错误检测截图维度异常")
    if array.shape[2] == 4:
        return cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    if array.shape[2] == 3:
        return np.ascontiguousarray(array)
    raise ValueError("服务器错误检测截图通道数异常")


def load_server_error_template(project_root: Path) -> np.ndarray:
    """从项目资源目录读取用户提供的连接错误原图。"""
    template_path = Path(project_root) / SERVER_ERROR_TEMPLATE_RELATIVE_PATH
    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        raise FileNotFoundError("服务器连接错误模板不存在：{}".format(template_path))
    height, width = template.shape[:2]
    required_width = max(
        SERVER_ERROR_MESSAGE_CROP[2],
        SERVER_ERROR_BUTTON_CROP[2],
    )
    required_height = max(
        SERVER_ERROR_MESSAGE_CROP[3],
        SERVER_ERROR_BUTTON_CROP[3],
    )
    if width < required_width or height < required_height:
        raise ValueError(
            "服务器连接错误模板尺寸不足：{}x{}".format(width, height)
        )
    return template


def _crop(image: np.ndarray, bounds) -> np.ndarray:
    left, top, right, bottom = [int(value) for value in bounds]
    return image[top:bottom, left:right]


def _match_gray(frame_gray: np.ndarray, template_gray: np.ndarray):
    if (
        frame_gray.shape[0] < template_gray.shape[0]
        or frame_gray.shape[1] < template_gray.shape[1]
    ):
        return 0.0, None
    result = cv2.matchTemplate(
        frame_gray,
        template_gray,
        cv2.TM_CCOEFF_NORMED,
    )
    _minimum, maximum, _minimum_location, maximum_location = cv2.minMaxLoc(result)
    return float(maximum), tuple(int(value) for value in maximum_location)


def detect_server_connection_error(
    frame,
    reference: np.ndarray,
) -> ServerErrorMatch:
    """同时匹配错误文字和确定按钮，并校验二者的相对位置。"""
    frame_bgr = normalize_bgr_frame(frame)
    reference_bgr = normalize_bgr_frame(reference)
    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    message_template = cv2.cvtColor(
        _crop(reference_bgr, SERVER_ERROR_MESSAGE_CROP),
        cv2.COLOR_BGR2GRAY,
    )
    button_template = cv2.cvtColor(
        _crop(reference_bgr, SERVER_ERROR_BUTTON_CROP),
        cv2.COLOR_BGR2GRAY,
    )
    message_confidence, message_location = _match_gray(
        frame_gray,
        message_template,
    )
    button_confidence, button_location = _match_gray(
        frame_gray,
        button_template,
    )
    if message_location is None or button_location is None:
        return ServerErrorMatch(
            False,
            message_confidence,
            button_confidence,
            message_location,
            button_location,
        )

    expected_delta_x = (
        SERVER_ERROR_BUTTON_CROP[0] - SERVER_ERROR_MESSAGE_CROP[0]
    )
    expected_delta_y = (
        SERVER_ERROR_BUTTON_CROP[1] - SERVER_ERROR_MESSAGE_CROP[1]
    )
    actual_delta_x = int(button_location[0] - message_location[0])
    actual_delta_y = int(button_location[1] - message_location[1])
    geometry_matches = (
        abs(actual_delta_x - expected_delta_x)
        <= SERVER_ERROR_GEOMETRY_TOLERANCE
        and abs(actual_delta_y - expected_delta_y)
        <= SERVER_ERROR_GEOMETRY_TOLERANCE
    )
    matched = bool(
        message_confidence >= SERVER_ERROR_MESSAGE_THRESHOLD
        and button_confidence >= SERVER_ERROR_BUTTON_THRESHOLD
        and geometry_matches
    )
    popup_top_left = (
        int(message_location[0] - SERVER_ERROR_MESSAGE_CROP[0]),
        int(message_location[1] - SERVER_ERROR_MESSAGE_CROP[1]),
    )
    return ServerErrorMatch(
        matched,
        message_confidence,
        button_confidence,
        message_location,
        button_location,
        popup_top_left,
    )


def save_server_error_screenshot(frame, project_root: Path) -> Path:
    """把触发停止的完整窗口截图以PNG形式保存到logs。"""
    frame_bgr = normalize_bgr_frame(frame)
    output_directory = Path(project_root) / "logs"
    output_directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    output_path = output_directory / "server_connection_error_{}_{:03d}.png".format(
        now.strftime("%Y%m%d_%H%M%S"),
        now.microsecond // 1000,
    )
    encoded, buffer = cv2.imencode(".png", frame_bgr)
    if not encoded:
        raise OSError("服务器连接错误截图PNG编码失败")
    buffer.tofile(str(output_path))
    return output_path

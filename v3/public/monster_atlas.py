"""怪物图鉴扫描与像素级识图模板生成。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from typing import Iterable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageOps


MAX_BASE_FEATURES_PER_MONSTER = 6
MAX_TEMPLATES_PER_MONSTER = MAX_BASE_FEATURES_PER_MONSTER * 2
# 保留旧常量名供现有界面/脚本导入；它现在表示上限，不再表示固定数量。
TEMPLATES_PER_MONSTER = MAX_TEMPLATES_PER_MONSTER
MONSTER_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp"})
MANIFEST_FILE_NAME = "怪物图鉴模板.json"
PREVIEW_FILE_NAME = "怪物图鉴模板预览.png"
SOURCE_ASSET_DIRECTORY_NAME = "图鉴原图"

DEFAULT_FEATURE_NAMES = (
    "left_eye",
    "right_eye",
    "mouth",
    "left_hair",
    "center_hair",
    "right_hair",
)

# 少数怪物的眼睛或身体纹理非常通用，自动选择容易造成地图误识别。
# 这里按图鉴相对路径保存人工确认过的单特征规则；以后可继续追加，而不需要
# 在识图算法中堆叠怪物名称判断。
MONSTER_FEATURE_POLICIES = {
    "level_01-10/blue snail.png": {
        "features": ("single_eye",),
        "crops": {"single_eye": (13, 4, 11, 9)},
        "reason": "人工确认：只保留头部单眼，蜗牛身体和壳不作为模板",
    },
    "level_01-10/orange mushroom.png": {
        "features": (
            "left_eye",
            "right_eye",
            "cap_highlight_small",
            "cap_highlight_large",
        ),
        "crops": {
            "left_eye": (21, 21, 11, 13),
            "right_eye": (33, 21, 9, 14),
            "cap_highlight_small": (30, 7, 18, 7),
            "cap_highlight_large": (28, 3, 22, 16),
        },
        "reason": "人工确认：只保留两只单眼和两个蘑菇帽亮斑",
    },
    "level_01-10/slime.png": {
        "features": ("single_eye",),
        "crops": {"single_eye": (23, 21, 13, 15)},
        "reason": "人工确认：只保留史莱姆单眼，绿色身体和普通轮廓不作为模板",
    },
    "level_21-30/wooden mask.png": {
        "features": ("mouth",),
        "crops": {"mouth": (33, 52, 21, 14)},
        "reason": "木纹和眼睛通用，只识别锯齿嘴巴",
    },
    "level_21-30/rocky mask.png": {
        "features": ("mouth",),
        "crops": {"mouth": (24, 42, 21, 14)},
        "reason": "石纹和眼睛通用，只识别锯齿嘴巴",
    },
}


@dataclass(frozen=True)
class MonsterEntry:
    """图鉴中的一只怪物，分组与名称完全来自素材目录。"""

    group: str
    name: str
    path: Path


@dataclass(frozen=True)
class MonsterGroup:
    """一个素材目录及其直属怪物图片。"""

    name: str
    monsters: tuple[MonsterEntry, ...]


@dataclass(frozen=True)
class MonsterTemplateReport:
    """一次图鉴模板替换的结果。"""

    output_directory: Path
    selected_monsters: tuple[MonsterEntry, ...]
    template_files: tuple[Path, ...]
    manifest_file: Path
    preview_file: Path
    source_asset_directory: Path
    templates_per_monster: int

    @property
    def template_count(self) -> int:
        return len(self.template_files)


@dataclass(frozen=True)
class _CropCandidate:
    rect: tuple[int, int, int, int]
    score: float
    in_head: bool
    relative_x: float
    relative_y: float
    eye_score: float
    mouth_score: float
    hair_score: float


def monster_library_directory(project_root: Path | str) -> Path:
    return Path(project_root) / "img" / "monsters"


def atlas_template_directory(project_root: Path | str) -> Path:
    return Path(project_root) / "img" / "自定义"


def _natural_key(value: str) -> tuple:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def scan_monster_library(project_root: Path | str) -> tuple[MonsterGroup, ...]:
    """按 ``img/monsters`` 的真实目录层级扫描图鉴。"""

    root = monster_library_directory(project_root)
    if not root.is_dir():
        return ()

    grouped: dict[str, list[MonsterEntry]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in MONSTER_IMAGE_EXTENSIONS:
            continue
        relative_parent = path.parent.relative_to(root)
        group_name = relative_parent.as_posix() if relative_parent.parts else "."
        grouped.setdefault(group_name, []).append(
            MonsterEntry(group=group_name, name=path.stem, path=path.resolve())
        )

    groups = []
    for group_name in sorted(grouped, key=_natural_key):
        monsters = tuple(sorted(grouped[group_name], key=lambda entry: _natural_key(entry.name)))
        groups.append(MonsterGroup(name=group_name, monsters=monsters))
    return tuple(groups)


def _feature_policy(entry: MonsterEntry) -> dict | None:
    key = "{}/{}".format(entry.group, entry.path.name).replace("\\", "/").casefold()
    return MONSTER_FEATURE_POLICIES.get(key)


def estimated_template_count(
    project_root: Path | str, selected_paths: Iterable[Path | str]
) -> int:
    """不扫描图鉴、不打开图片，按相对路径估算最终模板数量。"""

    library_root = monster_library_directory(Path(project_root).resolve()).resolve()
    total = 0
    for raw_path in selected_paths:
        path = Path(raw_path).resolve()
        try:
            key = path.relative_to(library_root).as_posix().casefold()
        except ValueError:
            total += MAX_TEMPLATES_PER_MONSTER
            continue
        policy = MONSTER_FEATURE_POLICIES.get(key)
        total += len(policy["features"]) * 2 if policy else MAX_TEMPLATES_PER_MONSTER
    return total


def _foreground_mask(rgba: np.ndarray, original_mode: str) -> np.ndarray:
    if original_mode in {"RGBA", "LA", "P"}:
        mask = rgba[:, :, 3] > 24
        if np.count_nonzero(mask):
            return mask

    rgb = rgba[:, :, :3].astype(np.int16)
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    colors, counts = np.unique(border.reshape(-1, 3), axis=0, return_counts=True)
    background = colors[int(np.argmax(counts))]
    distance = np.max(np.abs(rgb - background), axis=2)
    mask = distance > 12
    if np.count_nonzero(mask) < max(8, int(mask.size * 0.03)):
        mask = np.ones(mask.shape, dtype=bool)
    return mask


def _content_bounds(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if not len(xs):
        raise ValueError("怪物图片没有可用的非透明像素")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _window_sizes(content_width: int, content_height: int) -> tuple[tuple[int, int], ...]:
    """生成接近木棉模板风格的单特征窗口，不再生成大块身体裁剪。"""

    eye_width = min(content_width, max(10, min(14, round(content_width * 0.18))))
    eye_height = min(content_height, max(9, min(14, round(content_height * 0.18))))
    mouth_width = min(content_width, max(14, min(26, round(content_width * 0.25))))
    mouth_height = min(content_height, max(8, min(13, round(content_height * 0.15))))
    hair_width = min(content_width, max(12, min(18, round(content_width * 0.20))))
    hair_height = min(content_height, max(10, min(16, round(content_height * 0.20))))
    return tuple(dict.fromkeys(((eye_width, eye_height), (mouth_width, mouth_height), (hair_width, hair_height))))


def _rect_iou(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    overlap_width = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    overlap_height = max(0, min(ay + ah, by + bh) - max(ay, by))
    overlap = overlap_width * overlap_height
    if not overlap:
        return 0.0
    return overlap / float(aw * ah + bw * bh - overlap)


def _integral(values: np.ndarray) -> np.ndarray:
    return np.pad(values.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))


def _rect_sum(integral: np.ndarray, rect: tuple[int, int, int, int]) -> float:
    x, y, width, height = rect
    right = x + width
    bottom = y + height
    return float(
        integral[bottom, right]
        - integral[y, right]
        - integral[bottom, x]
        + integral[y, x]
    )


def _connected_components(binary_mask: np.ndarray):
    """返回小型像素素材的八邻域连通块，不依赖额外图像模型。"""

    height, width = binary_mask.shape
    visited = np.zeros(binary_mask.shape, dtype=bool)
    components = []
    for start_y, start_x in zip(*np.nonzero(binary_mask)):
        if visited[start_y, start_x]:
            continue
        stack = [(int(start_x), int(start_y))]
        visited[start_y, start_x] = True
        xs = []
        ys = []
        while stack:
            x, y = stack.pop()
            xs.append(x)
            ys.append(y)
            for offset_y in (-1, 0, 1):
                for offset_x in (-1, 0, 1):
                    if offset_x == 0 and offset_y == 0:
                        continue
                    neighbor_x = x + offset_x
                    neighbor_y = y + offset_y
                    if (
                        neighbor_x < 0
                        or neighbor_y < 0
                        or neighbor_x >= width
                        or neighbor_y >= height
                        or visited[neighbor_y, neighbor_x]
                        or not binary_mask[neighbor_y, neighbor_x]
                    ):
                        continue
                    visited[neighbor_y, neighbor_x] = True
                    stack.append((neighbor_x, neighbor_y))
        components.append(
            {
                "area": len(xs),
                "left": min(xs),
                "top": min(ys),
                "right": max(xs) + 1,
                "bottom": max(ys) + 1,
                "center_x": sum(xs) / len(xs),
                "center_y": sum(ys) / len(ys),
            }
        )
    return components


def _candidate_score(
    mask_integral: np.ndarray,
    color_integrals: tuple[np.ndarray, ...],
    square_integrals: tuple[np.ndarray, ...],
    edge_integral: np.ndarray,
    edge_count_integral: np.ndarray,
    dark_integral: np.ndarray,
    bright_neutral_integral: np.ndarray,
    colorful_integral: np.ndarray,
    rect: tuple[int, int, int, int],
    content_bounds: tuple[int, int, int, int],
) -> tuple[float, bool, float, float, float] | None:
    x, y, width, height = rect
    opaque_count = _rect_sum(mask_integral, rect)
    occupancy = opaque_count / float(width * height)
    if occupancy < 0.24:
        return None
    if opaque_count < 6:
        return None

    channel_variances = []
    for color_integral, square_integral in zip(color_integrals, square_integrals):
        channel_sum = _rect_sum(color_integral, rect)
        square_sum = _rect_sum(square_integral, rect)
        mean = channel_sum / opaque_count
        channel_variances.append(max(0.0, square_sum / opaque_count - mean * mean))
    color_std = float(np.mean(np.sqrt(channel_variances))) / 64.0
    edge_count = _rect_sum(edge_count_integral, rect)
    edge_score = (
        _rect_sum(edge_integral, rect) / edge_count / 64.0 if edge_count else 0.0
    )
    if color_std < 0.025 and edge_score < 0.025:
        return None

    left, top, right, bottom = content_bounds
    center_y = y + height / 2.0
    center_x = x + width / 2.0
    relative_x = (center_x - left) / max(1.0, right - left)
    relative_y = (center_y - top) / max(1.0, bottom - top)
    in_head = relative_y <= 0.62
    head_bonus = 0.75 if in_head else max(0.0, 0.35 - relative_y * 0.25)
    center_bonus = max(0.0, 0.45 - abs(relative_x - 0.5) * 0.5)
    occupancy_score = 1.0 - abs(occupancy - 0.76)
    dark_fraction = _rect_sum(dark_integral, rect) / opaque_count
    bright_fraction = _rect_sum(bright_neutral_integral, rect) / opaque_count
    colorful_fraction = _rect_sum(colorful_integral, rect) / opaque_count
    eye_score = (
        min(1.0, dark_fraction * 5.0)
        + min(1.0, bright_fraction * 4.0)
        + min(1.0, edge_score)
    )
    mouth_score = eye_score + min(0.8, max(0.0, width / max(1.0, height) - 1.0))
    hair_score = (
        min(1.2, colorful_fraction * 2.4)
        + min(1.0, edge_score)
        + min(1.0, color_std)
    )
    score = (
        edge_score * 2.8
        + color_std * 2.2
        + occupancy_score
        + head_bonus
        + center_bonus
    )
    return score, in_head, eye_score, mouth_score, hair_score


def suggest_feature_templates(
    source: Image.Image,
    count: int = MAX_BASE_FEATURES_PER_MONSTER,
    feature_names: Sequence[str] | None = None,
    crop_overrides: dict[str, tuple[int, int, int, int]] | None = None,
) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
    """按单眼、单嘴、单撮头发等语义位置选择原始像素裁剪。"""

    if count <= 0:
        raise ValueError("模板数量必须大于 0")
    requested_features = tuple(feature_names or DEFAULT_FEATURE_NAMES)[:count]
    if not requested_features:
        raise ValueError("至少需要选择一个单独五官")
    crop_overrides = crop_overrides or {}
    if all(feature_name in crop_overrides for feature_name in requested_features):
        validated = []
        for feature_name in requested_features:
            x, y, width, height = crop_overrides[feature_name]
            if width <= 0 or height <= 0 or x < 0 or y < 0:
                raise ValueError("{} 的人工裁剪区域无效".format(feature_name))
            if x + width > source.width or y + height > source.height:
                raise ValueError("{} 的人工裁剪区域超出怪物图片".format(feature_name))
            validated.append((feature_name, (x, y, width, height)))
        return tuple(validated)
    original_mode = source.mode
    rgba_image = source.convert("RGBA")
    rgba = np.asarray(rgba_image)
    rgb = rgba[:, :, :3]
    mask = _foreground_mask(rgba, original_mode)
    float_mask = mask.astype(np.float64)
    float_rgb = rgb.astype(np.float64)
    mask_integral = _integral(float_mask)
    color_integrals = tuple(
        _integral(float_rgb[:, :, channel] * float_mask) for channel in range(3)
    )
    square_integrals = tuple(
        _integral(float_rgb[:, :, channel] ** 2 * float_mask) for channel in range(3)
    )
    gray = float_rgb.mean(axis=2)
    saturation = float_rgb.max(axis=2) - float_rgb.min(axis=2)
    dark_mask = (gray < 82) & mask
    bright_neutral_mask = (gray > 172) & (saturation < 58) & mask
    colorful_mask = (saturation > 48) & mask
    dark_integral = _integral(dark_mask.astype(np.float64))
    bright_neutral_integral = _integral(bright_neutral_mask.astype(np.float64))
    colorful_integral = _integral(colorful_mask.astype(np.float64))
    edge_values = np.zeros(mask.shape, dtype=np.float64)
    edge_counts = np.zeros(mask.shape, dtype=np.float64)
    horizontal_mask = mask[:, 1:] & mask[:, :-1]
    vertical_mask = mask[1:, :] & mask[:-1, :]
    horizontal_edges = np.abs(gray[:, 1:] - gray[:, :-1]) * horizontal_mask
    vertical_edges = np.abs(gray[1:, :] - gray[:-1, :]) * vertical_mask
    edge_values[:, 1:] += horizontal_edges
    edge_values[1:, :] += vertical_edges
    edge_counts[:, 1:] += horizontal_mask
    edge_counts[1:, :] += vertical_mask
    edge_integral = _integral(edge_values)
    edge_count_integral = _integral(edge_counts)
    bounds = _content_bounds(mask)
    left, top, right, bottom = bounds
    content_width = right - left
    content_height = bottom - top
    candidates: list[_CropCandidate] = []

    for width, height in _window_sizes(content_width, content_height):
        maximum_x = max(left, right - width)
        maximum_y = max(top, bottom - height)
        # 每档限制在约 17x19 个采样点；大怪物不需要逐像素滑窗，
        # 否则一次多选几十只时会让图鉴对话框等待过久。
        step_x = max(2, width // 4, math.ceil(maximum_x - left + 1) // 17)
        step_y = max(2, height // 4, math.ceil(maximum_y - top + 1) // 19)
        x_positions = list(range(left, maximum_x + 1, step_x))
        y_positions = list(range(top, maximum_y + 1, step_y))
        if not x_positions or x_positions[-1] != maximum_x:
            x_positions.append(maximum_x)
        if not y_positions or y_positions[-1] != maximum_y:
            y_positions.append(maximum_y)
        for y in y_positions:
            for x in x_positions:
                rect = (x, y, width, height)
                scored = _candidate_score(
                    mask_integral,
                    color_integrals,
                    square_integrals,
                    edge_integral,
                    edge_count_integral,
                    dark_integral,
                    bright_neutral_integral,
                    colorful_integral,
                    rect,
                    bounds,
                )
                if scored is None:
                    continue
                score, in_head, eye_score, mouth_score, hair_score = scored
                center_x = x + width / 2.0
                center_y = y + height / 2.0
                candidates.append(
                    _CropCandidate(
                        rect=rect,
                        score=score,
                        in_head=in_head,
                        relative_x=(center_x - left) / max(1.0, content_width),
                        relative_y=(center_y - top) / max(1.0, content_height),
                        eye_score=eye_score,
                        mouth_score=mouth_score,
                        hair_score=hair_score,
                    )
                )

    if not candidates:
        raise ValueError("怪物图片过于透明或颜色过于单一，无法提取有效特征")
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)

    # 先从白色小块（眼白）和黑色小块（瞳孔/眼线）中寻找真正的单眼中心。
    # 这比单纯挑选最高对比边缘更接近木棉目录中的眼睛裁剪风格。
    eye_components = []
    content_area = max(1, content_width * content_height)
    for component_kind, component_mask, opposite_integral in (
        ("bright", bright_neutral_mask, dark_integral),
        ("dark", dark_mask, bright_neutral_integral),
    ):
        for component in _connected_components(component_mask):
            component_width = component["right"] - component["left"]
            component_height = component["bottom"] - component["top"]
            if (
                component["area"] < 2
                or component["area"] > max(90, int(content_area * 0.035))
                or component_width > 22
                or component_height > 22
            ):
                continue
            relative_x = (component["center_x"] - left) / max(1.0, content_width)
            relative_y = (component["center_y"] - top) / max(1.0, content_height)
            if relative_y > 0.72:
                continue
            padding_x = max(4, component_width // 2 + 3)
            padding_y = max(4, component_height // 2 + 3)
            sample_left = max(left, int(component["center_x"] - padding_x))
            sample_top = max(top, int(component["center_y"] - padding_y))
            sample_right = min(right, int(component["center_x"] + padding_x + 1))
            sample_bottom = min(bottom, int(component["center_y"] + padding_y + 1))
            sample_rect = (
                sample_left,
                sample_top,
                max(1, sample_right - sample_left),
                max(1, sample_bottom - sample_top),
            )
            opposite_fraction = _rect_sum(opposite_integral, sample_rect) / max(
                1.0, sample_rect[2] * sample_rect[3]
            )
            if opposite_fraction < 0.025:
                continue
            foreground_fraction = _rect_sum(mask_integral, sample_rect) / max(
                1.0, sample_rect[2] * sample_rect[3]
            )
            compactness = component["area"] / max(1.0, component_width * component_height)
            boundary_penalty = (
                1.2
                if relative_x < 0.06 or relative_x > 0.94 or relative_y > 0.64
                else 0.0
            )
            if foreground_fraction < 0.68:
                boundary_penalty += (0.68 - foreground_fraction) * 4.0
            size_bonus = (
                1.0
                if component["area"] <= 30 and component_width <= 12 and component_height <= 12
                else -min(1.2, max(0, component["area"] - 30) / 45.0)
            )
            eye_components.append(
                {
                    "kind": component_kind,
                    "x": relative_x,
                    "y": relative_y,
                    "score": (
                        opposite_fraction * 5.0
                        + compactness
                        + foreground_fraction * 1.8
                        + size_bonus
                        + max(0.0, 0.9 - relative_y * 0.6)
                        - boundary_penalty
                    ),
                    "width": component_width,
                    "height": component_height,
                }
            )

    eye_components.sort(key=lambda component: component["score"], reverse=True)
    face_seed = max(
        candidates,
        key=lambda candidate: (
            candidate.eye_score * 2.2
            + candidate.score * 0.45
            - abs(candidate.relative_y - 0.36) * 2.2
            - max(0.0, candidate.relative_y - 0.62) * 5.0
        ),
    )
    if eye_components:
        valid_pairs = []
        # 两个眼白被瞳孔切开后可能形成相邻小连通块；间距过小的组合其实是
        # 同一只眼睛，不能当作左右眼。较宽的上限则兼容眼距较大的怪物。
        minimum_eye_gap = max(0.10, 7.0 / max(1.0, content_width))
        for first_index, first in enumerate(eye_components):
            for second in eye_components[first_index + 1 :]:
                gap_x = abs(first["x"] - second["x"])
                gap_y = abs(first["y"] - second["y"])
                if (
                    first["kind"] != second["kind"]
                    or gap_y > 0.12
                    or gap_x < minimum_eye_gap
                    or gap_x > 0.58
                ):
                    continue
                pair_center_x = (first["x"] + second["x"]) / 2.0
                pair_score = (
                    first["score"]
                    + second["score"]
                    + max(0.0, 1.5 - abs(gap_x - 0.30) * 4.0)
                    + max(0.0, 0.8 - abs(pair_center_x - 0.5))
                    - gap_y * 3.0
                )
                valid_pairs.append((pair_score, first, second))
        if valid_pairs:
            _pair_score, first_eye, second_eye = max(valid_pairs, key=lambda item: item[0])
            left_eye_x, right_eye_x = sorted((first_eye["x"], second_eye["x"]))
            eye_y = (first_eye["y"] + second_eye["y"]) / 2.0
            face_x = (left_eye_x + right_eye_x) / 2.0
        else:
            primary_eye = eye_components[0]
            face_x = primary_eye["x"]
            eye_y = primary_eye["y"]
            left_eye_x = face_x - 0.065
            right_eye_x = face_x + 0.065
    else:
        face_x = face_seed.relative_x
        eye_y = face_seed.relative_y
        left_eye_x = face_x - 0.075
        right_eye_x = face_x + 0.075
    face_x = min(0.88, max(0.12, face_x))
    eye_y = min(0.64, max(0.10, eye_y))

    # 在眼睛下方寻找横向黑色连通块作为嘴巴/牙齿中心。
    mouth_components = []
    for component in _connected_components(dark_mask):
        component_width = component["right"] - component["left"]
        component_height = component["bottom"] - component["top"]
        relative_x = (component["center_x"] - left) / max(1.0, content_width)
        relative_y = (component["center_y"] - top) / max(1.0, content_height)
        if (
            component["area"] < 2
            or component_width < component_height
            or relative_y < eye_y + 0.075
            or relative_y > min(0.78, eye_y + 0.30)
            or abs(relative_x - face_x) > 0.24
        ):
            continue
        mouth_components.append(
            (
                component_width / max(1.0, component_height)
                + component["area"] / 20.0
                - abs(relative_x - face_x) * 2.0,
                relative_x,
                relative_y,
            )
        )
    if mouth_components:
        _mouth_score, mouth_x, mouth_y = max(mouth_components)
    else:
        mouth_x = face_x
        mouth_y = min(0.74, eye_y + 0.17)

    feature_profiles = (
        ("left_eye", left_eye_x, eye_y, "eye"),
        ("right_eye", right_eye_x, eye_y, "eye"),
        ("mouth", mouth_x, mouth_y, "mouth"),
        ("nose_or_teeth", face_x, min(0.69, eye_y + 0.09), "mouth"),
        ("left_hair", face_x - 0.14, max(0.05, eye_y - 0.18), "hair"),
        ("center_hair", face_x, max(0.04, eye_y - 0.21), "hair"),
        ("right_hair", face_x + 0.14, max(0.05, eye_y - 0.18), "hair"),
        ("head_ornament", face_x + 0.20, max(0.06, eye_y - 0.11), "hair"),
    )
    profiles_by_name = {profile[0]: profile for profile in feature_profiles}
    selected: list[tuple[str, _CropCandidate]] = []
    selected_rects: list[tuple[str, tuple[int, int, int, int]]] = []

    def semantic_score(candidate: _CropCandidate, kind: str) -> float:
        if kind == "eye":
            return candidate.eye_score
        if kind == "mouth":
            return candidate.mouth_score
        return candidate.hair_score

    def matches_feature_shape(candidate: _CropCandidate, kind: str) -> bool:
        _x, _y, width, height = candidate.rect
        if kind == "eye":
            return width <= 15 and height <= 15 and width / max(1.0, height) <= 1.45
        if kind == "mouth":
            return width >= 14 and width / max(1.0, height) >= 1.15
        return width <= 18 and height <= 16

    for feature_name in requested_features:
        override_rect = crop_overrides.get(feature_name)
        if override_rect is not None:
            x, y, width, height = override_rect
            if width <= 0 or height <= 0 or x < 0 or y < 0:
                raise ValueError("{} 的人工裁剪区域无效".format(feature_name))
            if x + width > source.width or y + height > source.height:
                raise ValueError("{} 的人工裁剪区域超出怪物图片".format(feature_name))
            selected_rects.append((feature_name, override_rect))
            continue
        profile = profiles_by_name.get(feature_name)
        if profile is None:
            continue
        _feature_name, target_x, target_y, feature_kind = profile
        target_x = min(0.92, max(0.08, target_x))
        target_y = min(0.72, max(0.06, target_y))
        ranked = sorted(
            (candidate for candidate in candidates if matches_feature_shape(candidate, feature_kind)),
            key=lambda candidate: (
                semantic_score(candidate, feature_kind) * 2.2
                + candidate.score * 0.55
                + max(
                    0.0,
                    4.2
                    - math.hypot(
                        (candidate.relative_x - target_x) / 0.18,
                        (candidate.relative_y - target_y) / 0.20,
                    )
                    * 2.0,
                )
                + (
                    max(0.0, candidate.rect[2] / max(1.0, candidate.rect[3]) - 1.1)
                    if feature_kind == "mouth"
                    else 0.0
                )
                - (0.8 if not candidate.in_head else 0.0)
            ),
            reverse=True,
        )
        chosen = next(
            (
                candidate
                for candidate in ranked
                if candidate.rect not in {item.rect for _, item in selected}
                and all(
                    _rect_iou(candidate.rect, item.rect) <= 0.62
                    for _, item in selected
                )
            ),
            None,
        )
        if chosen is not None:
            selected.append((feature_name, chosen))
    selected_rects.extend((name, candidate.rect) for name, candidate in selected)
    if not selected_rects:
        raise ValueError("怪物图片没有找到可靠的单独五官")
    return tuple(selected_rects)


def suggest_feature_crops(
    source: Image.Image, count: int = MAX_BASE_FEATURES_PER_MONSTER
) -> tuple[tuple[int, int, int, int], ...]:
    """兼容旧调用，只返回单特征裁剪矩形。"""

    return tuple(rect for _name, rect in suggest_feature_templates(source, count))


def _root_guai_files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file()
                and path.name.casefold().startswith("guai")
                and path.suffix.casefold() in MONSTER_IMAGE_EXTENSIONS
            ),
            key=lambda path: _natural_key(path.name),
        )
    )


def _save_contact_sheet(
    crops: Sequence[tuple[str, Image.Image]], output_path: Path
) -> None:
    if not crops:
        return
    columns = 8
    cell_width = 116
    cell_height = 92
    rows = math.ceil(len(crops) / columns)
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), (42, 42, 42))
    draw = ImageDraw.Draw(sheet)
    for index, (name, crop) in enumerate(crops):
        column = index % columns
        row = index // columns
        left = column * cell_width
        top = row * cell_height
        draw.text((left + 4, top + 3), name, fill=(245, 245, 245))
        scale = max(1, min(4, 76 // max(1, crop.height), 104 // max(1, crop.width)))
        preview = crop.convert("RGBA").resize(
            (crop.width * scale, crop.height * scale), Image.Resampling.NEAREST
        )
        background = Image.new("RGBA", preview.size, (24, 24, 24, 255))
        background.alpha_composite(preview)
        sheet.paste(background.convert("RGB"), (left + 4, top + 20))
    sheet.save(output_path, format="PNG")


def _validated_selection(
    project_root: Path, selected_paths: Iterable[Path | str]
) -> tuple[MonsterEntry, ...]:
    groups = scan_monster_library(project_root)
    available = {
        entry.path.resolve(): entry for group in groups for entry in group.monsters
    }
    selected_resolved = {Path(path).resolve() for path in selected_paths}
    if not selected_resolved:
        raise ValueError("请至少选择一只怪物")
    unknown = selected_resolved.difference(available)
    if unknown:
        raise ValueError("所选怪物不在 img\\monsters 图鉴目录中：{}".format(next(iter(unknown))))
    # 按图鉴显示顺序生成，避免勾选顺序导致 guai 编号随机变化。
    return tuple(
        entry
        for group in groups
        for entry in group.monsters
        if entry.path.resolve() in selected_resolved
    )


def generate_monster_templates(
    project_root: Path | str,
    selected_paths: Iterable[Path | str],
    templates_per_monster: int = MAX_TEMPLATES_PER_MONSTER,
) -> MonsterTemplateReport:
    """生成并安全替换根目录 ``img/自定义/guai*``。

    所有新模板先在临时目录完成并校验；只有全部成功后，才移走旧模板并
    安装新模板。子目录中的 ``guai*`` 永远不会被递归删除。
    """

    project_root = Path(project_root).resolve()
    selected = _validated_selection(project_root, selected_paths)
    if (
        templates_per_monster < 2
        or templates_per_monster > MAX_TEMPLATES_PER_MONSTER
        or templates_per_monster % 2
    ):
        raise ValueError(
            "每只怪物的模板上限必须是 2～{} 之间的偶数，确保原图和镜像成对生成".format(
                MAX_TEMPLATES_PER_MONSTER
            )
        )
    base_feature_limit = max(1, min(MAX_BASE_FEATURES_PER_MONSTER, templates_per_monster // 2))

    output_directory = atlas_template_directory(project_root)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_parent = output_directory.parent

    with tempfile.TemporaryDirectory(prefix="monster_atlas_", dir=output_parent) as raw_temp:
        temp_root = Path(raw_temp)
        staging = temp_root / "new"
        backup = temp_root / "old"
        source_staging = temp_root / "source_assets"
        staging.mkdir()
        backup.mkdir()
        source_staging.mkdir()
        manifest_entries = []
        previews: list[tuple[str, Image.Image]] = []
        next_index = 1

        try:
            for entry in selected:
                with Image.open(entry.path) as source:
                    source.load()
                    group_directory = (
                        source_staging
                        if entry.group == "."
                        else source_staging / Path(entry.group)
                    )
                    group_directory.mkdir(parents=True, exist_ok=True)
                    original_asset = group_directory / "{}_原图.png".format(entry.name)
                    mirrored_asset = group_directory / "{}_镜像.png".format(entry.name)
                    source.copy().save(original_asset, format="PNG")
                    mirrored = ImageOps.mirror(source)
                    mirrored.save(mirrored_asset, format="PNG")
                    policy = _feature_policy(entry)
                    requested_features = (
                        tuple(policy["features"])
                        if policy
                        else DEFAULT_FEATURE_NAMES[:base_feature_limit]
                    )
                    feature_templates = suggest_feature_templates(
                        source,
                        count=min(base_feature_limit, len(requested_features)),
                        feature_names=requested_features,
                        crop_overrides=dict(policy.get("crops", {})) if policy else None,
                    )
                    generated_names = []
                    feature_names = []
                    rects = []
                    template_sources = []
                    for feature_name, rect in feature_templates:
                        x, y, width, height = rect
                        crop = source.crop((x, y, x + width, y + height)).copy()
                        mirrored_rect = (source.width - x - width, y, width, height)
                        mirrored_crop = mirrored.crop(
                            (
                                mirrored_rect[0],
                                mirrored_rect[1],
                                mirrored_rect[0] + width,
                                mirrored_rect[1] + height,
                            )
                        ).copy()
                        for variant_name, variant_crop, variant_rect, variant_source in (
                            (feature_name, crop, rect, "original"),
                            (feature_name + "_mirrored", mirrored_crop, mirrored_rect, "mirrored"),
                        ):
                            if len(generated_names) >= templates_per_monster:
                                break
                            output_name = "guai{}.png".format(next_index)
                            variant_crop.save(staging / output_name, format="PNG")
                            previews.append((output_name, variant_crop.copy()))
                            generated_names.append(output_name)
                            feature_names.append(variant_name)
                            rects.append(variant_rect)
                            template_sources.append(variant_source)
                            next_index += 1
                        crop.close()
                        mirrored_crop.close()
                    mirrored.close()
                manifest_entries.append(
                    {
                        "group": entry.group,
                        "monster": entry.name,
                        "source": str(entry.path.relative_to(project_root)),
                        "templates": generated_names,
                        "features": feature_names,
                        "crops": [list(rect) for rect in rects],
                        "template_sources": template_sources,
                        "policy": policy.get("reason") if policy else "自动选择单个五官",
                        "manual_assets": {
                            "original": str(
                                Path(SOURCE_ASSET_DIRECTORY_NAME)
                                / original_asset.relative_to(source_staging)
                            ),
                            "mirrored": str(
                                Path(SOURCE_ASSET_DIRECTORY_NAME)
                                / mirrored_asset.relative_to(source_staging)
                            ),
                        },
                    }
                )

            expected_count = next_index - 1
            staged_files = tuple(sorted(staging.glob("guai*.png"), key=lambda p: _natural_key(p.name)))
            if len(staged_files) != expected_count:
                raise RuntimeError(
                    "模板生成数量不正确：预期 {}，实际 {}".format(expected_count, len(staged_files))
                )
            for staged_file in staged_files:
                with Image.open(staged_file) as image:
                    image.verify()

            manifest_data = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "source_directory": str(monster_library_directory(project_root)),
                "output_directory": str(output_directory),
                "max_templates_per_monster": templates_per_monster,
                "selected_monster_count": len(selected),
                "template_count": expected_count,
                "feature_priority": "单个五官优先；每个原图特征同时生成水平镜像，最多 12 张",
                "monsters": manifest_entries,
            }
            staging_manifest = staging / MANIFEST_FILE_NAME
            staging_manifest.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            staging_preview = staging / PREVIEW_FILE_NAME
            _save_contact_sheet(previews, staging_preview)
        finally:
            for _, preview in previews:
                preview.close()

        old_templates = _root_guai_files(output_directory)
        installed: list[Path] = []
        try:
            # 原图与镜像图不以 guai 开头，检测器不会加载；保留给用户手动截图。
            source_asset_directory = output_directory / SOURCE_ASSET_DIRECTORY_NAME
            for staged_asset in source_staging.rglob("*.png"):
                asset_target = source_asset_directory / staged_asset.relative_to(source_staging)
                asset_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_asset, asset_target)
            for old_file in old_templates:
                old_file.replace(backup / old_file.name)
            for staged_file in staged_files:
                target = output_directory / staged_file.name
                staged_file.replace(target)
                installed.append(target)
            (staging / MANIFEST_FILE_NAME).replace(output_directory / MANIFEST_FILE_NAME)
            (staging / PREVIEW_FILE_NAME).replace(output_directory / PREVIEW_FILE_NAME)
        except Exception:
            for target in installed:
                if target.exists():
                    target.unlink()
            for old_file in backup.iterdir():
                old_file.replace(output_directory / old_file.name)
            raise

    template_files = tuple(output_directory / "guai{}.png".format(index) for index in range(1, next_index))
    return MonsterTemplateReport(
        output_directory=output_directory,
        selected_monsters=selected,
        template_files=template_files,
        manifest_file=output_directory / MANIFEST_FILE_NAME,
        preview_file=output_directory / PREVIEW_FILE_NAME,
        source_asset_directory=output_directory / SOURCE_ASSET_DIRECTORY_NAME,
        templates_per_monster=templates_per_monster,
    )


__all__ = (
    "MANIFEST_FILE_NAME",
    "MAX_BASE_FEATURES_PER_MONSTER",
    "MAX_TEMPLATES_PER_MONSTER",
    "MONSTER_IMAGE_EXTENSIONS",
    "PREVIEW_FILE_NAME",
    "SOURCE_ASSET_DIRECTORY_NAME",
    "TEMPLATES_PER_MONSTER",
    "MonsterEntry",
    "MonsterGroup",
    "MonsterTemplateReport",
    "atlas_template_directory",
    "estimated_template_count",
    "generate_monster_templates",
    "monster_library_directory",
    "scan_monster_library",
    "suggest_feature_crops",
    "suggest_feature_templates",
)

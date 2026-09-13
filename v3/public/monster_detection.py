"""怪物模板、血条、YOLO节奏和自定义检测资源的公共实现。"""

import shutil
import sys
from pathlib import Path
from typing import FrozenSet, NamedTuple, Optional, Tuple

from ..config import default_config_path


CUSTOM_TEMPLATE_RELATIVE_DIRECTORY = Path("img") / "自定义"
CUSTOM_TEMPLATE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
)

# 生产模式和测试模式必须使用同一阈值，否则测试画面看起来正常，实际运行时却会
# 接受更多低置信度目标。0.65 比 Ultralytics 默认阈值更适合过滤死亡残影，同时
# 仍给不同地图的旧模型保留一定容错空间。
YOLO_MONSTER_CONFIDENCE = 0.65
YOLO_MAX_DETECTIONS = 30
# 正常自动化仍保持25Hz目标节奏，避免检测线程持续占满CPU/GPU影响路线按键。
YOLO_TARGET_FRAME_SECONDS = 0.04
# 截图测试模式允许完整自动化的YOLO以60Hz目标节奏推理；实际FPS仍受耗时限制。
TEST_YOLO_TARGET_FRAME_SECONDS = 1.0 / 60.0
TEMPLATE_TARGET_FRAME_SECONDS = 0.05
MONSTER_TEMPLATE_CONFIDENCE = 0.75
# 灰度只用于快速筛选可能位置，最终仍使用原彩色模板按正式阈值确认。
# 0.65 给颜色相关性达到0.75的候选保留足够余量，同时能过滤绝大多数背景位置。
MONSTER_GRAY_PREFILTER_CONFIDENCE = 0.65
MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET = 48.0
# 同一只怪物通常会被帽子、脸部等多张局部模板同时命中。不同模板的命中中心
# 可能相差二三十像素，使用较大的跨模板去重范围可避免把一只怪物计成多个目标；
# 同一模板仍沿用5px范围，避免合并真正并排的怪物。
MONSTER_SAME_TEMPLATE_DEDUP_PIXELS = 5
MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_X = 32
MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_Y = 32

# 一次攻击完成后继续截图和识别，但短时间内不再次发布攻击指令。这样路线线程会
# 原地等待复检，死亡动画消失后即可恢复移动；怪物仍存活时则在窗口结束后继续攻击。
# 日志实测 0.55 秒冷却叠加下一帧检测后，连续攻击最短约 0.70 秒。缩短到
# 0.45 秒以改善连击节奏，死亡动画仍由置信度、旧意图清理和战斗确认共同过滤。
POST_ATTACK_RECHECK_SECONDS = 0.45
ATTACK_INTENT_TTL_SECONDS = 0.30
COMBAT_TARGET_LOST_CONFIRMATION_FRAMES = 3
COMBAT_RELEASE_GRACE_SECONDS = 0.75
ATTACK_MOVE_LOCK_SECONDS = 0.75


class TemplateMatch(NamedTuple):
    local_box: Tuple[int, int, int, int]
    output_box: Tuple[int, int, int, int]
    center: Tuple[float, float]
    confidence: float


class PreparedMonsterTemplate(NamedTuple):
    """缓存怪物模板的彩色原图和灰度预筛图。"""

    color: object
    gray: object


class MonsterHealthBarMatch(NamedTuple):
    """描述一条仍含绿色血量的怪物血条位置和剩余比例。"""

    local_box: Tuple[int, int, int, int]
    output_box: Tuple[int, int, int, int]
    center: Tuple[float, float]
    green_width: int
    green_ratio: float


class MonsterHealthBarAssociation(NamedTuple):
    """描述血条与模板目标关联后的中心、活怪索引和自适应纵向偏移。"""

    centers: Tuple[Tuple[float, float], ...]
    alive_center_indices: FrozenSet[int]
    linked_count: int
    inferred_count: int
    y_offset_estimate: float


class MonsterHealthBarDetectionResult(NamedTuple):
    """公共血条检测结果，包含模板关联、漏检补点和诊断统计。"""

    matches: Tuple[MonsterHealthBarMatch, ...]
    centers: Tuple[Tuple[float, float], ...]
    alive_center_indices: FrozenSet[int]
    linked_count: int
    inferred_count: int
    y_offset_estimate: float
    max_green_ratio: Optional[float]


def detect_monster_health_bars(
    cv2,
    np,
    frame,
    x_offset: int = 0,
    y_offset: int = 0,
) -> Tuple[MonsterHealthBarMatch, ...]:
    """识别51×8怪物血条，并用荧光绿、白色外框和黑色内框排除树叶。

    绿色长度允许从1像素到满条变化；只有同时符合固定血条边框结构时才返回活怪
    证据。远处绿树、草地和技能特效即使颜色接近，也不会同时具备白色斜面外框
    与完整黑色内框。空血条没有荧光绿色像素，不会命中。
    """
    if frame is None or frame.size == 0:
        return ()

    # 血条绿在原始截图中接近 BGR(0,243,0)。直接约束颜色通道，比宽泛的HSV
    # 绿色范围更能排除树叶的黄绿色、暗绿色和带蓝绿色。
    blue = frame[:, :, 0].astype(np.int16)
    green = frame[:, :, 1].astype(np.int16)
    red = frame[:, :, 2].astype(np.int16)
    bright_green_mask = np.where(
        (
            (green >= 220)
            & (blue <= 50)
            & (red <= 50)
            & (green - blue >= 170)
            & (green - red >= 170)
        ),
        255,
        0,
    ).astype(np.uint8)
    tolerant_green_mask = np.where(
        (
            (green >= 185)
            & (blue <= 90)
            & (red <= 90)
            & (green - blue >= 110)
            & (green - red >= 110)
        ),
        255,
        0,
    ).astype(np.uint8)
    # 低血量时亮绿色可能只剩1像素，必须保留；较暗的截图颜色则要求至少形成
    # 3像素水平连续条，避免树叶、木纹和技能特效中的零散绿色拖慢候选验证。
    tolerant_green_mask = cv2.morphologyEx(
        tolerant_green_mask,
        cv2.MORPH_OPEN,
        np.ones((1, 3), dtype=np.uint8),
    )
    green_mask = cv2.bitwise_or(bright_green_mask, tolerant_green_mask)
    green_mask = cv2.morphologyEx(
        green_mask,
        cv2.MORPH_CLOSE,
        np.ones((1, 3), dtype=np.uint8),
    )
    contours, _hierarchy = cv2.findContours(
        green_mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    frame_height, frame_width = frame.shape[:2]
    pixel_max = np.max(frame, axis=2)
    pixel_min = np.min(frame, axis=2)
    dark_mask = pixel_max <= 85
    inner_dark_mask = pixel_max <= 125
    light_mask = pixel_min >= 145
    light_row_prefix = np.pad(
        np.cumsum(light_mask, axis=1, dtype=np.int16),
        ((0, 0), (1, 0)),
    )
    matches = []
    accepted_centers = []
    for contour in contours:
        green_x, green_y, green_width, green_height = cv2.boundingRect(contour)
        if not (1 <= green_width <= 48 and 1 <= green_height <= 6):
            continue

        best_candidate = None
        best_score = -1.0
        for left_inset in (2, 3, 4, 5):
            for top_inset in (2, 3, 4):
                bar_x = green_x - left_inset
                bar_y = green_y - top_inset
                bar_width = 51
                bar_height = 8
                if (
                    bar_x < 0
                    or bar_y < 0
                    or bar_x + bar_width > frame_width
                    or bar_y + bar_height > frame_height
                ):
                    continue
                top_light_count = int(
                    light_row_prefix[bar_y, bar_x + 51]
                    - light_row_prefix[bar_y, bar_x + 1]
                )
                bottom_light_count = int(
                    light_row_prefix[bar_y + 7, bar_x + 51]
                    - light_row_prefix[bar_y + 7, bar_x + 1]
                )
                if top_light_count < 30 or bottom_light_count < 30:
                    continue
                bar_green = green_mask[
                    bar_y:bar_y + bar_height,
                    bar_x:bar_x + bar_width,
                ]
                dark_pixels = dark_mask[
                    bar_y:bar_y + bar_height,
                    bar_x:bar_x + bar_width,
                ]
                inner_dark_pixels = inner_dark_mask[
                    bar_y:bar_y + bar_height,
                    bar_x:bar_x + bar_width,
                ]
                light_pixels = light_mask[
                    bar_y:bar_y + bar_height,
                    bar_x:bar_x + bar_width,
                ]
                left_dark_ratio = float(np.mean(dark_pixels[:, 0]))
                top_bevel_light_ratio = float(np.mean(light_pixels[0, 1:]))
                bottom_bevel_light_ratio = float(np.mean(light_pixels[-1, 1:]))
                right_bevel_light_ratio = float(np.mean(light_pixels[:, -1]))
                inner_top_dark_ratio = float(np.mean(inner_dark_pixels[1, 2:50]))
                inner_bottom_dark_ratio = float(np.mean(inner_dark_pixels[6, 2:50]))
                inner_left_dark_ratio = float(np.mean(inner_dark_pixels[1:7, 2]))
                inner_right_dark_ratio = float(
                    np.mean(inner_dark_pixels[1:7, 48:50])
                )
                inner_green_count = int(np.count_nonzero(bar_green[2:6, 3:48]))
                if inner_green_count <= 0:
                    continue
                if (
                    left_dark_ratio < 0.70
                    or top_bevel_light_ratio < 0.64
                    or bottom_bevel_light_ratio < 0.64
                    or right_bevel_light_ratio < 0.50
                    or inner_top_dark_ratio < 0.62
                    or inner_bottom_dark_ratio < 0.62
                    or inner_left_dark_ratio < 0.62
                    or inner_right_dark_ratio < 0.50
                ):
                    continue
                score = (
                    left_dark_ratio * 0.12
                    + top_bevel_light_ratio * 0.18
                    + bottom_bevel_light_ratio * 0.18
                    + right_bevel_light_ratio * 0.14
                    + inner_top_dark_ratio * 0.12
                    + inner_bottom_dark_ratio * 0.12
                    + inner_left_dark_ratio * 0.07
                    + inner_right_dark_ratio * 0.07
                )
                if score < 0.68:
                    continue
                if score > best_score:
                    best_score = score
                    best_candidate = (
                        bar_x,
                        bar_y,
                        bar_width,
                        bar_height,
                        inner_green_count,
                    )

        if best_candidate is None:
            continue
        bar_x, bar_y, bar_width, bar_height, inner_green_count = best_candidate
        center = (
            x_offset + bar_x + bar_width / 2.0,
            y_offset + bar_y + bar_height / 2.0,
        )
        if any(
            abs(old_x - center[0]) < 10 and abs(old_y - center[1]) < 6
            for old_x, old_y in accepted_centers
        ):
            continue
        accepted_centers.append(center)
        matches.append(
            MonsterHealthBarMatch(
                (bar_x, bar_y, bar_x + bar_width, bar_y + bar_height),
                (
                    x_offset + bar_x,
                    y_offset + bar_y,
                    x_offset + bar_x + bar_width,
                    y_offset + bar_y + bar_height,
                ),
                center,
                int(green_width),
                min(1.0, inner_green_count / float(45 * 4)),
            )
        )
    return tuple(matches)


def associate_monster_health_bars(
    centers,
    health_bar_matches,
    y_offset_estimate: float = MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET,
) -> MonsterHealthBarAssociation:
    """把绿色血条关联到模板中心，并为模板漏检的活怪推断中心位置。"""
    updated_centers = [
        (float(center[0]), float(center[1]))
        for center in centers
    ]
    alive_center_indices = set()
    used_center_indices = set()
    linked_count = 0
    inferred_count = 0
    offset_estimate = min(96.0, max(24.0, float(y_offset_estimate)))

    for health_bar in health_bar_matches:
        best_center_index = None
        best_score = float("inf")
        for center_index, (target_x, target_y) in enumerate(updated_centers):
            if center_index in used_center_indices:
                continue
            horizontal_delta = abs(float(target_x) - float(health_bar.center[0]))
            vertical_delta = float(target_y) - float(health_bar.center[1])
            predicted_vertical_delta = abs(vertical_delta - offset_estimate)
            if (
                horizontal_delta > 55
                or not (5 <= vertical_delta <= 120)
                or predicted_vertical_delta > 50
            ):
                continue
            score = horizontal_delta + predicted_vertical_delta * 0.45
            if score < best_score:
                best_score = score
                best_center_index = center_index

        if best_center_index is not None:
            used_center_indices.add(best_center_index)
            alive_center_indices.add(best_center_index)
            actual_offset = min(110.0, max(20.0, (
                updated_centers[best_center_index][1]
                - float(health_bar.center[1])
            )))
            offset_estimate = min(
                96.0,
                max(24.0, offset_estimate * 0.65 + actual_offset * 0.35),
            )
            linked_count += 1
            continue

        inferred_center = (
            float(health_bar.center[0]),
            float(health_bar.center[1]) + offset_estimate,
        )
        duplicate_index = next(
            (
                center_index
                for center_index, center in enumerate(updated_centers)
                if abs(center[0] - inferred_center[0]) < 36
                and abs(center[1] - inferred_center[1]) < 28
            ),
            None,
        )
        if duplicate_index is not None:
            alive_center_indices.add(duplicate_index)
            continue

        updated_centers.append(inferred_center)
        inferred_index = len(updated_centers) - 1
        used_center_indices.add(inferred_index)
        alive_center_indices.add(inferred_index)
        inferred_count += 1

    return MonsterHealthBarAssociation(
        tuple(updated_centers),
        frozenset(alive_center_indices),
        linked_count,
        inferred_count,
        offset_estimate,
    )


def detect_and_associate_monster_health_bars(
    cv2,
    np,
    frame,
    centers,
    y_offset_estimate: float = MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET,
    x_offset: int = 0,
    y_offset: int = 0,
) -> MonsterHealthBarDetectionResult:
    """统一执行血条检测、模板关联和模板漏检怪物补点。

    蘑菇V3、自定义录制路线以及其他通用战斗入口都通过该函数取得
    完全一致的活怪中心和诊断数据，避免各检测循环分别维护一套血条逻辑。
    """
    matches = detect_monster_health_bars(
        cv2,
        np,
        frame,
        x_offset=x_offset,
        y_offset=y_offset,
    )
    association = associate_monster_health_bars(
        centers,
        matches,
        y_offset_estimate,
    )
    max_green_ratio = (
        max(match.green_ratio for match in matches)
        if matches
        else None
    )
    return MonsterHealthBarDetectionResult(
        matches=matches,
        centers=association.centers,
        alive_center_indices=association.alive_center_indices,
        linked_count=association.linked_count,
        inferred_count=association.inferred_count,
        y_offset_estimate=association.y_offset_estimate,
        max_green_ratio=max_green_ratio,
    )


def effective_detector(default_detector: str, use_custom_templates: bool) -> str:
    """自定义模板开启时覆盖地图原检测方式，但不改变地图路线和攻击逻辑。"""
    return "template" if use_custom_templates else default_detector


def custom_template_directory(base_dir=None) -> Path:
    """返回固定的可写自定义模板目录；打包后位于程序文件旁边。"""
    root = Path(base_dir) if base_dir is not None else default_config_path().parent
    return root / CUSTOM_TEMPLATE_RELATIVE_DIRECTORY


def custom_template_files(directory: Path) -> Tuple[Path, ...]:
    """读取当前目录内所有 ``guai*`` 常见图片，不要求编号连续。"""

    def sort_key(path: Path):
        """让guai2排在guai10前，自定义后缀则按文件名排序。"""
        suffix = path.stem[4:]
        return (0, int(suffix), path.name.lower()) if suffix.isdigit() else (
            1,
            0,
            path.name.lower(),
        )

    root = Path(directory)
    if not root.is_dir():
        return ()
    files = (
        path
        for path in root.iterdir()
        if path.is_file()
        and path.name.lower().startswith("guai")
        and path.suffix.lower() in CUSTOM_TEMPLATE_EXTENSIONS
    )
    return tuple(sorted(files, key=sort_key))


def prepare_monster_template(cv2, template):
    """预生成灰度模板，同时保留彩色模板用于最终置信度确认。"""
    if template is None:
        return None
    if len(template.shape) == 2:
        gray = template
    else:
        conversion = (
            cv2.COLOR_BGRA2GRAY
            if template.shape[2] == 4
            else cv2.COLOR_BGR2GRAY
        )
        gray = cv2.cvtColor(template, conversion)
    return PreparedMonsterTemplate(template, gray)


def _monster_frame_gray(cv2, frame):
    """把一帧转换为灰度；已经是灰度图时直接复用原数组。"""
    if len(frame.shape) == 2:
        return frame
    conversion = cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY
    return cv2.cvtColor(frame, conversion)


def _monster_template_parts(template):
    """兼容已缓存模板和测试中直接传入的旧式图像数组。"""
    if isinstance(template, PreparedMonsterTemplate):
        return template.color, template.gray, True
    return template, template, False


def _is_duplicate_monster_center(center, template_index, accepted_centers):
    """按模板来源选择去重范围，保留相邻怪物并合并跨模板重复命中。"""
    for old_center, old_template_index in accepted_centers:
        if old_template_index == template_index:
            radius_x = radius_y = MONSTER_SAME_TEMPLATE_DEDUP_PIXELS
        else:
            radius_x = MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_X
            radius_y = MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_Y
        if (
            abs(old_center[0] - center[0]) < radius_x
            and abs(old_center[1] - center[1]) < radius_y
        ):
            return True
    return False


def match_monster_templates(
    cv2,
    np,
    frame,
    templates,
    threshold: float = MONSTER_TEMPLATE_CONFIDENCE,
    x_offset: int = 0,
    y_offset: int = 0,
    gray_frame=None,
) -> Tuple[TemplateMatch, ...]:
    """匹配怪物模板；缓存模板先灰度预筛，再用彩色模板确认。"""
    matches = []
    accepted_centers = []
    for template_index, template in enumerate(templates):
        if template is None:
            continue
        color_template, search_template, prepared = _monster_template_parts(template)
        height, width = search_template.shape[:2]
        search_frame = frame
        can_confirm_color = (
            prepared
            and len(frame.shape) == 3
            and len(color_template.shape) == 3
        )
        if prepared:
            if gray_frame is None:
                gray_frame = _monster_frame_gray(cv2, frame)
            search_frame = gray_frame
        if height > search_frame.shape[0] or width > search_frame.shape[1]:
            continue
        result = cv2.matchTemplate(
            search_frame,
            search_template,
            cv2.TM_CCOEFF_NORMED,
        )
        candidate_threshold = (
            min(float(threshold), MONSTER_GRAY_PREFILTER_CONFIDENCE)
            if can_confirm_color
            else float(threshold)
        )
        # 只保留 3×3 邻域内的局部峰值，避免同一只怪物在相邻像素产生大量候选，
        # 从源头减少后续 Python 去重循环的工作量。
        local_maxima = result == cv2.dilate(result, None)
        locations = np.where((result >= candidate_threshold) & local_maxima)
        for x, y in zip(locations[1], locations[0]):
            confidence = float(result[y, x])
            if can_confirm_color:
                color_region = frame[y:y + height, x:x + width]
                color_result = cv2.matchTemplate(
                    color_region,
                    color_template,
                    cv2.TM_CCOEFF_NORMED,
                )
                confidence = float(color_result[0, 0])
                if not np.isfinite(confidence) or confidence < threshold:
                    continue
            center = (x_offset + x + width / 2, y_offset + y + height / 2)
            if _is_duplicate_monster_center(
                center,
                template_index,
                accepted_centers,
            ):
                continue
            accepted_centers.append((center, template_index))
            matches.append(
                TemplateMatch(
                    (int(x), int(y), int(x + width), int(y + height)),
                    (
                        int(x_offset + x),
                        int(y_offset + y),
                        int(x_offset + x + width),
                        int(y_offset + y + height),
                    ),
                    center,
                    confidence,
                )
            )
    return tuple(matches)


def match_monster_templates_parallel(
    cv2,
    np,
    frame,
    templates,
    executor,
    threshold: float = MONSTER_TEMPLATE_CONFIDENCE,
    x_offset: int = 0,
    y_offset: int = 0,
) -> Tuple[TemplateMatch, ...]:
    """并行匹配多张怪物模板，并按原规则汇总、跨模板去重。

    每张模板只读取同一帧ROI，彼此没有共享写入；线程池由检测线程长期复用，
    避免每帧创建线程。结果按模板原始顺序汇总，保证攻击判定行为与串行版本一致。
    """
    template_list = tuple(template for template in templates if template is not None)
    if not template_list:
        return ()
    if executor is None or len(template_list) == 1:
        return match_monster_templates(
            cv2,
            np,
            frame,
            template_list,
            threshold,
            x_offset,
            y_offset,
        )

    gray_frame = (
        _monster_frame_gray(cv2, frame)
        if any(
            isinstance(template, PreparedMonsterTemplate)
            for template in template_list
        )
        else None
    )
    futures = [
        executor.submit(
            match_monster_templates,
            cv2,
            np,
            frame,
            (template,),
            threshold,
            x_offset,
            y_offset,
            gray_frame,
        )
        for template in template_list
    ]
    matches = []
    accepted_centers = []
    for template_index, future in enumerate(futures):
        for match in future.result():
            if _is_duplicate_monster_center(
                match.center,
                template_index,
                accepted_centers,
            ):
                continue
            accepted_centers.append((match.center, template_index))
            matches.append(match)
    return tuple(matches)


def ensure_custom_template_directory(base_dir=None) -> Path:
    """创建可写模板目录，并在单文件版首次使用时释放内置默认模板。

    PyInstaller 单文件程序会把打包资源临时解压到 ``sys._MEIPASS``。界面和
    自动化始终使用 EXE 同级的可写目录，因此只在外部目录还没有 guai* 图片
    时复制一次；用户后来替换的模板不会被覆盖。
    """
    directory = custom_template_directory(base_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if custom_template_files(directory):
        return directory

    bundled_root = getattr(sys, "_MEIPASS", None)
    if not bundled_root:
        return directory

    bundled_directory = Path(bundled_root) / CUSTOM_TEMPLATE_RELATIVE_DIRECTORY
    if not bundled_directory.is_dir():
        return directory

    for source in custom_template_files(bundled_directory):
        shutil.copy2(str(source), str(directory / source.name))

    instruction = bundled_directory / "图片放这里.txt"
    instruction_target = directory / instruction.name
    if instruction.is_file() and not instruction_target.exists():
        shutil.copy2(str(instruction), str(instruction_target))
    return directory


__all__ = (
    "ATTACK_INTENT_TTL_SECONDS",
    "ATTACK_MOVE_LOCK_SECONDS",
    "COMBAT_RELEASE_GRACE_SECONDS",
    "COMBAT_TARGET_LOST_CONFIRMATION_FRAMES",
    "CUSTOM_TEMPLATE_EXTENSIONS",
    "CUSTOM_TEMPLATE_RELATIVE_DIRECTORY",
    "MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_X",
    "MONSTER_CROSS_TEMPLATE_DEDUP_RADIUS_Y",
    "MONSTER_GRAY_PREFILTER_CONFIDENCE",
    "MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET",
    "MONSTER_TEMPLATE_CONFIDENCE",
    "MonsterHealthBarAssociation",
    "MonsterHealthBarDetectionResult",
    "MonsterHealthBarMatch",
    "POST_ATTACK_RECHECK_SECONDS",
    "TEMPLATE_TARGET_FRAME_SECONDS",
    "TEST_YOLO_TARGET_FRAME_SECONDS",
    "PreparedMonsterTemplate",
    "TemplateMatch",
    "YOLO_MAX_DETECTIONS",
    "YOLO_MONSTER_CONFIDENCE",
    "YOLO_TARGET_FRAME_SECONDS",
    "custom_template_directory",
    "custom_template_files",
    "associate_monster_health_bars",
    "detect_and_associate_monster_health_bars",
    "detect_monster_health_bars",
    "effective_detector",
    "ensure_custom_template_directory",
    "match_monster_templates",
    "match_monster_templates_parallel",
    "prepare_monster_template",
)

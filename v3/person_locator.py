import time
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple


# 人多、特效多时完整人物模板的相关度会略有波动；0.75 仍能过滤掉
# 大部分背景误匹配，同时避免因短暂遮挡让战斗层长期拿不到人物坐标。
PERSON_MATCH_THRESHOLD = 0.75
PERSON_MONITOR = {"top": 300, "left": 0, "width": 1280, "height": 330}
PERSON_LOCAL_SEARCH_RADIUS_X = 240
PERSON_LOCAL_SEARCH_RADIUS_Y = 120
PERSON_LOCAL_FAILURES_BEFORE_FULL = 2
PERSON_POSITION_HOLD_SECONDS = 0.20
SelectedTemplate = Optional[Tuple[str, object]]


def load_character_templates(cv2, base_dir: Path) -> Sequence[Tuple[str, object]]:
    """按灯泡优先、血量备用的顺序加载人物定位模板。"""
    templates = []
    candidates = (
        ("dengpao.png", ("dengpao.png",)),
        # 内部统一使用 xueliang.png 作为模板类型，同时兼容现有旧资源名和中文名。
        (
            "xueliang.png",
            ("xueliang.png", "血量.png", "renwuxueliang.png"),
        ),
    )
    for canonical_name, file_names in candidates:
        loaded = None
        for file_name in file_names:
            for path in (
                Path(base_dir) / file_name,
                Path(base_dir) / "resres" / file_name,
            ):
                loaded = cv2.imread(str(path))
                if loaded is not None:
                    break
            if loaded is not None:
                break
        if loaded is not None:
            templates.append((canonical_name, loaded))
    return templates


def require_character_templates(cv2, base_dir: Path) -> Sequence[Tuple[str, object]]:
    """加载人物模板；灯泡和血量图都不可用时立即抛出启动错误。"""
    templates = load_character_templates(cv2, base_dir)
    if templates:
        return templates
    root = Path(base_dir)
    raise FileNotFoundError(
        "找不到人物定位模板 dengpao.png 和血量模板 "
        "xueliang.png/血量.png/renwuxueliang.png；"
        "请把至少一张图片放到 {} 或 {}".format(root, root / "resres")
    )


class CharacterTracker:
    """使用固定灰度模板跟踪人物，并在连续局部失败后执行全区域重定位。"""

    def __init__(
        self,
        selected_template,
        monitor=PERSON_MONITOR,
        threshold=PERSON_MATCH_THRESHOLD,
        radius_x=PERSON_LOCAL_SEARCH_RADIUS_X,
        radius_y=PERSON_LOCAL_SEARCH_RADIUS_Y,
        failures_before_full=PERSON_LOCAL_FAILURES_BEFORE_FULL,
    ):
        """保存固定人物模板、屏幕偏移、局部范围和全区域重定位阈值。"""
        self.selected_template = selected_template
        self.monitor = dict(monitor)
        self.threshold = float(threshold)
        self.radius_x = int(radius_x)
        self.radius_y = int(radius_y)
        self.failures_before_full = max(1, int(failures_before_full))
        self.template_gray = None
        self.last_local_center = None
        self.last_screen_center = None
        self.last_success_at = 0.0
        self.last_mode = "full"
        self.local_failures = 0
        self.last_match_ms = 0.0

    def reset(self) -> None:
        """清除上一帧位置，使下一帧重新执行完整区域定位。"""
        self.last_local_center = None
        self.last_screen_center = None
        self.last_success_at = 0.0
        self.last_mode = "full"
        self.local_failures = 0
        self.last_match_ms = 0.0

    def locate(self, cv2, frame):
        """执行一次灰度定位并返回置信度、屏幕中心和采集区域内中心。

        已有可靠位置时只搜索上一帧附近；局部连续失败达到阈值后，才在当前帧
        执行一次全区域重定位，从而避免人物正常移动时反复扫描整张画面。
        """
        started = time.perf_counter()
        result = (0.0, None, None)
        if self.selected_template is None:
            self.last_mode = "missing-template"
            self.last_match_ms = (time.perf_counter() - started) * 1000
            return result

        try:
            frame_height, frame_width = frame.shape[:2]
            expected_width = int(self.monitor.get("width", frame_width))
            expected_height = int(self.monitor.get("height", frame_height))
            if (
                frame_width != expected_width
                or frame_height != expected_height
            ):
                self.last_mode = "frame-size-mismatch {}x{} != {}x{}".format(
                    frame_width,
                    frame_height,
                    expected_width,
                    expected_height,
                )
                self.last_local_center = None
                self.last_screen_center = None
                self.last_success_at = 0.0
                self.local_failures = 0
                return 0.0, None, None
            frame_gray = self._to_gray(cv2, frame)
            self._ensure_gray_template(cv2)

            if self.last_local_center is not None:
                local_result = self._locate_near_previous(cv2, frame_gray)
                if local_result is not None and local_result[0] >= self.threshold:
                    confidence, screen_center, local_center = local_result
                    self.last_local_center = local_center
                    self.last_screen_center = screen_center
                    self.last_success_at = time.monotonic()
                    self.local_failures = 0
                    self.last_mode = "local"
                    return local_result

                self.local_failures += 1
                if self.local_failures < self.failures_before_full:
                    confidence = local_result[0] if local_result is not None else 0.0
                    self.last_mode = "local-miss {}/{}".format(
                        self.local_failures,
                        self.failures_before_full,
                    )
                    return confidence, None, None

            result = self._locate_full(cv2, frame_gray)
            confidence, screen_center, local_center = result
            if screen_center is not None and confidence >= self.threshold:
                self.last_local_center = local_center
                self.last_screen_center = screen_center
                self.last_success_at = time.monotonic()
                self.local_failures = 0
                self.last_mode = "full"
            else:
                # 一次全区域失败后重新累计局部失败次数，避免下一帧继续全图扫描。
                self.local_failures = 0
                self.last_mode = "full-miss"
                result = (confidence, None, None)
            return result
        finally:
            self.last_match_ms = (time.perf_counter() - started) * 1000

    def recent_screen_center(
        self,
        max_age_seconds=PERSON_POSITION_HOLD_SECONDS,
        now=None,
    ):
        """短时返回上一可靠人物坐标；超时后必须重新定位，不能长期沿用旧位置。"""
        if self.last_screen_center is None or self.last_success_at <= 0:
            return None
        current = time.monotonic() if now is None else float(now)
        age_seconds = max(0.0, current - self.last_success_at)
        if age_seconds > max(0.0, float(max_age_seconds)):
            return None
        return self.last_screen_center

    def recent_screen_center_age_ms(self, now=None):
        """返回上一可靠人物坐标年龄，供战斗日志区分真实定位与短时保留。"""
        if self.last_screen_center is None or self.last_success_at <= 0:
            return None
        current = time.monotonic() if now is None else float(now)
        return max(0.0, current - self.last_success_at) * 1000.0

    @staticmethod
    def _to_gray(cv2, image):
        """把 BGR/BGRA 图像转为灰度；已是灰度图时直接复用。"""
        if len(image.shape) == 2:
            return image
        conversion = cv2.COLOR_BGRA2GRAY if image.shape[2] == 4 else cv2.COLOR_BGR2GRAY
        return cv2.cvtColor(image, conversion)

    def _ensure_gray_template(self, cv2) -> None:
        """首次定位时生成一次固定模板的灰度缓存，后续帧不重复转换。"""
        if self.template_gray is not None:
            return
        _name, template = self.selected_template
        self.template_gray = self._to_gray(cv2, template)

    def _locate_full(self, cv2, frame_gray):
        """在完整采集区域内匹配已经缓存的灰度人物模板。"""
        return self._match_region(cv2, frame_gray, self.monitor, 0, 0)

    def _locate_near_previous(self, cv2, frame_gray):
        """在上一帧中心周围裁剪 ROI，并把结果转换回完整区域坐标。"""
        center_x, center_y = self.last_local_center
        frame_height, frame_width = frame_gray.shape[:2]
        x1 = max(0, int(center_x - self.radius_x))
        y1 = max(0, int(center_y - self.radius_y))
        x2 = min(frame_width, int(center_x + self.radius_x))
        y2 = min(frame_height, int(center_y + self.radius_y))

        template_height, template_width = self.template_gray.shape[:2]
        if x2 - x1 < template_width or y2 - y1 < template_height:
            return None

        region = frame_gray[y1:y2, x1:x2]
        region_monitor = {
            "left": self.monitor["left"] + x1,
            "top": self.monitor["top"] + y1,
            "width": x2 - x1,
            "height": y2 - y1,
        }
        confidence, screen_center, region_center = self._match_region(
            cv2, region, region_monitor, x1, y1
        )
        if region_center is None:
            return confidence, screen_center, None
        return confidence, screen_center, region_center

    def _match_region(self, cv2, region_gray, region_monitor, x_offset, y_offset):
        """匹配一个灰度区域，并统一换算屏幕坐标和完整采集区域坐标。"""
        template_height, template_width = self.template_gray.shape[:2]
        if (
            template_height > region_gray.shape[0]
            or template_width > region_gray.shape[1]
        ):
            return 0.0, None, None

        response = cv2.matchTemplate(
            region_gray,
            self.template_gray,
            cv2.TM_CCOEFF_NORMED,
        )
        _, confidence, _, location = cv2.minMaxLoc(response)
        region_center = (
            location[0] + template_width // 2,
            location[1] + template_height // 2,
        )
        local_center = (
            x_offset + region_center[0],
            y_offset + region_center[1],
        )
        screen_center = (
            region_monitor["left"] + region_center[0],
            region_monitor["top"] + region_center[1],
        )
        return float(confidence), screen_center, local_center


def match_character_template(cv2, frame, selected_template, monitor=PERSON_MONITOR):
    """用灰度方式匹配一张固定人物模板并返回置信度及中心坐标。"""
    if selected_template is None:
        return 0.0, None, None

    _name, template = selected_template
    height, width = template.shape[:2]
    if height > frame.shape[0] or width > frame.shape[1]:
        return 0.0, None, None

    frame_gray = CharacterTracker._to_gray(cv2, frame)
    template_gray = CharacterTracker._to_gray(cv2, template)
    result = cv2.matchTemplate(frame_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    _, confidence, _, location = cv2.minMaxLoc(result)
    local_center = (location[0] + width // 2, location[1] + height // 2)
    screen_center = (
        monitor["left"] + local_center[0],
        monitor["top"] + local_center[1],
    )
    return confidence, screen_center, local_center


def select_character_template(
    cv2,
    np,
    grab_screen: Callable,
    templates,
    monitor=PERSON_MONITOR,
    stop_requested: Optional[Callable[[], bool]] = None,
    wait: Optional[Callable[[float], object]] = None,
    log_prefix="[人物定位]",
) -> SelectedTemplate:
    """启动时检测灯泡最多三次；失败后固定使用血量模板。"""
    stop_requested = stop_requested or (lambda: False)
    wait = wait or time.sleep
    templates_by_name = dict(templates)
    lamp = templates_by_name.get("dengpao.png")
    health = templates_by_name.get("xueliang.png")

    if lamp is not None:
        highest_confidence = 0.0
        for attempt in range(1, 4):
            if stop_requested():
                return None
            screenshot = grab_screen(monitor)
            frame = np.asarray(screenshot)
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            confidence, _screen_center, _local_center = match_character_template(
                cv2,
                frame,
                ("dengpao.png", lamp),
                monitor,
            )
            highest_confidence = max(highest_confidence, confidence)
            if confidence >= PERSON_MATCH_THRESHOLD:
                print(
                    "{} 第 {} 次识别到灯泡，conf={:.3f}。".format(
                        log_prefix,
                        attempt,
                        confidence,
                    )
                )
                return "dengpao.png", lamp
            if attempt < 3:
                wait(0.3)

        print(
            "{} 3 次均未识别到灯泡，最高 conf={:.3f}，固定使用 xueliang.png。".format(
                log_prefix,
                highest_confidence,
            )
        )

    if health is not None:
        return "xueliang.png", health
    if lamp is not None:
        print("{} 缺少 xueliang.png，只能固定使用 dengpao.png。".format(log_prefix))
        return "dengpao.png", lamp
    return None


def character_attack_reference_y(template_name: str, character_y: float) -> float:
    """返回与 1.9 完全一致的人物模板攻击参考高度。"""
    if template_name == "xueliang.png":
        return character_y - 40
    if template_name == "dengpao.png":
        return character_y + 70
    return character_y


def evaluate_attack_target(
    character_x: float,
    attack_reference_y: float,
    target_center,
    horizontal_range: float,
    vertical_range: float = 70,
):
    """按界面设置的横向距离和纵向高度返回 dx、dy 及是否进入攻击范围。"""
    horizontal_distance = abs(character_x - target_center[0])
    vertical_distance = abs(attack_reference_y - target_center[1])
    accepted = (
        horizontal_distance < horizontal_range
        and vertical_distance < vertical_range
    )
    return horizontal_distance, vertical_distance, accepted

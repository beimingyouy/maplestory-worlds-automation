import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .map_registry import MAP_BY_NAME


@dataclass
class AppConfig:
    map_name: str = "蘑菇半层"
    mushroom_v3_route_file: str = "蘑菇V3路线.json"
    test_mode: bool = False
    detection_only_mode: bool = False
    runtime_logging_enabled: bool = True
    wheel_detection_enabled: bool = False
    attack_distance: float = 300.0
    attack_height: float = 70.0
    yolo_confidence: float = 0.65
    use_custom_templates: bool = False
    custom_mode: bool = False
    custom_left_position: Optional[Tuple[int, int]] = None
    custom_right_position: Optional[Tuple[int, int]] = None
    single_attack_key: str = "x"
    group_attack_key: str = "f"
    flash_key: str = "c"
    group_attack: bool = False
    auto_group_attack_over_count: int = 2
    flash_enabled: bool = False
    draw_detection_boxes: bool = False
    red_percent: int = 50
    blue_percent: int = 5
    rope_delay: float = 1.0
    rope_rest_interval_minutes: float = 20.0
    rope_rest_duration_minutes: float = 1.0
    ignored_platform_numbers: Tuple[int, ...] = ()
    scheduled_keys: Tuple[Tuple[str, float], ...] = ()

    def validate(self) -> "AppConfig":
        """校验配置范围及字段间约束，成功时返回当前实例。"""
        if self.test_mode and self.detection_only_mode:
            raise ValueError("截图运行模式和纯识别测试模式不能同时开启")
        if self.map_name not in MAP_BY_NAME:
            raise ValueError("请选择有效地图")
        if self.map_name == "自定义录制路线":
            # JSON回放拥有独立入口，旧配置遗留的左右边界开关不能覆盖它。
            self.custom_mode = False
        route_file = str(self.mushroom_v3_route_file).strip()
        if not route_file:
            raise ValueError("录制路线JSON不能为空")
        if Path(route_file).suffix.lower() != ".json":
            raise ValueError("录制路线文件必须是JSON文件")
        if "\x00" in route_file:
            raise ValueError("录制路线文件名无效")
        if not 20 <= float(self.attack_distance) <= 1000:
            raise ValueError("攻击距离必须在 20 到 1000 之间")
        if not 10 <= float(self.attack_height) <= 1000:
            raise ValueError("攻击高度必须在 10 到 1000 之间")
        if not 0.30 <= float(self.yolo_confidence) <= 0.95:
            raise ValueError("YOLO 怪物置信度必须在 0.30 到 0.95 之间")
        for label, position in (
            ("左边", self.custom_left_position),
            ("右边", self.custom_right_position),
        ):
            if position is None:
                if self.custom_mode:
                    raise ValueError("自定义模式启动前请先点击“记录{}”".format(label))
                continue
            if len(position) != 2:
                raise ValueError("{}坐标格式不正确".format(label))
            x, y = int(position[0]), int(position[1])
            if not (20 <= x <= 290 and 110 <= y <= 300):
                raise ValueError("{}坐标不在小地图范围内：{}".format(label, position))
        if (
            self.custom_left_position is not None
            and self.custom_right_position is not None
            and int(self.custom_left_position[0]) >= int(self.custom_right_position[0])
        ):
            raise ValueError("左边坐标的 X 必须小于右边坐标的 X")
        if not 0 <= int(self.red_percent) <= 100:
            raise ValueError("红量百分比必须在 0 到 100 之间")
        if not 0 <= int(self.blue_percent) <= 100:
            raise ValueError("蓝量百分比必须在 0 到 100 之间")
        if not 0 <= int(self.auto_group_attack_over_count) <= 99:
            raise ValueError("自动群攻怪物数量阈值必须在 0 到 99 之间")
        self.auto_group_attack_over_count = int(
            self.auto_group_attack_over_count
        )
        if not 0 <= float(self.rope_delay) <= 30:
            raise ValueError("绳子延迟必须在 0 到 30 秒之间")
        if not 0 <= float(self.rope_rest_interval_minutes) <= 1440:
            raise ValueError("绳子休息间隔必须在 0 到 1440 分钟之间")
        if not 0 <= float(self.rope_rest_duration_minutes) <= 120:
            raise ValueError("绳子休息时长必须在 0 到 120 分钟之间")
        raw_ignored_platforms = self.ignored_platform_numbers or ()
        if isinstance(raw_ignored_platforms, str):
            raw_ignored_platforms = raw_ignored_platforms.split("/")
        normalized_ignored_platforms = []
        for raw_number in raw_ignored_platforms:
            text = str(raw_number).strip()
            if not text:
                continue
            if not text.isdigit():
                raise ValueError("忽略平台只能填写数字，多个平台请用 / 隔开")
            number = int(text)
            if not 1 <= number <= 999:
                raise ValueError("忽略平台编号必须在 1 到 999 之间")
            normalized_ignored_platforms.append(number)
        if len(set(normalized_ignored_platforms)) > 100:
            raise ValueError("忽略平台最多可同时设置 100 个")
        self.ignored_platform_numbers = tuple(
            sorted(set(normalized_ignored_platforms))
        )
        for label, value in (
            ("单体攻击按键", self.single_attack_key),
            ("群攻按键", self.group_attack_key),
            ("闪现按键", self.flash_key),
        ):
            if not str(value).strip():
                raise ValueError("{}不能为空".format(label))
        raw_scheduled_keys = self.scheduled_keys or ()
        normalized_scheduled_keys = []
        seen_scheduled_keys = set()
        if len(raw_scheduled_keys) > 12:
            raise ValueError("定时按键最多只能设置12个")
        for index, item in enumerate(raw_scheduled_keys, start=1):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError("第{}个定时按键格式不正确".format(index))
            key = str(item[0]).strip().lower()
            interval = float(item[1])
            if not key or any(character.isspace() for character in key):
                raise ValueError("第{}个定时按键不能为空或包含空格".format(index))
            if len(key) > 32:
                raise ValueError("第{}个定时按键名称过长".format(index))
            if not 1.0 <= interval <= 86400.0:
                raise ValueError("第{}个定时按键间隔必须在1到86400秒之间".format(index))
            if key in seen_scheduled_keys:
                raise ValueError("定时按键不能重复设置：{}".format(key))
            seen_scheduled_keys.add(key)
            normalized_scheduled_keys.append((key, interval))
        self.scheduled_keys = tuple(normalized_scheduled_keys)
        return self

    def to_dict(self) -> Dict[str, Any]:
        """把配置转换为可序列化字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        """从字典恢复配置，并兼容忽略旧版本遗留字段。"""
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in allowed}
        for key in ("custom_left_position", "custom_right_position"):
            if values.get(key) is not None:
                values[key] = tuple(values[key])
        if values.get("scheduled_keys") is not None:
            values["scheduled_keys"] = tuple(
                tuple(item) for item in values["scheduled_keys"]
            )
        if values.get("ignored_platform_numbers") is not None:
            values["ignored_platform_numbers"] = tuple(
                values["ignored_platform_numbers"]
            )
        return cls(**values).validate()


class ConfigStore:
    def __init__(self, path: Path):
        """绑定配置文件路径，但不立即执行磁盘读写。"""
        self.path = Path(path)

    def load(self) -> AppConfig:
        """读取并校验配置；文件损坏时回退到默认值。"""
        if not self.path.exists():
            return AppConfig()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return AppConfig.from_dict(data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print("[配置] 读取失败，已使用默认值：{}".format(exc))
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        """先写临时文件再原子替换，降低配置被截断的风险。"""
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary), str(self.path))


def default_config_path() -> Path:
    """返回源码模式或打包模式下的默认配置文件位置。"""
    if getattr(__import__("sys"), "frozen", False):
        return Path(os.path.dirname(__import__("sys").executable)) / "v3_settings.json"
    return Path(__file__).resolve().parent.parent / "v3_settings.json"

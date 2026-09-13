from typing import Dict, Tuple

from .map.definitions import MAP_SPECS, MapSpec

MAP_BY_NAME: Dict[str, MapSpec] = {spec.name: spec for spec in MAP_SPECS}


def get_map_spec(name: str) -> MapSpec:
    """按显示名称返回地图定义，不存在时给出可读错误。"""
    try:
        return MAP_BY_NAME[name]
    except KeyError as exc:
        raise ValueError("不支持的地图：{}".format(name)) from exc


def map_names() -> Tuple[str, ...]:
    """按定义顺序返回界面下拉框需要的地图名称。"""
    return tuple(spec.name for spec in MAP_SPECS)

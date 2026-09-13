"""录制路线回放共用的绳子几何行为。

本模块只根据路线JSON中的 ``rope_x``、``rope_top_y`` 和 ``rope_bottom_y``
计算动作，不包含任何地图名称或写死坐标，因此可供所有自定义地图复用。
"""

from typing import Optional, Tuple


DEFAULT_ROPE_ENTRY_OFFSET_X = 2


def rope_entry_target(
    point,
    character_x: int,
    offset_x: int = DEFAULT_ROPE_ENTRY_OFFSET_X,
) -> Tuple[int, str]:
    """根据人物位于绳子哪一侧，返回起跳X和朝向。"""
    if getattr(point, "rope_x", None) is not None:
        rope_x = int(point.rope_x)
    elif getattr(point, "horizontal", "none") == "left":
        rope_x = int(point.x) - int(offset_x)
    else:
        rope_x = int(point.x) + int(offset_x)
    if int(character_x) <= rope_x:
        return rope_x - int(offset_x), "right"
    return rope_x + int(offset_x), "left"


def rope_bounds(point) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """返回路线点记录的绳子X、顶部Y和底部Y。"""
    rope_x = getattr(point, "rope_x", None)
    top_y = getattr(point, "rope_top_y", None)
    bottom_y = getattr(point, "rope_bottom_y", None)
    return (
        int(rope_x) if rope_x is not None else None,
        int(top_y) if top_y is not None else None,
        int(bottom_y) if bottom_y is not None else None,
    )


def has_complete_rope_geometry(point) -> bool:
    """判断路线点是否携带可用于动态挂绳的完整几何信息。"""
    rope_x, top_y, bottom_y = rope_bounds(point)
    return rope_x is not None and top_y is not None and bottom_y is not None


__all__ = (
    "DEFAULT_ROPE_ENTRY_OFFSET_X",
    "has_complete_rope_geometry",
    "rope_bounds",
    "rope_entry_target",
)

"""地图定义包。

这里只保存地图元数据；具体路线动作仍由兼容引擎执行。
"""

from .definitions import MAP_SPECS, MapSpec

__all__ = ["MAP_SPECS", "MapSpec"]

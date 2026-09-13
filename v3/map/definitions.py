"""集中声明界面可选地图、动作入口和怪物检测方式。"""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class MapSpec:
    """描述一张地图如何映射到兼容引擎及检测器。"""

    name: str
    handler: str
    detector: str = "yolo"
    resource_name: str = ""

    @property
    def detection_resource_name(self) -> str:
        """返回模型或模板目录名；未覆盖时默认使用地图显示名称。"""
        return self.resource_name or self.name


MAP_SPECS: Tuple[MapSpec, ...] = (
    # V2 复用“蘑菇.pt”，但使用独立的战斗、巡逻和爬绳协调流程。
    MapSpec("蘑菇V2", "蘑菇地图V2", resource_name="蘑菇"),
    # V3读取页面录制的有序小地图路线；运行时强制使用img/自定义怪物模板。
    MapSpec("蘑菇V3", "蘑菇地图V3", resource_name="蘑菇"),
    # 独立加载任意录制JSON；移动只来自文件，通用绳子行为按JSON几何信息计算。
    MapSpec("自定义录制路线", "自定义录制路线", detector="template", resource_name="自定义"),
    # 界面沿用短名称“蘑菇”，内部路线仍是完整的“蘑菇地图1”。
    MapSpec("蘑菇", "蘑菇地图1"),
    MapSpec("蘑菇半层", "蘑菇地图1半图"),
    # 上层路线复用现有“蘑菇半层”模型，避免按显示名寻找不存在的模型文件。
    MapSpec("蘑菇半层上层", "蘑菇地图1半图上", resource_name="蘑菇半层"),
    # 复用“蘑菇半层上层”的路线，仅将怪物识别切换为图片模板匹配。
    MapSpec("蘑菇半图上层备用", "蘑菇地图1半图上", detector="template"),
    MapSpec("木面2", "木面2地图2"),
    MapSpec("天使猴子", "天使猴子"),
    MapSpec("军营2", "军营2"),
    MapSpec("通话妙月兔2", "通话妙月兔"),
    MapSpec("林中石头人", "林中石头人"),
    MapSpec("林中怪猫", "林中怪猫"),
    MapSpec("林中青龙", "林中青龙", detector="template"),
    MapSpec("天空蓝狮子", "天空蓝狮子", detector="template"),
    MapSpec("雪域冰原2大灰狼", "雪域冰原2大灰狼"),
    MapSpec("雪域冰原2黑雪人", "雪域冰原2黑雪人"),
    MapSpec("武陵迷雾森林", "武陵迷雾森林"),
    # 仓库没有同名 YOLO 模型，但已有完整的 guai*.png 模板。
    MapSpec("武陵海盗船2", "武陵海盗船2", detector="template"),
    MapSpec("时间之路1", "时间之路1"),
    MapSpec("火野猪1", "火野猪1"),
)

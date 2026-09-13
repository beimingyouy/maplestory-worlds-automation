# V3 公共行为模块

`public` 目录只保存与具体地图坐标无关、可以被多个地图和自定义路线复用的行为。
地图目录 `v3/map` 只负责地图边界、平台、绳子、跳跃点和路线编排。

## 模块职责

| 模块 | 行为职责 | 主要调用方 |
| --- | --- | --- |
| `monster_detection.py` | YOLO/模板检测参数、自定义怪物模板、怪物血条识别、模板匹配和去重 | 检测线程、纯识别模式、自动化服务 |
| `combat_strategy.py` | 读取怪物快照、过滤旧帧、攻击优先级、目标方向锁、追怪抗抖、死亡残影等待和AI动作意图 | 所有地图战斗流程 |
| `combat_actions.py` | 向地图提供稳定的战斗执行接口，统一攻击、转向、追怪、停顿和路线恢复 | 蘑菇V2、蘑菇V3、自定义路线 |
| `route_recording.py` | 分段录制平台和绳子、坐标去重、平台连接分析、动态起跳点、路线JSON和预览图生成 | 自定义路线录制页面 |
| `recorded_route_behavior.py` | 按JSON绳子几何信息计算通用入口、绳顶和绳底行为，不包含地图写死坐标 | 所有录制路线回放 |
| `recorded_route_player.py` | 严格执行所选JSON；只允许范围内原地攻击，禁止追怪和地图专用动作覆盖路线 | 自定义录制路线 |
| `minimap_tracking.py` | 小地图黄色人物点识别、单次坐标采集、持续坐标发布、速度和轨迹日志 | 人物定位线程、测试模式、路线录制 |
| `movement_actions.py` | 可被多个地图复用的移动、跳跃和攀爬按键动作 | 蘑菇地图及后续地图 |

## 依赖方向

```text
UI / 自动化服务
    ├── public/monster_detection.py
    ├── public/minimap_tracking.py
    └── public/route_recording.py

地图路线 / 自定义路线
    └── public/combat_actions.py
            └── public/combat_strategy.py
```

地图模块不能反向被公共模块导入，否则容易形成循环依赖。

## 导入约定

旧兼容模块已经删除。生产代码和测试代码都必须直接从`v3.public`下对应的
行为模块导入，避免公共实现再次分散到根目录或地图目录。

# 地图模块索引

`map`目录只保存具体地图坐标、平台、绳子和路线编排，公共行为统一位于`../public`。

- `mushroom_v2_rope.py`：蘑菇V2专用的爬绳、挂树恢复、上层离绳和定时休息。
- `mushroom_v2.py`：蘑菇V2状态、边界控制和主循环调度。
- `mushroom_route_variation.py`：蘑菇V2无怪巡逻时的低频随机跳跃和回头攻击。
- `mushroom_v3.py`：录制JSON路线回放和动态绳子入口处理。
- `definitions.py`：地图显示名与路线入口定义。
- `routes.py`：旧地图的具体路线实现。

后续新增路线时，只实现“没有怪物时怎么移动”，不要自行读取攻击意图或复制怪物判断。
统一从`v3.public.combat_actions`导入`RouteCombatState`、`read_monster_snapshot`、
`build_action_intent`、`trace_action_decision`和`apply_action_intent`。

排查文件按职责读取：

- 怪物模板、血条和YOLO参数：`../public/monster_detection.py`
- AI打怪优先级、目标锁和追怪策略：`../public/combat_strategy.py`
- 地图调用的战斗执行入口：`../public/combat_actions.py`
- 路线录制和平台绳子连接：`../public/route_recording.py`
- 共用移动、跳跃和爬绳动作：`../public/movement_actions.py`
- V2爬绳：`mushroom_v2_rope.py`
- V3 JSON回放：`mushroom_v3.py`

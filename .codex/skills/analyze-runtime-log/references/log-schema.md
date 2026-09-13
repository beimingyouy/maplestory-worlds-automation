# 运行轨迹紧凑日志格式

## 1. 通用外层

每行一个 JSON 对象（JSONL）：

```json
{"time":"2026-08-12T23:31:44.110","elapsed_ms":113.5,"thread":"v3-monster-detector","event":"detection_frame"}
```

通用字段保留长名，方便检索：

| 字段 | 含义 |
|---|---|
| `time` | 本地 ISO 时间 |
| `elapsed_ms` | 本次任务启动后的毫秒数 |
| `thread` | 线程名 |
| `event` | 事件名；为兼容现有分析，不缩写事件名 |

## 2. detection_frame

推荐格式：

```json
{"event":"detection_frame","p":[640,420],"m":[[328,423],[688,432]],"mr":[[-312,3],[48,12]],"n":2,"a":1,"c":1,"lr":[0,1],"cd":"r","cs":"combat_hold","hb":1}
```

| 短字段 | 含义 | 旧字段兼容 |
|---|---|---|
| `p` | 人物画面坐标 `[x,y]` | `person_position`、`self_position` |
| `m` | 怪物画面坐标列表，按 `abs(dx)` 排序，最多 12 个 | 无 |
| `mr` | 相对人物坐标 `[mx-px,my-py]` | 无 |
| `n` | 本帧怪物总数，截断前计数 | `targets` |
| `a` | 攻击范围内怪物数 | `nearby` |
| `c` | 智能追怪范围内怪物数 | `chase_targets` |
| `lr` | `[左侧攻击数,右侧攻击数]` | `left`、`right` |
| `cd` | 追怪方向：`l/r/null` | `chase_direction` |
| `dd` | 最终决策方向：`l/r/null` | `decision_direction` |
| `da` | 最终决策是否可攻击 | `decision_attackable` |
| `cs` | 战斗状态 | `combat_state` |
| `hb` | 血条目标数 | `health_bar_targets` |
| `lf` | 连续未识别怪物帧数 | `lost_frames` |
| `pc` | 人物识别置信度 | `person_confidence` |
| `pmode` | 人物定位模式 | `person_mode` |
| `fps` | 即时 FPS | `instant_fps` |

坐标约定：

- 使用实际传入目标评估函数的同一坐标平面。
- `dx>0` 表示怪物在人物右侧，`dx<0` 表示左侧。
- 屏幕 Y 向下递增，因此 `dy>0` 表示怪物画面位置更低。
- `m` 与 `mr` 是诊断样本；`n` 是完整数量。大量误识别不会无限放大日志。
- 只凭几何距离不能完全重建 `a/c`，因为目标评估还可能包含纵向、血条、平台边界、前后方策略和近身补刀规则。

## 3. template_detection_performance

推荐格式：

```json
{"event":"template_detection_performance","f":52657,"fi":32.1,"fm":41.8,"pm":2.3,"mm":31.5,"hm":4.2,"tm":12,"n":2,"a":1}
```

| 短字段 | 含义 | 旧字段兼容 |
|---|---|---|
| `f` | 帧序号 | `frame_number`、`frame_sequence` |
| `fi` | 帧间隔毫秒 | `frame_interval_ms` |
| `fm` | 完整帧耗时毫秒 | `frame_total_ms`、`frame_ms` |
| `cap` | 截图耗时毫秒 | `capture_ms` |
| `pm` | 人物识别耗时毫秒 | `person_ms` |
| `mm` | 怪物识别耗时毫秒 | `monster_ms` |
| `hm` | 血条识别耗时毫秒 | `health_bar_ms` |
| `pot` | 药水处理耗时毫秒 | `potion_ms` |
| `pv` | 预览处理耗时毫秒 | `preview_ms` |
| `tm` | 模板数量 | `template_count` |
| `n` | 怪物总数 | `targets` |
| `a` | 攻击范围数量 | `nearby` |

限频规则：

- 正常帧只保留周期摘要，建议每 3 秒一条。
- 慢帧立即保留：`fm>=80ms` 或 `fi>=120ms`。
- 性能异常开始、结束或阈值档位变化时立即保留。

## 4. 状态与动作事件

事件名暂不缩写，以免破坏现有 `rg` 查询和分析脚本。状态字段可使用短字段，但关键动作仍应保留可读事件名，例如：

- `attack_intent_published`、`attack_key`
- `combat_side_switch_*`、`smart_chase_*`
- `recorded_route_*`
- `rope_*`、`rest_*`
- `character_trajectory`
- `session_start`、`session_stop`

高频状态采用“状态变化立即记录 + 固定周期摘要”；一次性动作不得限频丢失。

业务事件不得覆盖通用 `elapsed_ms`。阶段自身耗时使用 `duration_ms`、
`approach_ms` 或其他业务专用字段。

## 5. 推荐检索顺序

1. `session_start`：确认配置、地图、攻击范围、忽略平台和单双向追怪模式。
2. `detection_frame`：读取 `p/m/mr/n/a/c/lr/cd/cs/hb`。
3. 攻击意图与按键事件：确认是否真的转向、攻击。
4. 路线/平台/绳子/休息事件：判断战斗状态是否正确释放给路线。
5. 性能事件：只在动作停顿与慢帧时间重合时认定 FPS/识别耗时是原因。

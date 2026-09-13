---
name: analyze-runtime-log
description: Analyze D:\PythonProject4 JSON-lines runtime trace logs, including compact p/m/mr/a/c/lr/cd/cs monster-coordinate fields and legacy verbose fields. Use when the user asks to analyze recent logs, a named 运行轨迹 log, combat pauses or empty attacks, missed monsters, smart-chase behavior, route stalls, one-direction walking, platform recovery, rope failures, rest-point failures, FPS, or VMware performance symptoms.
---

# 分析刷图运行日志

只读分析 `D:\PythonProject4\logs\运行轨迹_*.log`。不要启动游戏、发送按键、修改地图 JSON，或仅凭一条日志直接改代码。

## 分析流程

1. 用户未指定文件时，按 `LastWriteTime` 选择最新的 `运行轨迹_*.log`。
2. 用户说“最近 N 分钟”时，以日志内最后一个有效事件时间为终点截取，而不是用当前系统时间。
3. 先运行摘要脚本：

   ```powershell
   <python> .codex/skills/analyze-runtime-log/scripts/analyze_runtime_log.py --minutes 5
   ```

   指定日志：

   ```powershell
   <python> .codex/skills/analyze-runtime-log/scripts/analyze_runtime_log.py --log "D:\PythonProject4\logs\运行轨迹_xxx.log" --minutes 30
   ```

4. 根据摘要中的高频事件、战斗帧和异常时间点，用 `rg` 或 PowerShell 读取原始上下文。不要一次输出整个大日志。
5. 按事件顺序还原链路：`识别怪物 → 进入攻击/追怪范围 → 发布意图 → 停止路线移动 → 转向/攻击 → 怪物减少或消失 → 恢复路线`。
6. 将怪物相对坐标与人物/路线位移结合，判断人物是否实际靠近目标。不能只看 `targets>0` 就断言追怪失败。
7. 分开报告：直接证据、推断结论、仍缺少的证据。修改代码前指出具体函数或状态转换。

## 紧凑字段

完整字段表和兼容关系见 [references/log-schema.md](references/log-schema.md)。核心字段：

- `p=[x,y]`：人物画面坐标。
- `m=[[x,y],...]`：识别到的怪物画面坐标，最多保留离人物最近的 12 个。
- `mr=[[dx,dy],...]`：怪物相对人物坐标；`dx>0` 在右，`dx<0` 在左，屏幕坐标中 `dy>0` 在下。
- `n`：识别怪物总数；`a`：攻击范围内数量；`c`：智能追怪范围内数量。
- `lr=[左,右]`：人物左右攻击范围内怪物数量。
- `cd`：追怪方向；`cs`：战斗状态；`hb`：血条目标数量。

兼容旧日志时优先读取紧凑字段；缺失时再读取 `targets/nearby/left/right/chase_targets/chase_direction/combat_state` 等长字段。

## 结论规则

- **看见但不追**：连续多帧 `n>0`、目标在允许追怪方向和距离内，但 `c=0`，并且不是平台边界保护、纵向超限或多平台后方禁追。
- **追怪但未靠近**：`c>0` 或已有追怪意图，连续记录中目标 `abs(dx)` 没有总体下降，人物位置也未向目标移动。
- **攻击停顿**：`a>0` 且攻击动作之间间隔异常，同时路线移动被锁住；区分攻击冷却、转向确认、模板帧间隔和状态机等待。
- **空打**：攻击动作发生时相邻检测帧 `n=0/a=0/hb=0`。模板闪烁导致的短暂漏检要结合前后帧，不以单帧定性。
- **背后漏打**：`mr` 中存在符合配置攻击范围的背后目标，`lr` 对应侧非零，但没有转向或攻击事件。
- **左右抽动**：短时间方向反复切换，目标 `dx` 未穿越人物中心，或路线方向与战斗方向交替覆盖。
- **路线卡住**：有有效路线目标但人物坐标长时间基本不变，且无战斗锁、休息倒计时或绳子动作解释。
- **绳子失败**：连续上绳尝试后 Y 未沿绳子方向变化，或反复进入/退出入口阶段；同时核对入口最低点和平台边界。
- **休息点失败**：到达休息流程后没有进入倒计时，或被怪物碰撞后坐标离开休息点却未重新对齐。

## 输出要求

先给一句明确结论，再列出带时间戳/elapsed_ms 的证据链，最后给最小修改建议。若日志没有足够字段，明确指出下一次需要记录的字段，不猜测人物是否移动或怪物是否在前后方。

不要把日志乱码当作业务问题。优先依赖事件名、数字、布尔值、坐标和状态字段；需要显示中文路径或地图名时再处理编码。

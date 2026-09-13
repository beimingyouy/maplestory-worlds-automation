"""通用自定义录制路线回放器。

回放器仍读取所选JSON提供地图和绳子上下文，但平台阶段采用怪物优先模式：有怪
先追击和攻击，打完短暂停留复查后恢复录制路线巡逻，以便自然移动和拾取掉落物。
"""

import base64
import json
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Tuple

from . import combat_actions as combat_logic
from .recorded_route_behavior import (
    has_complete_rope_geometry,
    rope_bounds,
)
from .recorded_route_liveness import (
    RouteLivenessConfig,
    apply_route_liveness_watchdog,
)
from .recorded_route_navigation import (
    WalkOffPlan,
    evaluate_platform_rotation,
    evaluate_walk_off,
    infer_walk_off_plan,
)
from .route_recording import (
    build_route_variants_for_playback,
    resolve_mushroom_v3_route_path,
)


ROUTE_LOOP_SECONDS = 0.02
ROUTE_LOOKAHEAD_POINTS = 24
ROUTE_BACKTRACK_POINTS = 3
ROUTE_RELOCALIZE_DISTANCE = 18
ROUTE_PLATFORM_DIRECTION_NORMALIZE_MIN_SPAN = 4
ROUTE_JUMP_TRIGGER_DISTANCE = 6
ROUTE_ROPE_ENTRY_LEFT_MIN_OFFSET_X = -3
ROUTE_ROPE_ENTRY_LEFT_MAX_OFFSET_X = -1
ROUTE_ROPE_ENTRY_RIGHT_MIN_OFFSET_X = 1
ROUTE_ROPE_ENTRY_RIGHT_MAX_OFFSET_X = 3
ROUTE_ROPE_ENTRY_RUNUP_EXTRA_X = 4
ROUTE_ROPE_ENTRY_MAX_RUNUP_EXTRA_X = 7
ROUTE_ROPE_ENTRY_RUNUP_EDGE_MARGIN_X = 3
ROUTE_ROPE_BODY_TOLERANCE_X = 2
ROUTE_ROPE_ENTRY_TRIGGER_Y = 12
ROUTE_JUMP_LOOKAHEAD_POINTS = 12
ROUTE_JUMP_COOLDOWN_SECONDS = 0.28
ROUTE_JUMP_HOLD_SECONDS = 0.045
ROUTE_ROPE_PRE_JUMP_DIRECTION_HOLD_SECONDS = 0.04
ROUTE_ROPE_ENTRY_COAST_SECONDS = 0.18
ROUTE_ROPE_C_TO_UP_DELAY_SECONDS = 0.012
ROUTE_ROPE_UP_REASSERT_SECONDS = 0.18
ROUTE_ROPE_ENTRY_COMMIT_SECONDS = 1.0
ROUTE_ROPE_ENTRY_CONFIRM_PROGRESS_Y = 3
ROUTE_ROPE_ENTRY_EARLY_FALL_SECONDS = 0.4
ROUTE_ROPE_CONTACT_UP_REPRESS_RELEASE_SECONDS = 0.008
ROUTE_ROPE_ENTRY_MAX_COMMIT_BONUS_SECONDS = 0.5
ROUTE_ROPE_ENTRY_MAX_JUMP_HOLD_SECONDS = 0.09
ROUTE_ROPE_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS = 0.035
ROUTE_ROPE_ENTRY_MAX_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS = 0.075
ROUTE_ROPE_ENTRY_MIN_UP_REASSERT_SECONDS = 0.06
ROUTE_ROPE_ENTRY_RETRIES_PER_OFFSET = 1
ROUTE_ROPE_CLIMB_STALL_SECONDS = 1.2
# 小地图绳顶坐标会在录制值下方 1 像素稳定不动。先短暂确认，再进入离绳
# 状态继续补按上键，避免把已经到顶的人物反复清坐标、重新识别为绳身。
ROUTE_ROPE_TOP_EDGE_TOLERANCE_Y = 1
ROUTE_ROPE_TOP_EDGE_CONFIRM_SECONDS = 0.55
ROUTE_ROPE_TOP_EXIT_STALL_SECONDS = 0.65
ROUTE_ROPE_TOP_EXIT_UP_SECONDS = 0.18
ROUTE_ROPE_TOP_EXIT_MIN_X_CHANGE = 2
ROUTE_ROPE_BODY_RECOGNITION_MARGIN_Y = 1
# 绳顶下方 1 像素与上层平台坐标重叠，不能作为“启动时已在绳身”锁存区。
# 活动爬绳仍可在该坐标确认到顶；这里只让无活动绳状态从 top+2 开始接管。
ROUTE_ROPE_BODY_TOP_CLEARANCE_Y = ROUTE_ROPE_TOP_EDGE_TOLERANCE_Y + 1
ROUTE_PLATFORM_MATCH_TOLERANCE_Y = 6
ROUTE_PLATFORM_COVERAGE_TOLERANCE_X = 1
ROUTE_PLATFORM_MATCH_MARGIN_X = 20
# 下跳属于当前上方平台的最后离场动作。人物必须先覆盖平台左右边界，
# 然后才允许执行；每次下跳在录制X的正负5像素内锁定一个随机目标。
ROUTE_DOWN_JUMP_PATROL_EDGE_TOLERANCE_X = 5
ROUTE_DOWN_JUMP_RANDOM_OFFSET_X = 5
ROUTE_PLATFORM_STALL_SECONDS = 1.25
ROUTE_PLATFORM_STALL_OBSERVATION_GAP_SECONDS = 0.35
ROUTE_PLATFORM_STALL_EDGE_MARGIN_X = 3
ROUTE_PLATFORM_STALL_RIGHT_JUMP_COAST_SECONDS = 0.12
RECORDED_ROUTE_MULTI_PLATFORM_MAX_DWELL_SECONDS = 60.0
RECORDED_ROUTE_POSITION_MISSING_RELOCATE_SECONDS = 1.0
RECORDED_ROUTE_POSITION_MISSING_RETRY_SECONDS = 1.5
RECORDED_ROUTE_MONITOR_INTERVAL_SECONDS = 0.25
ROUTE_SINGLE_PLATFORM_RETURN_STALL_SECONDS = 1.2
ROUTE_SINGLE_PLATFORM_RETURN_MOVE_REASSERT_SECONDS = 0.18
ROUTE_SINGLE_PLATFORM_RETURN_JUMP_COAST_SECONDS = 0.16
ROPE_ENTRY_TRIGGER_Y = 4
# 绳顶坐标来自实际录制平台。提前 1~2 像素切换横向离绳会让人物仍挂在
# 绳身上，只能等待停滞兜底再次补按上键，因此正常流程必须真正到达绳顶 Y。
ROPE_TOP_TOLERANCE_Y = 0
ROPE_REST_FALL_TOLERANCE_Y = 4
ROPE_REST_HORIZONTAL_TOLERANCE_X = 3
# 小地图绳上Y会在人物实际静止时仍抖动1～2像素。进入休息目标附近后使用
# 较宽的停靠带释放上下键；只有超出停靠带才重新纠正，避免130↔128反复。
ROPE_REST_SETTLE_TOLERANCE_Y = 2
ROUTE_AI_MIN_INTERVAL_SECONDS = 6.0
ROUTE_AI_MAX_INTERVAL_SECONDS = 12.0
ROUTE_AI_PAUSE_MIN_SECONDS = 0.07
ROUTE_AI_PAUSE_MAX_SECONDS = 0.16
ROUTE_AI_BOUNDARY_MARGIN_X = 12
RECORDED_ROUTE_SMART_SEEK_MODE = True
# 丢目标总宽限已经持续复查了多帧；到期后只再留约一帧检测时间，避免路线与
# 战斗同一时刻抢控制权，同时去掉肉眼可见的二次停顿。
RECORDED_ROUTE_POST_COMBAT_SCAN_MIN_SECONDS = 0.0
RECORDED_ROUTE_POST_COMBAT_SCAN_MAX_SECONDS = 0.0
# 目标快照与人物定位来自不同检测线程。人物模板短暂丢失时，即使画面还有
# 怪物，也可能暂时无法生成 attack/chase 意图；保持一小段时间避免路线抢回控制。
RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS = 0.45
RECORDED_ROUTE_TARGET_LOSS_DIRECTION_COAST_SECONDS = 0.22
# 怪物快照持续存在、但路线线程始终没有完成攻击时，通常是旧截图、闪烁图标
# 或静态地图元素误命中。只按“最后一次有效攻击进展”释放；正常连续攻击没有
# 绝对时长上限，必须把当前平台前后活怪清完后才能恢复路线。
RECORDED_ROUTE_COMBAT_NO_PROGRESS_SECONDS = 1.5
RECORDED_ROUTE_COMBAT_SUPPRESS_SECONDS = 2.5
RECORDED_ROUTE_LOOT_APPROACH_MIN_SECONDS = 0.20
RECORDED_ROUTE_LOOT_APPROACH_MAX_SECONDS = 0.38
RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X = 3
# 游戏画面横向距离约每8px对应小地图1px。单有效平台清怪后用最后一张可靠
# 怪物快照换算死亡点，只在本平台边界内走去拾取；到点后原地驻守，不再往返。
RECORDED_ROUTE_LOOT_SCREEN_TO_MINIMAP_X = 20.0
RECORDED_ROUTE_SINGLE_PLATFORM_LOOT_TOLERANCE_X = 2
RECORDED_ROUTE_SINGLE_PLATFORM_LOOT_TIMEOUT_SECONDS = 3.0
# 平台边缘已经拒绝过一次向外追怪后，短时间内不要被同一张或相邻怪物快照
# 再次拉回追怪。仅屏蔽同平台、同方向的远距离 chase；进入攻击范围的 combat
# 始终优先，目标换边或人物回到平台内部也会立即解除。
RECORDED_ROUTE_CHASE_BOUNDARY_SUPPRESS_SECONDS = 0.65
RECORDED_ROUTE_CHASE_BOUNDARY_RELEASE_MARGIN_X = 10
# 路线行进方向相反、且距离很远的新追怪最容易由单帧模板闪现触发。此类目标
# 必须由三张不同的有效检测帧并稳定一小段时间后才允许首次回头；近怪、同向怪
# 和血条怪不等待。
RECORDED_ROUTE_REVERSE_FAR_CHASE_DISTANCE_X = 340.0
RECORDED_ROUTE_REVERSE_FAR_CHASE_CONFIRMATIONS = 3
RECORDED_ROUTE_REVERSE_FAR_CHASE_CONFIRM_WINDOW_SECONDS = 0.70
RECORDED_ROUTE_REVERSE_FAR_CHASE_STABLE_SECONDS = 0.18
# 仅供战斗卡死/路线静默恢复使用的临时路线保护时间。正常清怪后不会开启
# 这个窗口，否则怪物重新出现时会形成“先直走一段，再回头补打”的节奏。
RECORDED_ROUTE_RESUME_GRACE_SECONDS = 0.90
# 平台末端一旦进入绳子、下跳或跨平台连接，连接动作临时高于战斗。人物真正
# 到达下一平台后自动解除；超时则交给路线活性监控重新定位，不能永久锁死。
RECORDED_ROUTE_CONNECTION_MAX_SECONDS = 10.0
# 活性监控同时观察“有没有控制决策”和“控制是否产生预期位移”。人物被怪物
# 碰撞产生的被动坐标变化不算路线进展，避免状态已经失控却被假进展掩盖。
RECORDED_ROUTE_CONTROL_SILENCE_SECONDS = 1.80
RECORDED_ROUTE_MOVE_NO_PROGRESS_SECONDS = 1.60
RECORDED_ROUTE_COMBAT_STARVATION_SECONDS = 5.0
RECORDED_ROUTE_RECOVERY_SUPPRESS_SECONDS = 1.20
RECORDED_ROUTE_RECOVERY_JUMP_HOLD_SECONDS = 0.045

RECORDED_ROUTE_LIVENESS_CONFIG = RouteLivenessConfig(
    rope_entry_trigger_y=ROUTE_ROPE_ENTRY_TRIGGER_Y,
    loop_gap_reset_seconds=0.50,
    move_no_progress_seconds=RECORDED_ROUTE_MOVE_NO_PROGRESS_SECONDS,
    combat_starvation_seconds=RECORDED_ROUTE_COMBAT_STARVATION_SECONDS,
    control_silence_seconds=RECORDED_ROUTE_CONTROL_SILENCE_SECONDS,
    recovery_jump_hold_seconds=RECORDED_ROUTE_RECOVERY_JUMP_HOLD_SECONDS,
)
RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES = frozenset(
    {
        "rope_entry",
        "rope",
        "rope_exit",
        "down_jump",
        "right_return",
        "walk_off_left",
        "walk_off_right",
        "platform_jump_left",
        "platform_jump_right",
        "platform_jump_neutral",
        "platform_to_rope_left",
        "platform_to_rope_right",
        "rope_to_platform_left",
        "rope_to_platform_right",
    }
)
# When only one brush platform remains, every recorded connection below is an
# instruction to leave that platform.  Normal combat/patrol must never consume
# one of these points; rest-point navigation and recovery from another platform
# are the only flows allowed to do so.
RECORDED_ROUTE_SINGLE_PLATFORM_EXIT_SEGMENT_TYPES = (
    RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES
)
RECORDED_REST_POINT_LANDING_X_TOLERANCE = 5
# 持续方向键在 20ms 路线循环里可能一次跨过 2~4 个小地图像素。起跳前若只
# 接受精确到 1 像素的位置，人物会越过目标后立刻反向，最终在休息点下方来回
# 折返。起跳窗口与已经验证可落在休息点的 X 窗口保持一致，进入窗口就释放
# 水平键并起跳，避免永远无法进入休息倒计时。
RECORDED_REST_POINT_X_TOLERANCE = RECORDED_REST_POINT_LANDING_X_TOLERANCE
# 休息导航到达目标绳底附近后，不再依赖闭环路线最近点逐帧推进。这个范围
# 只负责把入口前的平台点切换为目标 rope_entry；真正起跳仍严格使用绳子
# 左右正负3像素的自适应入口逻辑。
RECORDED_REST_ROPE_ENTRY_LOCK_RADIUS_X = 24
# 休息点可能只比起跳平台高几个小地图像素。容差过大会让人物仍站在
# approach_y 平台时直接被判为已落到休息点，因此落点 Y 必须严格确认。
RECORDED_REST_POINT_LANDING_Y_TOLERANCE = 2
# 跳跃经过录制坐标的一帧不能算真正落稳。必须在严格落点范围内连续保持
# 一小段时间，避免刚经过目标Y就被怪物撞走却提前进入休息/测试停驻状态。
RECORDED_REST_POINT_LANDING_STABLE_SECONDS = 0.18
RECORDED_REST_POINT_LANDING_STABLE_FRAMES = 2
RECORDED_REST_POINT_JUMP_TIMEOUT_SECONDS = 2.0
RECORDED_REST_POINT_MAX_JUMP_ATTEMPTS = 3
RECORDED_REST_OPEN_BUTTON_MATCH_THRESHOLD = 0.82
RECORDED_REST_OPEN_BUTTON_GRAY_MATCH_THRESHOLD = 0.78
RECORDED_REST_OPEN_BUTTON_EDGE_MATCH_THRESHOLD = 0.52
RECORDED_REST_OPEN_BUTTON_SEARCH_SECONDS = 2.0
RECORDED_REST_OPEN_BUTTON_SEARCH_INTERVAL_SECONDS = 0.08
RECORDED_REST_OPEN_BUTTON_RESPONSE_SECONDS = 0.15
RECORDED_REST_SHOP_ANCHOR_MATCH_THRESHOLD = 0.90
RECORDED_REST_SHOP_ANCHOR_SEARCH_SECONDS = 1.5
RECORDED_REST_SHOP_BUTTON_OFFSET_X = 184
RECORDED_REST_SHOP_BUTTON_OFFSET_Y = 17
# 休息入口的黄色铃铛和橙色枫叶均通过用户提供的原始截图识别。模板内嵌在
# 源码中，避免剪贴板临时文件在重启或打包后消失。
RECORDED_REST_BELL_BUTTON_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACkAAAAuCAIAAAA6B6cZAAAIRklEQVRYCa1Y3WocyRXuV8gr+BX22rmxwIgBiTSjTG+Mp0zMVl2k1T3aRV1CTEpIHrnXGYZZo+yKRQzCGBM8EHKRCyNMXiAo5CYzYtlWMxcDuhD2K1Q4P1XdPZazNxFNq2emq77zfeenTlWgtcyl1Fpq2cY7P+Qavszxajzgyzik7Qe6B6l1W0sYRbP5SXCGDGbLs1xnp3k2GvYDmkVLiReMJAtoOsLONQy7w8rKbkCFIQxcvVzNkEuYB+9no/5k2A+YK9qLVhM8aJDTRE323gJEYqmQKBnNA5GoM8XNgNjMewjYYCzi8b2mG49BrRq8WdJUCJOKVGykAu4mFcwb7EYjKscx4zwHwU/zbDIaBiSmczYZwYo5ufCjczzODq+lqRjs/WG5WFj8Wy4Xg709gHdv+uHkMu/pXGeTkZmMTJM3xwjJjiHjqHupKTLSVKho3QMT/IcPt0Ygds1Z2vmYGDd4g78JshLfBQ4DM28Hj6TFhlLRKvbtbZpu+NhmuoCdnbr7aY4f87zJuwo3tKYmXT16UyGUWhdhqLe3ia6VU6vf07NSkUkFqt1w8ArpMWg+RM0JFWNNurjjHKNsBmFkmm4YAVKLMNzcXCuLnxh7emmnl/S8i9hpmgD8Cl1Ma0puiLXxQVAFuVeeBeBgpkRAB0f9nZ2yLBiS/r2/tu+v6bEsiliESkVggUlMmhiTjPCBeRM8yn4+HlN+U3i3pYenDKnVCiNEv/9VWTSBrQVgh72Xyk6rdaTjm5ult+/mZvndYGCMcMkNFeZ8fDBG3p4fW1CFnvMFRbUHDtyfB7DWzmezhw/v91R3sSKMtTfLJQZBo6aOD0DzqppKBHMup/iHmFdR1Om0CCkIAnu9hs+abKDvr+azrc3Nq/mMDZJTiEE5pY/IG2sqxAFTR3/XpYbgateDIBVChGEqH9MsgD29xxd8BRbQTxUwRd/0ssKG4CfebAFovlKK66gUZUpFndavfYghkrZWA/vpPUSt4OEjRUAt+Muy2FVRirkHpUbL8fgANUfSlFFatiVWGB905OlUSmLGpK0mSLBgeg9dUIOvRZ+1dlEWR1pj7mHiQdlgeMrv2nLS1D8VG0KE9SgDPMYGwe+GJ0utvZrPeqrb6bQo9w6//r3W6vh4N9fy+FhDjnnSJDLcNQqg2+TsOwTn2X8JezZTqvsUsXdVdPj1znOtjjOg/mfARqQKXkuKc1oZlYpWo6zB+7PU5/PZ1Xz+5Zdh/NXjhasKy+Xi7fn3z5+D5syb4CvSWFgIW4jQj6yizEnq9Nf249Ydjrf2auZSzg25WS6/+ebpQGd/2k+C3BUQwqYUT8WGiiKl1u+KMgo0x9gi8PWavV67/vZXYM31ms86h2ih4LtcPzzcMSbpJ0ltLakZkaYbIgyBtCtSn5CmiKuAKeUIvoHtU85hG1hpspMB+NsVc3rA+FJRlKaPP0/6LmBK9+k9gEfqbEEz5W6WC2OS8UHv+DgD3hhc1PVBYKtovdNqlWXxmdT6DPDlF1TsVrEr3S2uK3vAO88GmnlzESU3C/FAdbt+CJgP9atWT2o+BqnpcoUWsKng1BxflsWh3lZK+sI+GiUBZZSUbQBWkQgBeD7/TwP7F4Ed6Tr1etD9bnMtFiGvZtyoZ4QNwFHkgGdNYFe0OaM+blVciXET2FpNstexY9HdVRG2sLSiZAMN/kbGUaTjJ75+WWt5ffzfwHVUrjm8sNaX10W1lrgmTsuRQc1TIaJoXcdPCleA/r/YsI5FjR5yMjLGYH6nYqO/0+iHOL44xDCwV6T2jD1dHQTI2QcKPZRlkfd3aAHFtgmK+cgk3+7vgeZRtO6bTtJq6xIn0hjhn6JefuGrKb0f6GDrEq5GVbG2LIujOPaNM/s7B+xnu7sB5RXZGAQw/qOF68druAKC9zHlUBnSdSxBAEPq2L+5f//hw/tPWy0Cxg6fo+xUZ6ORORkcAXYUrXtsAvb3Ct7pD8JqbpJ8WJDFdeynrVan04J2RcBWARlj1+D2gicvj6Cuqabmdd6sJIChM2vaeu/4973gi7IQIoxF2ATmLpE2oT/8cAz5nQqxA7HG+4wgALUJle7AHuWln7wqddQ6sNbxLiyDkWe8wnsyGr46H1N+Qw3vyUc+x8jxK8DeL1uXHA1kk0el7kzrbd4Zkdq4J+UdmpZI2kwmw7+fnvBaQinek4/+/a9/egwvQH1272PSnF6mOxXtFR8TY+btNqGvXj17++Np4Lpxas0eSPnIi+9haOr5fDb/pA+hnxZlsS9lLEJi7KUGunXecNzQB8Ffji/++rpaS7RsC2xXek145lQUqrvVU926ZSRyrweolEuuhtBRU3V6gLzhqCPPs8mw/+bNyT9ev6beoVpDo6bjCdhilVDdruqubreAMWITYzzzwDx25wbe03zCNOxPhsM3b4fv/vYXwOb9APQt3DvAXrf4qXCRXxaFjp8IEYrwAfZScI/hI9yBsRANxrgBIK6VpzHQzob75+Mhl6Z6e5psx6R8Jn8LvWK0zncoEXiYlAqCwRMm+AbOmfiggRT+lDRvROCsJ88I9ecPtz9/uOX9t0kAlewwSUxdVJLEuC8R/JNvJv12woUSk3M608eKNzYLL17sB0FAqBcX7y4u3vEZF0WmSWLzx66W0mzHJum6jTi3cmgZbYzBShgi27WQhrWZtnq1L7mO5ih4EASE6rDhgBMmGhnYq41MDMcVSRfsSMAOqdsHSQLU3VYNUblENxnDVPyNzIg3TO7OUE9Rc61zugIsNP08z0YHcDby/bPs5TNY44xJANLr7E/A8BuEB9IufTMNwvLVMAi5UXadvcjPzjjQtqT8L2a7Q+Ux+om4AAAAAElFTkSuQmCC"
)

RECORDED_REST_OPEN_BUTTON_TEMPLATE_BASE64_LEGACY = (
    "iVBORw0KGgoAAAANSUhEUgAAACUAAAAgCAIAAAAaMSbnAAAHHklEQVRIDZ3X7Wscxx0H8P0biqEYSk0DARWEK4ip+yJ6kxIwFFrctw1KQbT4RXBfFL8xpooxSYlrWkhKkJXUDglOVMdK/BRLOj3cne6kk6U7RdLpJJ2ke97V3j7vzs7s7M7uauPVyKuNTk6hYllml5nfZ76zD7digtif7yPbDoIAIwRVpAit5tJSIZOc5jiO9hoZODcycI62OZZNTU0u5QutnbKuKhjBwMbExjaCAUKujf0gCDwcuMQNSOA4rksCQpjnHKaSbQd7exYADs/zpdJaNpvhODbCAiW5V7l+SHLsbDK9vrzI8zwEBoYWwchF0IOya5Mg8AKbBC4JXBxWIIdeePzcw5aFFEWuViuZzHSb5w8xz/QS/V6iP062eT6VTNTLm5rSti3kIuwiaAMtzOd4rk3cAIfJQg+H+TDe830fQhPrmq5rmqaKolCvlxfnFhKJJ0cwd7CXbl6iP0qZePRoITe3s1NT+F1Nk0xFg4KAoem7hGArcAPXpnlIQDBDCNE01TAEjpNqter29tbaWjGbTA6PDLOtVtyLMHewN+6xzea9u//NpdPFlZWtjY3G5ia3s6OIAtSgaYKDe8LGHsGBTRjZlERWLJc3C4X8zEx6YuKbr+/evXnr5rW336bY8OXzwf5Kxj1K3r7wCu1z7eq1oaHBr+59NfXNWHJ6cnkmWy6X2u22JksWsBwLBYFn29i3McM36qurK4uLC5Ik0cFH9qGnJI9g+MZZL9EfeUeGiIJQWFxcX15vNeqyrGMTeNjzbC/0Vlc3c7m5drt9ZEx0eKwHBnp+wAuCQGjzT3PzpdIKz7KaprquG+YzMZPP5wVBoNWHL5+Pb9HJvcr1eD4w0AMGetzB3ijf7QuvxDc6UBTEfH6hslHRpLYDHM+GPkKMoihR3UBJBp4ZbkoyUJLU3qtc9xL9kUexGHnm9oUz9DmJ+nz8p1/QmoqsrK4s8VwbY4xsRAx08LwPXz5/pK6X6KfwizBKUglcPQOunpEudrN9XWxflzvY++Gb3ZRcms+xDdbSLR8hgr7vRRM8thFP1tmOMOp98IefU292dp6t13UdEoSQCg7z+Ut/OZahJzuB+Jk4dsRbzGUbVdbSITGBiyAjiiKdyCdvvR5furgdL93Z7sT++fuXaU1RFFcKS3yriXUEIXQ0yKTTaZY9eI/c+vOrnWQnED/Tid343UsU4zgul5lZW9tQhF3LsIiKIQRMNjv9YOQB7dHpxUt3to9gdCUjb+zx6OxstlVpaKIAoe2E+QBTLBRHRx+y7MGPzs2+X+7df+P/uGYUe+83P6NT3+W41ESqXCpKvKQKsgMdRwFhvlq5nMlknjx4WKvVaFdKdqaJnzk22d/PnaIVatXaxNhosbDaaNSBomNF9xCBGiAQMHy9/u23848fPrr1n6H19XU64F+/fdkd7I0D8fax2Duv/5SO3djY+OzmrdTERG2rqnJtw7AcABAK8xHdYRiGKZVK4+OPPnz//XK5/D+9OMb9sYt9M9zcwd53fv0TOnarvPXRjX+np1JcnQOKbhsG1ICHyMF6fjlZYBgmkXj8+Z3P6YB3Xzu1d/8N6WJ3PBNtxzH6Kon2YKDnb6+epBXuffbF3Ex6t86GVw5gogLLIo4KiA6YockCJaOn4t3XTnmJfulid5ykh7R6GGv/vRXfP/vFuPKrH1OPa7GzqSRX56AqQ4gdCDyNXj/n0KNdr5w9GWHUiPbx6se2wUDPpdMnaJ2psVSz0jB5iSAAFeBZBKrA0+CBRyM++4B7kUfXkzLxdhyOe8mx8cZ2XVFER3UQCvMhFbjWCzww0ENjRW+1Z/O4cvYkGOhh+7ounT5x6fQJd7CXPqkRGffS46nmTgO0ZQQBUQE2PKICT4fMl/vXL54vet6ffaRQ5srZ8EaIPHr41+4fRXA0rWg9c8l0vcaaqkSgY+nItQjSYcwrVBnm4LciMmhdej2o5w72sn1dR85Tle5pZ0WUi0sFoSIYhkZ0x7KQryNLhJ6Fnufb9yTx+E+m6Fai69lJRnMKgkCWpJXsfG2tqgmaLeuWhYgOsOFbOvIN73tefiGvSHJ8cKPe+Md7N37Aq+5U4v0VSV5eXN3KLXNsy1RMBE3P8nAo+VhCPsbP78/9fFvrleVCIZuenUokp8Ynp0YTD0buf/rpnV324P+VaKmpwbW4O0OfjHwxMvn4ycxkKpeZKT5drZY2hbWmIYs2sJEGfYwwRtjAWMIH3tBkYWj/LdPmhd0WxzX4nY1Ko9ba2qmXl7cLuaW5zFzn16nIC9npXH56bv3p8uZmpbGxXas1xVpLYAWzyQMZu8j1DYQtzzewb/i2jPcMzExWq9Sjt6gNCdQBFBDSDShbmiiw1fZWqby2VtQ0PVo6XdVWZlfW88XmZk2p8KYsIQ0iUbUBdhQDsqZnWX6YB/sGtrFnG9iWsW3j7wBmPk/MEoblwAAAAABJRU5ErkJggg=="
)

# 用户最新提供的38x39枫叶截图。生产识别使用该完整模板在整个游戏窗口动态
# 定位，点击实际匹配中心，不再依赖任何固定屏幕坐标。
RECORDED_REST_OPEN_BUTTON_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACYAAAAnCAIAAADsA61cAAAIoUlEQVRYCZ2X32/b1hXH9TcMHbAMw7qHbUiBIDXQYMaAuQM6FOheinV7XJEMMDbkYcgehgxF4UHtgLZAtmUDhm7uUNQFmsJNi7hu4sSJ7Fq2ZGmWaUmWZEmURFHiL/E3JVISSZEiuV5eiWFsJwNGEweH9557Pvd7ec+lHBEEUhVIgSIFQQBOyKqCIAhkl+MYBuepRruNNpuVRuOo0ThqNivtGkphDYZBCaItUBhFkRSFMTRNAR+jmo+1kRDMZ4MZPGR3OY6mm51WFUUPq4V9BNlNp+Pp7TiS2i0UUmjhEEWLNF4liPqEdBqsOcU3/d7IROWUBJVBKuCJTbrZLJUOkFRqdzcuCoLnX6LIb2/HU/EH2Wy6Wq3gkDpNTVEYxASwh7opDCLBAj6EqcDv9XiJwTutVq10sLMTi8XusmwH8qBlO8y9e3c3Nu5ms2kULbZqKEk2qCYGpUwY00mAxukCTFRCWaoqqKIAfYVlOY6oVPJ7yeTG+m2aIiHpVvSlW9GXoE+RxPrtlS+/XM/nM/V6qV2vBXnhu3yIh0jfRkSRVkVBFGlNljSZ8al0r8eLTIto1PP7u/HNDSrE8+S427wWopLr63eSycTRURbHq0DoVA1wQjfcYgKFRTRZUhRWk6V+X9Zkpq/ImiypqshRJIpm4xsxtsME+rxxfxybH8fmw9QOw9y/cwtBUrVa6ZF95PN8Eib4+xlsVYqM9H2eJrM9UVR4XpJIkWVZsoGhaDKZWF9fPcazF+fgPY7NB1pXVm5tx9ZK2QO8WiEbRxjWoPFqp4UTRJ0nCQqQAFUARYhFul2hJ0kcR3N0C8dxDEVR9DCXyyUSG58uL9MUFUYGPHtxLowkCeKjjz54sLaSSOzk9v5TLKbRwmGzWiHJOtNCOaLtw2D1kxFJYgWWxfEaWi4fHOzubG3F7q+tfPbZ+//+15/eeAPyPnn9FbikYSSkLl1+DsYsvPbau+/+/eOPP1xd/XT9i9VEYgPUz2G+1arSdJOnSFGk/R3DRESeJ/B68TCPZNLCtOxglsACpBw/xjP+MjuOzQfIIBg6PM8mEpvpdLxczpNkneOIvsJ1u3y3y0cIolk6zKZSKY5jjw0LHk9FatGZJyA9z2MZZnt7M5fabTZrHE33eqKuyINBN4LVawcHCM/zEPDJ66+E76DRbV4Lq9SiM1p0xl6cC1QuXX4ufMOBHMdmMqlKpcgzjKYpuq6Z5jBSLBZlWQ5Se3LcG/fBLcc9OQ7xbvPaODYfICEvRL2wdPkCLJ4g5v1fPwtzSpJUKOQ5lh0MNGs0skajSO7gIOAdSz2OzUP243iQCmHamxe0Ny+IV87RF8/SF8/ai3P/vHQOZj5AELbD6MPB2LZs24ogSCaMDKZ5qhPWd9IPeBD5j18+AzMj+xmW7QyHA8cZO844ks0iAdLJ/e5UEmw8yQi3hHnHkNlsluNYw9AnyFKpKEkSpH742xfDaxjGh7Of9E/wnr/+i+/DnJIklUolRVFM05ggcRzPZPY6ncmH6YPf/Ogk9SQj3HKS99effRfyWJZFEKTZxHq9rm3bjjP2PC/CcVw+n3/w4AEMOokMZz/pH+PRl56x3/vx9Z9PJG5ubhYKBZ7nB4O+Ay4fqapqq9VKJBIsOzkK3rv4A3f11f/j/fmv8Pk/vzyRyHHc/v5+q9XSNM00Ddd1J0jTNAWBL5fLW1tbBEFArZB6UlO45bg+vzDe+el3YAaSJJPJJEmSsiybpuk4DmwHO3Y8Hg8GAxixvLyMoijs+9vL37MX58KMsH8q760Xvw3H1uv127dvHx2V+v2+YejjMagN2AXepeu6pmnCRVhaWmo0Gv8TGeYxvzpLXwK3vTj31k++BcdiGHbjxo1cLtftdkejkeu6sH2ysI4zHo1GPM8jCPL55yuw7+0XnnZXXxWvnAsrg36YBw+awGrRmejcN2GGO3fu5HI5SZJGoxFsCWzE87zRaMRxXCaTCXbQ2y88PY7Ni1fOhanwEQKAOP9gC1stOvPHH34DpoYJeZ4/HWmaJsMwqVQKRi/Mngl4EBPYMOBUX4vO/OHZr8M86XS60+mYpul53iPv0vM8wzBomk4mk09GwoWFpLAfZmvRmavnn4J5kskkTdOGYcDHwIKFhchjKrXoDBQXHHtfqV+YPaNFZ+iLZ6+ef+rq+afsxTlYwQE1jEylUo9FmqbJsmwmM/mkLMyeCY6Cr37gQNLC7BnP8wIkfPz9ua8F7GBmgUoEQRiGhgsbSARF4hhDy7IkSapU8oqiwL4AA1MHAxZmz9iLc/TFs8faIRhaGKwocqVSkSTJsqxgOHTAwtq2LagqSZLlcjn4hXAsDj4GKk9Sw/GyLJfLZZIkVVW1bTvcBVSanuHojq4rUkfCcbxcPpSnWmEoSZLXr19/AhLH8XBSRVHK5TKO45Ik6boeHHVBDFDpum7fsgYDqdORSJKsVCoIktrd3d3c2dna2rr3xerNmzeZ6b8JwZrDFJ0Os7y8vLa2trV1Px7fRRCkUsmTJNmRmMFgYFmW6+qe5/mVArauaRoR0zQ8X6hlWbquqIIqSRLBsjQO/h9ut9sojler1TySF0UxmCl0RFHc39/f29ur1WrtdpumaZZtdzqSqqp6V7csy9EdUJWPXhHIN03PcHVHdwZ23+pbptnrGYbRM3Rd6cv94VDDCKJSKfR6vWB4r9srVAoYhnEc1+N7ylA2DMNUTcuy/K/x0ABHq+EBpjFRaXqmB1Wahq8VqIbaQV7TANMw3IHV13Vd4RSKokqlYrFY3N/PFIvFWq1GNTFJkmQZbEvX0H1BBrBgLLDABFSQFMwALKzpLzGI8zHQAdH+GMPQbdtW1Z6mCSyrCALFUzxNU4qiiKKg63q/rzmOM0kdmvRUwFTldB4RO3SBcgGX5//5nl9CrmWNHMc2DF3XR6PRcDgcjYa6btm2Ab9Np4yBxQcy+VUSRHj2fwFdaLKhA1eSDgAAAABJRU5ErkJggg=="
)


# 实际识别使用更小的“便捷杂货店”房屋图标，既避开用户截图中的红色箭头，
# 也不受文字抗锯齿差异影响。点击位置仍由该图标计算到同一行右侧按钮。
RECORDED_REST_SHOP_ICON_TEMPLATE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAgCAIAAADvz61XAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAJLSURBVFhH1ZTNS1RRGMZPCO4SF4EMEW5c5EKSwLaCgftooVG5czMu3Alitohc6Kap9G5CIyzUNFBwEfiRghrSHxDipkVu2ihlCFHeHnnw5eWce869M8UVDz+Gd87H87vvcOeYh8/flqZWn82s5YwZefXemsoH83T6gzWVD8b6nhvnSnwxaVh7UilPTMfxcr9wFHXtdrZ8bLvaUlcHrP0ByhCLErKD3jYN3S+uXC8UCtYpH+lidolxu7EmDN3GZGomZRN8v76tg/1PY2HeDbSX5c4k3irWg8/9DV/niqxRsMYn5rdHO7U4iztdjG5EoGWsAWst/j5eleoOrhmDLIiRm4qIYSVht3/BmHivAyAuI9JuFrdn9tQKEAcWrjVZHK3UEmte3CiI637yZilFrP+mGnksALdeolvjih9Fs16x3BJyOfBucoF7oblJu7UVuOLB0muvGOeh1Nfh3sueRLiKRmnN0vHwxGKyGNBNK9IPNiPeJC77GxH1lo+4VuIVA5wR65+fX+Lj3yLjV4CCM9iG/e6wMoWQGOCk1ev80F3S03oZveoluMHhjx3A57DSNJ7f4XQgHUg0fHEcM1q7+WvTevPShWJzNQoct2I1CWs4cJKlbn+6aUUuETefCUi7pBKxviZdMRpiT7ppijWViDlgRS7BSwQNZK4YSr5lolx5cGv0zg0kWMka75qI5eUKiImISeXie+2t/whCfHrPrDGLc1Og9HiABZiMSlLreYAlzPhIdHvF1uEwfd33A5Qn/r/DygfJ4hw4I/HM2l/ywGzGISNdXwAAAABJRU5ErkJggg=="
)

_recorded_rest_bell_button_template = None
_recorded_rest_open_button_template = None
_recorded_rest_shop_anchor_template = None


@dataclass(frozen=True)
class RecordedRoutePoint:
    """保存一个严格按顺序回放的小地图坐标及动作。"""

    x: int
    y: int
    horizontal: str
    vertical: str
    action: str
    kind: str
    segment_type: str
    platform_id: Optional[str] = None
    rope_x: Optional[int] = None
    rope_top_y: Optional[int] = None
    rope_bottom_y: Optional[int] = None

    @property
    def position(self) -> Tuple[int, int]:
        """返回便于距离计算的小地图坐标。"""
        return self.x, self.y


@dataclass(frozen=True)
class RecordedPlatformRange:
    """保存一段平台路线的索引、坐标范围和完整刷图起点。"""

    start_index: int
    end_index: int
    minimum_x: int
    maximum_x: int
    minimum_y: int
    maximum_y: int
    start_x: int
    platform_number: Optional[int] = None


@dataclass(frozen=True)
class RecordedRouteVariant:
    """保存一条录制路线变体及其选择概率。"""

    name: str
    probability: int
    points: List[RecordedRoutePoint]
    closed_loop: bool
    platform_patrol: bool
    platform_min_x: Optional[int] = None
    platform_max_x: Optional[int] = None
    platform_ranges: Tuple[RecordedPlatformRange, ...] = ()


@dataclass(frozen=True)
class RecordedRoutePlan:
    """保存当前JSON内所有可回放路线及录制元数据。"""

    path: Path
    variants: List[RecordedRouteVariant]
    recording_mode: str
    route_profile: str
    rest_point: Optional["RecordedRestPoint"] = None
    fallback_rest_point: Optional["RecordedRestPoint"] = None
    fallback_rest_rope_geometry: Optional[Tuple[int, int, int]] = None


@dataclass(frozen=True)
class RecordedRestPoint:
    """保存定时休息的实际落点及进入该点所用跳跃键。"""

    x: int
    y: int
    approach_y: Optional[int] = None
    interval_minutes: Optional[float] = None
    duration_minutes: Optional[float] = None
    jump_key: str = "c"


@dataclass
class RopeEntryAdaptiveProfile:
    """Learn a reliable take-off offset and input cadence for one rope."""

    attempt_count: int = 0
    success_count: int = 0
    consecutive_failures: int = 0
    candidate_index: int = 0
    preferred_offset_x: Optional[int] = None
    last_success_offset_x: Optional[int] = None
    offset_attempts: dict = field(default_factory=dict)
    offset_successes: dict = field(default_factory=dict)
    offset_failures: dict = field(default_factory=dict)


def _normalized_rope_geometry(point) -> Optional[Tuple[int, int, int]]:
    """Return a usable rope geometry with its vertical endpoints normalized."""
    rope_x, first_y, second_y = rope_bounds(point)
    if rope_x is None or first_y is None or second_y is None:
        return None
    top_y = min(int(first_y), int(second_y))
    bottom_y = max(int(first_y), int(second_y))
    if bottom_y <= top_y:
        return None
    return int(rope_x), top_y, bottom_y


def _derive_rope_middle_rest_point(
    variants: List[RecordedRouteVariant],
) -> Tuple[Optional[RecordedRestPoint], Optional[Tuple[int, int, int]]]:
    """Use the first valid recorded rope midpoint as the deterministic fallback."""
    for variant in variants:
        for point in variant.points:
            if point.segment_type != "rope_entry":
                continue
            geometry = _normalized_rope_geometry(point)
            if geometry is None:
                continue
            rope_x, top_y, bottom_y = geometry
            middle_y = int(round((top_y + bottom_y) / 2.0))
            return RecordedRestPoint(x=rope_x, y=middle_y), geometry
    return None, None


@dataclass
class RecordedRouteState:
    """保存通用回放游标、按键、跳跃、绳子和战斗状态。"""

    route_index: Optional[int] = None
    active_variant_index: int = 0
    vertical_direction: Optional[str] = None
    last_jump_index: Optional[int] = None
    last_jump_at: float = 0.0
    last_relocalized_index: Optional[int] = None
    last_relocalized_at: float = 0.0
    completed: bool = False
    platform_direction: Optional[str] = None
    active_rope_direction: Optional[str] = None
    active_rope_x: Optional[int] = None
    active_rope_top_y: Optional[int] = None
    active_rope_bottom_y: Optional[int] = None
    active_rope_started_at: float = 0.0
    active_rope_best_y: Optional[int] = None
    active_rope_progress_at: float = 0.0
    active_rope_contacted: bool = False
    active_rope_contacted_at: float = 0.0
    active_rope_contact_start_y: Optional[int] = None
    active_rope_confirmed: bool = False
    active_rope_success_recorded: bool = False
    active_rope_contact_up_repressed: bool = False
    active_rope_last_up_key_at: float = 0.0
    active_rope_exit_direction: Optional[str] = None
    active_rope_exit_index: Optional[int] = None
    active_rope_entry_index: Optional[int] = None
    active_rope_attempt_offset_x: Optional[int] = None
    active_rope_profile_key: Optional[Tuple[int, int, int]] = None
    rope_entry_profiles: dict = field(default_factory=dict)
    rope_entry_runup_index: Optional[int] = None
    rope_entry_runup_target_offset_x: Optional[int] = None
    rope_entry_runup_staging_offset_x: Optional[int] = None
    rope_entry_runup_ready: bool = False
    rope_entry_runup_started_at: float = 0.0
    rope_stall_relocation_count: int = 0
    rope_top_exit_pending: bool = False
    rope_top_exit_direction: Optional[str] = None
    rope_top_exit_route_index: Optional[int] = None
    rope_top_exit_rope_x: Optional[int] = None
    rope_top_exit_top_y: Optional[int] = None
    rope_top_exit_bottom_y: Optional[int] = None
    rope_top_exit_start_x: Optional[int] = None
    rope_top_exit_started_at: float = 0.0
    rope_top_exit_retry_count: int = 0
    next_rope_rest_at: float = 0.0
    rope_rest_pending: bool = False
    rope_resting: bool = False
    rope_rest_until: float = 0.0
    rope_rest_remaining_seconds: float = 0.0
    rope_rest_target_y: Optional[int] = None
    rope_rest_count: int = 0
    rope_rest_test_active: bool = False
    rope_rest_test_resume_seconds: float = 0.0
    recorded_rest_point_enabled: bool = False
    recorded_rest_point: Optional[RecordedRestPoint] = None
    recorded_rest_on_rope: bool = False
    recorded_rest_rope_x: Optional[int] = None
    recorded_rest_rope_top_y: Optional[int] = None
    recorded_rest_rope_bottom_y: Optional[int] = None
    rope_rest_fallback_enabled: bool = False
    recorded_rest_phase: Optional[str] = None
    recorded_rest_jump_at: float = 0.0
    recorded_rest_jump_attempts: int = 0
    recorded_rest_recovery_count: int = 0
    recorded_rest_landing_candidate_at: float = 0.0
    recorded_rest_landing_candidate_frames: int = 0
    recorded_rest_member_panel_opened: bool = False
    recorded_rest_shop_open_failures: int = 0
    rest_point_test_parked: bool = False
    recorded_rest_interval_seconds: Optional[float] = None
    recorded_rest_duration_seconds: Optional[float] = None
    active_platform_range_index: Optional[int] = None
    platform_coverage_target_x: Optional[int] = None
    platform_coverage_route_index: Optional[int] = None
    platform_coverage_count: int = 0
    platform_dwell_range_index: Optional[int] = None
    platform_dwell_started_at: float = 0.0
    platform_dwell_forced_count: int = 0
    platform_rotation_active: bool = False
    platform_rotation_source_key: Optional[int] = None
    platform_rotation_started_at: float = 0.0
    walk_off_active: bool = False
    walk_off_direction: Optional[str] = None
    walk_off_route_index: Optional[int] = None
    walk_off_source_platform_index: Optional[int] = None
    walk_off_target_platform_index: Optional[int] = None
    walk_off_source_platform_number: Optional[int] = None
    walk_off_target_platform_number: Optional[int] = None
    walk_off_landing_x: Optional[int] = None
    walk_off_landing_y: Optional[int] = None
    walk_off_started_at: float = 0.0
    down_jump_patrol_platform_index: Optional[int] = None
    down_jump_patrol_seen_min_x: bool = False
    down_jump_patrol_seen_max_x: bool = False
    down_jump_patrol_completed: bool = False
    down_jump_patrol_started_traced: bool = False
    down_jump_random_index: Optional[int] = None
    down_jump_random_offset_x: Optional[int] = None
    ignored_platform_numbers: Tuple[int, ...] = ()
    active_ignored_platform_number: Optional[int] = None
    ignored_platform_exit_index: Optional[int] = None
    platform_replan_active: bool = False
    platform_replan_source_number: Optional[int] = None
    platform_replan_target_number: Optional[int] = None
    platform_replan_exit_index: Optional[int] = None
    platform_replan_started_at: float = 0.0
    platform_replan_count: int = 0
    platform_replan_last_platform_number: Optional[int] = None
    single_active_platform_number: Optional[int] = None
    single_active_platform_patrol_enabled: bool = False
    single_platform_stationary_enabled: bool = False
    single_platform_return_watch_platform_number: Optional[int] = None
    single_platform_return_watch_target_number: Optional[int] = None
    single_platform_return_watch_x: Optional[int] = None
    single_platform_return_watch_y: Optional[int] = None
    single_platform_return_progress_at: float = 0.0
    single_platform_return_recovery_stage: int = 0
    single_platform_return_recovery_count: int = 0
    platform_stall_direction: Optional[str] = None
    platform_stall_route_index: Optional[int] = None
    platform_stall_x: Optional[int] = None
    platform_stall_y: Optional[int] = None
    platform_stall_progress_at: float = 0.0
    platform_stall_observed_at: float = 0.0
    platform_stall_recovery_count: int = 0
    platform_stall_right_jump_attempted: bool = False
    last_rope_entry_recovery_index: Optional[int] = None
    last_rope_entry_recovery_at: float = 0.0
    last_ignored_chase_signature: Optional[tuple] = None
    last_reverse_chase_signature: Optional[tuple] = None
    last_chase_boundary_signature: Optional[tuple] = None
    chase_boundary_blocked_until: float = 0.0
    chase_boundary_blocked_direction: Optional[str] = None
    chase_boundary_blocked_platform_index: Optional[int] = None
    reverse_chase_candidate_direction: Optional[str] = None
    reverse_chase_candidate_route_direction: Optional[str] = None
    reverse_chase_candidate_detected_at: float = 0.0
    reverse_chase_candidate_started_at: float = 0.0
    reverse_chase_candidate_count: int = 0
    smart_seek_target_active: bool = False
    smart_seek_scan_until: float = 0.0
    smart_seek_mode: Optional[str] = None
    smart_seek_target_loss_started_at: float = 0.0
    route_resume_grace_until: float = 0.0
    connection_priority_active: bool = False
    connection_priority_source_platform_index: Optional[int] = None
    connection_priority_route_index: Optional[int] = None
    connection_priority_started_at: float = 0.0
    combat_lock_started_at: float = 0.0
    combat_lock_last_progress_at: float = 0.0
    combat_lock_last_attack_completed_at: float = 0.0
    last_monster_direction: Optional[str] = None
    last_monster_anchor_x: Optional[int] = None
    last_monster_nearest_dx: Optional[float] = None
    last_monster_target_x: Optional[int] = None
    loot_approach_direction: Optional[str] = None
    loot_approach_until: float = 0.0
    single_platform_loot_target_x: Optional[int] = None
    single_platform_loot_started_at: float = 0.0
    single_platform_loot_attack_completed_at: float = 0.0
    single_platform_idle_active: bool = False
    single_platform_exit_blocked_index: Optional[int] = None
    single_platform_exit_blocked_at: float = 0.0
    next_ai_action_at: float = 0.0
    ai_action_count: int = 0
    last_control_intent_at: float = 0.0
    last_control_intent_kind: Optional[str] = None
    last_observed_attack_completed_at: float = 0.0
    route_command_direction: Optional[str] = None
    route_command_observed_direction: Optional[str] = None
    route_command_anchor_x: Optional[int] = None
    route_command_progress_at: float = 0.0
    last_route_progress_at: float = 0.0
    liveness_last_checked_at: float = 0.0
    route_recovery_stage: int = 0
    route_recovery_count: int = 0
    position_missing_started_at: float = 0.0
    position_missing_last_relocate_at: float = 0.0
    focus_pause_seconds_seen: float = 0.0
    monitor_last_emitted_at: float = 0.0
    monitor_last_signature: Optional[tuple] = None
    monitor_last_platform_number: Optional[int] = None
    monitor_last_next_platform_number: Optional[int] = None
    combat: combat_logic.RouteCombatState = None

    def __post_init__(self):
        """为每次回放创建独立的公共战斗状态。"""
        if self.combat is None:
            self.combat = combat_logic.RouteCombatState()
        if self.next_ai_action_at <= 0:
            self.next_ai_action_at = time.monotonic() + random.uniform(
                ROUTE_AI_MIN_INTERVAL_SECONDS,
                ROUTE_AI_MAX_INTERVAL_SECONDS,
            )
        now = time.monotonic()
        if self.last_control_intent_at <= 0:
            self.last_control_intent_at = now
        if self.route_command_progress_at <= 0:
            self.route_command_progress_at = now
        if self.last_route_progress_at <= 0:
            self.last_route_progress_at = now
        if self.liveness_last_checked_at <= 0:
            self.liveness_last_checked_at = now


def _configure_rope_middle_fallback(state, variant) -> None:
    """Bind fallback rest state to the first valid rope in the active variant."""
    rest_point, geometry = _derive_rope_middle_rest_point([variant])
    state.recorded_rest_point = rest_point
    state.rope_rest_fallback_enabled = rest_point is not None and geometry is not None
    state.recorded_rest_on_rope = False
    if geometry is None:
        state.recorded_rest_rope_x = None
        state.recorded_rest_rope_top_y = None
        state.recorded_rest_rope_bottom_y = None
        return
    (
        state.recorded_rest_rope_x,
        state.recorded_rest_rope_top_y,
        state.recorded_rest_rope_bottom_y,
    ) = geometry


def _route_path(runtime) -> Path:
    """解析页面当前选择的自定义录制JSON绝对路径。"""
    project_root = Path(getattr(runtime, "PROJECT_ROOT", runtime.get_base_dir()))
    configured_path = getattr(
        runtime,
        "自定义录制路线文件",
        getattr(runtime, "蘑菇V3路线文件", "蘑菇V3路线.json"),
    )
    return resolve_mushroom_v3_route_path(project_root, configured_path)


def _distance(first: Tuple[int, int], second: Tuple[int, int]) -> int:
    """计算两个小地图坐标的曼哈顿距离。"""
    return abs(int(first[0]) - int(second[0])) + abs(int(first[1]) - int(second[1]))


def _rope_entry_candidate_offsets(point) -> Tuple[int, ...]:
    """绳子左右两侧都允许从正负3像素起跳。"""
    # 录制点的 horizontal 表示录制当时的跳跃方向，不代表这根绳子只能从
    # 对侧进入。候选顺序保持中立，实际首选侧由人物当前所在侧及历史成功率
    # 决定；失败后再轮换另一侧。
    return (-3, 3)


def _rope_entry_profile_key(point) -> Optional[Tuple[int, int, int]]:
    """Use normalized rope geometry as the stable runtime learning key."""
    return _normalized_rope_geometry(point)


def _rope_entry_profile(state, point) -> Optional[RopeEntryAdaptiveProfile]:
    """Get or create the in-memory adaptive profile for one rope."""
    profile_key = _rope_entry_profile_key(point)
    if profile_key is None:
        return None
    profile = state.rope_entry_profiles.get(profile_key)
    if profile is None:
        profile = RopeEntryAdaptiveProfile()
        state.rope_entry_profiles[profile_key] = profile
    return profile


def _rope_entry_target_offset(
    state,
    point,
    position_x: Optional[int] = None,
    rope_x: Optional[int] = None,
) -> int:
    """按历史成功率和人物当前侧选择本次起跳偏移。"""
    if state.rope_entry_runup_target_offset_x in (-3, 3):
        # 一次助跑开始后锁定起跳侧，不能在靠近绳子过程中逐帧换边。
        return int(state.rope_entry_runup_target_offset_x)
    candidates = _rope_entry_candidate_offsets(point)
    profile = _rope_entry_profile(state, point)
    if profile is None:
        if position_x is not None and rope_x is not None:
            return -3 if int(position_x) <= int(rope_x) else 3
        return candidates[0]
    current_side_offset = None
    if position_x is not None and rope_x is not None:
        current_side_offset = -3 if int(position_x) <= int(rope_x) else 3
    if current_side_offset in candidates and profile.consecutive_failures == 0:
        # 正常一轮永远就近使用人物当前所在侧；历史成功率不能让人物横穿绳子
        # 去另一侧。只有本次实际失败、consecutive_failures>0 后才启用自适应
        # 候选和成功率排序。
        profile.candidate_index = candidates.index(current_side_offset)
        return int(current_side_offset)
    successful_candidates = [
        int(offset)
        for offset in candidates
        if int(profile.offset_successes.get(int(offset), 0)) > 0
    ]
    if successful_candidates and profile.consecutive_failures == 0:
        return max(
            successful_candidates,
            key=lambda offset: (
                float(profile.offset_successes.get(offset, 0))
                / max(1, int(profile.offset_attempts.get(offset, 0))),
                int(profile.offset_successes.get(offset, 0)),
                -int(profile.offset_failures.get(offset, 0)),
                int(offset == current_side_offset),
                -candidates.index(offset),
            ),
        )
    profile.candidate_index %= len(candidates)
    return int(candidates[profile.candidate_index])


def _rope_entry_jump_direction(
    position_x: int,
    rope_x: int,
    target_offset_x: Optional[int] = None,
) -> Optional[str]:
    """到达绳子左右精确3像素起跳点后，返回朝向绳子的跳跃方向。"""
    offset_x = int(position_x) - int(rope_x)
    if target_offset_x is not None:
        target_offset_x = int(target_offset_x)
        if (
            target_offset_x < 0
            and ROUTE_ROPE_ENTRY_LEFT_MIN_OFFSET_X
            <= offset_x
            <= ROUTE_ROPE_ENTRY_LEFT_MAX_OFFSET_X
        ):
            return "right"
        if (
            target_offset_x > 0
            and ROUTE_ROPE_ENTRY_RIGHT_MIN_OFFSET_X
            <= offset_x
            <= ROUTE_ROPE_ENTRY_RIGHT_MAX_OFFSET_X
        ):
            return "left"
        return None
    if ROUTE_ROPE_ENTRY_LEFT_MIN_OFFSET_X <= offset_x <= ROUTE_ROPE_ENTRY_LEFT_MAX_OFFSET_X:
        return "right"
    if ROUTE_ROPE_ENTRY_RIGHT_MIN_OFFSET_X <= offset_x <= ROUTE_ROPE_ENTRY_RIGHT_MAX_OFFSET_X:
        return "left"
    return None


def _rope_entry_approach_direction(state, point, position_x: int, rope_x: int) -> str:
    """Move into the learned take-off band instead of requiring one exact X."""
    current_x = int(position_x)
    rope_x = int(rope_x)
    target_offset_x = _rope_entry_target_offset(
        state,
        point,
        position_x=position_x,
        rope_x=rope_x,
    )
    if _rope_entry_jump_direction(
        current_x,
        rope_x,
        target_offset_x=target_offset_x,
    ) is not None:
        return "none"
    target_x = rope_x + target_offset_x
    if current_x < target_x:
        return "right"
    if current_x > target_x:
        return "left"
    return "none"


def _clear_rope_entry_runup(state) -> None:
    """Clear the temporary back-off-and-return phase for one rope attempt."""

    state.rope_entry_runup_index = None
    state.rope_entry_runup_target_offset_x = None
    state.rope_entry_runup_staging_offset_x = None
    state.rope_entry_runup_ready = False
    state.rope_entry_runup_started_at = 0.0


def _rope_entry_runup_extra_x(state, point) -> int:
    """Increase the run-up distance slightly after repeated misses."""

    profile = _rope_entry_profile(state, point)
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    return min(
        ROUTE_ROPE_ENTRY_MAX_RUNUP_EXTRA_X,
        ROUTE_ROPE_ENTRY_RUNUP_EXTRA_X + failure_count // 2,
    )


def _apply_rope_entry_runup(
    runtime,
    state,
    variant,
    point,
    entry_index: int,
    position,
    rope_x: int,
) -> str:
    """Back away within the current platform, then turn toward the rope."""

    position_x = int(position[0])
    rope_x = int(rope_x)
    target_offset_x = _rope_entry_target_offset(
        state,
        point,
        position_x=position_x,
        rope_x=rope_x,
    )
    profile = _rope_entry_profile(state, point)
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    if failure_count <= 0:
        # 第一次尝试优先使用人物当前所在侧。只要已进入左右任一 ±1~3px
        # 起跳带，就直接朝绳子跳；不再固定先向外走、再回头制造机械折返。
        state.rope_entry_runup_index = int(entry_index)
        state.rope_entry_runup_target_offset_x = int(target_offset_x)
        state.rope_entry_runup_staging_offset_x = int(target_offset_x)
        state.rope_entry_runup_ready = True
        return _rope_entry_approach_direction(
            state,
            point,
            position_x,
            rope_x,
        )

    side = -1 if target_offset_x < 0 else 1
    requested_runup_extra_x = _rope_entry_runup_extra_x(state, point)
    target_x = rope_x + target_offset_x
    desired_staging_x = target_x + side * requested_runup_extra_x
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=entry_index,
    )
    platform_bounds = None
    if platform_index is None:
        # 无法确认平台边界时禁止额外向外后退，只保留容差起跳修复。
        staging_x = target_x
    else:
        platform = variant.platform_ranges[platform_index]
        safe_min_x = int(platform.minimum_x) + ROUTE_ROPE_ENTRY_RUNUP_EDGE_MARGIN_X
        safe_max_x = int(platform.maximum_x) - ROUTE_ROPE_ENTRY_RUNUP_EDGE_MARGIN_X
        platform_bounds = [int(platform.minimum_x), int(platform.maximum_x)]
        if safe_min_x > safe_max_x:
            staging_x = target_x
        elif side < 0:
            staging_x = min(target_x, max(safe_min_x, desired_staging_x))
        else:
            staging_x = max(target_x, min(safe_max_x, desired_staging_x))
    staging_offset_x = int(staging_x) - rope_x
    actual_runup_extra_x = abs(int(staging_x) - target_x)
    runup_changed = (
        state.rope_entry_runup_index != int(entry_index)
        or state.rope_entry_runup_target_offset_x != int(target_offset_x)
        or state.rope_entry_runup_staging_offset_x != int(staging_offset_x)
    )
    if runup_changed:
        state.rope_entry_runup_index = int(entry_index)
        state.rope_entry_runup_target_offset_x = int(target_offset_x)
        state.rope_entry_runup_staging_offset_x = int(staging_offset_x)
        state.rope_entry_runup_ready = False
        state.rope_entry_runup_started_at = time.monotonic()
        runtime.trace_event(
            "recorded_route_rope_runup_started",
            position_x=position_x,
            rope_x=rope_x,
            route_index=entry_index,
            target_offset_x=target_offset_x,
            staging_offset_x=staging_offset_x,
            requested_runup_extra_x=requested_runup_extra_x,
            actual_runup_extra_x=actual_runup_extra_x,
            platform_index=platform_index,
            platform_bounds=platform_bounds,
            edge_margin_x=ROUTE_ROPE_ENTRY_RUNUP_EDGE_MARGIN_X,
            runup_clamped=actual_runup_extra_x < requested_runup_extra_x,
            action="back_off_then_turn_toward_rope",
        )

    if not state.rope_entry_runup_ready:
        staging_x = rope_x + staging_offset_x
        staging_reached = (
            position_x <= staging_x if side < 0 else position_x >= staging_x
        )
        if not staging_reached:
            return "left" if side < 0 else "right"
        state.rope_entry_runup_ready = True
        runtime.trace_event(
            "recorded_route_rope_runup_turn_back",
            position_x=position_x,
            rope_x=rope_x,
            route_index=entry_index,
            target_offset_x=target_offset_x,
            staging_offset_x=staging_offset_x,
            runup_distance_x=abs(position_x - (rope_x + target_offset_x)),
            action="return_to_takeoff_band",
        )

    return _rope_entry_approach_direction(state, point, position_x, rope_x)


def _parse_points(raw_points, route_path: Path, route_name: str) -> List[RecordedRoutePoint]:
    """校验JSON动作并转换为不可变路线点。"""
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError("{}没有足够的有效坐标：{}".format(route_name, route_path))
    points: List[RecordedRoutePoint] = []
    for index, raw_point in enumerate(raw_points):
        try:
            horizontal, vertical, action = str(raw_point["command"]).split()
            if horizontal not in ("left", "right", "none", "stop"):
                raise ValueError("未知水平动作")
            if vertical not in ("up", "down", "none", "stop"):
                raise ValueError("未知垂直动作")
            if action not in ("jump", "none"):
                raise ValueError("未知一次性动作")
            points.append(
                RecordedRoutePoint(
                    x=int(raw_point["x"]),
                    y=int(raw_point["y"]),
                    horizontal=horizontal,
                    vertical=vertical,
                    action=action,
                    kind=str(raw_point.get("kind", "unknown")),
                    segment_type=str(raw_point.get("segment_type", "unknown")),
                    platform_id=(
                        str(raw_point["platform_id"])
                        if raw_point.get("platform_id") is not None
                        else None
                    ),
                    rope_x=(int(raw_point["rope_x"]) if raw_point.get("rope_x") is not None else None),
                    rope_top_y=(int(raw_point["rope_top_y"]) if raw_point.get("rope_top_y") is not None else None),
                    rope_bottom_y=(int(raw_point["rope_bottom_y"]) if raw_point.get("rope_bottom_y") is not None else None),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("{}第{}个坐标无效：{}".format(route_name, index, exc)) from exc
    # 兼容旧JSON：同一分段的同一坐标可能因30Hz采样保存了相反方向命令，
    # 最近点选择会在这些索引之间抖动。运行时也做一次无损去重；若同点包含
    # 跳跃动作，优先保留跳跃，分段边界不同的同坐标仍分别保留。
    deduplicated: List[RecordedRoutePoint] = []
    deduplicated_raw_points = []
    coordinate_indices = {}
    fallback_run_index = 0
    previous_fallback_signature = None
    for raw_point, point in zip(raw_points, points):
        segment_id = raw_point.get("segment_id")
        fallback_signature = (point.segment_type, point.platform_id)
        if fallback_signature != previous_fallback_signature:
            fallback_run_index += 1
            previous_fallback_signature = fallback_signature
        group_key = (
            ("segment", int(segment_id))
            if segment_id is not None
            else ("run", fallback_run_index)
        )
        coordinate_key = (
            group_key,
            # 同一绳段的绳顶坐标会同时包含 rope 和 rope_exit；出口是必须
            # 执行的状态转换，不能因为坐标相同被绳身点吞掉。
            point.segment_type,
            # 平台完整巡逻后返回下一连接会第二次经过相同坐标。生成器用
            # route_pass 标记这段有意折返，播放器也必须按通行阶段保留。
            str(raw_point.get("route_pass", "primary")),
            int(point.x),
            int(point.y),
        )
        existing_index = coordinate_indices.get(coordinate_key)
        if existing_index is None:
            coordinate_indices[coordinate_key] = len(deduplicated)
            deduplicated.append(point)
            deduplicated_raw_points.append(raw_point)
            continue
        existing = deduplicated[existing_index]
        if existing.action != "jump" and point.action == "jump":
            deduplicated[existing_index] = point
            deduplicated_raw_points[existing_index] = raw_point

    grouped_indices = {}
    for index, (raw_point, point) in enumerate(
        zip(deduplicated_raw_points, deduplicated)
    ):
        if (
            point.segment_type != "platform"
            or point.vertical not in ("none", "stop")
            or point.action != "none"
        ):
            continue
        segment_id = raw_point.get("segment_id")
        key = (
            ("segment", int(segment_id))
            if segment_id is not None
            else ("platform", point.platform_id)
        )
        key = (key, str(raw_point.get("route_pass", "primary")))
        grouped_indices.setdefault(key, []).append(index)
    for indices in grouped_indices.values():
        if len(indices) < 2:
            continue
        first_x = int(deduplicated[indices[0]].x)
        last_x = int(deduplicated[indices[-1]].x)
        if abs(last_x - first_x) < ROUTE_PLATFORM_DIRECTION_NORMALIZE_MIN_SPAN:
            continue
        dominant = "right" if last_x > first_x else "left"
        for index in indices:
            point = deduplicated[index]
            if point.horizontal in ("left", "right") and point.horizontal != dominant:
                deduplicated[index] = replace(point, horizontal=dominant)
    if len(deduplicated) < 2:
        raise ValueError("{}去重后没有足够的有效坐标：{}".format(route_name, route_path))
    return deduplicated


def _infer_legacy_rope_geometry(
    points: List[RecordedRoutePoint],
) -> List[RecordedRoutePoint]:
    """从旧JSON的连续绳身点推算入口几何，不修改磁盘上的录制文件。"""
    normalized = list(points)
    for index, entry in enumerate(points):
        if (
            entry.segment_type != "rope_entry"
            or has_complete_rope_geometry(entry)
        ):
            continue
        rope_body = []
        for candidate in points[index + 1:]:
            if candidate.segment_type != "rope":
                break
            rope_body.append(candidate)
        if not rope_body:
            continue
        rope_x_values = sorted(int(point.x) for point in rope_body)
        rope_y_values = [int(point.y) for point in rope_body]
        rope_x = rope_x_values[len(rope_x_values) // 2]
        rope_top_y = min(rope_y_values)
        rope_bottom_y = max([int(entry.y)] + rope_y_values)
        if rope_bottom_y <= rope_top_y:
            continue
        normalized[index] = replace(
            entry,
            rope_x=rope_x,
            rope_top_y=rope_top_y,
            rope_bottom_y=rope_bottom_y,
        )
    # 更老的JSON可能只有rope绳身点，没有可执行的rope_entry。若休息点选择了
    # 这段绳子，按绳身范围和前一平台的行进方向合成入口；只修改内存路线。
    result = []
    index = 0
    while index < len(normalized):
        point = normalized[index]
        if point.segment_type != "rope":
            result.append(point)
            index += 1
            continue
        rope_run = []
        while index < len(normalized) and normalized[index].segment_type == "rope":
            rope_run.append(normalized[index])
            index += 1
        has_entry = bool(
            result
            and result[-1].segment_type == "rope_entry"
            and has_complete_rope_geometry(result[-1])
        )
        if not has_entry and rope_run:
            rope_x_values = sorted(int(item.x) for item in rope_run)
            rope_y_values = [int(item.y) for item in rope_run]
            rope_x = rope_x_values[len(rope_x_values) // 2]
            rope_top_y = min(rope_y_values)
            rope_bottom_y = max(rope_y_values)
            preceding_platform = next(
                (
                    candidate
                    for candidate in reversed(result)
                    if candidate.segment_type == "platform"
                ),
                None,
            )
            if rope_bottom_y > rope_top_y and preceding_platform is not None:
                approach_from_left = preceding_platform.horizontal == "right"
                result.append(
                    RecordedRoutePoint(
                        x=rope_x - 2 if approach_from_left else rope_x + 2,
                        y=int(preceding_platform.y),
                        horizontal="right" if approach_from_left else "left",
                        vertical="up",
                        action="jump",
                        kind="jump",
                        segment_type="rope_entry",
                        platform_id=preceding_platform.platform_id,
                        rope_x=rope_x,
                        rope_top_y=rope_top_y,
                        rope_bottom_y=rope_bottom_y,
                    )
                )
        result.extend(rope_run)
    return result


def _normalize_rope_entry_approach(
    points: List[RecordedRoutePoint],
) -> List[RecordedRoutePoint]:
    """Keep a generated rope-entry point on the side used by its platform run-up.

    A segmented recording stores both legal rope entry rules, while an assembled
    playback variant selects one of them.  Some older assembled variants keep the
    platform return sweep from one side but retain the opposite entry rule.  For
    example, a sweep ending at ``rope_x + 2`` followed by ``rope_x - 2`` with a
    right jump makes the nearest-point cursor prefer the platform point forever.

    Playback already chooses the actual take-off side adaptively.  This
    normalization only makes the entry waypoint eligible as soon as the platform
    sweep reaches the rope, so connection priority can take ownership before
    combat or relocalization has a chance to pull the cursor back to the platform.
    It changes the in-memory route only; the user's recording JSON remains intact.
    """
    normalized = list(points)
    for index, entry in enumerate(points):
        if (
            index <= 0
            or entry.segment_type != "rope_entry"
            or entry.action != "jump"
            or not has_complete_rope_geometry(entry)
        ):
            continue
        preceding = normalized[index - 1]
        if preceding.segment_type != "platform":
            continue
        rope_x, _rope_top_y, _rope_bottom_y = rope_bounds(entry)
        if rope_x is None:
            continue

        # Prefer the actual final platform coordinate.  When it overlaps the
        # rope exactly, fall back to the recorded sweep direction: left means
        # that the character arrived from the right, and vice versa.
        previous_offset_x = int(preceding.x) - int(rope_x)
        if previous_offset_x > 0:
            approach_side = 1
        elif previous_offset_x < 0:
            approach_side = -1
        elif preceding.horizontal == "left":
            approach_side = 1
        elif preceding.horizontal == "right":
            approach_side = -1
        else:
            continue

        # The runtime accepts a ±1..3px take-off band.  Preserve a compatible
        # recorded offset when available, otherwise use the conventional ±2px
        # waypoint produced by the recording graph.
        magnitude = abs(previous_offset_x)
        if magnitude < 1 or magnitude > 3:
            magnitude = 2
        normalized[index] = replace(
            entry,
            x=int(rope_x) + approach_side * magnitude,
            y=int(preceding.y),
            horizontal="left" if approach_side > 0 else "right",
        )
    return normalized


def _is_closed_variant(data, raw_variant, points: List[RecordedRoutePoint]) -> bool:
    """根据回程段或首尾距离判断路线是否确实形成闭环。"""
    if isinstance(raw_variant, dict) and "closed_loop" in raw_variant:
        return bool(raw_variant.get("closed_loop"))
    transition_id = raw_variant.get("transition_segment_id") if isinstance(raw_variant, dict) else None
    graph = data.get("route_graph") if isinstance(data, dict) else None
    transitions = graph.get("return_transitions") if isinstance(graph, dict) else None
    if transition_id is not None or (isinstance(transitions, list) and transitions):
        return True
    return _distance(points[0].position, points[-1].position) <= ROUTE_RELOCALIZE_DISTANCE


def _platform_patrol_bounds(points, closed_loop: bool) -> Tuple[bool, Optional[int], Optional[int]]:
    """把仅包含平台移动的非闭环录制识别为左右边界往返路线。"""
    platform_patrol = (
        not closed_loop
        and all(point.segment_type == "platform" for point in points)
        and all(point.vertical in ("none", "stop") for point in points)
        and all(point.action == "none" for point in points)
    )
    if not platform_patrol:
        return False, None, None
    xs = [int(point.x) for point in points]
    minimum_x = min(xs)
    maximum_x = max(xs)
    if maximum_x - minimum_x < 4:
        return False, None, None
    return True, minimum_x, maximum_x


def _platform_number(platform_id) -> Optional[int]:
    """Extract the numeric platform label used by route previews and settings."""
    if platform_id is None:
        return None
    digits = "".join(character for character in str(platform_id) if character.isdigit())
    return int(digits) if digits else None


def _recorded_platform_ranges(points) -> Tuple[RecordedPlatformRange, ...]:
    """把连续的平台点整理成可用于切换后完整覆盖的平台范围。"""
    ranges = []
    start_index = None
    active_platform_id = None
    for index in range(len(points) + 1):
        point = points[index] if index < len(points) else None
        is_platform = point is not None and point.segment_type == "platform"
        point_platform_id = point.platform_id if is_platform else None
        same_platform = (
            is_platform
            and start_index is not None
            and point_platform_id == active_platform_id
        )
        if is_platform and start_index is None:
            start_index = index
            active_platform_id = point_platform_id
            continue
        if same_platform:
            continue
        if start_index is not None:
            end_index = index - 1
            segment = points[start_index:index]
            xs = [int(segment_point.x) for segment_point in segment]
            ys = [int(segment_point.y) for segment_point in segment]
            platform_number = _platform_number(active_platform_id)
            if platform_number is None:
                platform_number = len(ranges) + 1
            ranges.append(
                RecordedPlatformRange(
                    start_index=start_index,
                    end_index=end_index,
                    minimum_x=min(xs),
                    maximum_x=max(xs),
                    minimum_y=min(ys),
                    maximum_y=max(ys),
                    start_x=int(segment[0].x),
                    platform_number=platform_number,
                )
            )
            start_index = None
            active_platform_id = None
        if is_platform:
            start_index = index
            active_platform_id = point_platform_id
    return tuple(ranges)


def _load_recorded_route(runtime) -> RecordedRoutePlan:
    """读取当前选择的JSON，且不注入或改写任何录制动作。"""
    route_path = _route_path(runtime)
    if not route_path.exists():
        raise FileNotFoundError("自定义录制路线不存在：{}".format(route_path))
    data = json.loads(route_path.read_text(encoding="utf-8"))
    try:
        schema_version = int(data.get("schema_version", 1))
    except (AttributeError, TypeError, ValueError):
        schema_version = 1
    raw_rest_point = data.get("rest_point") if isinstance(data, dict) else None
    rest_point = None
    if isinstance(raw_rest_point, dict):
        try:
            rest_point = RecordedRestPoint(
                x=int(raw_rest_point["x"]),
                y=int(raw_rest_point["y"]),
                approach_y=(
                    int(raw_rest_point["approach_y"])
                    if raw_rest_point.get("approach_y") is not None
                    else None
                ),
                interval_minutes=(
                    float(raw_rest_point["interval_minutes"])
                    if raw_rest_point.get("interval_minutes") is not None
                    else None
                ),
                duration_minutes=(
                    float(raw_rest_point["duration_minutes"])
                    if raw_rest_point.get("duration_minutes") is not None
                    else None
                ),
                jump_key=str(raw_rest_point.get("jump_key", "c")),
            )
        except (KeyError, TypeError, ValueError):
            rest_point = None
    variants: List[RecordedRouteVariant] = []
    raw_variants = data.get("route_variants") if isinstance(data, dict) else None
    route_graph = data.get("route_graph") if isinstance(data, dict) else None
    graph_platforms = (
        route_graph.get("platforms")
        if isinstance(route_graph, dict)
        else None
    )
    if (
        schema_version < 2
        and
        isinstance(graph_platforms, list)
        and len(graph_platforms) > 1
        and isinstance(data.get("raw_points"), list)
        and isinstance(data.get("segments"), list)
    ):
        rebuilt_variants = build_route_variants_for_playback(
            data["raw_points"],
            data["segments"],
            route_graph,
        )
        if rebuilt_variants:
            raw_variants = rebuilt_variants
    if isinstance(raw_variants, list):
        for index, raw_variant in enumerate(raw_variants):
            if not isinstance(raw_variant, dict):
                continue
            name = str(raw_variant.get("name", "variant_{}".format(index + 1)))
            points = _normalize_rope_entry_approach(
                _infer_legacy_rope_geometry(
                    _parse_points(raw_variant.get("points"), route_path, name)
                )
            )
            closed_loop = _is_closed_variant(data, raw_variant, points)
            platform_patrol, minimum_x, maximum_x = _platform_patrol_bounds(
                points,
                closed_loop,
            )
            platform_ranges = _recorded_platform_ranges(points)
            variants.append(
                RecordedRouteVariant(
                    name=name,
                    probability=max(0, int(raw_variant.get("probability", 0))),
                    points=points,
                    closed_loop=closed_loop,
                    platform_patrol=platform_patrol,
                    platform_min_x=minimum_x,
                    platform_max_x=maximum_x,
                    platform_ranges=platform_ranges,
                )
            )
    if not variants:
        points = _normalize_rope_entry_approach(_infer_legacy_rope_geometry(
            _parse_points(data.get("points"), route_path, "自定义录制路线")
        ))
        closed_loop = _is_closed_variant(data, {}, points)
        platform_patrol, minimum_x, maximum_x = _platform_patrol_bounds(
            points,
            closed_loop,
        )
        platform_ranges = _recorded_platform_ranges(points)
        variants.append(
            RecordedRouteVariant(
                name="recorded_route",
                probability=100,
                points=points,
                closed_loop=closed_loop,
                platform_patrol=platform_patrol,
                platform_min_x=minimum_x,
                platform_max_x=maximum_x,
                platform_ranges=platform_ranges,
            )
        )
    fallback_rest_point = None
    fallback_rest_rope_geometry = None
    if rest_point is None:
        (
            fallback_rest_point,
            fallback_rest_rope_geometry,
        ) = _derive_rope_middle_rest_point(variants)
    return RecordedRoutePlan(
        path=route_path,
        variants=variants,
        recording_mode=str(data.get("recording_mode", "legacy_ordered")),
        route_profile=str(data.get("route_profile", "strict_recorded")),
        rest_point=rest_point,
        fallback_rest_point=fallback_rest_point,
        fallback_rest_rope_geometry=fallback_rest_rope_geometry,
    )


def _choose_variant_index(plan: RecordedRoutePlan) -> int:
    """按照JSON保存的概率选择当前回放变体。"""
    weights = [max(0, variant.probability) for variant in plan.variants]
    if sum(weights) <= 0:
        weights = [1 for _variant in plan.variants]
    return random.choices(range(len(plan.variants)), weights=weights, k=1)[0]


def _candidate_indices(state, variant: RecordedRouteVariant) -> List[int]:
    """只从当前游标向前选点；受击小位移不能把路线拉回已完成坐标。"""
    total = len(variant.points)
    if state.route_index is None or variant.platform_patrol:
        return list(range(total))
    start = int(state.route_index)
    end = int(state.route_index) + ROUTE_LOOKAHEAD_POINTS
    if variant.closed_loop:
        candidates = [index % total for index in range(start, end)]
    else:
        candidates = list(range(max(0, start), min(total, end)))
    if state.connection_priority_active:
        return candidates

    # 下跳点、绳入口和走出台阶是平台切换边界。连接尚未正式取得独占权时，
    # 普通前瞻不能越过该边界去匹配下一平台的同坐标副本；否则人物仍在上层
    # 平台时，游标就可能提前跳到下层平台，绕过录制的下跳/上绳动作。
    bounded_candidates = []
    for index in candidates:
        bounded_candidates.append(index)
        if variant.points[index].segment_type in RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES:
            break
    return bounded_candidates


def _nearest_index(points, position, candidates) -> Tuple[int, int]:
    """在候选点中返回离人物最近且同距离时更靠后的索引。"""
    if not candidates:
        raise ValueError("路线候选点不能为空")
    selected = int(candidates[0])
    selected_distance = _distance(points[selected].position, position)
    for index in candidates[1:]:
        current_distance = _distance(points[index].position, position)
        if current_distance <= selected_distance:
            selected = int(index)
            selected_distance = current_distance
    return selected, selected_distance


def _preceding_rope_entry_index(variant, selected_index: int) -> Optional[int]:
    """从绳身点向前查找同一段绳子的动态入口点。"""
    total = len(variant.points)
    for offset in range(1, total):
        raw_index = int(selected_index) - offset
        if raw_index < 0 and not variant.closed_loop:
            break
        index = raw_index % total
        point = variant.points[index]
        if point.segment_type == "rope_entry" and has_complete_rope_geometry(point):
            return index
        if point.segment_type != "rope":
            break
    return None


def _recover_missed_rope_entry(runtime, state, variant, selected_index, position) -> int:
    """人物错过绳子却匹配到绳身点时，回退到入口重新对齐并起跳。"""
    selected_point = variant.points[selected_index]
    if state.active_rope_x is not None or selected_point.segment_type != "rope":
        return selected_index

    entry_index = _preceding_rope_entry_index(variant, selected_index)

    # 下跳或走落后，前向游标可能仍落在上一圈的绳身上。人物一旦已经进入
    # 任一真实平台的容差范围，应以实际落台位置为准重新规划；否则旧逻辑会
    # 把刚落地的人物反复拉回上一条绳子的入口。
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=None,
    )
    if platform_index is not None:
        platform = variant.platform_ranges[int(platform_index)]
        # 人物仍位于该绳入口前的平台末段时，小地图±1～2px抖动可能让未来
        # 绳身点比平台点更近。这里应继续进入紧邻的 rope_entry，而不是按同一
        # 平台的重复坐标重新定位回较早位置。真正从连接落到其他平台时，当前
        # 平台不会同时满足“紧邻该入口且游标仍在入口前平台范围”条件。
        if entry_index is not None:
            preceding_platform_index = _preceding_platform_range_index(
                variant,
                entry_index,
            )
            route_index = state.route_index
            cursor_still_on_preceding_platform = (
                route_index is not None
                and (
                    int(platform.start_index)
                    <= int(route_index)
                    <= int(platform.end_index)
                    # 小地图抖动可能提前一帧把游标推进相邻 rope_entry；人物
                    # 实际仍在入口平台时也应保持前向入口，不能按重复坐标
                    # 重定位回完整平台的较早位置。
                    or int(route_index) == int(entry_index)
                )
            )
            if (
                preceding_platform_index == int(platform_index)
                and cursor_still_on_preceding_platform
                and int(entry_index) == int(platform.end_index) + 1
            ):
                runtime.trace_event(
                    "recorded_route_rope_entry_jitter_recovered",
                    position=position,
                    selected_rope_index=selected_index,
                    recovered_entry_index=entry_index,
                    platform_index=platform_index,
                    platform_number=platform.platform_number,
                    action="keep_forward_cursor_at_adjacent_rope_entry",
                )
                return int(entry_index)
        platform_candidates = list(
            range(int(platform.start_index), int(platform.end_index) + 1)
        )
        platform_route_index, platform_distance = _nearest_index(
            variant.points,
            position,
            platform_candidates,
        )
        state.last_rope_entry_recovery_index = None
        state.last_rope_entry_recovery_at = 0.0
        runtime.trace_event(
            "recorded_route_landed_platform_relocalized",
            position=position,
            previous_rope_index=selected_index,
            route_index=int(platform_route_index),
            platform_index=int(platform_index),
            platform_number=platform.platform_number,
            distance=int(platform_distance),
            action="prefer_actual_landing_platform_over_stale_rope_cursor",
        )
        return int(platform_route_index)
    if entry_index is None:
        return selected_index
    entry_point = variant.points[entry_index]
    rope_x, _top_y, bottom_y = rope_bounds(entry_point)
    if rope_x is None or bottom_y is None:
        return selected_index
    below_rope = int(position[1]) > int(bottom_y)
    away_from_rope = (
        abs(int(position[0]) - int(rope_x)) > ROUTE_ROPE_BODY_TOLERANCE_X
    )
    if not below_rope and not away_from_rope:
        return selected_index

    now = time.monotonic()
    if (
        entry_index != state.last_rope_entry_recovery_index
        or now - state.last_rope_entry_recovery_at >= 1.0
    ):
        state.last_rope_entry_recovery_index = entry_index
        state.last_rope_entry_recovery_at = now
        runtime.trace_event(
            "recorded_route_missed_rope_recovered",
            position=position,
            selected_rope_index=selected_index,
            recovered_entry_index=entry_index,
            rope_x=rope_x,
            rope_bottom_y=bottom_y,
            below_rope=below_rope,
            away_from_rope=away_from_rope,
            action="return_to_rope_entry",
        )
    return entry_index


def _rest_navigation_rope_entry_index(state, variant, position) -> Optional[int]:
    """休息导航接近目标绳底时返回对应入口，避免继续走出平台边缘。"""
    if (
        not _rest_route_navigation_active(state)
        or state.rest_point_test_parked
        or state.rope_resting
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
        or state.recorded_rest_rope_x is None
        or state.recorded_rest_rope_top_y is None
        or state.recorded_rest_rope_bottom_y is None
    ):
        return None

    target_geometry = (
        int(state.recorded_rest_rope_x),
        min(
            int(state.recorded_rest_rope_top_y),
            int(state.recorded_rest_rope_bottom_y),
        ),
        max(
            int(state.recorded_rest_rope_top_y),
            int(state.recorded_rest_rope_bottom_y),
        ),
    )
    position_x = int(position[0])
    position_y = int(position[1])
    target_rope_x, _target_top_y, target_bottom_y = target_geometry
    if (
        abs(position_x - target_rope_x)
        > RECORDED_REST_ROPE_ENTRY_LOCK_RADIUS_X
        or position_y < target_bottom_y - ROUTE_ROPE_ENTRY_TRIGGER_Y
        or position_y > target_bottom_y + ROUTE_PLATFORM_MATCH_TOLERANCE_Y
    ):
        return None

    candidates = []
    for index, point in enumerate(variant.points):
        if point.segment_type != "rope_entry" or point.action != "jump":
            continue
        geometry = _normalized_rope_geometry(point)
        if geometry != target_geometry:
            continue
        candidates.append(
            (
                abs(position_x - int(point.x))
                + abs(position_y - int(point.y)),
                int(index),
            )
        )
    if not candidates:
        return None
    return min(candidates)[1]


def _platform_rotation_rope_entry_index(state, variant, position) -> Optional[int]:
    """Claim the next rope entry as soon as a platform rotation reaches its bottom.

    Platform rotations intentionally permit combat while moving across the source
    platform.  Once the character is at the rope bottom, however, yielding one
    more frame to ordinary nearest-point selection can pick an earlier overlapping
    platform coordinate after combat releases.  Select the forward entry here so
    the regular connection-priority branch claims it in the same loop iteration.
    """
    if (
        not state.platform_rotation_active
        or state.connection_priority_active
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
    ):
        return None
    current_platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if current_platform_index is None:
        return None

    candidates = []
    position_x, position_y = int(position[0]), int(position[1])
    # The regular cursor look-ahead is intentionally short to avoid combat
    # jitter jumping across a platform.  During a timed rotation the cursor may
    # still be an older patrol point (the observed index 125), so scan entries
    # globally here and retain only the one directly owned by this platform.
    for index, point in enumerate(variant.points):
        if (
            point.segment_type != "rope_entry"
            or point.action != "jump"
            or not has_complete_rope_geometry(point)
            or _preceding_platform_range_index(variant, int(index))
            != int(current_platform_index)
        ):
            continue
        rope_x, _top_y, bottom_y = rope_bounds(point)
        if rope_x is None or bottom_y is None:
            continue
        # This is deliberately tighter than the generic relocalize window.  It
        # only captures the short take-off band around the rope, never a normal
        # platform patrol point such as the observed [45, 154] relocalization.
        if (
            abs(position_x - int(rope_x))
            > ROUTE_ROPE_ENTRY_RUNUP_EXTRA_X + ROUTE_ROPE_ENTRY_RIGHT_MAX_OFFSET_X
            or position_y < int(bottom_y) - ROUTE_ROPE_ENTRY_TRIGGER_Y
            or position_y > int(bottom_y) + ROUTE_PLATFORM_MATCH_TOLERANCE_Y
        ):
            continue
        candidates.append(
            (
                abs(position_x - int(point.x)) + abs(position_y - int(point.y)),
                int(index),
            )
        )
    return min(candidates)[1] if candidates else None


def _select_route_index(runtime, state, variant, position) -> int:
    """沿当前录制顺序匹配坐标，偏离过远时才在本JSON内全局重定位。"""
    # 单一有效平台的正常刷怪不再让游标靠近平台后的右跳、走出、下跳或绳子
    # 连接点。若先选中连接点、再由动作层阻止离台，就会形成每帧“松开方向键
    # -> 平台巡逻重新按下”的脚步抖动。这里直接把候选范围限制在人物当前所在
    # 的唯一有效平台内部，使平台巡逻从选点到按键全程保持同一个控制权。
    single_platform_index = _single_platform_route_index(
        state,
        variant,
        position,
    )
    if single_platform_index is not None:
        return int(single_platform_index)
    # 定向走出开始后不再按“最近坐标”切换游标。人物离开平台到落台之间
    # 往往没有任何可匹配路线点；此时游标若跳回平台末端，边界保护会松开
    # 方向键。保持走出点，直到专用状态确认真正到达目标平台。
    if (
        state.walk_off_active
        and state.walk_off_route_index is not None
        and 0 <= int(state.walk_off_route_index) < len(variant.points)
    ):
        state.route_index = int(state.walk_off_route_index)
        return int(state.walk_off_route_index)
    # 绳入口开始独占连接后，人物助跑、回头或起跳过程中会短暂更接近入口前的
    # 平台点以及入口后的绳身点。此时不能让“最近点”把游标从 rope_entry 拉回
    # platform，否则平台停滞跳和普通录制跳会插入上绳流程，形成左右走、反复跳。
    locked_connection_index = state.connection_priority_route_index
    if (
        state.connection_priority_active
        and locked_connection_index is not None
        and 0 <= int(locked_connection_index) < len(variant.points)
        and state.active_rope_x is None
        and not state.rope_top_exit_pending
    ):
        locked_point = variant.points[int(locked_connection_index)]
        _locked_rope_x, locked_top_y, locked_bottom_y = rope_bounds(locked_point)
        locked_entry_min_y = (
            int(locked_bottom_y)
            - max(
                2,
                (int(locked_bottom_y) - int(locked_top_y)) // 2,
            )
            if locked_top_y is not None and locked_bottom_y is not None
            else None
        )
        if (
            locked_point.segment_type == "rope_entry"
            and has_complete_rope_geometry(locked_point)
            # 只在绳底助跑/起跳区域锁定入口。人物到达绳顶并离绳后必须恢复
            # 最近点选择，才能匹配上层平台并正常结束连接独占状态。
            and locked_bottom_y is not None
            and locked_entry_min_y is not None
            and int(position[1]) >= int(locked_entry_min_y)
        ):
            state.route_index = int(locked_connection_index)
            return int(locked_connection_index)

        # walk_off、平台跳、绳子离场等连接一旦开始，最近点只能在已锁定连接
        # 及其后的落点平台里向前选择。普通候选窗口包含连接前的平台末点，人物
        # 位于边界时会每轮重新选回该点，形成“按方向 -> 边界保护松键 -> 再按”
        # 的20ms抖动。这里保留连接内的坐标反馈，同时允许落地后选中下一平台，
        # 供 _update_connection_priority 正常确认连接完成。
        connection_candidates = []
        platform_points_after_connection = 0
        total = len(variant.points)
        for offset in range(total):
            raw_index = int(locked_connection_index) + offset
            if raw_index >= total and not variant.closed_loop:
                break
            index = raw_index % total
            point = variant.points[index]
            connection_candidates.append(index)
            if offset > 0 and point.segment_type == "platform":
                platform_points_after_connection += 1
                if platform_points_after_connection >= ROUTE_LOOKAHEAD_POINTS:
                    break
            elif platform_points_after_connection > 0:
                break
        if connection_candidates:
            selected, _distance_to_connection = _nearest_index(
                variant.points,
                position,
                connection_candidates,
            )
            state.route_index = int(selected)
            return int(selected)

    # 定时休息和手动“测试休息点”不会开启普通跨平台 connection_priority，
    # 因此人物来到目标绳子下方时，最近点算法仍可能选中绳入口前最后一个
    # 平台点。若该点记录为继续向平台边缘行走，人物会越过绳子并一直走到
    # 地图最左/最右侧。进入目标绳底附近后直接锁定对应 rope_entry，让专用
    # 上绳助跑接管；这不会把位于其他楼层的人物强行拉向绳子。
    rest_rope_entry_index = _rest_navigation_rope_entry_index(
        state,
        variant,
        position,
    )
    if rest_rope_entry_index is not None:
        if state.route_index != int(rest_rope_entry_index):
            target_point = variant.points[int(rest_rope_entry_index)]
            rope_x, _rope_top_y, rope_bottom_y = rope_bounds(target_point)
            runtime.trace_event(
                "recorded_route_rest_rope_entry_locked",
                position=position,
                previous_route_index=state.route_index,
                route_index=int(rest_rope_entry_index),
                rope_x=rope_x,
                rope_bottom_y=rope_bottom_y,
                test_mode=state.rope_rest_test_active,
                action="stop_platform_edge_walk_and_start_rope_entry",
            )
        state.route_index = int(rest_rope_entry_index)
        return int(rest_rope_entry_index)

    rotation_rope_entry_index = _platform_rotation_rope_entry_index(
        state,
        variant,
        position,
    )
    if rotation_rope_entry_index is not None:
        if state.route_index != int(rotation_rope_entry_index):
            target_point = variant.points[int(rotation_rope_entry_index)]
            rope_x, _rope_top_y, rope_bottom_y = rope_bounds(target_point)
            runtime.trace_event(
                "recorded_route_rotation_rope_entry_locked",
                position=position,
                previous_route_index=state.route_index,
                route_index=int(rotation_rope_entry_index),
                rope_x=rope_x,
                rope_bottom_y=rope_bottom_y,
                action="claim_rope_entry_before_combat_or_relocalization",
            )
        # `_update_connection_priority` validates that the pending connection
        # starts on the active platform.  Refresh it from the observed minimap
        # platform here, because a combat interruption may have skipped the
        # ordinary platform-patrol branch that normally assigns this field.
        source_platform_index = _preceding_platform_range_index(
            variant,
            int(rotation_rope_entry_index),
        )
        if source_platform_index is not None:
            state.active_platform_range_index = int(source_platform_index)
        state.route_index = int(rotation_rope_entry_index)
        return int(rotation_rope_entry_index)

    if state.route_index is None:
        # 启动或明确重定位后，人物若已经站在平台上，应先以实际平台建立
        # 游标。下跳末点经常与下一圈平台首点同坐标；直接在整条闭环中按
        # “同距取后”会选中上一圈的 down_jump 末点，导致尚未发生下跳却
        # 进入连接段。平台切换必须由录制连接触发，初始化不能伪造切换。
        initial_platform_index = _current_platform_range_index(
            variant,
            position,
            preferred_route_index=None,
        )
        if initial_platform_index is not None:
            initial_platform = variant.platform_ranges[int(initial_platform_index)]
            candidates = list(
                range(
                    int(initial_platform.start_index),
                    int(initial_platform.end_index) + 1,
                )
            )
        else:
            candidates = _candidate_indices(state, variant)
    else:
        candidates = _candidate_indices(state, variant)
    selected, distance = _nearest_index(variant.points, position, candidates)
    if distance > ROUTE_RELOCALIZE_DISTANCE:
        # 同一物理平台可能在闭环中同时存在上行、下行两份坐标。人物仍站在
        # 当前平台时，不能仅因坐标相同就全局跳到另一份平台副本；平台切换
        # 必须由 rope/down_jump/walk_off 等已录制连接完成，或由人物实际落到
        # 不同高度的平台来确认。优先在当前坐标匹配且最接近现有游标的平台
        # 段内重定位，只有完全匹配不到平台时才退回全局搜索。
        matched_platform_index = _current_platform_range_index(
            variant,
            position,
            preferred_route_index=state.route_index,
        )
        if matched_platform_index is not None:
            matched_platform = variant.platform_ranges[int(matched_platform_index)]
            platform_candidates = list(
                range(
                    int(matched_platform.start_index),
                    int(matched_platform.end_index) + 1,
                )
            )
            selected, distance = _nearest_index(
                variant.points,
                position,
                platform_candidates,
            )
        else:
            selected, distance = _nearest_index(
                variant.points,
                position,
                list(range(len(variant.points))),
            )
        now = time.monotonic()
        if selected != state.last_relocalized_index or now - state.last_relocalized_at >= 1.0:
            state.last_relocalized_index = selected
            state.last_relocalized_at = now
            runtime.trace_event(
                "recorded_route_relocalized",
                position=position,
                route_index=selected,
                distance=distance,
                platform_range_index=matched_platform_index,
                reason=(
                    "stay_on_current_platform_until_recorded_connection"
                    if matched_platform_index is not None
                    else "no_platform_match_global_relocalization"
                ),
            )
    elif state.route_index is None:
        runtime.trace_event(
            "recorded_route_initialized",
            position=position,
            route_index=selected,
            distance=distance,
        )
    selected = _recover_missed_rope_entry(
        runtime,
        state,
        variant,
        selected,
        position,
    )
    if state.last_jump_index is not None:
        if variant.closed_loop:
            progressed = (selected - state.last_jump_index) % len(variant.points)
            if ROUTE_JUMP_LOOKAHEAD_POINTS < progressed < len(variant.points) - ROUTE_BACKTRACK_POINTS:
                state.last_jump_index = None
        elif selected > state.last_jump_index + ROUTE_JUMP_LOOKAHEAD_POINTS:
            state.last_jump_index = None
    state.route_index = selected
    return selected


def _switch_variant_after_cycle(runtime, plan, state, variant, previous_index, selected_index, position):
    """闭环游标从末尾回到开头时，按JSON概率选择下一圈路线变体。"""
    if _single_platform_exit_blocked(state, variant, position):
        # 单平台游标只在该平台段内往返，并没有完成整条闭环路线；平台段恰好
        # 跨越路线首尾时也不能据此切换变体或清空稳定的巡逻方向。
        return variant, selected_index
    total = len(variant.points)
    wrapped = (
        variant.closed_loop
        and previous_index is not None
        and previous_index >= max(1, int(total * 0.75))
        and selected_index <= max(1, int(total * 0.25))
    )
    if not wrapped:
        return variant, selected_index
    previous_variant_index = state.active_variant_index
    state.active_variant_index = _choose_variant_index(plan)
    next_variant = plan.variants[state.active_variant_index]
    state.route_index = None
    state.last_jump_index = None
    state.last_relocalized_index = None
    state.platform_direction = None
    state.active_platform_range_index = None
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    state.platform_dwell_range_index = None
    state.platform_dwell_started_at = 0.0
    state.platform_rotation_active = False
    state.platform_rotation_source_key = None
    state.platform_rotation_started_at = 0.0
    _clear_walk_off_state(state)
    _reset_down_jump_patrol_state(state)
    state.active_ignored_platform_number = None
    state.ignored_platform_exit_index = None
    _reset_platform_replan(state)
    _clear_active_rope(state)
    _clear_connection_priority(state)
    state.route_resume_grace_until = 0.0
    state.last_route_progress_at = time.monotonic()
    if not state.recorded_rest_point_enabled:
        _configure_rope_middle_fallback(state, next_variant)
    selected_index = _select_route_index(runtime, state, next_variant, position)
    runtime.trace_event(
        "recorded_route_variant_selected",
        previous_variant=plan.variants[previous_variant_index].name,
        selected_variant=next_variant.name,
        selected_probability=next_variant.probability,
        position=position,
    )
    return next_variant, selected_index


def _apply_horizontal(runtime, state, direction: str) -> None:
    """仅在录制方向变化时切换持续水平按键。"""
    normalized = direction if direction in ("left", "right") else None
    state.route_command_direction = normalized
    if normalized is not None:
        update_chase_forward = getattr(runtime, "更新智能追怪前进方向", None)
        if callable(update_chase_forward):
            update_chase_forward(normalized)
        # 公共战斗模块必须使用录制路线真正的稳定前进方向判断“前方攻击范围”。
        # 攻击造成的临时转身不会经过本入口，因此不会把身后怪误算为前方怪。
        state.combat.patrol_direction = normalized
        state.last_control_intent_at = time.monotonic()
        state.last_control_intent_kind = "route_move"
    if normalized == state.combat.applied_direction:
        return
    if normalized is None:
        runtime.释放水平移动键(reason="recorded_route")
    else:
        runtime.切换持续移动(normalized, reason="recorded_route")
    state.combat.applied_direction = normalized


def _apply_vertical(runtime, state, direction: str) -> None:
    """仅在录制方向变化时切换持续上下按键。"""
    normalized = direction if direction in ("up", "down") else None
    if normalized is not None:
        state.last_control_intent_at = time.monotonic()
        state.last_control_intent_kind = "route_vertical"
    if normalized == state.vertical_direction:
        return
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("down")
    if normalized is not None:
        runtime.pydirectinput.keyDown(normalized)
    state.vertical_direction = normalized


def _rope_up_reassert_seconds(state) -> float:
    """Increase up-key refresh frequency gradually after entry failures."""
    profile = state.rope_entry_profiles.get(state.active_rope_profile_key)
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    return max(
        ROUTE_ROPE_ENTRY_MIN_UP_REASSERT_SECONDS,
        ROUTE_ROPE_UP_REASSERT_SECONDS - min(failure_count, 8) * 0.015,
    )


def _keep_rope_up_pressed(runtime, state) -> None:
    """爬绳期间持续保持上键，并周期性补发按下事件避免游戏漏掉输入。"""
    now = time.monotonic()
    _apply_vertical(runtime, state, "up")
    reassert_seconds = _rope_up_reassert_seconds(state)
    if now - state.active_rope_last_up_key_at < reassert_seconds:
        return
    runtime.pydirectinput.keyDown("up")
    state.vertical_direction = "up"
    state.active_rope_last_up_key_at = now


def _rope_entry_commit_seconds(state) -> float:
    """Slowly extend the latch window after misses, with a strict upper bound."""
    profile = state.rope_entry_profiles.get(state.active_rope_profile_key)
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    return ROUTE_ROPE_ENTRY_COMMIT_SECONDS + min(
        ROUTE_ROPE_ENTRY_MAX_COMMIT_BONUS_SECONDS,
        failure_count * 0.08,
    )


def _rope_entry_jump_hold_seconds(profile) -> float:
    """Gradually strengthen the direction+C+up take-off pulse after failures."""
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    return min(
        ROUTE_ROPE_ENTRY_MAX_JUMP_HOLD_SECONDS,
        ROUTE_JUMP_HOLD_SECONDS + failure_count * 0.006,
    )


def _rope_entry_horizontal_after_jump_hold_seconds(profile) -> float:
    """Keep left/right pressed briefly after C is released while up stays held."""
    failure_count = int(profile.consecutive_failures) if profile is not None else 0
    return min(
        ROUTE_ROPE_ENTRY_MAX_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS,
        ROUTE_ROPE_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS
        + failure_count * 0.005,
    )


def _confirm_rope_entry_contact(runtime, state, position) -> None:
    """Mark stable rope contact without counting it as a completed entry yet."""
    if state.active_rope_confirmed:
        return
    state.active_rope_confirmed = True
    runtime.trace_event(
        "recorded_route_rope_entry_confirmed",
        position=position,
        rope_x=state.active_rope_x,
        rope_top_y=state.active_rope_top_y,
        rope_bottom_y=state.active_rope_bottom_y,
        attempt_offset_x=state.active_rope_attempt_offset_x,
        action="continue_climb_before_scoring_success",
    )


def _record_rope_entry_success(runtime, state, position) -> None:
    """Count success only after reaching rope top, then update per-offset rates."""
    if state.active_rope_success_recorded:
        return
    state.active_rope_success_recorded = True
    profile = state.rope_entry_profiles.get(state.active_rope_profile_key)
    if profile is None:
        return
    attempt_offset = state.active_rope_attempt_offset_x
    profile.attempt_count += 1
    profile.success_count += 1
    profile.consecutive_failures = 0
    if attempt_offset is not None:
        attempt_offset = int(attempt_offset)
        profile.offset_attempts[attempt_offset] = (
            int(profile.offset_attempts.get(attempt_offset, 0)) + 1
        )
        profile.offset_successes[attempt_offset] = (
            int(profile.offset_successes.get(attempt_offset, 0)) + 1
        )
        profile.preferred_offset_x = attempt_offset
        profile.last_success_offset_x = attempt_offset
        candidates = _rope_entry_candidate_offsets_from_direction(
            state.active_rope_direction
        )
        if attempt_offset in candidates:
            profile.candidate_index = candidates.index(attempt_offset)
    offset_attempts = int(profile.offset_attempts.get(attempt_offset, 0))
    offset_successes = int(profile.offset_successes.get(attempt_offset, 0))
    success_rate = (
        float(offset_successes) / float(offset_attempts)
        if offset_attempts > 0
        else 0.0
    )
    runtime.trace_event(
        "recorded_route_rope_entry_learning_success",
        position=position,
        rope_x=state.active_rope_x,
        rope_top_y=state.active_rope_top_y,
        rope_bottom_y=state.active_rope_bottom_y,
        attempt_offset_x=attempt_offset,
        attempts=profile.attempt_count,
        successes=profile.success_count,
        learned_offset_x=profile.preferred_offset_x,
        offset_attempts=offset_attempts,
        offset_successes=offset_successes,
        offset_success_rate=round(success_rate, 4),
        action="prefer_highest_success_rate_offset_on_next_lap",
    )


def _rope_entry_candidate_offsets_from_direction(direction: Optional[str]) -> Tuple[int, ...]:
    """失败后按左右两侧的中立候选顺序切换。"""
    return (-3, 3)


def _learn_rope_entry_failure(runtime, state, position, reason: str) -> dict:
    """Advance one small calibration step after a failed rope entry."""
    profile = state.rope_entry_profiles.get(state.active_rope_profile_key)
    if profile is None:
        return {
            "failure_count": 0,
            "next_offset_x": None,
            "commit_seconds": ROUTE_ROPE_ENTRY_COMMIT_SECONDS,
        }
    profile.attempt_count += 1
    profile.consecutive_failures += 1
    candidates = _rope_entry_candidate_offsets_from_direction(
        state.active_rope_direction
    )
    attempt_offset = state.active_rope_attempt_offset_x
    if attempt_offset is not None:
        attempt_offset = int(attempt_offset)
        profile.offset_attempts[attempt_offset] = (
            int(profile.offset_attempts.get(attempt_offset, 0)) + 1
        )
        profile.offset_failures[attempt_offset] = (
            int(profile.offset_failures.get(attempt_offset, 0)) + 1
        )
    if attempt_offset in candidates:
        profile.candidate_index = (candidates.index(attempt_offset) + 1) % len(candidates)
    else:
        profile.candidate_index = (profile.candidate_index + 1) % len(candidates)
    if profile.preferred_offset_x is not None and profile.consecutive_failures >= 2:
        profile.preferred_offset_x = None
    next_offset_x = int(candidates[profile.candidate_index])
    offset_attempts = int(profile.offset_attempts.get(attempt_offset, 0))
    offset_successes = int(profile.offset_successes.get(attempt_offset, 0))
    offset_success_rate = (
        float(offset_successes) / float(offset_attempts)
        if offset_attempts > 0
        else 0.0
    )
    commit_seconds = ROUTE_ROPE_ENTRY_COMMIT_SECONDS + min(
        ROUTE_ROPE_ENTRY_MAX_COMMIT_BONUS_SECONDS,
        profile.consecutive_failures * 0.08,
    )
    runtime.trace_event(
        "recorded_route_rope_entry_adapted",
        position=position,
        rope_x=state.active_rope_x,
        attempt_offset_x=state.active_rope_attempt_offset_x,
        offset_attempts=offset_attempts,
        offset_successes=offset_successes,
        offset_success_rate=round(offset_success_rate, 4),
        failure_reason=reason,
        consecutive_failures=profile.consecutive_failures,
        next_offset_x=next_offset_x,
        next_commit_ms=round(commit_seconds * 1000.0, 1),
        next_jump_hold_ms=round(
            _rope_entry_jump_hold_seconds(profile) * 1000.0,
            1,
        ),
        next_horizontal_after_jump_hold_ms=round(
            _rope_entry_horizontal_after_jump_hold_seconds(profile) * 1000.0,
            1,
        ),
        action="adjust_offset_and_strengthen_latch_input",
    )
    return {
        "failure_count": profile.consecutive_failures,
        "next_offset_x": next_offset_x,
        "commit_seconds": commit_seconds,
    }


def _current_platform_range_index(
    variant,
    position,
    preferred_route_index: Optional[int] = None,
) -> Optional[int]:
    """按人物坐标返回当前所在的平台段，优先选择垂直和水平距离最近的一段。"""
    candidates = []
    position_x = int(position[0])
    position_y = int(position[1])
    for index, platform in enumerate(variant.platform_ranges):
        vertical_distance = max(
            platform.minimum_y - position_y,
            position_y - platform.maximum_y,
            0,
        )
        horizontal_distance = max(
            platform.minimum_x - position_x,
            position_x - platform.maximum_x,
            0,
        )
        if vertical_distance > ROUTE_PLATFORM_MATCH_TOLERANCE_Y:
            continue
        if horizontal_distance > ROUTE_PLATFORM_MATCH_MARGIN_X:
            continue
        if preferred_route_index is None:
            route_distance = 0
        elif platform.start_index <= int(preferred_route_index) <= platform.end_index:
            route_distance = 0
        else:
            route_distance = min(
                abs(int(preferred_route_index) - platform.start_index),
                abs(int(preferred_route_index) - platform.end_index),
            )
        candidates.append(
            (vertical_distance, horizontal_distance, route_distance, index)
        )
    if not candidates:
        return None
    return min(candidates)[3]


def _ignored_platform_transition_index(variant, platform) -> int:
    """Return the first recorded transition point after an ignored platform."""
    next_index = int(platform.end_index) + 1
    if next_index < len(variant.points):
        return next_index
    if variant.closed_loop:
        return 0
    return int(platform.end_index)


def _active_brush_platform_numbers(variant, ignored_platform_numbers) -> set:
    """返回当前过滤条件下允许正常刷图的平台编号集合。"""

    ignored_numbers = {
        int(number) for number in ignored_platform_numbers if int(number) > 0
    }
    return {
        int(platform.platform_number)
        for platform in variant.platform_ranges
        if platform.platform_number is not None
        and int(platform.platform_number) not in ignored_numbers
    }


def _active_brush_platform_count(variant, ignored_platform_numbers) -> int:
    """Count active platforms even when an old recording has no platform number."""

    numbered = _active_brush_platform_numbers(variant, ignored_platform_numbers)
    if numbered:
        return len(numbered)
    ignored_numbers = {
        int(number) for number in ignored_platform_numbers if int(number) > 0
    }
    geometries = {
        (
            int(platform.minimum_x),
            int(platform.maximum_x),
            int(platform.minimum_y),
            int(platform.maximum_y),
        )
        for platform in variant.platform_ranges
        if platform.platform_number is None
        or int(platform.platform_number) not in ignored_numbers
    }
    return len(geometries)


def _update_platform_rotation(runtime, state, variant, position) -> bool:
    """Track dwell time and enter a soft route-first transition after one minute."""

    if (
        variant.platform_patrol
        or state.single_active_platform_patrol_enabled
        or state.single_platform_stationary_enabled
        or _rest_route_navigation_active(state)
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
    ):
        # Rest-point navigation and single-platform modes own the route.  Never
        # let a previously armed multi-platform rotation leak into those flows.
        state.platform_rotation_active = False
        state.platform_rotation_source_key = None
        state.platform_rotation_started_at = 0.0
        return False
    active_platform_count = _active_brush_platform_count(
        variant,
        state.ignored_platform_numbers,
    )
    current_platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    now = time.monotonic()
    decision = evaluate_platform_rotation(
        current_platform_index=current_platform_index,
        platform_ranges=variant.platform_ranges,
        active_platform_count=active_platform_count,
        tracked_platform_key=state.platform_dwell_range_index,
        dwell_started_at=state.platform_dwell_started_at,
        rotation_active=state.platform_rotation_active,
        rotation_source_key=state.platform_rotation_source_key,
        now=now,
        max_dwell_seconds=RECORDED_ROUTE_MULTI_PLATFORM_MAX_DWELL_SECONDS,
    )
    if decision.complete_rotation:
        elapsed = max(0.0, now - state.platform_rotation_started_at)
        previous_key = state.platform_rotation_source_key
        state.platform_rotation_active = False
        state.platform_rotation_source_key = None
        state.platform_rotation_started_at = 0.0
        state.platform_dwell_range_index = decision.current_platform_key
        state.platform_dwell_started_at = now
        runtime.trace_event(
            "recorded_route_platform_rotation_completed",
            position=position,
            source_platform_key=previous_key,
            arrived_platform_key=decision.current_platform_key,
            elapsed_ms=round(elapsed * 1000.0, 1),
            action="start_new_platform_dwell_timer",
        )
        return False
    if decision.reset_dwell:
        state.platform_dwell_range_index = decision.current_platform_key
        state.platform_dwell_started_at = now
    if decision.start_rotation:
        state.platform_rotation_active = True
        state.platform_rotation_source_key = decision.current_platform_key
        state.platform_rotation_started_at = now
        state.platform_dwell_forced_count += 1
        # Do not teleport the route cursor to an exit.  Merely stop requesting
        # another full platform sweep; normal JSON points now approach the next
        # connection, while in-range combat can still interrupt along the way.
        state.platform_coverage_target_x = None
        state.platform_coverage_route_index = None
        state.route_resume_grace_until = 0.0
        runtime.trace_event(
            "recorded_route_platform_rotation_started",
            position=position,
            source_platform_key=decision.current_platform_key,
            dwell_seconds=round(decision.elapsed_seconds, 3),
            max_dwell_seconds=RECORDED_ROUTE_MULTI_PLATFORM_MAX_DWELL_SECONDS,
            rotation_count=state.platform_dwell_forced_count,
            action="follow_json_toward_next_connection_and_attack_in_range",
        )
    return state.platform_rotation_active


def _next_active_platform_number(variant, current_platform_index, active_numbers):
    """Return the next distinct active platform in recorded playback order."""
    if current_platform_index is None or not variant.platform_ranges:
        return None
    total = len(variant.platform_ranges)
    current = variant.platform_ranges[int(current_platform_index)]
    current_number = current.platform_number
    for offset in range(1, total + 1):
        candidate = variant.platform_ranges[(int(current_platform_index) + offset) % total]
        number = candidate.platform_number
        if number is None or int(number) not in active_numbers:
            continue
        if current_number is None or int(number) != int(current_number):
            return int(number)
    return None


def _emit_route_monitor(runtime, state, variant, position, *, force=False, **extra):
    """Publish compact platform, rotation countdown, focus and recovery state."""
    callback = getattr(runtime, "recorded_route_monitor_callback", None)
    if not callable(callback):
        return
    now = time.monotonic()
    current_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    current_number = None
    if current_index is not None:
        current_number = variant.platform_ranges[int(current_index)].platform_number
        if current_number is not None:
            current_number = int(current_number)
            state.monitor_last_platform_number = current_number
    active_numbers = _active_brush_platform_numbers(
        variant,
        state.ignored_platform_numbers,
    )
    next_number = _next_active_platform_number(
        variant,
        current_index,
        active_numbers,
    )
    if next_number is not None:
        state.monitor_last_next_platform_number = int(next_number)
    if current_number is None:
        current_number = state.monitor_last_platform_number
    if next_number is None:
        next_number = state.monitor_last_next_platform_number
    countdown = None
    if (
        len(active_numbers) > 1
        and current_index is not None
        and state.platform_dwell_started_at > 0
        and not state.platform_rotation_active
    ):
        countdown = max(
            0.0,
            RECORDED_ROUTE_MULTI_PLATFORM_MAX_DWELL_SECONDS
            - (now - state.platform_dwell_started_at),
        )
    status = "rotating" if state.platform_rotation_active else "brushing"
    if state.platform_replan_active:
        status = "replanning"
        next_number = state.platform_replan_target_number
    elif state.walk_off_active:
        status = "transitioning"
        next_number = state.walk_off_target_platform_number
    elif state.active_rope_x is not None or state.rope_top_exit_pending:
        status = "rope"
    payload = {
        "focus_status": "focused",
        "status": status,
        "platform_number": current_number,
        "next_platform_number": next_number,
        "switch_remaining_seconds": countdown,
        "rotation_active": bool(state.platform_rotation_active),
        "recovery_stage": int(state.route_recovery_stage),
        "recovery_count": int(state.route_recovery_count),
    }
    payload.update(extra)
    signature = (
        payload.get("focus_status"),
        payload.get("status"),
        payload.get("platform_number"),
        payload.get("next_platform_number"),
        int(float(payload.get("switch_remaining_seconds") or 0.0)),
        payload.get("recovery_stage"),
        payload.get("message"),
    )
    if (
        not force
        and signature == state.monitor_last_signature
        and now - state.monitor_last_emitted_at
        < RECORDED_ROUTE_MONITOR_INTERVAL_SECONDS
    ):
        return
    state.monitor_last_signature = signature
    state.monitor_last_emitted_at = now
    callback(payload)


def _sync_focus_pause_clock(runtime, state) -> None:
    """Exclude time spent outside the game window from route and dwell timers."""
    total = max(0.0, float(getattr(runtime, "游戏窗口失焦累计秒数", 0.0)))
    delta = total - float(state.focus_pause_seconds_seen)
    if delta <= 0:
        return
    state.focus_pause_seconds_seen = total
    for name in (
        "platform_dwell_started_at",
        "platform_rotation_started_at",
        "last_control_intent_at",
        "route_command_progress_at",
        "last_route_progress_at",
        "liveness_last_checked_at",
        "position_missing_started_at",
        "position_missing_last_relocate_at",
    ):
        value = float(getattr(state, name, 0.0) or 0.0)
        if value > 0:
            setattr(state, name, value + delta)


def _handle_missing_position(runtime, state, variant) -> None:
    """Relocate after focused minimap loss; never fire recovery while unfocused."""
    focus_event = getattr(runtime, "游戏窗口焦点事件", None)
    focused = focus_event is None or focus_event.is_set()
    if not focused:
        state.position_missing_started_at = 0.0
        state.position_missing_last_relocate_at = 0.0
        return
    now = time.monotonic()
    if state.position_missing_started_at <= 0:
        state.position_missing_started_at = now
        _emit_route_monitor(
            runtime,
            state,
            variant,
            (0, 0),
            force=True,
            status="position_missing",
            message="角色坐标暂时丢失，等待重新识别",
        )
        return
    missing_seconds = now - state.position_missing_started_at
    retry_due = (
        state.position_missing_last_relocate_at <= 0
        or now - state.position_missing_last_relocate_at
        >= RECORDED_ROUTE_POSITION_MISSING_RETRY_SECONDS
    )
    if (
        missing_seconds >= RECORDED_ROUTE_POSITION_MISSING_RELOCATE_SECONDS
        and retry_due
    ):
        state.position_missing_last_relocate_at = now
        relocate = getattr(runtime, "请求人物重新定位", None)
        if callable(relocate):
            relocate(reason="recorded_route_position_missing")
        else:
            event = getattr(runtime, "人物全图重定位事件", None)
            if event is not None:
                event.set()
        runtime.trace_event(
            "recorded_route_position_missing_relocated",
            missing_ms=round(missing_seconds * 1000.0, 1),
            recovery_count=state.route_recovery_count,
            action="request_full_minimap_relocation",
        )
        _emit_route_monitor(
            runtime,
            state,
            variant,
            (0, 0),
            force=True,
            status="relocalizing",
            message="角色坐标持续丢失，正在全图重新定位",
        )


def _forward_route_distance(variant, start_index: int, target_index: int) -> Optional[int]:
    """计算录制顺序中从当前出口到目标平台的可执行前向距离。"""

    total = len(variant.points)
    if total <= 0:
        return None
    start_index = max(0, min(int(start_index), total - 1))
    target_index = max(0, min(int(target_index), total - 1))
    if target_index >= start_index:
        return target_index - start_index
    if variant.closed_loop:
        return total - start_index + target_index
    return None


def _plan_platform_recovery(variant, current_platform, active_numbers):
    """沿JSON可执行方向选择最近的有效刷图平台和当前平台出口。"""

    if not active_numbers:
        return None
    exit_index = _ignored_platform_transition_index(variant, current_platform)
    candidates = []
    for platform_index, platform in enumerate(variant.platform_ranges):
        if (
            platform.platform_number is None
            or int(platform.platform_number) not in active_numbers
        ):
            continue
        route_distance = _forward_route_distance(
            variant,
            exit_index,
            int(platform.start_index),
        )
        if route_distance is None:
            continue
        candidates.append(
            (
                int(route_distance),
                int(platform.start_index),
                int(platform.platform_number),
                int(platform_index),
            )
        )
    if not candidates:
        return None
    route_distance, target_index, target_number, platform_index = min(candidates)
    return {
        "exit_index": int(exit_index),
        "target_index": int(target_index),
        "target_number": int(target_number),
        "target_platform_index": int(platform_index),
        "route_distance": int(route_distance),
    }


def _reset_platform_replan(state) -> None:
    """清除路线外平台的返程规划状态。"""

    state.platform_replan_active = False
    state.platform_replan_source_number = None
    state.platform_replan_target_number = None
    state.platform_replan_exit_index = None
    state.platform_replan_started_at = 0.0
    state.platform_replan_last_platform_number = None


def _finish_platform_replan(runtime, state, platform, position) -> None:
    """人物回到有效刷图平台后结束返程规划并恢复正常路线。"""

    target_number = state.platform_replan_target_number
    elapsed_seconds = (
        max(0.0, time.monotonic() - state.platform_replan_started_at)
        if state.platform_replan_started_at > 0
        else 0.0
    )
    runtime.trace_event(
        "recorded_route_platform_replan_completed",
        position=position,
        source_platform_number=state.platform_replan_source_number,
        target_platform_number=target_number,
        arrived_platform_number=int(platform.platform_number),
        elapsed_ms=round(elapsed_seconds * 1000.0, 1),
        replan_count=state.platform_replan_count,
        action="resume_brush_route",
    )
    state.route_index = int(platform.start_index)
    state.last_relocalized_index = None
    state.active_ignored_platform_number = None
    state.ignored_platform_exit_index = None
    _reset_single_platform_return_watchdog(state)
    _reset_platform_stall_tracking(state)
    _reset_platform_replan(state)


def _apply_platform_route_replan(
    runtime,
    state,
    variant,
    position,
    preferred_route_index=None,
) -> bool:
    """检测路线外平台，并沿录制连接重新规划到最近有效刷图平台。"""

    if not variant.platform_ranges or _rest_route_navigation_active(state):
        if _rest_route_navigation_active(state):
            _reset_platform_replan(state)
        return False
    active_numbers = _active_brush_platform_numbers(
        variant,
        state.ignored_platform_numbers,
    )
    if not active_numbers:
        _reset_platform_replan(state)
        return False

    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=preferred_route_index,
    )
    if platform_index is None:
        if not state.platform_replan_active:
            return False
        # 掉落、跳台或连接段暂时不属于任何平台。返程规划必须继续独占路线，
        # 否则追怪和普通巡逻会在两个平台之间改写游标，再次走偏。
        selected_index = _select_route_index(runtime, state, variant, position)
        combat_logic.clear_combat_for_movement(runtime, state.combat)
        combat_logic.reset_attack_direction_lock(
            runtime,
            state.combat,
            reason="platform_route_replan_transition",
        )
        _apply_recorded_command(
            runtime,
            state,
            variant,
            position,
            selected_index,
        )
        return True

    platform = variant.platform_ranges[platform_index]
    if platform.platform_number is None:
        return False
    current_number = int(platform.platform_number)
    if current_number in active_numbers:
        if state.platform_replan_active:
            _finish_platform_replan(runtime, state, platform, position)
        return False

    plan = _plan_platform_recovery(variant, platform, active_numbers)
    if plan is None:
        runtime.trace_event(
            "recorded_route_platform_replan_unavailable",
            position=position,
            current_platform_number=current_number,
            active_platform_numbers=sorted(active_numbers),
            closed_loop=variant.closed_loop,
            action="keep_existing_route",
        )
        return False

    target_number = int(plan["target_number"])
    exit_index = int(plan["exit_index"])
    platform_changed = (
        state.platform_replan_last_platform_number != current_number
    )
    new_plan = (
        not state.platform_replan_active
        or state.platform_replan_target_number != target_number
    )
    if new_plan:
        if not state.platform_replan_active:
            state.platform_replan_started_at = time.monotonic()
            state.platform_replan_count += 1
            state.platform_replan_source_number = current_number
        state.platform_replan_active = True
        state.platform_replan_target_number = target_number
        runtime.trace_event(
            "recorded_route_platform_ignored",
            position=position,
            platform_number=current_number,
            platform_index=platform_index,
            platform_x_range=[platform.minimum_x, platform.maximum_x],
            platform_y_range=[platform.minimum_y, platform.maximum_y],
            transition_index=exit_index,
            single_active_platform_number=state.single_active_platform_number,
            replanned_target_platform_number=target_number,
            action="return_to_planned_brush_platform",
        )
        runtime.trace_event(
            "recorded_route_platform_replanned",
            position=position,
            current_platform_number=current_number,
            target_platform_number=target_number,
            active_platform_numbers=sorted(active_numbers),
            exit_index=exit_index,
            target_index=int(plan["target_index"]),
            route_distance=int(plan["route_distance"]),
            closed_loop=variant.closed_loop,
            action="follow_recorded_connections_to_valid_platform",
        )
    elif platform_changed:
        runtime.trace_event(
            "recorded_route_platform_replan_progress",
            position=position,
            source_platform_number=state.platform_replan_source_number,
            current_platform_number=current_number,
            target_platform_number=target_number,
            exit_index=exit_index,
            target_index=int(plan["target_index"]),
            remaining_route_distance=int(plan["route_distance"]),
            action="continue_replanned_route",
        )
    state.platform_replan_exit_index = exit_index
    state.platform_replan_last_platform_number = current_number

    state.active_ignored_platform_number = current_number
    state.ignored_platform_exit_index = exit_index
    state.active_platform_range_index = platform_index
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    state.platform_direction = None
    state.smart_seek_target_active = False
    state.smart_seek_scan_until = 0.0
    state.smart_seek_mode = None
    _clear_smart_seek_target_loss(state)
    state.route_resume_grace_until = 0.0
    _clear_connection_priority(state)
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    state.last_ignored_chase_signature = None
    state.route_index = exit_index

    runtime.zant = 0
    clear_attack_intent = getattr(runtime, "清除攻击意图", None)
    if callable(clear_attack_intent):
        clear_attack_intent()
    release_attack_keys = getattr(runtime, "释放攻击键", None)
    if callable(release_attack_keys):
        release_attack_keys()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="platform_route_replan",
    )
    if _apply_single_platform_return_watchdog(
        runtime,
        state,
        variant,
        position,
        exit_index,
        current_number,
    ):
        return True
    _apply_recorded_command(
        runtime,
        state,
        variant,
        position,
        exit_index,
    )
    return True


def _reset_single_platform_return_watchdog(state) -> None:
    """Clear the independent watchdog used while returning from a filtered platform."""

    state.single_platform_return_watch_platform_number = None
    state.single_platform_return_watch_target_number = None
    state.single_platform_return_watch_x = None
    state.single_platform_return_watch_y = None
    state.single_platform_return_progress_at = 0.0
    state.single_platform_return_recovery_stage = 0


def _single_platform_return_direction(variant, route_index: int, position) -> str:
    """Find the next concrete horizontal command on the recorded return path."""

    total = len(variant.points)
    if total <= 0:
        return "none"
    for offset in range(min(ROUTE_JUMP_LOOKAHEAD_POINTS, total)):
        raw_index = int(route_index) + offset
        if raw_index >= total and not variant.closed_loop:
            break
        point = variant.points[raw_index % total]
        if point.horizontal in ("left", "right"):
            return point.horizontal
        if point.segment_type == "walk_off_left":
            return "left"
        if point.segment_type == "walk_off_right":
            return "right"
        if int(point.x) < int(position[0]):
            return "left"
        if int(point.x) > int(position[0]):
            return "right"
    return "none"


def _force_single_platform_return_direction(runtime, state, direction: str) -> None:
    """Reassert a movement key even when the cached applied direction is stale."""

    release = getattr(runtime, "释放水平移动键", None)
    if callable(release):
        release(reason="single_platform_return_watchdog")
    state.combat.applied_direction = None
    switch = getattr(runtime, "切换持续移动", None)
    if callable(switch):
        switch(direction, reason="single_platform_return_watchdog")
        state.combat.applied_direction = direction


def _apply_single_platform_return_watchdog(
    runtime,
    state,
    variant,
    position,
    route_index: int,
    current_platform_number: int,
) -> bool:
    """Recover when a knocked-down character stops on the way back to one platform."""

    if (
        not state.single_active_platform_patrol_enabled
        or state.single_active_platform_number is None
        or _rest_route_navigation_active(state)
    ):
        _reset_single_platform_return_watchdog(state)
        return False
    target_platform_number = int(state.single_active_platform_number)
    current_platform_number = int(current_platform_number)
    if current_platform_number == target_platform_number:
        _reset_single_platform_return_watchdog(state)
        return False

    point = variant.points[int(route_index)]
    if point.segment_type in ("rope", "rope_entry", "rope_exit"):
        _reset_single_platform_return_watchdog(state)
        return False
    direction = _single_platform_return_direction(variant, route_index, position)
    if direction not in ("left", "right"):
        return False

    now = time.monotonic()
    current_x = int(position[0])
    current_y = int(position[1])
    watch_changed = (
        state.single_platform_return_watch_platform_number != current_platform_number
        or state.single_platform_return_watch_target_number != target_platform_number
    )
    if watch_changed or state.single_platform_return_watch_x is None:
        state.single_platform_return_watch_platform_number = current_platform_number
        state.single_platform_return_watch_target_number = target_platform_number
        state.single_platform_return_watch_x = current_x
        state.single_platform_return_watch_y = current_y
        state.single_platform_return_progress_at = now
        state.single_platform_return_recovery_stage = 0
        return False

    if current_x != int(state.single_platform_return_watch_x):
        state.single_platform_return_watch_x = current_x
        state.single_platform_return_watch_y = current_y
        state.single_platform_return_progress_at = now
        state.single_platform_return_recovery_stage = 0
        return False
    if now - state.single_platform_return_progress_at < ROUTE_SINGLE_PLATFORM_RETURN_STALL_SECONDS:
        return False

    state.single_platform_return_recovery_stage += 1
    state.single_platform_return_recovery_count += 1
    recovery_stage = state.single_platform_return_recovery_stage
    state.single_platform_return_progress_at = now
    state.single_platform_return_watch_y = current_y

    runtime.zant = 0
    clear_attack_intent = getattr(runtime, "清除攻击意图", None)
    if callable(clear_attack_intent):
        clear_attack_intent()
    release_attack_keys = getattr(runtime, "释放攻击键", None)
    if callable(release_attack_keys):
        release_attack_keys()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="single_platform_return_stalled",
    )
    _apply_vertical(runtime, state, "none")
    _force_single_platform_return_direction(runtime, state, direction)

    action = "reassert_return_direction"
    completed = True
    if recovery_stage == 1:
        completed = runtime.可中断等待(
            ROUTE_SINGLE_PLATFORM_RETURN_MOVE_REASSERT_SECONDS,
            interval=0.01,
        )
    else:
        action = "jump_toward_return_connection"
        runtime.pydirectinput.keyDown("c")
        try:
            completed = runtime.可中断等待(
                ROUTE_JUMP_HOLD_SECONDS,
                interval=0.01,
            )
        finally:
            runtime.pydirectinput.keyUp("c")
        if completed:
            completed = runtime.可中断等待(
                ROUTE_SINGLE_PLATFORM_RETURN_JUMP_COAST_SECONDS,
                interval=0.01,
            )
        state.last_jump_index = None
        state.last_jump_at = time.monotonic()
        if recovery_stage == 3:
            request_relocation = getattr(runtime, "请求人物重新定位", None)
            if callable(request_relocation):
                request_relocation(reason="single_platform_return_stalled")
            state.last_relocalized_index = None
            state.last_relocalized_at = 0.0
            action = "jump_and_refresh_return_route"

    runtime.trace_event(
        "recorded_route_single_platform_return_recovered",
        position=position,
        current_platform_number=current_platform_number,
        target_platform_number=target_platform_number,
        route_index=route_index,
        direction=direction,
        recovery_stage=recovery_stage,
        recovery_count=state.single_platform_return_recovery_count,
        stalled_seconds=ROUTE_SINGLE_PLATFORM_RETURN_STALL_SECONDS,
        completed=completed,
        action=action,
    )
    return True


def _apply_ignored_platform_route(
    runtime,
    state,
    variant,
    position,
    preferred_route_index=None,
) -> bool:
    """兼容旧调用入口，把过滤平台交给统一的平台路线重规划器。"""

    if not state.ignored_platform_numbers:
        state.active_ignored_platform_number = None
        state.ignored_platform_exit_index = None
        _reset_platform_replan(state)
        return False
    return _apply_platform_route_replan(
        runtime,
        state,
        variant,
        position,
        preferred_route_index=preferred_route_index,
    )


def _single_unignored_platform_patrol_variant(variant, ignored_platform_numbers):
    """过滤后只剩一个平台时，返回仅覆盖该平台的往返巡逻变体。"""
    ignored_numbers = {
        int(number) for number in ignored_platform_numbers if int(number) > 0
    }
    if not ignored_numbers or len(variant.platform_ranges) < 2:
        return None
    platform_numbers = {
        int(platform.platform_number)
        for platform in variant.platform_ranges
        if platform.platform_number is not None
    }
    active_numbers = sorted(platform_numbers - ignored_numbers)
    if len(platform_numbers) <= 1 or len(active_numbers) != 1:
        return None
    active_number = int(active_numbers[0])
    active_ranges = [
        platform
        for platform in variant.platform_ranges
        if platform.platform_number is not None
        and int(platform.platform_number) == active_number
    ]
    if not active_ranges:
        return None
    minimum_x = min(int(platform.minimum_x) for platform in active_ranges)
    maximum_x = max(int(platform.maximum_x) for platform in active_ranges)
    first_range = min(active_ranges, key=lambda platform: int(platform.start_index))
    active_points = list(
        variant.points[int(first_range.start_index):int(first_range.end_index) + 1]
    )
    patrol_variant = replace(
        variant,
        points=active_points or list(variant.points),
        closed_loop=True,
        platform_patrol=True,
        platform_min_x=minimum_x,
        platform_max_x=maximum_x,
        platform_ranges=tuple(active_ranges),
    )
    return active_number, patrol_variant


def _rest_route_navigation_active(state) -> bool:
    """休息点流程进行中时允许暂时离开唯一有效平台并恢复JSON路线。"""
    return bool(
        state.rope_rest_pending
        or state.rope_resting
        or state.rope_rest_test_active
        or state.recorded_rest_phase is not None
    )


def _single_platform_stationary_mode(state, variant) -> bool:
    """地图或过滤结果只剩一个刷怪平台时，启用清怪后拾取并驻守。"""
    return bool(
        variant.platform_patrol
        or state.single_active_platform_patrol_enabled
        or state.single_platform_stationary_enabled
    )


def _single_platform_exit_blocked(state, variant, position) -> bool:
    """人物已在唯一有效平台时，禁止普通刷图路线再次进入离台连接。"""
    if (
        not _single_platform_stationary_mode(state, variant)
        or _rest_route_navigation_active(state)
        or state.platform_replan_active
    ):
        return False
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        # 启动时人物可能已经在绳子上，或正从错误平台返回；此时必须允许
        # JSON连接继续把人物送回唯一有效平台。
        return False
    platform = variant.platform_ranges[int(platform_index)]
    if state.single_active_platform_number is not None:
        if (
            platform.platform_number is None
            or int(platform.platform_number)
            != int(state.single_active_platform_number)
        ):
            return False
    return True


def _single_platform_route_index(state, variant, position) -> Optional[int]:
    """正常单平台刷怪时，只在当前唯一有效平台的录制点中选择最近游标。"""
    if not _single_platform_exit_blocked(state, variant, position):
        return None
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return None
    platform = variant.platform_ranges[int(platform_index)]
    candidates = list(
        range(
            max(0, int(platform.start_index)),
            min(len(variant.points), int(platform.end_index) + 1),
        )
    )
    if not candidates:
        return None
    selected_index, _distance = _nearest_index(
        variant.points,
        position,
        candidates,
    )
    state.route_index = int(selected_index)
    return int(selected_index)


def _pin_single_platform_route(runtime, state, variant, position, blocked_index) -> None:
    """清除旧连接锁，并把路线游标重新定位到唯一有效平台内部。"""
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    pinned_index = state.route_index
    platform_number = state.single_active_platform_number
    if platform_index is not None:
        platform = variant.platform_ranges[int(platform_index)]
        platform_number = platform.platform_number
        candidates = list(
            range(
                max(0, int(platform.start_index)),
                min(len(variant.points), int(platform.end_index) + 1),
            )
        )
        if candidates:
            pinned_index, _distance = _nearest_index(
                variant.points,
                position,
                candidates,
            )
            state.route_index = int(pinned_index)

    _apply_horizontal(runtime, state, "none")
    _apply_vertical(runtime, state, "none")
    _clear_connection_priority(state)
    _clear_walk_off_state(state)
    _clear_rope_entry_runup(state)
    _clear_active_rope(state)
    _clear_rope_top_exit(state)
    _reset_down_jump_patrol_state(state, platform_index)
    _reset_platform_stall_tracking(state)
    state.last_jump_index = None
    state.last_jump_at = 0.0
    state.completed = False
    state.platform_rotation_active = False
    state.platform_rotation_source_key = None
    state.platform_rotation_started_at = 0.0
    state.last_control_intent_at = time.monotonic()
    state.last_control_intent_kind = "single_platform_patrol"

    now = time.monotonic()
    blocked_index = int(blocked_index) if blocked_index is not None else None
    if (
        state.single_platform_exit_blocked_index != blocked_index
        or now - state.single_platform_exit_blocked_at >= 1.0
    ):
        state.single_platform_exit_blocked_index = blocked_index
        state.single_platform_exit_blocked_at = now
        point = (
            variant.points[blocked_index]
            if blocked_index is not None
            and 0 <= blocked_index < len(variant.points)
            else None
        )
        runtime.trace_event(
            "recorded_route_single_platform_exit_blocked",
            position=position,
            platform_number=platform_number,
            blocked_route_index=blocked_index,
            blocked_segment_type=(point.segment_type if point is not None else None),
            pinned_route_index=pinned_index,
            reason="only_one_active_brush_platform",
            action="stay_on_platform_and_continue_combat_patrol",
        )


def _single_platform_bounds(state, variant, position):
    """返回人物当前唯一有效平台的安全X边界；人物掉层时返回空。"""
    if variant.platform_patrol:
        return int(variant.platform_min_x), int(variant.platform_max_x)
    if not (
        state.single_active_platform_patrol_enabled
        or state.single_platform_stationary_enabled
    ):
        return None
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return None
    platform = variant.platform_ranges[int(platform_index)]
    if (
        state.single_active_platform_number is not None
        and (
            platform.platform_number is None
            or int(platform.platform_number) != int(state.single_active_platform_number)
        )
    ):
        return None
    return int(platform.minimum_x), int(platform.maximum_x)


def _remember_single_platform_monster_target(state, variant, position, intent) -> None:
    """把最后一只可靠怪物的画面距离换算成当前平台内的小地图目标X。"""
    if not _single_platform_stationary_mode(state, variant):
        return
    if intent.source not in ("combat", "chase") or intent.nearest_dx is None:
        return
    bounds = _single_platform_bounds(state, variant, position)
    if bounds is None:
        return

    left_count = max(
        int(intent.attackable_left_count),
        int(intent.chase_left_count),
    )
    right_count = max(
        int(intent.attackable_right_count),
        int(intent.chase_right_count),
    )
    if left_count > 0 and right_count <= 0:
        direction = "left"
    elif right_count > 0 and left_count <= 0:
        direction = "right"
    else:
        direction = intent.target_direction
    if direction not in ("left", "right"):
        return

    minimum_x, maximum_x = bounds
    distance_x = max(
        1,
        int(
            round(
                float(intent.nearest_dx)
                / RECORDED_ROUTE_LOOT_SCREEN_TO_MINIMAP_X
            )
        ),
    )
    signed_distance = -distance_x if direction == "left" else distance_x
    target_x = max(
        minimum_x + RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X,
        min(
            maximum_x - RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X,
            int(position[0]) + signed_distance,
        ),
    )
    state.last_monster_direction = direction
    state.last_monster_anchor_x = int(position[0])
    state.last_monster_nearest_dx = float(intent.nearest_dx)
    state.last_monster_target_x = int(target_x)


def _arm_single_platform_loot_approach(
    runtime,
    state,
    variant,
    position,
    attack_completed_at: float,
) -> bool:
    """最后一次攻击确认清怪后，锁定死亡点；每场战斗只启动一次。"""
    if not _single_platform_stationary_mode(state, variant):
        return False
    attack_completed_at = float(attack_completed_at or 0.0)
    if (
        attack_completed_at <= state.single_platform_loot_attack_completed_at
        or state.last_monster_target_x is None
        or _single_platform_bounds(state, variant, position) is None
    ):
        return False
    state.single_platform_loot_attack_completed_at = attack_completed_at
    state.single_platform_loot_target_x = int(state.last_monster_target_x)
    state.single_platform_loot_started_at = time.monotonic()
    state.single_platform_idle_active = False
    runtime.trace_event(
        "recorded_route_single_platform_loot_armed",
        position=position,
        target_x=state.single_platform_loot_target_x,
        monster_anchor_x=state.last_monster_anchor_x,
        monster_direction=state.last_monster_direction,
        monster_screen_distance_x=state.last_monster_nearest_dx,
        attack_completed_at=round(attack_completed_at, 6),
        action="walk_to_last_monster_death_position_then_idle",
    )
    return True


def _apply_single_platform_loot_or_idle(runtime, state, variant, position) -> bool:
    """走到末只怪物死亡点拾取，随后释放移动键并在唯一平台原地驻守。"""
    if not _single_platform_stationary_mode(state, variant):
        return False
    bounds = _single_platform_bounds(state, variant, position)
    if bounds is None or _rest_route_navigation_active(state):
        state.single_platform_idle_active = False
        state.single_platform_loot_target_x = None
        state.single_platform_loot_started_at = 0.0
        return False

    minimum_x, maximum_x = bounds
    target_x = state.single_platform_loot_target_x
    # “唯一平台”只改变清怪后的行为。脚本刚启动、尚未打过怪时仍须执行
    # 原JSON/平台巡逻寻找第一批怪，不能直接进入永久驻守。
    if target_x is None and not state.single_platform_idle_active:
        return False
    now = time.monotonic()
    if target_x is not None:
        target_x = max(
            minimum_x + RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X,
            min(
                maximum_x - RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X,
                int(target_x),
            ),
        )
        state.single_platform_loot_target_x = target_x
        delta_x = int(target_x) - int(position[0])
        timed_out = (
            state.single_platform_loot_started_at > 0
            and now - state.single_platform_loot_started_at
            >= RECORDED_ROUTE_SINGLE_PLATFORM_LOOT_TIMEOUT_SECONDS
        )
        if (
            abs(delta_x) > RECORDED_ROUTE_SINGLE_PLATFORM_LOOT_TOLERANCE_X
            and not timed_out
        ):
            state.single_platform_idle_active = False
            combat_logic.clear_combat_for_movement(runtime, state.combat)
            _apply_vertical(runtime, state, "none")
            _apply_horizontal(runtime, state, "left" if delta_x < 0 else "right")
            return True
        runtime.trace_event(
            "recorded_route_single_platform_loot_reached",
            position=position,
            target_x=target_x,
            delta_x=delta_x,
            timed_out=timed_out,
            elapsed_ms=round(
                max(0.0, now - state.single_platform_loot_started_at) * 1000.0,
                1,
            ),
            action="stop_and_idle_on_single_platform",
        )
        state.single_platform_loot_target_x = None
        state.single_platform_loot_started_at = 0.0

    if not state.single_platform_idle_active:
        _release_all_keys(runtime, state, "single_platform_idle")
        combat_logic.set_route_phase(
            runtime,
            state.combat,
            "single_platform_idle",
            position=position,
        )
        state.single_platform_idle_active = True
        state.last_control_intent_at = now
        state.last_control_intent_kind = "single_platform_idle"
        state.last_route_progress_at = now
        runtime.trace_event(
            "recorded_route_single_platform_idle_started",
            position=position,
            platform_x_range=[minimum_x, maximum_x],
            action="wait_for_next_monster_without_patrol",
        )
    else:
        # 驻守是预期状态，不应被路线静默看门狗误判为卡死并重新启动巡逻。
        state.last_control_intent_at = now
        state.last_control_intent_kind = "single_platform_idle"
        state.last_route_progress_at = now
    return True


def _wake_single_platform_idle_for_combat(
    runtime,
    state,
    variant,
    position,
) -> bool:
    """唯一平台驻守时用最新攻击快照直接唤醒公共战斗，不受路线保护吞掉。"""
    if (
        not state.single_platform_idle_active
        or not _single_platform_stationary_mode(state, variant)
        or _rest_route_navigation_active(state)
        or state.active_rope_x is not None
        or state.rope_rest_pending
    ):
        return False
    now = time.monotonic()
    if now < getattr(state.combat, "combat_suppressed_until", 0.0):
        return False
    snapshot = combat_logic.read_monster_snapshot(runtime)
    if not snapshot.fresh or snapshot.attackable_count <= 0:
        return False
    intent = combat_logic.build_action_intent(state.combat, snapshot)
    if intent.source != "combat" or intent.attackable_count <= 0:
        return False

    # 驻守已经是本平台的终态，不应再被上一轮JSON连接或路线恢复保护挡住。
    # 新怪进入攻击范围后直接以同一份快照唤醒战斗，避免检测线程 zant=1，
    # 但路线线程仍不断走 single_platform_idle 的永久原地锁。
    state.single_platform_idle_active = False
    state.single_platform_loot_target_x = None
    state.single_platform_loot_started_at = 0.0
    state.route_resume_grace_until = 0.0
    _clear_connection_priority(state)
    state.route_command_direction = None
    state.route_command_observed_direction = None
    state.route_command_anchor_x = None
    _remember_single_platform_monster_target(
        state,
        variant,
        position,
        intent,
    )
    _reset_combat_lock_watch(state)
    runtime.trace_event(
        "recorded_route_single_platform_idle_woken",
        position=position,
        target_direction=intent.target_direction,
        target_age_ms=intent.target_age_ms,
        attackable_count=intent.attackable_count,
        attackable_left_count=intent.attackable_left_count,
        attackable_right_count=intent.attackable_right_count,
        action="enter_common_combat_immediately",
    )
    _apply_vertical(runtime, state, "none")
    decision_changed = combat_logic.trace_action_decision(
        runtime,
        state.combat,
        intent,
        position,
    )
    combat_logic.apply_action_intent(
        runtime,
        state.combat,
        intent,
        position,
        decision_changed,
    )
    state.last_control_intent_at = now
    state.last_control_intent_kind = "combat"
    return True


def _apply_single_unignored_platform_patrol(
    runtime,
    state,
    variant,
    position,
) -> bool:
    """正常刷图固定唯一有效平台；掉到其他平台时让忽略逻辑送回。"""
    selection = _single_unignored_platform_patrol_variant(
        variant,
        state.ignored_platform_numbers,
    )
    if selection is None:
        state.single_active_platform_number = None
        state.single_active_platform_patrol_enabled = False
        return False
    active_number, patrol_variant = selection
    state.single_active_platform_number = int(active_number)
    state.single_active_platform_patrol_enabled = True
    if _rest_route_navigation_active(state):
        return False
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return False
    current_platform = variant.platform_ranges[platform_index]
    if (
        current_platform.platform_number is None
        or int(current_platform.platform_number) != int(active_number)
    ):
        # 其他平台都属于过滤平台；前面的忽略平台逻辑会持续推进JSON，
        # 直到重新落回唯一有效平台，不能在错误平台原地巡逻。
        return False
    state.active_platform_range_index = platform_index
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    _reset_single_platform_return_watchdog(state)
    _reset_platform_stall_tracking(state)
    if _apply_single_platform_loot_or_idle(
        runtime,
        state,
        patrol_variant,
        position,
    ):
        return True
    _apply_platform_patrol(runtime, state, patrol_variant, position)
    return True


def _apply_platform_full_coverage(runtime, state, variant, position) -> bool:
    """进入新平台后先走到录制起点，再完整执行该平台的全部刷图路线。"""
    if len(variant.platform_ranges) < 2 or state.platform_rotation_active:
        return False
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return False
    platform = variant.platform_ranges[platform_index]
    if platform_index != state.active_platform_range_index:
        previous_platform_index = state.active_platform_range_index
        state.active_platform_range_index = platform_index
        state.platform_coverage_target_x = platform.start_x
        state.platform_coverage_route_index = platform.start_index
        runtime.trace_event(
            "recorded_route_platform_coverage_started",
            position=position,
            previous_platform_index=previous_platform_index,
            platform_index=platform_index,
            platform_x_range=[platform.minimum_x, platform.maximum_x],
            platform_y_range=[platform.minimum_y, platform.maximum_y],
            target_x=platform.start_x,
            route_start_index=platform.start_index,
            action="reach_platform_start_before_sweep",
        )
    target_x = state.platform_coverage_target_x
    if target_x is None:
        return False
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="platform_full_coverage",
    )
    _apply_vertical(runtime, state, "none")
    if int(position[0]) < int(target_x) - ROUTE_PLATFORM_COVERAGE_TOLERANCE_X:
        _apply_horizontal(runtime, state, "right")
        return True
    if int(position[0]) > int(target_x) + ROUTE_PLATFORM_COVERAGE_TOLERANCE_X:
        _apply_horizontal(runtime, state, "left")
        return True

    _apply_horizontal(runtime, state, "none")
    state.route_index = state.platform_coverage_route_index
    state.last_jump_index = None
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    state.platform_coverage_count += 1
    runtime.trace_event(
        "recorded_route_platform_coverage_completed",
        position=position,
        platform_index=platform_index,
        route_start_index=platform.start_index,
        coverage_count=state.platform_coverage_count,
        action="start_full_platform_sweep",
    )
    return True


def _reset_platform_stall_tracking(state) -> None:
    """清除平台持续移动的停滞观察，不改变当前路线游标。"""
    state.platform_stall_direction = None
    state.platform_stall_route_index = None
    state.platform_stall_x = None
    state.platform_stall_y = None
    state.platform_stall_progress_at = 0.0
    state.platform_stall_observed_at = 0.0
    state.platform_stall_right_jump_attempted = False


def _apply_platform_coordinate_boundary_guard(
    runtime,
    state,
    variant,
    position,
    selected_index: int,
    point,
) -> bool:
    """平台坐标范围是硬边界，禁止普通路线继续向平台外侧移动。"""
    if point.segment_type != "platform" or point.horizontal not in ("left", "right"):
        return False
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=selected_index,
    )
    if platform_index is None:
        return False
    platform = variant.platform_ranges[platform_index]
    current_x = int(position[0])
    direction = point.horizontal
    outside_or_at_boundary = (
        current_x <= int(platform.minimum_x)
        if direction == "left"
        else current_x >= int(platform.maximum_x)
    )
    if not outside_or_at_boundary:
        return False

    # 先立即释放向外方向，不能再等待平台停滞计时器。若录制顺序表明平台
    # 末端紧接着绳子、下跳或走出连接，则直接把游标交给该连接点。
    _apply_horizontal(runtime, state, "none")
    _apply_vertical(runtime, state, "none")
    _reset_platform_stall_tracking(state)
    near_platform_end = int(selected_index) >= int(platform.end_index) - 2
    next_index = int(platform.end_index) + 1
    if next_index >= len(variant.points):
        next_index = 0 if variant.closed_loop else len(variant.points) - 1
    next_point = variant.points[next_index]
    if near_platform_end and next_point.segment_type != "platform":
        state.route_index = int(next_index)
        # 边界保护发生在本轮连接优先级判定之后。如果只改route_index，下一
        # 轮最近点选择会再次把游标拉回平台末点，形成边界处左右轻点却永远
        # 进不了rope_entry。这里当场建立连接独占，让入口助跑从下一帧起
        # 稳定持有游标，直到到达下一平台或连接超时。
        state.active_platform_range_index = int(platform_index)
        state.platform_coverage_target_x = None
        state.platform_coverage_route_index = None
        connection_locked = _update_connection_priority(
            runtime,
            state,
            variant,
            position,
            int(next_index),
        )
        runtime.trace_event(
            "recorded_route_platform_boundary_connection",
            position=position,
            platform_index=platform_index,
            platform_number=platform.platform_number,
            platform_x_range=[platform.minimum_x, platform.maximum_x],
            previous_route_index=int(selected_index),
            next_route_index=int(next_index),
            next_segment_type=next_point.segment_type,
            blocked_direction=direction,
            connection_locked=connection_locked,
            action="stop_outward_move_and_start_connection",
        )
        _apply_recorded_command(
            runtime,
            state,
            variant,
            position,
            int(next_index),
        )
        return True

    # 当前点并非平台出口，说明路线游标或人物位置发生了偏差。此时回到该平台
    # 的录制起点方向，不允许在边界处左右抖动。
    state.route_index = int(platform.start_index)
    state.active_platform_range_index = None
    state.platform_coverage_target_x = int(platform.start_x)
    state.platform_coverage_route_index = int(platform.start_index)
    inward_direction = "right" if direction == "left" else "left"
    _apply_horizontal(runtime, state, inward_direction)
    runtime.trace_event(
        "recorded_route_platform_boundary_recovered",
        position=position,
        platform_index=platform_index,
        platform_number=platform.platform_number,
        platform_x_range=[platform.minimum_x, platform.maximum_x],
        previous_route_index=int(selected_index),
        reset_route_index=int(platform.start_index),
        blocked_direction=direction,
        recovery_direction=inward_direction,
        action="return_inside_recorded_platform_range",
    )
    return True


def _recover_stalled_platform_movement(
    runtime,
    state,
    variant,
    position,
    selected_index: int,
    point,
) -> bool:
    """多平台路线在边缘无位移时前进到连接，断路时转回平台内部。

    该保护只处理无战斗时实际执行的平台命令。战斗、战后扫描或追怪造成的
    调度间隔会重置观察窗口，避免把正常停火误判成平台卡死。
    """
    if state.rope_rest_test_active:
        # 手动测试的目标是确定的休息点/绳子。这里若继续执行普通巡逻的
        # “固定向右跳一次”兜底，会把已经靠近绳入口的人物再次推离目标。
        # 测试导航由目标入口锁和上绳自适应负责恢复，不混入平台巡逻动作。
        _reset_platform_stall_tracking(state)
        return False
    if (
        len(variant.platform_ranges) < 2
        or point.segment_type != "platform"
        or point.horizontal not in ("left", "right")
    ):
        _reset_platform_stall_tracking(state)
        return False

    now = time.monotonic()
    current_x = int(position[0])
    current_y = int(position[1])
    direction = point.horizontal
    observation_gap = now - state.platform_stall_observed_at
    signature_changed = (
        state.platform_stall_direction != direction
        or state.platform_stall_route_index != int(selected_index)
        or observation_gap > ROUTE_PLATFORM_STALL_OBSERVATION_GAP_SECONDS
    )
    if state.platform_stall_x is None:
        state.platform_stall_direction = direction
        state.platform_stall_route_index = int(selected_index)
        state.platform_stall_x = current_x
        state.platform_stall_y = current_y
        state.platform_stall_progress_at = now
        state.platform_stall_observed_at = now
        state.platform_stall_right_jump_attempted = False
        return False
    if signature_changed:
        preserve_failed_right_jump = (
            state.platform_stall_right_jump_attempted
            and int(state.platform_stall_x) == current_x
        )
        state.platform_stall_direction = direction
        state.platform_stall_route_index = int(selected_index)
        state.platform_stall_observed_at = now
        if preserve_failed_right_jump:
            # 跳起时Y和最近路线点可能变化，但只要X仍没动，就保留“已经右跳过”
            # 的状态，落地后可继续进入第二级连接恢复。
            return False
        state.platform_stall_x = current_x
        state.platform_stall_y = current_y
        state.platform_stall_progress_at = now
        state.platform_stall_right_jump_attempted = False
        return False

    previous_x = int(state.platform_stall_x)
    previous_y = (
        int(state.platform_stall_y)
        if state.platform_stall_y is not None
        else current_y
    )
    horizontal_position_changed = current_x != previous_x
    vertical_position_changed = abs(current_y - previous_y) >= 2
    # 右跳脱困发出后，只有X真正发生变化才算脱困成功；单纯跳起又落回原地
    # 不能清除尝试标记，否则会在同一坐标无限重复右跳而永远不进入连接恢复。
    made_progress = (
        horizontal_position_changed
        if state.platform_stall_right_jump_attempted
        else horizontal_position_changed or vertical_position_changed
    )
    state.platform_stall_observed_at = now
    if made_progress:
        state.platform_stall_x = current_x
        state.platform_stall_y = current_y
        state.platform_stall_progress_at = now
        state.platform_stall_right_jump_attempted = False
        return False
    if now - state.platform_stall_progress_at < ROUTE_PLATFORM_STALL_SECONDS:
        return False

    if not state.platform_stall_right_jump_attempted:
        # 在同一坐标停留过久时先向右跳一次脱困。只有跳跃后仍持续无位移，
        # 才继续使用下面的连接推进/平台回扫逻辑，避免过早跳过当前平台。
        stalled_seconds = now - state.platform_stall_progress_at
        state.platform_stall_right_jump_attempted = True
        state.platform_stall_progress_at = now
        state.platform_stall_observed_at = now
        combat_logic.clear_combat_for_movement(runtime, state.combat)
        combat_logic.reset_attack_direction_lock(
            runtime,
            state.combat,
            reason="platform_coordinate_stalled_right_jump",
        )
        _apply_vertical(runtime, state, "none")
        _apply_horizontal(runtime, state, "right")
        runtime.pydirectinput.keyDown("c")
        try:
            completed = runtime.可中断等待(
                ROUTE_JUMP_HOLD_SECONDS,
                interval=0.01,
            )
        finally:
            runtime.pydirectinput.keyUp("c")
        if completed:
            runtime.可中断等待(
                ROUTE_PLATFORM_STALL_RIGHT_JUMP_COAST_SECONDS,
                interval=0.01,
            )
        _apply_horizontal(runtime, state, "none")
        state.last_jump_index = None
        state.last_jump_at = time.monotonic()
        runtime.trace_event(
            "recorded_route_platform_stall_right_jump",
            position=position,
            route_index=selected_index,
            recorded_direction=direction,
            stalled_seconds=round(stalled_seconds, 3),
            completed=completed,
            action="jump_right_before_route_advance",
        )
        return True

    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        _reset_platform_stall_tracking(state)
        return False
    platform = variant.platform_ranges[platform_index]
    at_boundary = (
        current_x <= platform.minimum_x + ROUTE_PLATFORM_STALL_EDGE_MARGIN_X
        if direction == "left"
        else current_x >= platform.maximum_x - ROUTE_PLATFORM_STALL_EDGE_MARGIN_X
    )
    near_platform_end = int(selected_index) >= int(platform.end_index) - 2
    if not at_boundary or not near_platform_end:
        state.platform_stall_x = current_x
        state.platform_stall_y = current_y
        state.platform_stall_progress_at = now
        state.platform_stall_right_jump_attempted = False
        return False

    next_index = int(platform.end_index) + 1
    if next_index >= len(variant.points):
        next_index = 0 if variant.closed_loop else len(variant.points) - 1
    next_point = variant.points[next_index]
    state.platform_stall_recovery_count += 1
    recovery_count = state.platform_stall_recovery_count
    _reset_platform_stall_tracking(state)

    if next_point.segment_type != "platform":
        state.route_index = next_index
        _apply_horizontal(runtime, state, "none")
        runtime.trace_event(
            "recorded_route_platform_stall_recovered",
            position=position,
            platform_index=platform_index,
            previous_route_index=selected_index,
            next_route_index=next_index,
            next_segment_type=next_point.segment_type,
            direction=direction,
            recovery_count=recovery_count,
            action="advance_to_recorded_connection",
        )
        _apply_recorded_command(
            runtime,
            state,
            variant,
            position,
            next_index,
        )
        return True

    opposite = "right" if direction == "left" else "left"
    state.route_index = platform.start_index
    state.active_platform_range_index = None
    state.platform_coverage_target_x = platform.start_x
    state.platform_coverage_route_index = platform.start_index
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="platform_route_gap",
    )
    _apply_vertical(runtime, state, "none")
    _apply_horizontal(runtime, state, opposite)
    runtime.trace_event(
        "recorded_route_platform_gap_recovered",
        position=position,
        platform_index=platform_index,
        previous_route_index=selected_index,
        next_route_index=next_index,
        next_platform_y=[next_point.y, point.y],
        previous_direction=direction,
        direction=opposite,
        recovery_count=recovery_count,
        action="reverse_and_rescan_current_platform",
    )
    return True


def _clear_active_rope(state) -> None:
    """清除本次动态挂绳和爬绳状态。"""
    state.active_rope_direction = None
    state.active_rope_x = None
    state.active_rope_top_y = None
    state.active_rope_bottom_y = None
    state.active_rope_started_at = 0.0
    state.active_rope_best_y = None
    state.active_rope_progress_at = 0.0
    state.active_rope_contacted = False
    state.active_rope_contacted_at = 0.0
    state.active_rope_contact_start_y = None
    state.active_rope_confirmed = False
    state.active_rope_success_recorded = False
    state.active_rope_contact_up_repressed = False
    state.active_rope_last_up_key_at = 0.0
    state.active_rope_exit_direction = None
    state.active_rope_exit_index = None
    state.active_rope_entry_index = None
    state.active_rope_attempt_offset_x = None
    state.active_rope_profile_key = None


def _clear_rope_top_exit(state) -> None:
    """清除绳顶反馈式离绳状态。"""
    state.rope_top_exit_pending = False
    state.rope_top_exit_direction = None
    state.rope_top_exit_route_index = None
    state.rope_top_exit_rope_x = None
    state.rope_top_exit_top_y = None
    state.rope_top_exit_bottom_y = None
    state.rope_top_exit_start_x = None
    state.rope_top_exit_started_at = 0.0


def _next_rope_exit(variant, entry_index: int) -> Tuple[Optional[int], Optional[str]]:
    """从绳子入口向后查找同一段绳子的离绳点和平台方向。"""
    total = len(variant.points)
    for offset in range(1, total):
        raw_index = int(entry_index) + offset
        if raw_index >= total and not variant.closed_loop:
            break
        index = raw_index % total
        point = variant.points[index]
        if point.segment_type == "rope_exit":
            direction = point.horizontal
            if direction in ("left", "right"):
                return index, direction
            return index, None
        if point.segment_type not in ("rope", "rope_entry"):
            break
    return None, None


def _latch_rope_from_current_position(runtime, state, variant, position) -> bool:
    """人物已在录制绳身上时重建活动绳状态，无需重新回到底部起跳。"""
    if (
        state.active_rope_direction in ("left", "right")
        and state.active_rope_x is not None
        and state.active_rope_top_y is not None
        and state.active_rope_bottom_y is not None
    ):
        return False

    position_x = int(position[0])
    position_y = int(position[1])
    candidates = []
    seen_geometry = set()
    for entry_index, point in enumerate(variant.points):
        if point.segment_type != "rope_entry":
            continue
        geometry = _normalized_rope_geometry(point)
        if geometry is None or geometry in seen_geometry:
            continue
        seen_geometry.add(geometry)
        rope_x, top_y, bottom_y = geometry
        x_delta = abs(position_x - rope_x)
        if x_delta > ROUTE_ROPE_BODY_TOLERANCE_X:
            continue
        body_top_y = top_y + max(
            ROUTE_ROPE_BODY_RECOGNITION_MARGIN_Y,
            ROUTE_ROPE_BODY_TOP_CLEARANCE_Y,
        )
        body_bottom_y = bottom_y - ROUTE_ROPE_BODY_RECOGNITION_MARGIN_Y
        if body_bottom_y < body_top_y:
            body_top_y = top_y
            body_bottom_y = bottom_y
        if not body_top_y <= position_y <= body_bottom_y:
            continue
        middle_y = int(round((top_y + bottom_y) / 2.0))
        candidates.append(
            (
                x_delta,
                abs(position_y - middle_y),
                entry_index,
                point,
                rope_x,
                top_y,
                bottom_y,
            )
        )
    if not candidates:
        return False

    candidates.sort(key=lambda candidate: candidate[:3])
    (
        x_delta,
        _middle_delta,
        entry_index,
        entry_point,
        rope_x,
        top_y,
        bottom_y,
    ) = candidates[0]
    exit_index, exit_direction = _next_rope_exit(variant, entry_index)
    entry_direction = entry_point.horizontal
    if entry_direction not in ("left", "right"):
        if position_x < rope_x:
            entry_direction = "right"
        elif position_x > rope_x:
            entry_direction = "left"
        elif exit_direction in ("left", "right"):
            entry_direction = exit_direction
        else:
            entry_direction = "right"

    now = time.monotonic()
    route_was_uninitialized = state.route_index is None
    state.route_index = entry_index
    state.last_jump_index = entry_index
    state.last_jump_at = now
    state.completed = False
    state.platform_direction = None
    state.active_rope_direction = entry_direction
    state.active_rope_x = rope_x
    state.active_rope_top_y = top_y
    state.active_rope_bottom_y = bottom_y
    state.active_rope_started_at = now
    state.active_rope_best_y = position_y
    state.active_rope_progress_at = now
    state.active_rope_contacted = True
    state.active_rope_contacted_at = now
    state.active_rope_contact_start_y = position_y
    state.active_rope_confirmed = True
    state.active_rope_success_recorded = True
    state.active_rope_contact_up_repressed = True
    state.active_rope_last_up_key_at = 0.0
    state.active_rope_exit_direction = exit_direction
    state.active_rope_exit_index = exit_index
    state.active_rope_entry_index = entry_index
    state.active_rope_attempt_offset_x = position_x - rope_x
    state.active_rope_profile_key = (rope_x, top_y, bottom_y)
    runtime.trace_event(
        "recorded_route_rope_body_latched",
        position=position,
        route_was_uninitialized=route_was_uninitialized,
        entry_index=entry_index,
        entry_direction=entry_direction,
        rope_x=rope_x,
        rope_top_y=top_y,
        rope_bottom_y=bottom_y,
        x_delta=x_delta,
        exit_index=exit_index,
        exit_direction=exit_direction,
        action="resume_climb_from_current_rope_position",
    )
    return True


def _apply_rope_top_exit(runtime, state, variant, position) -> bool:
    """反馈式离开绳顶；持续按上和平台方向，直到确认人物已经离绳。"""
    if (
        not state.rope_top_exit_pending
        or state.rope_top_exit_direction not in ("left", "right")
        or state.rope_top_exit_top_y is None
        or state.rope_top_exit_bottom_y is None
    ):
        return False
    direction = state.rope_top_exit_direction
    top_y = int(state.rope_top_exit_top_y)
    bottom_y = int(state.rope_top_exit_bottom_y)
    position_x = int(position[0])
    position_y = int(position[1])
    if position_y > bottom_y:
        previous_route_index = state.rope_top_exit_route_index
        _release_rope_rest_keys(runtime, state)
        _clear_rope_top_exit(state)
        state.route_index = None
        state.last_jump_index = None
        state.last_jump_at = 0.0
        runtime.请求人物重新定位(reason="recorded_route_rope_top_exit_fell")
        runtime.trace_event(
            "recorded_route_rope_top_exit_interrupted",
            position=position,
            previous_route_index=previous_route_index,
            reason="fell_below_rope",
            action="relocalize_and_reselect_route",
        )
        return True
    # 小地图经常稳定停在录制绳顶下方 1 像素。这个位置已经由活动爬绳阶段
    # 连续确认过，可以直接尝试横向离绳；若没有产生 X 位移，下面的反馈重试
    # 会短按上键后再次横移，不必无限等待定位偶尔跳到精确 top_y。
    if position_y > top_y + ROUTE_ROPE_TOP_EDGE_TOLERANCE_Y:
        _apply_horizontal(runtime, state, "none")
        _apply_vertical(runtime, state, "up")
        state.rope_top_exit_start_x = position_x
        state.rope_top_exit_started_at = time.monotonic()
        return True

    now = time.monotonic()
    platform_range_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.rope_top_exit_route_index,
    )
    reached_platform = platform_range_index is not None
    if state.rope_top_exit_start_x is None:
        state.rope_top_exit_start_x = position_x
        state.rope_top_exit_started_at = now
        combat_logic.clear_combat_for_movement(runtime, state.combat)
        # 到达录制 top_y 只证明人物进入绳顶坐标带，不一定已经完成翻上平台
        # 的最后一小段动画。离绳确认前持续保持上+目标方向，避免先松上后在
        # 绳顶悬停 0.65 秒，画面上看起来像“没有爬到最顶”。
        _apply_vertical(runtime, state, "up")
        _apply_horizontal(runtime, state, direction)
        runtime.trace_event(
            "recorded_route_rope_top_exit_started",
            position=position,
            direction=direction,
            rope_x=state.rope_top_exit_rope_x,
            rope_top_y=top_y,
            route_index=state.rope_top_exit_route_index,
        )
        return True
    start_x = int(state.rope_top_exit_start_x)
    moved_x = abs(position_x - start_x)
    directional_moved_x = (
        position_x - start_x
        if direction == "right"
        else start_x - position_x
    )
    platform_range = (
        variant.platform_ranges[platform_range_index]
        if platform_range_index is not None
        else None
    )
    inside_platform_x = (
        platform_range is not None
        and int(platform_range.minimum_x)
        <= position_x
        <= int(platform_range.maximum_x)
    )
    # 绳身上的 X 坐标不会正常移动。确认已经处于绳顶、坐标匹配到上层平台，
    # 且 X 产生了实际位移后，即可认为人物已离绳，不再要求 Y 精确等于录制 top_y。
    if (
        directional_moved_x >= ROUTE_ROPE_TOP_EXIT_MIN_X_CHANGE
        and reached_platform
        and inside_platform_x
    ):
        route_index = state.rope_top_exit_route_index
        retry_count = state.rope_top_exit_retry_count
        _apply_vertical(runtime, state, "none")
        _clear_rope_top_exit(state)
        # 上一根绳子的入口已经完整结束。后续若被击落并重新规划回同一个
        # rope_entry，必须允许它作为一次全新的跳跃再次发送C。
        state.last_jump_index = None
        state.last_jump_at = 0.0
        _clear_rope_entry_runup(state)
        if route_index is not None:
            state.route_index = route_index
        runtime.trace_event(
            "recorded_route_rope_top_exit_completed",
            position=position,
            direction=direction,
            moved_x=moved_x,
            directional_moved_x=directional_moved_x,
            platform_range_index=platform_range_index,
            platform_x_range=(
                [platform_range.minimum_x, platform_range.maximum_x]
                if platform_range is not None
                else None
            ),
            confirmation_reason="top_platform_x_changed",
            retry_count=retry_count,
            route_index=route_index,
        )
        return False
    if now - state.rope_top_exit_started_at < ROUTE_ROPE_TOP_EXIT_STALL_SECONDS:
        combat_logic.clear_combat_for_movement(runtime, state.combat)
        _apply_vertical(runtime, state, "up")
        _apply_horizontal(runtime, state, direction)
        return True

    _apply_horizontal(runtime, state, "none")
    _apply_vertical(runtime, state, "up")
    completed = runtime.可中断等待(
        ROUTE_ROPE_TOP_EXIT_UP_SECONDS,
        interval=0.02,
    )
    _apply_vertical(runtime, state, "none")
    if not completed:
        return True
    latest_position = runtime.读取人物位置() or position
    state.rope_top_exit_retry_count += 1
    state.rope_top_exit_start_x = int(latest_position[0])
    state.rope_top_exit_started_at = time.monotonic()
    runtime.trace_event(
        "recorded_route_rope_top_exit_up_retry",
        position=latest_position,
        direction=direction,
        retry_count=state.rope_top_exit_retry_count,
        up_hold_seconds=ROUTE_ROPE_TOP_EXIT_UP_SECONDS,
        reason="horizontal_x_unchanged",
    )
    _apply_horizontal(runtime, state, direction)
    return True


def _mark_active_rope_top_reached(runtime, state, position, reason: str) -> None:
    """结束活动爬绳并切换到反馈式离绳，保留必要的绳顶几何。"""
    _record_rope_entry_success(runtime, state, position)
    rope_x = int(state.active_rope_x)
    top_y = int(state.active_rope_top_y)
    bottom_y = int(state.active_rope_bottom_y)
    exit_direction = state.active_rope_exit_direction
    exit_route_index = state.active_rope_exit_index
    _apply_horizontal(runtime, state, "none")
    # 精确到达录制绳顶后直接进入“上+离绳方向”，不要先松开上键等待下一轮
    # 路线循环。这样从正常爬绳到翻上平台之间不存在一次人为的按键空档。
    _apply_vertical(
        runtime,
        state,
        "up" if exit_direction in ("left", "right") else "none",
    )
    _clear_active_rope(state)
    if exit_direction in ("left", "right"):
        state.rope_top_exit_pending = True
        state.rope_top_exit_direction = exit_direction
        state.rope_top_exit_route_index = exit_route_index
        state.rope_top_exit_rope_x = rope_x
        state.rope_top_exit_top_y = top_y
        state.rope_top_exit_bottom_y = bottom_y
        state.rope_top_exit_start_x = None
        state.rope_top_exit_started_at = 0.0
        state.rope_top_exit_retry_count = 0
    runtime.trace_event(
        "recorded_route_rope_top_reached",
        position=position,
        rope_x=rope_x,
        rope_top_y=top_y,
        rope_bottom_y=bottom_y,
        exit_direction=exit_direction,
        exit_route_index=exit_route_index,
        confirmation_reason=reason,
    )


def _recover_stalled_rope_climb(runtime, state, position) -> bool:
    """持续按上但Y无上升进展时，强制重定位并清空路线游标重新规划。"""
    current_y = int(position[1])
    now = time.monotonic()
    if state.active_rope_best_y is None:
        state.active_rope_best_y = current_y
        state.active_rope_progress_at = now
        return False
    if current_y < int(state.active_rope_best_y):
        state.active_rope_best_y = current_y
        state.active_rope_progress_at = now
        return False
    if state.active_rope_progress_at <= 0:
        state.active_rope_progress_at = now
        return False
    stalled_seconds = now - state.active_rope_progress_at
    if stalled_seconds < ROUTE_ROPE_CLIMB_STALL_SECONDS:
        return False

    rope_x = state.active_rope_x
    rope_top_y = state.active_rope_top_y
    rope_bottom_y = state.active_rope_bottom_y
    previous_route_index = state.route_index
    _release_rope_rest_keys(runtime, state)
    runtime.请求人物重新定位(reason="recorded_route_rope_y_stalled")
    state.route_index = None
    state.last_jump_index = None
    state.last_jump_at = 0.0
    state.last_relocalized_index = None
    state.last_relocalized_at = 0.0
    state.completed = False
    state.rope_stall_relocation_count += 1
    _clear_active_rope(state)
    runtime.trace_event(
        "recorded_route_rope_stalled_relocation",
        position=position,
        stalled_seconds=round(stalled_seconds, 3),
        previous_route_index=previous_route_index,
        rope_x=rope_x,
        rope_top_y=rope_top_y,
        rope_bottom_y=rope_bottom_y,
        relocation_count=state.rope_stall_relocation_count,
        action="clear_position_then_reacquire_rope_body",
    )
    return True


def _rope_rest_settings(runtime, state=None) -> Tuple[float, float]:
    """优先读取路线休息点的自定义时间，没有时使用页面全局配置。"""
    interval_override = getattr(state, "recorded_rest_interval_seconds", None)
    duration_override = getattr(state, "recorded_rest_duration_seconds", None)
    interval_seconds = (
        max(0.0, float(interval_override))
        if interval_override is not None
        else max(
            0.0,
            float(getattr(runtime, "绳子休息间隔分钟", 20.0)) * 60.0,
        )
    )
    duration_seconds = (
        max(0.0, float(duration_override))
        if duration_override is not None
        else max(
            0.0,
            float(getattr(runtime, "绳子休息时长分钟", 1.0)) * 60.0,
        )
    )
    return interval_seconds, duration_seconds


def _notify_rope_rest_schedule(
    runtime,
    state,
    status,
    remaining_seconds=0.0,
) -> None:
    """向页面同步真实休息阶段；页面只负责显示，不自行推算路线状态。"""
    callback = getattr(runtime, "recorded_rest_schedule_callback", None)
    if not callable(callback):
        return
    if state.recorded_rest_point_enabled:
        rest_mode = (
            "recorded_rope_point"
            if state.recorded_rest_on_rope
            else "recorded_platform_point"
        )
    elif state.rope_rest_fallback_enabled:
        rest_mode = "rope_middle"
    else:
        rest_mode = "disabled"
    try:
        callback(
            {
                "status": str(status),
                "remaining_seconds": round(max(0.0, float(remaining_seconds)), 2),
                "rest_count": int(state.rope_rest_count + 1),
                "rest_mode": rest_mode,
            }
        )
    except Exception as exc:
        runtime.trace_event(
            "recorded_route_rest_schedule_callback_failed",
            status=str(status),
            error="{}: {}".format(type(exc).__name__, exc),
        )


def _enter_rest_point_test_control(runtime, state) -> None:
    """测试期间让路线独占移动控制，并立即清除残留攻击和追怪状态。"""
    runtime.休息点测试进行中 = True
    runtime.zant = 0
    # “忽略平台”会把路线游标强制推进到该平台之后。测试休息点必须从人物
    # 当前真实位置重新定位，否则刚跳过平台后发起测试仍会沿用错误的后续游标。
    state.active_ignored_platform_number = None
    state.ignored_platform_exit_index = None
    _reset_platform_replan(state)
    state.route_index = None
    state.last_relocalized_index = None
    state.last_jump_index = None
    state.last_jump_at = 0.0
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    state.platform_direction = None
    _reset_platform_stall_tracking(state)
    _clear_rope_entry_runup(state)
    state.smart_seek_target_active = False
    state.smart_seek_scan_until = 0.0
    state.smart_seek_mode = None
    _clear_smart_seek_target_loss(state)
    state.route_resume_grace_until = 0.0
    _clear_connection_priority(state)
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    state.recorded_rest_member_panel_opened = False
    state.recorded_rest_shop_open_failures = 0
    state.rest_point_test_parked = False
    # 先释放测试按钮按下前遗留的巡逻方向，避免 route_index 已经清空但旧的
    # left/right 物理键仍保持按下，导致重新定位的第一帧继续冲向地图边缘。
    _apply_horizontal(runtime, state, "none")
    if state.active_rope_x is None:
        _apply_vertical(runtime, state, "none")
    full_relocation_event = getattr(runtime, "人物全图重定位事件", None)
    if full_relocation_event is not None:
        full_relocation_event.set()
    runtime.重置攻击状态()
    runtime.重置战斗感知()
    runtime.释放攻击键()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="rest_point_test_started",
    )
    runtime.trace_event(
        "recorded_route_rest_point_test_control_acquired",
        action="suppress_combat_and_prioritize_rest_route",
    )


def _maintain_rest_point_test_control(runtime, state) -> None:
    """抵达前持续压制检测线程刚产生的攻击按键，不破坏绳子持续键。"""
    if not state.rope_rest_test_active:
        return
    runtime.休息点测试进行中 = True
    runtime.zant = 0
    runtime.清除攻击意图()
    runtime.释放攻击键()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="rest_point_test_active",
    )


def _maintain_rest_navigation_control(runtime, state) -> None:
    """所有休息导航期间暂停战斗抢占，让路线持续返回休息点。"""
    if not _rest_route_navigation_active(state) or state.rest_point_test_parked:
        return
    runtime.zant = 0
    runtime.清除攻击意图()
    runtime.释放攻击键()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="rest_navigation_active",
    )


def _leave_rest_point_test_control(runtime, state, reason) -> None:
    """测试成功、失败或任务停止时恢复公共战斗检测。"""
    runtime.休息点测试进行中 = False
    runtime.zant = 0
    runtime.清除攻击意图()
    runtime.释放攻击键()
    runtime.重置战斗感知()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(runtime, state.combat, reason=reason)
    runtime.trace_event(
        "recorded_route_rest_point_test_control_released",
        reason=reason,
    )


def _consume_rope_rest_test_request(runtime, state) -> bool:
    """消费页面的一次性测试请求，并立刻切换到正常休息点流程。"""
    request_event = getattr(runtime, "测试休息点事件", None)
    if request_event is None or not request_event.is_set():
        return False
    request_event.clear()
    interval_seconds, duration_seconds = _rope_rest_settings(runtime, state)
    unavailable_reason = None
    if not (
        state.recorded_rest_point_enabled
        or state.rope_rest_fallback_enabled
    ):
        unavailable_reason = "no_recorded_rest_point_or_valid_rope"
    elif interval_seconds <= 0 or duration_seconds <= 0:
        unavailable_reason = "rest_interval_or_duration_disabled"
    elif state.rope_rest_pending or state.rope_resting:
        unavailable_reason = "rest_cycle_already_active"
    if unavailable_reason is not None:
        runtime.trace_event(
            "recorded_route_rest_point_test_rejected",
            reason=unavailable_reason,
            interval_seconds=round(interval_seconds, 2),
            duration_seconds=round(duration_seconds, 2),
        )
        if unavailable_reason == "rest_cycle_already_active":
            active_remaining = state.rope_rest_remaining_seconds
            if state.rope_resting and state.rope_rest_until > 0:
                active_remaining = max(
                    0.0,
                    state.rope_rest_until - time.monotonic(),
                )
            _notify_rope_rest_schedule(
                runtime,
                state,
                "resting" if state.rope_resting else "pending",
                active_remaining,
            )
        else:
            _notify_rope_rest_schedule(runtime, state, "test_unavailable")
        return False

    state.rope_rest_test_active = True
    current_time = time.monotonic()
    state.rope_rest_test_resume_seconds = (
        max(0.0, state.next_rope_rest_at - current_time)
        if state.next_rope_rest_at > current_time
        else interval_seconds
    )
    state.rope_rest_pending = True
    state.rope_resting = False
    state.rope_rest_until = 0.0
    state.rope_rest_remaining_seconds = duration_seconds
    state.recorded_rest_phase = None
    state.recorded_rest_jump_at = 0.0
    state.recorded_rest_jump_attempts = 0
    state.recorded_rest_recovery_count = 0
    state.recorded_rest_landing_candidate_at = 0.0
    state.recorded_rest_landing_candidate_frames = 0
    state.recorded_rest_member_panel_opened = False
    state.recorded_rest_shop_open_failures = 0
    state.rest_point_test_parked = False
    _enter_rest_point_test_control(runtime, state)
    _notify_rope_rest_schedule(runtime, state, "pending", duration_seconds)
    runtime.trace_event(
        "recorded_route_rest_point_test_started",
        rest_count=state.rope_rest_count + 1,
        duration_seconds=round(duration_seconds, 2),
        rest_mode=(
            "recorded_rope_point"
            if state.recorded_rest_on_rope
            else (
                "recorded_platform_point"
                if state.recorded_rest_point_enabled
                else "rope_middle"
            )
        ),
    )
    return True


def _count_recorded_rope_rest_segments(plan: RecordedRoutePlan) -> int:
    """统计所有路线变体中可计算动态中点的有效绳子入口。"""
    return sum(
        1
        for variant in plan.variants
        for point in variant.points
        if point.segment_type == "rope_entry"
        and _normalized_rope_geometry(point) is not None
    )


def _find_recorded_rest_rope_geometry(
    plan: RecordedRoutePlan,
) -> Optional[Tuple[int, int, int]]:
    """若录制休息点位于某段绳身内部，返回该绳子的X、顶部Y和底部Y。"""
    rest_point = plan.rest_point
    if rest_point is None:
        return None
    candidates = []
    for variant in plan.variants:
        for point in variant.points:
            if point.segment_type != "rope_entry":
                continue
            geometry = _normalized_rope_geometry(point)
            if geometry is None:
                continue
            rope_x, top_y, bottom_y = geometry
            # 排除绳顶/绳底与平台重叠的端点，只把明确位于绳身内部的坐标
            # 识别成绳上休息点，避免平台休息点刚好靠近绳子时误分类。
            if not (
                abs(int(rest_point.x) - int(rope_x))
                <= RECORDED_REST_POINT_LANDING_X_TOLERANCE
                and int(top_y) + 2
                <= int(rest_point.y)
                <= int(bottom_y) - 2
            ):
                continue
            candidates.append(
                (
                    abs(int(rest_point.x) - int(rope_x)),
                    abs(
                        int(rest_point.y)
                        - int(round((int(top_y) + int(bottom_y)) / 2.0))
                    ),
                    int(rope_x),
                    int(top_y),
                    int(bottom_y),
                )
            )
    if not candidates:
        return None
    candidates.sort()
    _x_delta, _middle_delta, rope_x, top_y, bottom_y = candidates[0]
    return rope_x, top_y, bottom_y


def _initialize_rope_rest_schedule(runtime, state) -> None:
    """从本轮路线启动时间开始安排第一次绳中休息。"""
    if not (
        state.recorded_rest_point_enabled
        or state.rope_rest_fallback_enabled
    ):
        state.next_rope_rest_at = 0.0
        _notify_rope_rest_schedule(runtime, state, "disabled")
        return
    interval_seconds, duration_seconds = _rope_rest_settings(runtime, state)
    if interval_seconds <= 0 or duration_seconds <= 0:
        state.next_rope_rest_at = 0.0
        _notify_rope_rest_schedule(runtime, state, "disabled")
        return
    state.next_rope_rest_at = time.monotonic() + interval_seconds
    _notify_rope_rest_schedule(runtime, state, "scheduled", interval_seconds)


def _update_rope_rest_schedule(runtime, state, now=None) -> bool:
    """到达配置周期后标记休息，优先使用JSON休息点，否则等待绳子中点。"""
    current_time = time.monotonic() if now is None else float(now)
    if not (
        state.recorded_rest_point_enabled
        or state.rope_rest_fallback_enabled
    ):
        state.next_rope_rest_at = 0.0
        state.rope_rest_pending = False
        return False
    interval_seconds, duration_seconds = _rope_rest_settings(runtime, state)
    if interval_seconds <= 0 or duration_seconds <= 0:
        state.next_rope_rest_at = 0.0
        state.rope_rest_pending = False
        state.rope_resting = False
        state.rope_rest_until = 0.0
        state.rope_rest_remaining_seconds = 0.0
        state.recorded_rest_phase = None
        state.recorded_rest_jump_at = 0.0
        state.recorded_rest_jump_attempts = 0
        return False
    if state.next_rope_rest_at <= 0:
        state.next_rope_rest_at = current_time + interval_seconds
        _notify_rope_rest_schedule(runtime, state, "scheduled", interval_seconds)
        return False
    if (
        state.rope_rest_pending
        or state.rope_resting
        or current_time < state.next_rope_rest_at
    ):
        return False
    state.rope_rest_pending = True
    state.rope_rest_remaining_seconds = duration_seconds
    state.recorded_rest_recovery_count = 0
    _notify_rope_rest_schedule(runtime, state, "pending", duration_seconds)
    runtime.trace_event(
        (
            "recorded_route_rest_point_scheduled"
            if state.recorded_rest_point_enabled
            else "recorded_route_rope_rest_scheduled"
        ),
        rest_count=state.rope_rest_count + 1,
        interval_minutes=round(interval_seconds / 60.0, 2),
        duration_minutes=round(duration_seconds / 60.0, 2),
        rest_mode=(
            (
                "recorded_rope_point"
                if state.recorded_rest_on_rope
                else "recorded_point"
            )
            if state.recorded_rest_point_enabled
            else "rope_middle"
        ),
    )
    return True


def _active_rope_middle_y(state) -> Optional[int]:
    """根据当前JSON绳子的顶部和底部坐标动态计算中点Y。"""
    if state.active_rope_top_y is None or state.active_rope_bottom_y is None:
        return None
    top_y = min(
        int(state.active_rope_top_y),
        int(state.active_rope_bottom_y),
    )
    bottom_y = max(
        int(state.active_rope_top_y),
        int(state.active_rope_bottom_y),
    )
    if bottom_y <= top_y:
        return None
    return int(round((top_y + bottom_y) / 2.0))


def _release_rope_rest_keys(runtime, state) -> None:
    """休息期间释放全部动作键，但保留当前绳子的动态几何状态。"""
    _apply_horizontal(runtime, state, "none")
    _apply_vertical(runtime, state, "none")
    runtime.pydirectinput.keyUp("c")
    runtime.清除攻击意图()
    runtime.释放攻击键()


def _load_recorded_rest_bell_button_template(runtime):
    """按需解码休息入口黄色铃铛模板，并在后续休息周期中复用。"""
    global _recorded_rest_bell_button_template
    if _recorded_rest_bell_button_template is not None:
        return _recorded_rest_bell_button_template
    raw = base64.b64decode(RECORDED_REST_BELL_BUTTON_TEMPLATE_BASE64)
    template = runtime.cv2.imdecode(
        runtime.np.frombuffer(raw, dtype=runtime.np.uint8),
        runtime.cv2.IMREAD_COLOR,
    )
    if template is None:
        raise ValueError("休息入口黄色铃铛模板解码失败")
    _recorded_rest_bell_button_template = template
    return template


def _load_recorded_rest_open_button_template(runtime):
    """解码最新和旧版枫叶模板，覆盖图标闪烁时的不同显示状态。"""
    global _recorded_rest_open_button_template
    if _recorded_rest_open_button_template is not None:
        return _recorded_rest_open_button_template
    templates = []
    for encoded in (
        RECORDED_REST_OPEN_BUTTON_TEMPLATE_BASE64,
        RECORDED_REST_OPEN_BUTTON_TEMPLATE_BASE64_LEGACY,
    ):
        raw = base64.b64decode(encoded)
        template = runtime.cv2.imdecode(
            runtime.np.frombuffer(raw, dtype=runtime.np.uint8),
            runtime.cv2.IMREAD_COLOR,
        )
        if template is None:
            raise ValueError("休息入口橙色枫叶模板解码失败")
        templates.append(template)
    _recorded_rest_open_button_template = tuple(templates)
    return _recorded_rest_open_button_template


def _best_recorded_rest_icon_match(runtime, frame, templates):
    """以彩色、灰度和边缘三种方式匹配模板，降低闪烁亮度变化造成的漏检。"""
    gray_frame = runtime.cv2.cvtColor(frame, runtime.cv2.COLOR_BGR2GRAY)
    edge_frame = runtime.cv2.Canny(gray_frame, 45, 130)
    best_match = None
    for template_index, template in enumerate(templates):
        template_height, template_width = template.shape[:2]
        if frame.shape[0] < template_height or frame.shape[1] < template_width:
            continue
        gray_template = runtime.cv2.cvtColor(
            template,
            runtime.cv2.COLOR_BGR2GRAY,
        )
        edge_template = runtime.cv2.Canny(gray_template, 45, 130)
        candidates = (
            (
                "color",
                frame,
                template,
                RECORDED_REST_OPEN_BUTTON_MATCH_THRESHOLD,
            ),
            (
                "gray",
                gray_frame,
                gray_template,
                RECORDED_REST_OPEN_BUTTON_GRAY_MATCH_THRESHOLD,
            ),
            (
                "edge",
                edge_frame,
                edge_template,
                RECORDED_REST_OPEN_BUTTON_EDGE_MATCH_THRESHOLD,
            ),
        )
        for match_mode, search_image, search_template, threshold in candidates:
            result = runtime.cv2.matchTemplate(
                search_image,
                search_template,
                runtime.cv2.TM_CCOEFF_NORMED,
            )
            _minimum, confidence, _minimum_location, location = (
                runtime.cv2.minMaxLoc(result)
            )
            confidence = float(confidence)
            quality = confidence / max(0.01, float(threshold))
            candidate = (
                quality,
                confidence,
                location,
                template_width,
                template_height,
                match_mode,
                threshold,
                template_index,
            )
            if best_match is None or candidate[0] > best_match[0]:
                best_match = candidate
    return best_match


def _click_recorded_rest_icon_button(
    runtime,
    state,
    position,
    template_loader,
    event_prefix,
    icon_label,
) -> bool:
    """在整个游戏窗口识别休息图标，并点击实际匹配区域的中心。"""
    rest_count = state.rope_rest_count + 1
    try:
        loaded_templates = template_loader(runtime)
        templates = (
            tuple(loaded_templates)
            if isinstance(loaded_templates, (tuple, list))
            else (loaded_templates,)
        )
    except Exception as exc:
        runtime.trace_event(
            "{}_not_found".format(event_prefix),
            rest_count=rest_count,
            position=position,
            reason="template_decode_failed",
            error="{}: {}".format(type(exc).__name__, exc),
        )
        return False

    capture_width = max(
        1,
        int(getattr(runtime, "游戏窗口外框宽度", 1280)),
    )
    capture_height = max(
        1,
        int(getattr(runtime, "游戏窗口外框高度", 831)),
    )
    search_attempts = max(
        1,
        int(
            round(
                RECORDED_REST_OPEN_BUTTON_SEARCH_SECONDS
                / RECORDED_REST_OPEN_BUTTON_SEARCH_INTERVAL_SECONDS
            )
        ),
    )
    highest_confidence = 0.0
    last_error = None

    for attempt in range(1, search_attempts + 1):
        # 可中断等待同时会等待游戏窗口重新取得焦点，避免弹窗遮住游戏时
        # 在其他窗口上识别或点击同色图案。
        if not runtime.可中断等待(0.01, interval=0.01):
            last_error = "automation_stopped"
            break
        try:
            screenshot = runtime.grab_screen(
                {
                    "left": 0,
                    "top": 0,
                    "width": capture_width,
                    "height": capture_height,
                }
            )
            frame = runtime.np.asarray(screenshot)
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = runtime.cv2.cvtColor(frame, runtime.cv2.COLOR_BGRA2BGR)
            elif frame.ndim != 3 or frame.shape[2] != 3:
                raise ValueError("游戏窗口截图通道数异常")
            best_match = _best_recorded_rest_icon_match(
                runtime,
                frame,
                templates,
            )
            if best_match is None:
                raise ValueError("游戏窗口截图小于{}模板".format(icon_label))
            (
                _quality,
                confidence,
                location,
                template_width,
                template_height,
                match_mode,
                match_threshold,
                template_index,
            ) = best_match
            highest_confidence = max(highest_confidence, confidence)
            if confidence >= match_threshold:
                click_x = int(location[0] + template_width / 2)
                click_y = int(location[1] + template_height / 2)
                runtime.pydirectinput.click(click_x, click_y)
                runtime.trace_event(
                    "{}_clicked".format(event_prefix),
                    rest_count=rest_count,
                    position=position,
                    icon=icon_label,
                    confidence=round(confidence, 4),
                    match_mode=match_mode,
                    match_threshold=match_threshold,
                    template_index=template_index,
                    matched_position=[int(location[0]), int(location[1])],
                    click_position=[click_x, click_y],
                    fixed_position=False,
                    capture_size=[capture_width, capture_height],
                    attempt=attempt,
                )
                runtime.可中断等待(
                    RECORDED_REST_OPEN_BUTTON_RESPONSE_SECONDS,
                    interval=0.01,
                )
                return True
        except Exception as exc:
            last_error = "{}: {}".format(type(exc).__name__, exc)

        if attempt < search_attempts:
            if not runtime.可中断等待(
                RECORDED_REST_OPEN_BUTTON_SEARCH_INTERVAL_SECONDS,
                interval=0.02,
            ):
                last_error = "automation_stopped"
                break

    runtime.trace_event(
        "{}_not_found".format(event_prefix),
        rest_count=rest_count,
        position=position,
        icon=icon_label,
        highest_confidence=round(highest_confidence, 4),
        threshold=RECORDED_REST_OPEN_BUTTON_MATCH_THRESHOLD,
        gray_threshold=RECORDED_REST_OPEN_BUTTON_GRAY_MATCH_THRESHOLD,
        edge_threshold=RECORDED_REST_OPEN_BUTTON_EDGE_MATCH_THRESHOLD,
        attempts=search_attempts,
        error=last_error,
    )
    return False


def _click_recorded_rest_bell_button(runtime, state, position) -> bool:
    """识别并点击黄色铃铛；橙色枫叶未直接出现时才调用。"""
    return _click_recorded_rest_icon_button(
        runtime,
        state,
        position,
        _load_recorded_rest_bell_button_template,
        "recorded_route_rest_bell_button",
        "黄色铃铛",
    )


def _click_recorded_rest_open_button(runtime, state, position) -> bool:
    """识别并点击橙色枫叶，打开包含便捷杂货店的会员界面。"""
    return _click_recorded_rest_icon_button(
        runtime,
        state,
        position,
        _load_recorded_rest_open_button_template,
        "recorded_route_rest_open_button",
        "橙色枫叶",
    )


def _load_recorded_rest_shop_anchor_template(runtime):
    """按需解码“便捷杂货店”房屋图标定位模板。"""
    global _recorded_rest_shop_anchor_template
    if _recorded_rest_shop_anchor_template is not None:
        return _recorded_rest_shop_anchor_template
    raw = base64.b64decode(RECORDED_REST_SHOP_ICON_TEMPLATE_BASE64)
    template = runtime.cv2.imdecode(
        runtime.np.frombuffer(raw, dtype=runtime.np.uint8),
        runtime.cv2.IMREAD_COLOR,
    )
    if template is None:
        raise ValueError("便捷杂货店定位模板解码失败")
    _recorded_rest_shop_anchor_template = template
    return template


def _click_recorded_rest_shop_open_button(runtime, state, position) -> bool:
    """定位便捷杂货店行并点击该行右侧的第二个“打开”按钮。"""
    rest_count = state.rope_rest_count + 1
    try:
        template = _load_recorded_rest_shop_anchor_template(runtime)
    except Exception as exc:
        runtime.trace_event(
            "recorded_route_rest_shop_open_button_not_found",
            rest_count=rest_count,
            position=position,
            reason="template_decode_failed",
            error="{}: {}".format(type(exc).__name__, exc),
        )
        return False

    capture_width = max(
        1,
        int(getattr(runtime, "游戏窗口外框宽度", 1280)),
    )
    capture_height = max(
        1,
        int(getattr(runtime, "游戏窗口外框高度", 831)),
    )
    template_height, template_width = template.shape[:2]
    search_attempts = max(
        1,
        int(
            round(
                RECORDED_REST_SHOP_ANCHOR_SEARCH_SECONDS
                / RECORDED_REST_OPEN_BUTTON_SEARCH_INTERVAL_SECONDS
            )
        ),
    )
    highest_confidence = 0.0
    last_error = None

    for attempt in range(1, search_attempts + 1):
        if not runtime.可中断等待(0.01, interval=0.01):
            last_error = "automation_stopped"
            break
        try:
            screenshot = runtime.grab_screen(
                {
                    "left": 0,
                    "top": 0,
                    "width": capture_width,
                    "height": capture_height,
                }
            )
            frame = runtime.np.asarray(screenshot)
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = runtime.cv2.cvtColor(frame, runtime.cv2.COLOR_BGRA2BGR)
            elif frame.ndim != 3 or frame.shape[2] != 3:
                raise ValueError("游戏窗口截图通道数异常")
            if frame.shape[0] < template_height or frame.shape[1] < template_width:
                raise ValueError("游戏窗口截图小于便捷杂货店定位模板")

            result = runtime.cv2.matchTemplate(
                frame,
                template,
                runtime.cv2.TM_CCOEFF_NORMED,
            )
            _minimum, confidence, _minimum_location, location = (
                runtime.cv2.minMaxLoc(result)
            )
            confidence = float(confidence)
            highest_confidence = max(highest_confidence, confidence)
            if confidence >= RECORDED_REST_SHOP_ANCHOR_MATCH_THRESHOLD:
                click_x = int(location[0] + RECORDED_REST_SHOP_BUTTON_OFFSET_X)
                click_y = int(location[1] + RECORDED_REST_SHOP_BUTTON_OFFSET_Y)
                if not (
                    0 <= click_x < capture_width
                    and 0 <= click_y < capture_height
                ):
                    raise ValueError("便捷杂货店打开按钮坐标超出游戏窗口")
                runtime.pydirectinput.click(click_x, click_y)
                runtime.trace_event(
                    "recorded_route_rest_shop_open_button_clicked",
                    rest_count=rest_count,
                    position=position,
                    confidence=round(confidence, 4),
                    anchor_position=[int(location[0]), int(location[1])],
                    click_position=[click_x, click_y],
                    attempt=attempt,
                )
                runtime.可中断等待(
                    RECORDED_REST_OPEN_BUTTON_RESPONSE_SECONDS,
                    interval=0.01,
                )
                return True
        except Exception as exc:
            last_error = "{}: {}".format(type(exc).__name__, exc)

        if attempt < search_attempts:
            if not runtime.可中断等待(
                RECORDED_REST_OPEN_BUTTON_SEARCH_INTERVAL_SECONDS,
                interval=0.02,
            ):
                last_error = "automation_stopped"
                break

    runtime.trace_event(
        "recorded_route_rest_shop_open_button_not_found",
        rest_count=rest_count,
        position=position,
        highest_confidence=round(highest_confidence, 4),
        threshold=RECORDED_REST_SHOP_ANCHOR_MATCH_THRESHOLD,
        attempts=search_attempts,
        error=last_error,
    )
    return False


def _begin_recorded_rest(runtime, state, rest_point, position, now) -> bool:
    """人物到达录制落点后直接启动非阻塞定时休息，不再操作休息界面。"""
    _release_rope_rest_keys(runtime, state)
    _interval_seconds, duration_seconds = _rope_rest_settings(runtime, state)
    remaining_seconds = state.rope_rest_remaining_seconds
    if state.rope_rest_test_active:
        # 测试只验证寻路和落点，成功后保持停驻，不等待正式休息时长。
        remaining_seconds = 0.0
    elif remaining_seconds <= 0:
        remaining_seconds = duration_seconds

    # 新休息规则只要求人物挂在绳子/停在录制点计时。旧的铃铛、枫叶、
    # 便捷杂货店入口均不再执行，也不需要在结束时按 Esc 关闭界面。
    state.recorded_rest_member_panel_opened = False
    state.recorded_rest_shop_open_failures = 0
    state.recorded_rest_phase = "resting"
    state.rope_rest_remaining_seconds = remaining_seconds
    state.rope_rest_until = now + remaining_seconds
    _notify_rope_rest_schedule(runtime, state, "resting", remaining_seconds)
    runtime.trace_event(
        "recorded_route_rest_point_started",
        rest_count=state.rope_rest_count + 1,
        position=position,
        rest_point=[rest_point.x, rest_point.y],
        approach_y=rest_point.approach_y,
        jump_attempts=state.recorded_rest_jump_attempts,
        remaining_seconds=round(remaining_seconds, 2),
        test_mode=state.rope_rest_test_active,
        rest_mode=(
            "recorded_rope_point"
            if state.recorded_rest_on_rope
            else "recorded_platform_point"
        ),
        action="hold_position_for_configured_duration",
        interface_actions_skipped=True,
    )
    # 只在已经确认落在录制的 X/Y 范围、真正开始停留时通知页面。靠近、跳跃
    # 以及跳跃重试都不会触发，避免 UI 将失败尝试误显示为一次休息。
    callback = getattr(runtime, "recorded_rest_arrival_callback", None)
    if callable(callback):
        try:
            callback(
                {
                    "rest_count": state.rope_rest_count + 1,
                    "position": tuple(position),
                    "rest_point": (rest_point.x, rest_point.y),
                    "approach_y": rest_point.approach_y,
                    "remaining_seconds": round(remaining_seconds, 2),
                    "test_mode": state.rope_rest_test_active,
                    "rest_mode": (
                        "recorded_rope_point"
                        if state.recorded_rest_on_rope
                        else "recorded_platform_point"
                    ),
                }
            )
        except Exception as exc:
            # 页面截图失败不应影响已经抵达的休息流程。
            runtime.trace_event(
                "recorded_route_rest_point_ui_snapshot_failed",
                error="{}: {}".format(type(exc).__name__, exc),
            )
    if state.rope_rest_test_active:
        return _park_completed_rest_point_test(
            runtime,
            state,
            position,
            reason="recorded_rest_position_test_completed",
        )
    return True


def _press_recorded_rest_exit_escape(runtime) -> Tuple[bool, Optional[str]]:
    """休息结束时轻按一次Esc；失败只返回诊断，不中断后续路线。"""
    try:
        runtime.pydirectinput.keyDown("esc")
        try:
            runtime.可中断等待(0.05, interval=0.01)
        finally:
            runtime.pydirectinput.keyUp("esc")
        runtime.可中断等待(
            RECORDED_REST_OPEN_BUTTON_RESPONSE_SECONDS,
            interval=0.01,
        )
        return True, None
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)


def _park_completed_rest_point_test(runtime, state, position, reason) -> bool:
    """测试成功后保持人物停在当前休息位置，不执行任何界面按键。"""
    state.rest_point_test_parked = True
    state.rope_rest_pending = False
    state.rope_resting = False
    state.rope_rest_until = 0.0
    state.rope_rest_remaining_seconds = 0.0
    state.recorded_rest_phase = "test_parked"
    state.recorded_rest_member_panel_opened = False
    state.recorded_rest_shop_open_failures = 0
    _release_rope_rest_keys(runtime, state)
    _notify_rope_rest_schedule(runtime, state, "test_parked", 0.0)
    runtime.trace_event(
        "recorded_route_rest_point_test_parked",
        position=position,
        reason=reason,
        action="hold_current_position_without_interface_actions",
        interface_actions_skipped=True,
    )
    return True


def _rest_point_test_park_is_valid(state, position) -> bool:
    """测试显示成功后仍校验人物没有被怪物从休息位置撞开。"""
    if not state.rest_point_test_parked:
        return False
    rest_point = state.recorded_rest_point
    if state.recorded_rest_point_enabled and rest_point is not None:
        return (
            abs(int(position[0]) - int(rest_point.x))
            <= RECORDED_REST_POINT_LANDING_X_TOLERANCE
            and abs(int(position[1]) - int(rest_point.y))
            <= RECORDED_REST_POINT_LANDING_Y_TOLERANCE
        )
    if state.active_rope_x is None or state.rope_rest_target_y is None:
        return False
    return (
        abs(int(position[0]) - int(state.active_rope_x))
        <= ROPE_REST_HORIZONTAL_TOLERANCE_X
        and abs(int(position[1]) - int(state.rope_rest_target_y))
        <= ROPE_REST_FALL_TOLERANCE_Y
    )


def _rearm_recorded_rest_navigation(
    runtime,
    state,
    position,
    reason,
    remaining_seconds=None,
) -> bool:
    """休息进入或停留被打断后，保留本轮任务并从真实位置重新规划。"""
    if remaining_seconds is not None:
        state.rope_rest_remaining_seconds = max(
            0.0,
            float(remaining_seconds),
        )
    state.rope_rest_pending = True
    state.rope_resting = False
    state.rope_rest_until = 0.0
    state.rope_rest_target_y = None
    state.recorded_rest_phase = (
        "approach" if state.recorded_rest_point_enabled else None
    )
    state.recorded_rest_jump_at = 0.0
    failed_attempts = state.recorded_rest_jump_attempts
    state.recorded_rest_jump_attempts = 0
    state.recorded_rest_recovery_count += 1
    state.recorded_rest_landing_candidate_at = 0.0
    state.recorded_rest_landing_candidate_frames = 0
    state.rest_point_test_parked = False

    _release_rope_rest_keys(runtime, state)
    _clear_active_rope(state)
    _clear_rope_entry_runup(state)
    _clear_rope_top_exit(state)
    _clear_connection_priority(state)
    _reset_platform_replan(state)
    _reset_platform_stall_tracking(state)
    state.route_index = None
    state.last_relocalized_index = None
    state.last_relocalized_at = 0.0
    state.last_jump_index = None
    state.last_jump_at = 0.0
    state.platform_coverage_target_x = None
    state.platform_coverage_route_index = None
    state.platform_direction = None
    state.smart_seek_target_active = False
    state.smart_seek_scan_until = 0.0
    state.smart_seek_mode = None
    _clear_smart_seek_target_loss(state)
    state.route_resume_grace_until = 0.0
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    _maintain_rest_navigation_control(runtime, state)

    request_relocation = getattr(runtime, "请求人物重新定位", None)
    if callable(request_relocation):
        request_relocation(reason="recorded_rest_recovery")
    else:
        relocation_event = getattr(runtime, "人物全图重定位事件", None)
        if relocation_event is not None:
            relocation_event.set()
    _notify_rope_rest_schedule(
        runtime,
        state,
        "returning",
        state.rope_rest_remaining_seconds,
    )
    runtime.trace_event(
        "recorded_route_rest_navigation_rearmed",
        rest_count=state.rope_rest_count + 1,
        position=position,
        reason=reason,
        failed_attempts=failed_attempts,
        recovery_count=state.recorded_rest_recovery_count,
        remaining_seconds=round(state.rope_rest_remaining_seconds, 2),
        test_mode=state.rope_rest_test_active,
        action="full_relocation_and_retry_same_rest_cycle",
    )
    return True


def _abort_recorded_rest_cycle(runtime, state, position, reason) -> bool:
    """多次无法进入休息点时结束本轮尝试，并按正常周期安排下一次。"""
    interval_seconds, _duration_seconds = _rope_rest_settings(runtime, state)
    was_test = state.rope_rest_test_active
    next_rest_seconds = (
        state.rope_rest_test_resume_seconds
        if was_test
        else interval_seconds
    )
    state.rope_rest_pending = False
    state.rope_rest_until = 0.0
    state.rope_rest_remaining_seconds = 0.0
    state.recorded_rest_phase = None
    state.recorded_rest_jump_at = 0.0
    attempts = state.recorded_rest_jump_attempts
    state.recorded_rest_jump_attempts = 0
    state.recorded_rest_recovery_count = 0
    state.recorded_rest_landing_candidate_at = 0.0
    state.recorded_rest_landing_candidate_frames = 0
    state.recorded_rest_member_panel_opened = False
    state.recorded_rest_shop_open_failures = 0
    state.rest_point_test_parked = False
    if was_test:
        _leave_rest_point_test_control(
            runtime,
            state,
            reason="rest_point_test_aborted",
        )
    state.rope_rest_test_active = False
    state.rope_rest_test_resume_seconds = 0.0
    state.next_rope_rest_at = time.monotonic() + next_rest_seconds
    _notify_rope_rest_schedule(runtime, state, "scheduled", next_rest_seconds)
    runtime.trace_event(
        "recorded_route_rest_point_aborted",
        rest_count=state.rope_rest_count + 1,
        position=position,
        jump_attempts=attempts,
        reason=reason,
        next_rest_in_seconds=round(next_rest_seconds, 2),
        test_mode=was_test,
    )
    return True


def _apply_recorded_rest_point(runtime, state, rest_point, position) -> bool:
    """到期后对齐休息点X、跳跃进入该点，并在记录的X/Y处完成休息。"""
    if rest_point is None:
        return False
    phase = state.recorded_rest_phase
    if not state.rope_rest_pending and phase is None:
        return False

    now = time.monotonic()
    current_x = int(position[0])
    current_y = int(position[1])
    # 录制休息点的 Y 可能与起跳平台非常接近，不能只凭宽松坐标判断。
    # 必须已经实际发送过一次跳跃，并且落点坐标进入严格范围后才开始休息。
    inside_rest_point = (
        state.recorded_rest_jump_attempts > 0
        and
        abs(current_x - int(rest_point.x))
        <= RECORDED_REST_POINT_LANDING_X_TOLERANCE
        and abs(current_y - int(rest_point.y))
        <= RECORDED_REST_POINT_LANDING_Y_TOLERANCE
    )
    if inside_rest_point:
        if state.recorded_rest_landing_candidate_at <= 0:
            state.recorded_rest_landing_candidate_at = now
            state.recorded_rest_landing_candidate_frames = 1
        else:
            state.recorded_rest_landing_candidate_frames += 1
    else:
        state.recorded_rest_landing_candidate_at = 0.0
        state.recorded_rest_landing_candidate_frames = 0
    at_rest_point = (
        inside_rest_point
        and state.recorded_rest_landing_candidate_frames
        >= RECORDED_REST_POINT_LANDING_STABLE_FRAMES
        and now - state.recorded_rest_landing_candidate_at
        >= RECORDED_REST_POINT_LANDING_STABLE_SECONDS
    )

    if phase == "resting":
        _release_rope_rest_keys(runtime, state)
        remaining_seconds = max(0.0, state.rope_rest_until - now)
        state.rope_rest_remaining_seconds = remaining_seconds
        if not at_rest_point:
            runtime.trace_event(
                "recorded_route_rest_point_interrupted",
                rest_count=state.rope_rest_count + 1,
                position=position,
                rest_point=[rest_point.x, rest_point.y],
                remaining_seconds=round(remaining_seconds, 2),
                reason="left_recorded_rest_point",
            )
            return _rearm_recorded_rest_navigation(
                runtime,
                state,
                position,
                reason="left_recorded_rest_point",
                remaining_seconds=remaining_seconds,
            )
        if remaining_seconds > 0:
            return True
        now = time.monotonic()
        interval_seconds, _duration_seconds = _rope_rest_settings(runtime, state)
        was_test = state.rope_rest_test_active
        next_rest_seconds = (
            state.rope_rest_test_resume_seconds
            if was_test
            else interval_seconds
        )
        state.rope_rest_pending = False
        state.rope_rest_until = 0.0
        state.rope_rest_remaining_seconds = 0.0
        state.recorded_rest_phase = None
        state.recorded_rest_jump_at = 0.0
        state.recorded_rest_jump_attempts = 0
        state.recorded_rest_recovery_count = 0
        state.recorded_rest_landing_candidate_at = 0.0
        state.recorded_rest_landing_candidate_frames = 0
        state.recorded_rest_member_panel_opened = False
        state.recorded_rest_shop_open_failures = 0
        if was_test:
            _leave_rest_point_test_control(
                runtime,
                state,
                reason="rest_point_test_completed",
            )
        state.rope_rest_test_active = False
        state.rope_rest_test_resume_seconds = 0.0
        if not was_test:
            state.rope_rest_count += 1
        state.next_rope_rest_at = now + next_rest_seconds
        _notify_rope_rest_schedule(runtime, state, "scheduled", next_rest_seconds)
        state.route_index = None
        state.last_relocalized_index = None
        runtime.人物全图重定位事件.set()
        runtime.trace_event(
            "recorded_route_rest_point_completed",
            rest_count=state.rope_rest_count,
            position=position,
            rest_point=[rest_point.x, rest_point.y],
            next_rest_in_seconds=round(next_rest_seconds, 2),
            test_mode=was_test,
            action="resume_route_with_full_relocation",
            interface_actions_skipped=True,
        )
        return True

    if at_rest_point:
        return _begin_recorded_rest(runtime, state, rest_point, position, now)

    if phase == "jumping":
        _release_rope_rest_keys(runtime, state)
        if now - state.recorded_rest_jump_at < RECORDED_REST_POINT_JUMP_TIMEOUT_SECONDS:
            return True
        if state.recorded_rest_jump_attempts >= RECORDED_REST_POINT_MAX_JUMP_ATTEMPTS:
            return _rearm_recorded_rest_navigation(
                runtime,
                state,
                position,
                reason="jump_landing_timeout",
                remaining_seconds=state.rope_rest_remaining_seconds,
            )
        state.recorded_rest_phase = "approach"
        state.recorded_rest_jump_at = 0.0
        runtime.trace_event(
            "recorded_route_rest_point_jump_retry",
            position=position,
            rest_point=[rest_point.x, rest_point.y],
            next_attempt=state.recorded_rest_jump_attempts + 1,
        )
        return True

    state.recorded_rest_phase = "approach"
    if (
        rest_point.approach_y is not None
        and abs(current_y - int(rest_point.approach_y))
        > ROUTE_PLATFORM_MATCH_TOLERANCE_Y
    ):
        # 尚未走到休息点下方的平台时继续执行原JSON；到达对应平台后再接管X对齐。
        return False
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="recorded_rest_point_approach",
    )
    _apply_vertical(runtime, state, "none")
    if current_x < int(rest_point.x) - RECORDED_REST_POINT_X_TOLERANCE:
        _apply_horizontal(runtime, state, "right")
        return True
    if current_x > int(rest_point.x) + RECORDED_REST_POINT_X_TOLERANCE:
        _apply_horizontal(runtime, state, "left")
        return True

    _apply_horizontal(runtime, state, "none")
    jump_key = rest_point.jump_key or "c"
    runtime.pydirectinput.keyDown(jump_key)
    completed = runtime.可中断等待(ROUTE_JUMP_HOLD_SECONDS, interval=0.01)
    runtime.pydirectinput.keyUp(jump_key)
    if not completed:
        return True
    state.recorded_rest_phase = "jumping"
    state.recorded_rest_jump_at = time.monotonic()
    state.recorded_rest_jump_attempts += 1
    runtime.trace_event(
        "recorded_route_rest_point_jump",
        rest_count=state.rope_rest_count + 1,
        position=position,
        rest_point=[rest_point.x, rest_point.y],
        approach_y=rest_point.approach_y,
        jump_key=jump_key,
        jump_attempt=state.recorded_rest_jump_attempts,
    )
    return True


def _start_rope_rest(runtime, state, position) -> bool:
    """在目标绳段爬到录制Y；无录制点时回退到当前绳子的动态中点。"""
    if not state.rope_rest_pending or state.rope_resting:
        return False
    if (
        state.active_rope_x is None
        or state.active_rope_top_y is None
        or state.active_rope_bottom_y is None
    ):
        return False
    rest_point = state.recorded_rest_point
    if state.recorded_rest_point_enabled:
        if not state.recorded_rest_on_rope or rest_point is None:
            return False
        if (
            state.active_rope_x is None
            or state.active_rope_top_y is None
            or state.active_rope_bottom_y is None
            or state.recorded_rest_rope_x is None
            or state.recorded_rest_rope_top_y is None
            or state.recorded_rest_rope_bottom_y is None
            or abs(
                int(state.active_rope_x)
                - int(state.recorded_rest_rope_x)
            ) > 2
            or int(rest_point.y) < int(state.active_rope_top_y) + 2
            or int(rest_point.y) > int(state.active_rope_bottom_y) - 2
        ):
            return False
        target_y = int(rest_point.y)
        rest_mode = "recorded_rope_point"
    else:
        if not state.rope_rest_fallback_enabled:
            return False
        target_y = None
        if (
            rest_point is not None
            and state.recorded_rest_rope_x is not None
            and state.recorded_rest_rope_top_y is not None
            and state.recorded_rest_rope_bottom_y is not None
        ):
            if (
                state.active_rope_x is None
                or state.active_rope_top_y is None
                or state.active_rope_bottom_y is None
                or abs(
                    int(state.active_rope_x)
                    - int(state.recorded_rest_rope_x)
                ) > 2
            ):
                return False
            active_top_y = min(
                int(state.active_rope_top_y),
                int(state.active_rope_bottom_y),
            )
            active_bottom_y = max(
                int(state.active_rope_top_y),
                int(state.active_rope_bottom_y),
            )
            if not active_top_y <= int(rest_point.y) <= active_bottom_y:
                return False
            target_y = int(rest_point.y)
        if target_y is None:
            target_y = _active_rope_middle_y(state)
        if target_y is None:
            return False
        rest_mode = "rope_middle"
    if not state.active_rope_confirmed:
        # 起跳重新挂绳时，必须先让活动绳逻辑根据向上Y进度完成真实接触确认。
        # 不能因为人物已经靠近休息目标Y就提前按下/释放上下键，否则会截断
        # contact_start_y -> upward_progress 的确认流程，长期卡在目标附近往返。
        return False
    current_y = int(position[1])
    if current_y > target_y + ROPE_REST_SETTLE_TOLERANCE_Y:
        if state.recorded_rest_landing_candidate_at > 0:
            runtime.trace_event(
                "recorded_route_rope_rest_settle_reset",
                position=position,
                target_y=target_y,
                reason="below_settle_band",
                candidate_frames=state.recorded_rest_landing_candidate_frames,
            )
        state.recorded_rest_landing_candidate_at = 0.0
        state.recorded_rest_landing_candidate_frames = 0
        rope_x = int(state.active_rope_x)
        if int(position[0]) < rope_x - 1:
            horizontal = "right"
        elif int(position[0]) > rope_x + 1:
            horizontal = "left"
        else:
            horizontal = "none"
        _apply_horizontal(runtime, state, horizontal)
        _apply_vertical(runtime, state, "up")
        return True
    if current_y < target_y - ROPE_REST_SETTLE_TOLERANCE_Y:
        if state.recorded_rest_landing_candidate_at > 0:
            runtime.trace_event(
                "recorded_route_rope_rest_settle_reset",
                position=position,
                target_y=target_y,
                reason="above_settle_band",
                candidate_frames=state.recorded_rest_landing_candidate_frames,
            )
        state.recorded_rest_landing_candidate_at = 0.0
        state.recorded_rest_landing_candidate_frames = 0
        # 若人物已经越过目标Y，先沿绳子向下回到录制点或动态中点再休息。
        rope_x = int(state.active_rope_x)
        if int(position[0]) < rope_x - 1:
            horizontal = "right"
        elif int(position[0]) > rope_x + 1:
            horizontal = "left"
        else:
            horizontal = "none"
        _apply_horizontal(runtime, state, horizontal)
        _apply_vertical(runtime, state, "down")
        return True
    rope_landing_confirmed = (
        state.active_rope_confirmed
        and abs(int(position[0]) - int(state.active_rope_x))
        <= ROPE_REST_HORIZONTAL_TOLERANCE_X
    )
    if not rope_landing_confirmed:
        state.recorded_rest_landing_candidate_at = 0.0
        state.recorded_rest_landing_candidate_frames = 0
        return False
    now = time.monotonic()
    if state.recorded_rest_landing_candidate_at <= 0:
        state.recorded_rest_landing_candidate_at = now
        state.recorded_rest_landing_candidate_frames = 1
        runtime.trace_event(
            "recorded_route_rope_rest_settle_started",
            position=position,
            target_y=target_y,
            tolerance_y=ROPE_REST_SETTLE_TOLERANCE_Y,
            test_mode=state.rope_rest_test_active,
            action="release_vertical_and_confirm_stable_rope_position",
        )
    else:
        state.recorded_rest_landing_candidate_frames += 1
    _release_rope_rest_keys(runtime, state)
    if (
        state.recorded_rest_landing_candidate_frames
        < RECORDED_REST_POINT_LANDING_STABLE_FRAMES
        or now - state.recorded_rest_landing_candidate_at
        < RECORDED_REST_POINT_LANDING_STABLE_SECONDS
    ):
        return True
    if rest_mode == "recorded_rope_point":
        _begin_recorded_rest(
            runtime,
            state,
            rest_point,
            position,
            now,
        )
        if state.rest_point_test_parked:
            return True
        remaining_seconds = state.rope_rest_remaining_seconds
    else:
        _interval_seconds, duration_seconds = _rope_rest_settings(runtime, state)
        remaining_seconds = state.rope_rest_remaining_seconds
        if state.rope_rest_test_active:
            remaining_seconds = 0.0
        elif remaining_seconds <= 0:
            remaining_seconds = duration_seconds
        state.rope_rest_remaining_seconds = remaining_seconds
        state.rope_rest_until = now + remaining_seconds
        _notify_rope_rest_schedule(runtime, state, "resting", remaining_seconds)
    state.rope_resting = True
    state.rope_rest_target_y = target_y
    state.recorded_rest_landing_candidate_at = 0.0
    state.recorded_rest_landing_candidate_frames = 0
    state.active_rope_best_y = int(position[1])
    state.active_rope_progress_at = time.monotonic()
    runtime.trace_event(
        "recorded_route_rope_rest_started",
        rest_count=state.rope_rest_count + 1,
        position=position,
        rope_x=state.active_rope_x,
        rope_top_y=state.active_rope_top_y,
        rope_bottom_y=state.active_rope_bottom_y,
        target_y=target_y,
        rest_mode=rest_mode,
        remaining_seconds=round(remaining_seconds, 2),
    )
    return True


def _apply_rope_rest(runtime, state, position) -> bool:
    """保持绳中休息；被打落时保留剩余时长，休息结束后自动继续爬绳。"""
    if not state.rope_resting:
        return False
    _release_rope_rest_keys(runtime, state)
    now = time.monotonic()
    recorded_rope_point_rest = (
        state.recorded_rest_point_enabled
        and state.recorded_rest_on_rope
    )
    # 正常休息不计入“持续按上无进展”的卡绳计时。
    state.active_rope_best_y = int(position[1])
    state.active_rope_progress_at = now
    remaining_seconds = max(0.0, state.rope_rest_until - now)
    state.rope_rest_remaining_seconds = remaining_seconds
    target_y = state.rope_rest_target_y
    rope_x = state.active_rope_x
    fell_from_rope = (
        target_y is not None
        and int(position[1]) > int(target_y) + ROPE_REST_FALL_TOLERANCE_Y
    )
    moved_from_rope = (
        rope_x is not None
        and abs(int(position[0]) - int(rope_x))
        > ROPE_REST_HORIZONTAL_TOLERANCE_X
    )
    if fell_from_rope or moved_from_rope:
        runtime.trace_event(
            "recorded_route_rope_rest_interrupted",
            rest_count=state.rope_rest_count + 1,
            position=position,
            remaining_seconds=round(remaining_seconds, 2),
            reason="knocked_down" if fell_from_rope else "moved_away_from_rope",
        )
        return _rearm_recorded_rest_navigation(
            runtime,
            state,
            position,
            reason=(
                "rope_rest_knocked_down"
                if fell_from_rope
                else "rope_rest_moved_away"
            ),
            remaining_seconds=remaining_seconds,
        )
    if remaining_seconds > 0:
        return True
    if state.rope_rest_test_active:
        return _park_completed_rest_point_test(
            runtime,
            state,
            position,
            reason="rope_middle_rest_test_completed",
        )
    interval_seconds, _duration_seconds = _rope_rest_settings(runtime, state)
    was_test = state.rope_rest_test_active
    next_rest_seconds = (
        state.rope_rest_test_resume_seconds
        if was_test
        else interval_seconds
    )
    state.rope_rest_pending = False
    state.rope_resting = False
    state.rope_rest_until = 0.0
    state.rope_rest_remaining_seconds = 0.0
    state.rope_rest_target_y = None
    if recorded_rope_point_rest:
        state.recorded_rest_phase = None
        state.recorded_rest_jump_at = 0.0
        state.recorded_rest_jump_attempts = 0
        state.recorded_rest_recovery_count = 0
    if was_test:
        _leave_rest_point_test_control(
            runtime,
            state,
            reason="rope_rest_point_test_completed",
        )
    if not was_test:
        state.rope_rest_count += 1
    state.rope_rest_test_active = False
    state.rope_rest_test_resume_seconds = 0.0
    state.next_rope_rest_at = now + next_rest_seconds
    _notify_rope_rest_schedule(runtime, state, "scheduled", next_rest_seconds)
    runtime.trace_event(
        "recorded_route_rope_rest_completed",
        rest_count=state.rope_rest_count,
        position=position,
        next_rest_in_seconds=round(next_rest_seconds, 2),
        test_mode=was_test,
        rest_mode=(
            "recorded_rope_point"
            if recorded_rope_point_rest
            else "rope_middle"
        ),
        action="resume_rope_climb",
        interface_actions_skipped=True,
    )
    # 完成后立即恢复上键并跳过本帧战斗判断，避免休息结束仍停在绳子中间。
    _apply_vertical(runtime, state, "up")
    return True


def _apply_active_rope(runtime, state, position) -> bool:
    """挂绳后按JSON绳顶坐标持续向上，被打断掉落时自动重新对齐。"""
    if (
        state.active_rope_direction not in ("left", "right")
        or state.active_rope_x is None
        or state.active_rope_top_y is None
        or state.active_rope_bottom_y is None
    ):
        return False
    rope_x = int(state.active_rope_x)
    top_y = int(state.active_rope_top_y)
    bottom_y = int(state.active_rope_bottom_y)
    if _apply_rope_rest(runtime, state, position):
        return True
    if _start_rope_rest(runtime, state, position):
        return True
    position_x = int(position[0])
    position_y = int(position[1])
    if state.active_rope_contacted and not state.active_rope_confirmed:
        contact_start_y = (
            int(state.active_rope_contact_start_y)
            if state.active_rope_contact_start_y is not None
            else bottom_y
        )
        if (
            abs(position_x - rope_x) <= ROUTE_ROPE_BODY_TOLERANCE_X
            and contact_start_y - position_y
            >= ROUTE_ROPE_ENTRY_CONFIRM_PROGRESS_Y
        ):
            if (
                state.active_rope_best_y is None
                or position_y < int(state.active_rope_best_y)
            ):
                state.active_rope_best_y = position_y
                state.active_rope_progress_at = time.monotonic()
            elif state.active_rope_progress_at <= 0:
                state.active_rope_progress_at = time.monotonic()
            _confirm_rope_entry_contact(runtime, state, position)
    if position_y <= top_y + ROPE_TOP_TOLERANCE_Y:
        _mark_active_rope_top_reached(
            runtime,
            state,
            position,
            reason="exact_top_coordinate",
        )
        return True
    top_edge_stable = (
        state.active_rope_confirmed
        and position_y <= top_y + ROUTE_ROPE_TOP_EDGE_TOLERANCE_Y
        and abs(position_x - rope_x) <= ROUTE_ROPE_BODY_TOLERANCE_X
        and state.active_rope_progress_at > 0
        and time.monotonic() - state.active_rope_progress_at
        >= ROUTE_ROPE_TOP_EDGE_CONFIRM_SECONDS
    )
    if top_edge_stable:
        _mark_active_rope_top_reached(
            runtime,
            state,
            position,
            reason="stable_one_pixel_top_edge",
        )
        return True
    elapsed = time.monotonic() - state.active_rope_started_at
    reached_rope_band = (
        int(position[1]) <= bottom_y
        or (
            abs(int(position[0]) - rope_x) <= 1
            and int(position[1]) <= bottom_y + 1
        )
    )
    if reached_rope_band and not state.active_rope_contacted:
        state.active_rope_contacted = True
        state.active_rope_contacted_at = time.monotonic()
        state.active_rope_contact_start_y = position_y
        # Up was already held during the airborne jump. Some game frames do not
        # treat that as a fresh latch command when the character actually meets
        # the rope, so send one short release->press transition at contact.
        runtime.pydirectinput.keyUp("up")
        state.vertical_direction = None
        completed = runtime.可中断等待(
            ROUTE_ROPE_CONTACT_UP_REPRESS_RELEASE_SECONDS,
            interval=0.004,
        )
        if completed:
            runtime.pydirectinput.keyDown("up")
            state.vertical_direction = "up"
            state.active_rope_contact_up_repressed = True
            state.active_rope_last_up_key_at = time.monotonic()
        runtime.trace_event(
            "recorded_route_rope_contact_latched",
            position=position,
            rope_x=rope_x,
            rope_bottom_y=bottom_y,
            entry_direction=state.active_rope_direction,
            elapsed_ms=round(elapsed * 1000.0, 2),
            confirmed=False,
            up_repressed=state.active_rope_contact_up_repressed,
            up_release_ms=round(
                ROUTE_ROPE_CONTACT_UP_REPRESS_RELEASE_SECONDS * 1000.0,
                1,
            ),
            up_hold_mode="hold_until_confirmed_or_entry_failure",
            action="tentative_contact_keep_centering_until_y_progress",
        )
        if not completed:
            return True
    if state.active_rope_contacted and not state.active_rope_confirmed:
        contact_start_y = (
            int(state.active_rope_contact_start_y)
            if state.active_rope_contact_start_y is not None
            else bottom_y
        )
        upward_progress = contact_start_y - position_y
        if (
            abs(position_x - rope_x) <= ROUTE_ROPE_BODY_TOLERANCE_X
            and upward_progress >= ROUTE_ROPE_ENTRY_CONFIRM_PROGRESS_Y
        ):
            if (
                state.active_rope_best_y is None
                or position_y < int(state.active_rope_best_y)
            ):
                state.active_rope_best_y = position_y
                state.active_rope_progress_at = time.monotonic()
            elif state.active_rope_progress_at <= 0:
                state.active_rope_progress_at = time.monotonic()
            _confirm_rope_entry_contact(runtime, state, position)
    commit_seconds = _rope_entry_commit_seconds(state)
    tentative_fell = (
        state.active_rope_contacted
        and not state.active_rope_confirmed
        and time.monotonic() - state.active_rope_contacted_at
        >= ROUTE_ROPE_ENTRY_EARLY_FALL_SECONDS
        and position_y > bottom_y + 1
    )
    entry_timed_out = (
        elapsed > commit_seconds
        and position_y > bottom_y
        and not state.active_rope_confirmed
    )
    confirmed_fell = state.active_rope_confirmed and position_y > bottom_y + 2
    if tentative_fell or entry_timed_out or confirmed_fell:
        if tentative_fell:
            failure_reason = "tentative_contact_fell"
        elif confirmed_fell:
            failure_reason = "confirmed_contact_interrupted"
        else:
            failure_reason = "entry_commit_timeout"
        failed_jump_index = state.last_jump_index
        adaptive = _learn_rope_entry_failure(
            runtime,
            state,
            position,
            reason=failure_reason,
        )
        state.last_jump_index = None
        state.last_jump_at = 0.0
        _clear_active_rope(state)
        runtime.trace_event(
            "recorded_route_rope_retry_rearmed",
            position=position,
            failed_jump_index=failed_jump_index,
            rope_x=rope_x,
            rope_top_y=top_y,
            rope_bottom_y=bottom_y,
            reason=failure_reason,
            adaptive_failure_count=adaptive["failure_count"],
            next_offset_x=adaptive["next_offset_x"],
            next_commit_ms=round(adaptive["commit_seconds"] * 1000.0, 1),
        )
        return False
    if state.active_rope_confirmed:
        if _recover_stalled_rope_climb(runtime, state, position):
            return True
        if int(position[0]) < rope_x - ROUTE_ROPE_BODY_TOLERANCE_X:
            horizontal = "right"
        elif int(position[0]) > rope_x + ROUTE_ROPE_BODY_TOLERANCE_X:
            horizontal = "left"
        else:
            horizontal = "none"
    elif state.active_rope_contacted:
        # A one-frame rope-band hit is not a real latch. Keep nudging exactly
        # toward rope X until upward Y progress confirms that the character is climbing.
        if position_x < rope_x:
            horizontal = "right"
        elif position_x > rope_x:
            horizontal = "left"
        else:
            horizontal = "none"
    else:
        # 方向+C+上只作为一次短脉冲；起跳后先依靠惯性滑向绳子，避免持续
        # 按住方向直接从绳子一侧穿到另一侧。惯性窗口结束仍未靠近时才轻推。
        if elapsed < ROUTE_ROPE_ENTRY_COAST_SECONDS:
            horizontal = "none"
        elif int(position[0]) < rope_x - ROUTE_ROPE_BODY_TOLERANCE_X:
            horizontal = "right"
        elif int(position[0]) > rope_x + ROUTE_ROPE_BODY_TOLERANCE_X:
            horizontal = "left"
        else:
            horizontal = "none"
    _apply_horizontal(runtime, state, horizontal)
    _keep_rope_up_pressed(runtime, state)
    return True


def _apply_platform_patrol(runtime, state, variant, position) -> None:
    """在单个平台录制范围的左右边界之间持续往返刷怪。"""
    minimum_x = int(variant.platform_min_x)
    maximum_x = int(variant.platform_max_x)
    previous_direction = state.platform_direction
    if int(position[0]) <= minimum_x + 2:
        state.platform_direction = "right"
    elif int(position[0]) >= maximum_x - 2:
        state.platform_direction = "left"
    elif state.platform_direction not in ("left", "right"):
        distance_to_left = abs(int(position[0]) - minimum_x)
        distance_to_right = abs(maximum_x - int(position[0]))
        if distance_to_left < distance_to_right:
            state.platform_direction = "right"
        elif distance_to_right < distance_to_left:
            state.platform_direction = "left"
        else:
            recorded_direction = variant.points[0].horizontal
            state.platform_direction = (
                recorded_direction
                if recorded_direction in ("left", "right")
                else "left"
            )
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(runtime, state.combat, reason="platform_patrol")
    _apply_vertical(runtime, state, "none")
    _apply_horizontal(runtime, state, state.platform_direction)
    if previous_direction != state.platform_direction:
        runtime.trace_event(
            "recorded_route_platform_turn",
            position=position,
            previous_direction=previous_direction,
            direction=state.platform_direction,
            platform_min_x=minimum_x,
            platform_max_x=maximum_x,
        )


def _schedule_next_ai_action(state) -> None:
    """为下一次平台随机动作生成低频执行时间。"""
    state.next_ai_action_at = time.monotonic() + random.uniform(
        ROUTE_AI_MIN_INTERVAL_SECONDS,
        ROUTE_AI_MAX_INTERVAL_SECONDS,
    )


def _maybe_apply_platform_ai_action(
    runtime,
    state,
    variant,
    position,
    selected_index: Optional[int] = None,
) -> bool:
    """在平台安全区低频暂停或轻跳，随后恢复录制方向。

    单平台巡逻和多平台JSON回放都可使用，但多平台只在普通平台段中部执行。
    绳子、下跳、跨平台连接、返程和平台覆盖阶段保持完全确定性。
    """
    now = time.monotonic()
    if now < state.next_ai_action_at:
        return False
    if runtime.zant != 0 or runtime.攻击移动仍锁定():
        return False
    monster_snapshot = combat_logic.read_monster_snapshot(runtime)
    if (
        monster_snapshot.attackable_count > 0
        or monster_snapshot.chase_count > 0
        or state.combat.combat_active
        or state.combat.chase_active
        or state.combat.locked_attack_direction in ("left", "right")
        or state.combat.route_resume_pending_at > 0
        or state.smart_seek_target_active
        or state.smart_seek_mode in ("target", "scan")
    ):
        # 随机动作只能在最新检测明确无怪、且战斗/追怪状态完全退出后执行。
        # 不重排路线必需的跨平台、下跳和上绳动作。
        return False
    if (
        state.active_rope_direction is not None
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
        or state.connection_priority_active
        or state.platform_replan_active
        or state.active_ignored_platform_number is not None
        or state.platform_coverage_target_x is not None
        or state.rope_entry_runup_index is not None
        or _rest_route_navigation_active(state)
    ):
        return False
    point = None
    current_platform = None
    if selected_index is not None:
        if not 0 <= int(selected_index) < len(variant.points):
            _schedule_next_ai_action(state)
            return False
        point = variant.points[int(selected_index)]
        if point.segment_type != "platform" or point.action == "jump":
            return False
        platform_index = _current_platform_range_index(
            variant,
            position,
            preferred_route_index=selected_index,
        )
        if platform_index is None:
            return False
        current_platform = variant.platform_ranges[platform_index]
        minimum_x = int(current_platform.minimum_x)
        maximum_x = int(current_platform.maximum_x)
        original_direction = (
            point.horizontal
            if point.horizontal in ("left", "right")
            else state.route_command_direction
        )
        # 平台最后几个点通常马上连接绳子/下跳；此处不插入随机动作。
        if int(selected_index) >= int(current_platform.end_index) - 4:
            return False
    else:
        minimum_x = int(variant.platform_min_x)
        maximum_x = int(variant.platform_max_x)
        original_direction = state.platform_direction
    if original_direction not in ("left", "right"):
        _schedule_next_ai_action(state)
        return False
    if not (
        minimum_x + ROUTE_AI_BOUNDARY_MARGIN_X
        < int(position[0])
        < maximum_x - ROUTE_AI_BOUNDARY_MARGIN_X
    ):
        return False

    action = random.choices(
        ("pause", "jump"),
        weights=(0.60, 0.40),
        k=1,
    )[0]
    state.ai_action_count += 1
    if action == "pause":
        duration = random.uniform(
            ROUTE_AI_PAUSE_MIN_SECONDS,
            ROUTE_AI_PAUSE_MAX_SECONDS,
        )
        _apply_horizontal(runtime, state, "none")
        completed = runtime.可中断等待(duration, interval=0.01)
        if completed and not runtime.已请求停止() and runtime.zant == 0:
            _apply_horizontal(runtime, state, original_direction)
        runtime.trace_event(
            "recorded_route_ai_action",
            action="pause",
            position=position,
            patrol_direction=original_direction,
            duration_ms=round(duration * 1000, 1),
            action_count=state.ai_action_count,
            platform_number=(
                current_platform.platform_number
                if current_platform is not None
                else None
            ),
            completed=completed,
        )
        _schedule_next_ai_action(state)
        return True
    if action == "jump":
        # 选中随机跳后再读取一次最新快照，避免怪物恰好在动作选择和按键之间
        # 出现时仍按下跳跃键。
        latest_snapshot = combat_logic.read_monster_snapshot(runtime)
        if (
            runtime.zant != 0
            or runtime.攻击移动仍锁定()
            or latest_snapshot.attackable_count > 0
            or latest_snapshot.chase_count > 0
            or state.combat.combat_active
            or state.combat.chase_active
            or state.combat.locked_attack_direction in ("left", "right")
            or state.combat.route_resume_pending_at > 0
            or state.smart_seek_target_active
            or state.smart_seek_mode in ("target", "scan")
        ):
            return False
        completed = False
        try:
            runtime.pydirectinput.keyDown("c")
            completed = runtime.可中断等待(ROUTE_JUMP_HOLD_SECONDS, interval=0.01)
        finally:
            runtime.pydirectinput.keyUp("c")
        runtime.trace_event(
            "recorded_route_ai_action",
            action="jump",
            position=position,
            patrol_direction=original_direction,
            action_count=state.ai_action_count,
            platform_number=(
                current_platform.platform_number
                if current_platform is not None
                else None
            ),
            completed=completed,
        )
        _schedule_next_ai_action(state)
        return True


def _reset_down_jump_patrol_state(
    state,
    platform_index: Optional[int] = None,
) -> None:
    """开始新一轮平台巡逻，并清除上一次下跳的随机落点。"""
    state.down_jump_patrol_platform_index = platform_index
    state.down_jump_patrol_seen_min_x = False
    state.down_jump_patrol_seen_max_x = False
    state.down_jump_patrol_completed = False
    state.down_jump_patrol_started_traced = False
    state.down_jump_random_index = None
    state.down_jump_random_offset_x = None


def _preceding_platform_range_index(variant, route_index: int) -> Optional[int]:
    """返回指定过渡点前最后一个平台范围。"""
    candidates = [
        (int(platform.end_index), platform_index)
        for platform_index, platform in enumerate(variant.platform_ranges)
        if int(platform.end_index) < int(route_index)
    ]
    if not candidates:
        return None
    return max(candidates)[1]


def _update_down_jump_patrol_progress(runtime, state, variant, position) -> None:
    """记录当前平台左右边界是否都已经被本轮刷图覆盖。"""
    platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=state.route_index,
    )
    if platform_index is None:
        return
    if platform_index != state.down_jump_patrol_platform_index:
        _reset_down_jump_patrol_state(state, platform_index)
    platform = variant.platform_ranges[platform_index]
    current_x = int(position[0])
    if current_x <= int(platform.minimum_x) + ROUTE_DOWN_JUMP_PATROL_EDGE_TOLERANCE_X:
        state.down_jump_patrol_seen_min_x = True
    if current_x >= int(platform.maximum_x) - ROUTE_DOWN_JUMP_PATROL_EDGE_TOLERANCE_X:
        state.down_jump_patrol_seen_max_x = True
    completed = bool(
        state.down_jump_patrol_seen_min_x
        and state.down_jump_patrol_seen_max_x
    )
    if completed and not state.down_jump_patrol_completed:
        state.down_jump_patrol_completed = True
        runtime.trace_event(
            "recorded_route_down_jump_patrol_completed",
            position=position,
            platform_index=platform_index,
            platform_number=platform.platform_number,
            platform_x_range=[platform.minimum_x, platform.maximum_x],
            action="allow_down_jump_as_final_platform_action",
        )


def _down_jump_patrol_ready(state, variant, jump_index: int) -> bool:
    """仅在下跳点所属上方平台已完整巡逻时返回True。"""
    if (
        state.platform_replan_active
        or state.active_ignored_platform_number is not None
        or _rest_route_navigation_active(state)
    ):
        # 被忽略平台、掉层返程和休息点导航必须优先离开，不能为了刷怪
        # 覆盖平台边界而延迟既定连接。
        return True
    source_platform_index = _preceding_platform_range_index(variant, jump_index)
    if source_platform_index is None:
        # 旧JSON没有平台范围时保持兼容，不能让路线永久卡死。
        return True
    return bool(
        state.down_jump_patrol_completed
        and state.down_jump_patrol_platform_index == source_platform_index
    )


def _down_jump_random_target_x(state, jump_index: int, point) -> int:
    """为本次下跳锁定录制点正负5像素内的随机X。"""
    if state.down_jump_random_index != int(jump_index):
        state.down_jump_random_index = int(jump_index)
        state.down_jump_random_offset_x = random.randint(
            -ROUTE_DOWN_JUMP_RANDOM_OFFSET_X,
            ROUTE_DOWN_JUMP_RANDOM_OFFSET_X,
        )
    return int(point.x) + int(state.down_jump_random_offset_x or 0)


def _apply_down_jump_patrol_gate(
    runtime,
    state,
    variant,
    position,
    selected_index: int,
) -> bool:
    """下跳被提前选中时，继续巡逻尚未覆盖的平台边界。"""
    point = variant.points[selected_index]
    if point.segment_type != "down_jump":
        return False
    source_platform_index = _preceding_platform_range_index(variant, selected_index)
    if source_platform_index is None or _down_jump_patrol_ready(
        state,
        variant,
        selected_index,
    ):
        return False
    platform = variant.platform_ranges[source_platform_index]
    if not state.down_jump_patrol_started_traced:
        state.down_jump_patrol_started_traced = True
        runtime.trace_event(
            "recorded_route_down_jump_waiting_for_patrol",
            position=position,
            route_index=selected_index,
            platform_index=source_platform_index,
            platform_number=platform.platform_number,
            seen_min_x=state.down_jump_patrol_seen_min_x,
            seen_max_x=state.down_jump_patrol_seen_max_x,
            action="finish_platform_patrol_before_down_jump",
        )
    if not state.down_jump_patrol_seen_min_x:
        target_x = int(platform.minimum_x)
    elif not state.down_jump_patrol_seen_max_x:
        target_x = int(platform.maximum_x)
    else:
        return False
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="down_jump_wait_platform_patrol",
    )
    _apply_vertical(runtime, state, "none")
    if int(position[0]) < target_x - ROUTE_DOWN_JUMP_PATROL_EDGE_TOLERANCE_X:
        _apply_horizontal(runtime, state, "right")
    elif int(position[0]) > target_x + ROUTE_DOWN_JUMP_PATROL_EDGE_TOLERANCE_X:
        _apply_horizontal(runtime, state, "left")
    else:
        _apply_horizontal(runtime, state, "none")
    return True


def _select_jump_index(state, variant, position, selected_index) -> Optional[int]:
    """在当前点附近向前查找JSON明确记录的下一跳跃动作。"""
    if time.monotonic() - state.last_jump_at < ROUTE_JUMP_COOLDOWN_SECONDS:
        return None
    total = len(variant.points)
    end = selected_index + ROUTE_JUMP_LOOKAHEAD_POINTS
    candidates = []
    for raw_index in range(selected_index, end + 1):
        if not variant.closed_loop and raw_index >= total:
            break
        index = raw_index % total
        point = variant.points[index]
        if point.action != "jump" or index == state.last_jump_index:
            continue
        if point.segment_type == "down_jump":
            if not _down_jump_patrol_ready(state, variant, index):
                continue
            target_position = (
                _down_jump_random_target_x(state, index, point),
                int(point.y),
            )
        else:
            target_position = point.position
        if point.segment_type == "rope_entry" and has_complete_rope_geometry(point):
            rope_x, _top_y, bottom_y = rope_bounds(point)
            target_offset_x = _rope_entry_target_offset(state, point)
            profile = _rope_entry_profile(state, point)
            failure_count = (
                int(profile.consecutive_failures) if profile is not None else 0
            )
            runup_ready = failure_count <= 0 or (
                state.rope_entry_runup_index == index
                and state.rope_entry_runup_ready
                and state.rope_entry_runup_target_offset_x == target_offset_x
            )
            inside_trigger = (
                rope_x is not None
                and runup_ready
                and _rope_entry_jump_direction(
                    position[0],
                    rope_x,
                    target_offset_x=target_offset_x,
                ) is not None
                # 绳子入口是绳段物理最低点（屏幕坐标最大的Y），不是人物站在
                # 平台上的rope_entry录制Y。人物可以从入口下方的平台高度起跳。
                and bottom_y is not None
                and abs(int(position[1]) - int(bottom_y))
                <= ROUTE_ROPE_ENTRY_TRIGGER_Y
            )
        else:
            inside_trigger = (
                _distance(position, target_position)
                <= ROUTE_JUMP_TRIGGER_DISTANCE
            )
        if inside_trigger:
            candidates.append(index)
    return candidates[0] if candidates else None


def _rearm_revisited_rope_entry(runtime, state, point, entry_index, position) -> bool:
    """重新进入同一绳子入口时清除上一轮跳跃锁，允许再次发送C。"""

    if (
        point.segment_type != "rope_entry"
        or point.action != "jump"
        or state.last_jump_index != int(entry_index)
        or state.active_rope_x is not None
        or state.rope_top_exit_pending
    ):
        return False
    previous_jump_at = state.last_jump_at
    state.last_jump_index = None
    state.last_jump_at = 0.0
    _clear_rope_entry_runup(state)
    runtime.trace_event(
        "recorded_route_rope_entry_rearmed",
        position=position,
        route_index=int(entry_index),
        rope_x=point.rope_x,
        previous_jump_age_ms=(
            round(max(0.0, time.monotonic() - previous_jump_at) * 1000.0, 1)
            if previous_jump_at > 0
            else None
        ),
        reason="revisited_entry_without_active_rope",
        action="allow_fresh_rope_jump",
    )
    return True


def _has_reached_open_endpoint(variant, selected_index: int, position) -> bool:
    """判断非闭环路线是否已到最后录制点，避免继续按键走出地图。"""
    if variant.closed_loop or selected_index < len(variant.points) - 1:
        return False
    point = variant.points[-1]
    if point.horizontal == "left":
        horizontal_reached = int(position[0]) <= point.x + 3
    elif point.horizontal == "right":
        horizontal_reached = int(position[0]) >= point.x - 3
    else:
        horizontal_reached = abs(int(position[0]) - point.x) <= 2
    if point.vertical == "up":
        vertical_reached = int(position[1]) <= point.y
    elif point.vertical == "down":
        vertical_reached = int(position[1]) >= point.y
    else:
        vertical_reached = abs(int(position[1]) - point.y) <= 4
    return horizontal_reached and vertical_reached


def _apply_recorded_command(runtime, state, variant, position, selected_index) -> None:
    """执行当前JSON点的方向和跳跃，不添加追怪或平台脱困动作。"""
    selected_point = variant.points[int(selected_index)]
    if (
        selected_point.segment_type
        in RECORDED_ROUTE_SINGLE_PLATFORM_EXIT_SEGMENT_TYPES
        and _single_platform_exit_blocked(state, variant, position)
    ):
        _pin_single_platform_route(
            runtime,
            state,
            variant,
            position,
            selected_index,
        )
        return
    if _apply_active_rope(runtime, state, position):
        return
    _update_down_jump_patrol_progress(
        runtime,
        state,
        variant,
        position,
    )
    if _has_reached_open_endpoint(variant, selected_index, position):
        _apply_horizontal(runtime, state, "none")
        _apply_vertical(runtime, state, "none")
        if not state.completed:
            state.completed = True
            runtime.trace_event(
                "recorded_route_endpoint_reached",
                position=position,
                route_index=selected_index,
                reason="route_is_not_closed",
            )
        return
    state.completed = False
    point = variant.points[selected_index]
    if _apply_platform_coordinate_boundary_guard(
        runtime,
        state,
        variant,
        position,
        selected_index,
        point,
    ):
        return
    if _apply_down_jump_patrol_gate(
        runtime,
        state,
        variant,
        position,
        selected_index,
    ):
        return
    _rearm_revisited_rope_entry(
        runtime,
        state,
        point,
        selected_index,
        position,
    )
    jump_index = _select_jump_index(state, variant, position, selected_index)
    if jump_index is not None:
        point = variant.points[jump_index]
        if (
            point.segment_type
            in RECORDED_ROUTE_SINGLE_PLATFORM_EXIT_SEGMENT_TYPES
            and _single_platform_exit_blocked(state, variant, position)
        ):
            _pin_single_platform_route(
                runtime,
                state,
                variant,
                position,
                jump_index,
            )
            return
        state.route_index = jump_index
    elif point.action == "jump":
        # 最近点可能因坐标重复提前选中绳子入口；未通过起跳范围判定时只能
        # 水平靠近入口，不能先执行“方向+上”后又因为jump_index为空跳过C。
        if point.segment_type == "rope_entry" and has_complete_rope_geometry(point):
            rope_x, _top_y, _bottom_y = rope_bounds(point)
            approach_direction = _apply_rope_entry_runup(
                runtime,
                state,
                variant,
                point,
                selected_index,
                position,
                rope_x,
            )
        else:
            target_x = (
                _down_jump_random_target_x(state, selected_index, point)
                if point.segment_type == "down_jump"
                else int(point.x)
            )
            if int(position[0]) < int(target_x) - ROUTE_ROPE_BODY_TOLERANCE_X:
                approach_direction = "right"
            elif int(position[0]) > int(target_x) + ROUTE_ROPE_BODY_TOLERANCE_X:
                approach_direction = "left"
            else:
                approach_direction = "none"
        combat_logic.clear_combat_for_movement(runtime, state.combat)
        combat_logic.reset_attack_direction_lock(runtime, state.combat, reason="rope_entry_align")
        _apply_horizontal(runtime, state, approach_direction)
        _apply_vertical(runtime, state, "none")
        return
    horizontal = point.horizontal
    vertical = point.vertical
    if jump_index is not None and point.segment_type == "rope_entry" and has_complete_rope_geometry(point):
        rope_x, _top_y, _bottom_y = rope_bounds(point)
        target_offset_x = _rope_entry_target_offset(state, point)
        horizontal = _rope_entry_jump_direction(
            position[0],
            rope_x,
            target_offset_x=target_offset_x,
        ) or point.horizontal
        vertical = "up"
    if (
        jump_index is None
        and _recover_stalled_platform_movement(
            runtime,
            state,
            variant,
            position,
            selected_index,
            point,
        )
    ):
        return
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(runtime, state.combat, reason="recorded_route")
    is_rope_jump = (
        jump_index is not None
        and point.segment_type == "rope_entry"
        and has_complete_rope_geometry(point)
    )
    rope_profile = _rope_entry_profile(state, point) if is_rope_jump else None
    rope_target_offset_x = (
        _rope_entry_target_offset(state, point) if is_rope_jump else None
    )
    jump_hold_seconds = (
        _rope_entry_jump_hold_seconds(rope_profile)
        if is_rope_jump
        else ROUTE_JUMP_HOLD_SECONDS
    )
    horizontal_after_jump_hold_seconds = (
        _rope_entry_horizontal_after_jump_hold_seconds(rope_profile)
        if is_rope_jump
        else 0.0
    )
    if is_rope_jump:
        # 人物已越过绳子并回头。继续保持回头方向走一小段，再直接发送起跳
        # 脉冲；不能在这里松开方向键，否则刚形成的反向助跑速度会被清掉。
        _apply_horizontal(runtime, state, horizontal)
        _apply_vertical(runtime, state, "none")
        if not runtime.可中断等待(
            ROUTE_ROPE_PRE_JUMP_DIRECTION_HOLD_SECONDS,
            interval=0.01,
        ):
            return
    _apply_horizontal(runtime, state, horizontal)
    if jump_index is None or point.action != "jump":
        _apply_vertical(runtime, state, vertical)
        return
    if is_rope_jump:
        # 按实际游戏动作顺序发送“方向 → C → 上”，上键在C和方向松开后继续保持。
        _apply_vertical(runtime, state, "none")
        runtime.pydirectinput.keyDown("c")
        completed = runtime.可中断等待(
            ROUTE_ROPE_C_TO_UP_DELAY_SECONDS,
            interval=0.004,
        )
        if completed:
            runtime.pydirectinput.keyDown("up")
            state.vertical_direction = "up"
            completed = runtime.可中断等待(
                max(
                    0.0,
                    jump_hold_seconds - ROUTE_ROPE_C_TO_UP_DELAY_SECONDS,
                ),
                interval=0.005,
            )
    else:
        _apply_vertical(runtime, state, vertical)
        runtime.pydirectinput.keyDown("c")
        completed = runtime.可中断等待(
            ROUTE_JUMP_HOLD_SECONDS,
            interval=0.01,
        )
    runtime.pydirectinput.keyUp("c")
    if not completed:
        if is_rope_jump:
            _apply_horizontal(runtime, state, "none")
        return
    if is_rope_jump:
        # Keep the take-off direction a little longer after releasing C. Up remains
        # held throughout, giving left/right+jump+up enough horizontal travel to latch.
        completed = runtime.可中断等待(
            horizontal_after_jump_hold_seconds,
            interval=0.005,
        )
        _apply_horizontal(runtime, state, "none")
        if not completed:
            return
    state.last_jump_index = jump_index
    state.last_jump_at = time.monotonic()
    rope_geometry = (
        _normalized_rope_geometry(point)
        if point.segment_type == "rope_entry"
        else None
    )
    if rope_geometry is not None:
        rope_x, top_y, bottom_y = rope_geometry
        exit_index, exit_direction = _next_rope_exit(variant, jump_index)
        state.active_rope_direction = horizontal
        state.active_rope_x = rope_x
        state.active_rope_top_y = top_y
        state.active_rope_bottom_y = bottom_y
        state.active_rope_exit_direction = exit_direction
        state.active_rope_exit_index = exit_index
        # 从实际按下跳跃键完成后开始计算入绳承诺窗口。
        state.active_rope_started_at = state.last_jump_at
        state.active_rope_best_y = int(position[1])
        state.active_rope_progress_at = state.last_jump_at
        state.active_rope_contacted = False
        state.active_rope_contacted_at = 0.0
        state.active_rope_contact_start_y = None
        state.active_rope_confirmed = False
        state.active_rope_success_recorded = False
        state.active_rope_contact_up_repressed = False
        state.active_rope_last_up_key_at = state.last_jump_at
        state.active_rope_entry_index = jump_index
        state.active_rope_attempt_offset_x = int(position[0]) - rope_x
        state.active_rope_profile_key = (rope_x, top_y, bottom_y)
        _clear_rope_entry_runup(state)
    runtime.trace_event(
        "recorded_route_jump",
        position=position,
        route_index=jump_index,
        horizontal=horizontal,
        vertical=vertical,
        segment_type=point.segment_type,
        rope_x=point.rope_x,
        rope_offset_x=(
            int(position[0]) - int(point.rope_x)
            if point.rope_x is not None
            else None
        ),
        adaptive_target_offset_x=rope_target_offset_x,
        adaptive_failure_count=(
            int(rope_profile.consecutive_failures)
            if rope_profile is not None
            else 0
        ),
        down_jump_random_offset_x=(
            int(state.down_jump_random_offset_x or 0)
            if point.segment_type == "down_jump"
            else None
        ),
        down_jump_target_x=(
            _down_jump_random_target_x(state, jump_index, point)
            if point.segment_type == "down_jump" and jump_index is not None
            else None
        ),
        jump_hold_ms=(
            round(jump_hold_seconds * 1000.0, 1)
            if is_rope_jump
            else None
        ),
        horizontal_after_jump_hold_ms=(
            round(horizontal_after_jump_hold_seconds * 1000.0, 1)
            if is_rope_jump
            else None
        ),
        horizontal_total_hold_ms=(
            round(
                (
                    jump_hold_seconds
                    + horizontal_after_jump_hold_seconds
                ) * 1000.0,
                1,
            )
            if is_rope_jump
            else None
        ),
        rope_top_y=point.rope_top_y,
        rope_bottom_y=point.rope_bottom_y,
        input_sequence=("direction_c_up" if is_rope_jump else "recorded_jump"),
        up_reassert_ms=(
            round(_rope_up_reassert_seconds(state) * 1000.0, 1)
            if is_rope_jump
            else None
        ),
        entry_commit_ms=(
            round(ROUTE_ROPE_ENTRY_COMMIT_SECONDS * 1000.0, 1)
            if is_rope_jump
            else None
        ),
    )


def _release_all_keys(runtime, state, reason: str) -> None:
    """释放通用回放可能持有的移动、绳子、跳跃和攻击按键。"""
    runtime.释放水平移动键(reason=reason)
    runtime.pydirectinput.keyUp("up")
    runtime.pydirectinput.keyUp("down")
    runtime.pydirectinput.keyUp("c")
    runtime.释放攻击键()
    state.vertical_direction = None
    state.combat.applied_direction = None
    state.route_command_direction = None
    state.route_command_observed_direction = None
    state.route_command_anchor_x = None
    _clear_active_rope(state)


def _route_resume_grace_active(state) -> bool:
    """清怪后短暂把控制权完整交还JSON路线。"""
    return time.monotonic() < state.route_resume_grace_until


def _clear_connection_priority(state) -> None:
    """清除跨平台连接的独占状态。"""
    state.connection_priority_active = False
    state.connection_priority_source_platform_index = None
    state.connection_priority_route_index = None
    state.connection_priority_started_at = 0.0


def _clear_walk_off_state(state) -> None:
    """Clear the directional walk-off latch without touching other connections."""

    state.walk_off_active = False
    state.walk_off_direction = None
    state.walk_off_route_index = None
    state.walk_off_source_platform_index = None
    state.walk_off_target_platform_index = None
    state.walk_off_source_platform_number = None
    state.walk_off_target_platform_number = None
    state.walk_off_landing_x = None
    state.walk_off_landing_y = None
    state.walk_off_started_at = 0.0


def _start_walk_off_state(runtime, state, variant, route_index: int, source_index: int) -> bool:
    """Latch a recorded left/right walk-off until its target platform is reached."""

    plan = infer_walk_off_plan(
        variant.points,
        variant.platform_ranges,
        route_index,
        source_index,
        variant.closed_loop,
    )
    if plan is None:
        return False
    state.walk_off_active = True
    state.walk_off_direction = plan.direction
    state.walk_off_route_index = plan.route_index
    state.walk_off_source_platform_index = plan.source_platform_index
    state.walk_off_target_platform_index = plan.target_platform_index
    state.walk_off_source_platform_number = plan.source_platform_number
    state.walk_off_target_platform_number = plan.target_platform_number
    state.walk_off_landing_x = plan.landing_x
    state.walk_off_landing_y = plan.landing_y
    state.walk_off_started_at = time.monotonic()
    runtime.trace_event(
        "recorded_route_walk_off_started",
        route_index=plan.route_index,
        direction=plan.direction,
        source_platform_index=plan.source_platform_index,
        target_platform_index=plan.target_platform_index,
        source_platform_number=plan.source_platform_number,
        target_platform_number=plan.target_platform_number,
        landing_position=[plan.landing_x, plan.landing_y],
        action="hold_direction_until_target_platform",
    )
    return True


def _maintain_walk_off(runtime, state, variant, position) -> bool:
    """Keep the walk-off direction pressed until the recorded target landing."""

    if not state.walk_off_active:
        return False
    if _single_platform_exit_blocked(state, variant, position):
        blocked_index = state.walk_off_route_index
        _pin_single_platform_route(
            runtime,
            state,
            variant,
            position,
            blocked_index,
        )
        return True
    if (
        state.walk_off_direction not in ("left", "right")
        or state.walk_off_route_index is None
        or state.walk_off_source_platform_index is None
    ):
        _clear_walk_off_state(state)
        _clear_connection_priority(state)
        return False

    plan = WalkOffPlan(
        route_index=int(state.walk_off_route_index),
        direction=str(state.walk_off_direction),
        source_platform_index=int(state.walk_off_source_platform_index),
        target_platform_index=state.walk_off_target_platform_index,
        source_platform_number=state.walk_off_source_platform_number,
        target_platform_number=state.walk_off_target_platform_number,
        landing_x=int(state.walk_off_landing_x),
        landing_y=int(state.walk_off_landing_y),
    )
    current_platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=None,
    )
    decision = evaluate_walk_off(
        plan,
        current_platform_index,
        variant.platform_ranges,
        position=position,
    )
    if decision.completed:
        elapsed_seconds = max(0.0, time.monotonic() - state.walk_off_started_at)
        target_index = decision.current_platform_index
        _apply_horizontal(runtime, state, "none")
        _apply_vertical(runtime, state, "none")
        _clear_connection_priority(state)
        _clear_walk_off_state(state)
        if target_index is not None:
            target_platform = variant.platform_ranges[int(target_index)]
            candidate_indices = list(
                range(
                    int(target_platform.start_index),
                    int(target_platform.end_index) + 1,
                )
            )
            selected_index, _distance = _nearest_index(
                variant.points,
                position,
                candidate_indices,
            )
            state.route_index = int(selected_index)
        state.last_route_progress_at = time.monotonic()
        runtime.trace_event(
            "recorded_route_walk_off_completed",
            position=position,
            direction=plan.direction,
            source_platform_index=plan.source_platform_index,
            target_platform_index=target_index,
            source_platform_number=plan.source_platform_number,
            target_platform_number=plan.target_platform_number,
            landing_position=[plan.landing_x, plan.landing_y],
            elapsed_ms=round(elapsed_seconds * 1000.0, 1),
            action="release_direction_and_resume_platform_route",
        )
        return False

    # Do not run boundary recovery, nearest-point relocation, combat or the route
    # liveness jump while crossing.  Not matching any platform is the normal
    # airborne state and still means "continue in the recorded direction".
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="recorded_route_walk_off",
    )
    _apply_vertical(runtime, state, "none")
    _apply_horizontal(runtime, state, plan.direction)
    state.last_control_intent_at = time.monotonic()
    state.last_control_intent_kind = "walk_off_{}".format(plan.direction)
    return True


def _update_connection_priority(
    runtime,
    state,
    variant,
    position,
    selected_index: int,
) -> bool:
    """进入平台连接后暂停战斗抢占，直到人物真正落到下一平台。"""
    if _single_platform_exit_blocked(state, variant, position):
        point = variant.points[int(selected_index)]
        blocked_index = (
            state.connection_priority_route_index
            if state.connection_priority_active
            and state.connection_priority_route_index is not None
            else selected_index
        )
        if (
            state.connection_priority_active
            or point.segment_type
            in RECORDED_ROUTE_SINGLE_PLATFORM_EXIT_SEGMENT_TYPES
        ):
            _pin_single_platform_route(
                runtime,
                state,
                variant,
                position,
                blocked_index,
            )
        return False
    now = time.monotonic()
    current_platform_index = _current_platform_range_index(
        variant,
        position,
        preferred_route_index=selected_index,
    )
    if state.connection_priority_active:
        source_index = state.connection_priority_source_platform_index
        arrived_next_platform = (
            current_platform_index is not None
            and source_index is not None
            and int(current_platform_index) != int(source_index)
            and variant.points[selected_index].segment_type == "platform"
        )
        if arrived_next_platform:
            runtime.trace_event(
                "recorded_route_connection_priority_completed",
                position=position,
                source_platform_index=source_index,
                arrived_platform_index=current_platform_index,
                route_index=selected_index,
                elapsed_ms=round(
                    max(0.0, now - state.connection_priority_started_at) * 1000.0,
                    1,
                ),
                action="resume_platform_combat",
            )
            _clear_connection_priority(state)
            state.last_route_progress_at = now
            return False
        if (
            not state.walk_off_active
            and state.connection_priority_started_at > 0
            and now - state.connection_priority_started_at
            >= RECORDED_ROUTE_CONNECTION_MAX_SECONDS
        ):
            runtime.trace_event(
                "recorded_route_connection_priority_timeout",
                position=position,
                source_platform_index=source_index,
                route_index=selected_index,
                elapsed_ms=round(
                    (now - state.connection_priority_started_at) * 1000.0,
                    1,
                ),
                action="clear_and_relocalize",
            )
            _clear_connection_priority(state)
            state.route_index = None
            state.last_jump_index = None
            _clear_rope_entry_runup(state)
            return False
        return True

    point = variant.points[selected_index]
    if point.segment_type not in RECORDED_ROUTE_CONNECTION_SEGMENT_TYPES:
        return False
    if point.segment_type == "down_jump" and not _down_jump_patrol_ready(
        state,
        variant,
        selected_index,
    ):
        return False
    source_index = _preceding_platform_range_index(variant, selected_index)
    if (
        source_index is None
        or state.active_platform_range_index != source_index
        or state.platform_coverage_target_x is not None
    ):
        return False
    state.connection_priority_active = True
    state.connection_priority_source_platform_index = int(source_index)
    state.connection_priority_route_index = int(selected_index)
    state.connection_priority_started_at = now
    if point.segment_type in ("walk_off_left", "walk_off_right"):
        _start_walk_off_state(
            runtime,
            state,
            variant,
            int(selected_index),
            int(source_index),
        )
    _reset_platform_stall_tracking(state)
    state.route_resume_grace_until = 0.0
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="recorded_route_connection_priority",
    )
    release_attack_keys = getattr(runtime, "释放攻击键", None)
    if callable(release_attack_keys):
        release_attack_keys()
    runtime.trace_event(
        "recorded_route_connection_priority_started",
        position=position,
        source_platform_index=source_index,
        route_index=selected_index,
        segment_type=point.segment_type,
        action="finish_recorded_connection_before_combat",
    )
    return True


def _force_route_resume(runtime, state, position, reason: str) -> None:
    """清除短暂战斗残留，并为JSON路线保留连续控制窗口。"""
    now = time.monotonic()
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(runtime, state.combat, reason=reason)
    state.combat.combat_suppressed_until = max(
        getattr(state.combat, "combat_suppressed_until", 0.0),
        now + RECORDED_ROUTE_RECOVERY_SUPPRESS_SECONDS,
    )
    suppress_runtime_combat = getattr(runtime, "暂时忽略战斗", None)
    if callable(suppress_runtime_combat):
        suppress_runtime_combat(
            RECORDED_ROUTE_RECOVERY_SUPPRESS_SECONDS,
            reason=reason,
        )
    release_attack_keys = getattr(runtime, "释放攻击键", None)
    if callable(release_attack_keys):
        release_attack_keys()
    state.smart_seek_target_active = False
    state.smart_seek_scan_until = 0.0
    state.smart_seek_mode = "patrol"
    _clear_smart_seek_target_loss(state)
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    state.route_resume_grace_until = max(
        state.route_resume_grace_until,
        now + RECORDED_ROUTE_RESUME_GRACE_SECONDS,
    )
    state.route_command_direction = None
    state.route_command_observed_direction = None
    state.route_command_anchor_x = None
    state.last_control_intent_at = now
    state.last_control_intent_kind = "route_recovery"
    runtime.trace_event(
        "recorded_route_flow_resumed",
        position=position,
        reason=reason,
        grace_ms=round(RECORDED_ROUTE_RESUME_GRACE_SECONDS * 1000.0, 1),
        action="temporarily_prioritize_json_route",
    )


def _apply_route_liveness_watchdog(runtime, state, variant, position) -> bool:
    """把路线活性判断委托给可独立回滚的公共模块。"""
    return apply_route_liveness_watchdog(
        runtime,
        state,
        variant,
        position,
        config=RECORDED_ROUTE_LIVENESS_CONFIG,
        rope_bounds=rope_bounds,
        has_complete_rope_geometry=has_complete_rope_geometry,
        rest_route_navigation_active=_rest_route_navigation_active,
        current_platform_range_index=_current_platform_range_index,
        read_monster_snapshot=combat_logic.read_monster_snapshot,
        force_route_resume=_force_route_resume,
        apply_vertical=_apply_vertical,
        apply_horizontal=_apply_horizontal,
        clear_rope_entry_runup=_clear_rope_entry_runup,
        clear_connection_priority=_clear_connection_priority,
    )
def _reset_combat_lock_watch(state) -> None:
    """清空录制路线的战斗锁看门狗，不改变公共怪物快照。"""
    state.combat_lock_started_at = 0.0
    state.combat_lock_last_progress_at = 0.0
    state.combat_lock_last_attack_completed_at = 0.0


def _release_stalled_combat(runtime, state, snapshot, position, reason: str) -> None:
    """释放没有有效进展的战斗，并短暂屏蔽同一静态误识别。"""
    now = time.monotonic()
    locked_seconds = (
        max(0.0, now - state.combat_lock_started_at)
        if state.combat_lock_started_at > 0
        else 0.0
    )
    no_progress_seconds = (
        max(0.0, now - state.combat_lock_last_progress_at)
        if state.combat_lock_last_progress_at > 0
        else locked_seconds
    )
    attacks = int(state.combat.combat_attack_count)
    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="recorded_route_combat_lock_timeout",
    )
    state.combat.combat_suppressed_until = (
        now + RECORDED_ROUTE_COMBAT_SUPPRESS_SECONDS
    )
    suppress_runtime_combat = getattr(runtime, "暂时忽略战斗", None)
    if callable(suppress_runtime_combat):
        suppress_runtime_combat(
            RECORDED_ROUTE_COMBAT_SUPPRESS_SECONDS,
            reason=reason,
        )
    state.smart_seek_target_active = False
    state.smart_seek_scan_until = 0.0
    state.smart_seek_mode = "patrol"
    _clear_smart_seek_target_loss(state)
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    runtime.trace_event(
        "recorded_route_combat_lock_released",
        reason=reason,
        position=position,
        locked_ms=round(locked_seconds * 1000.0, 1),
        no_progress_ms=round(no_progress_seconds * 1000.0, 1),
        attacks=attacks,
        target_age_ms=snapshot.age_ms,
        attackable_count=snapshot.attackable_count,
        chase_count=snapshot.chase_count,
        suppress_ms=round(RECORDED_ROUTE_COMBAT_SUPPRESS_SECONDS * 1000.0, 1),
        action="resume_recorded_route",
    )
    print(
        "[战斗] 当前识别长时间没有有效进展，已暂时忽略并继续路线。"
    )
    _reset_combat_lock_watch(state)


def _combat_lock_timed_out(runtime, state, snapshot, intent, position) -> bool:
    """判断本轮战斗是否卡死；正常完成攻击会持续刷新进展时间。"""
    if intent.source != "combat" or intent.attackable_count <= 0:
        # combat_hold、追怪、爬绳和正常路线都不是正在执行攻击的会话。立即
        # 清表，避免上一只怪或上一层平台的计时残留到下一场战斗。
        _reset_combat_lock_watch(state)
        return False

    now = time.monotonic()
    if not state.combat.combat_active or state.combat_lock_started_at <= 0:
        # build_action_intent 先于 _start_combat 执行；combat_active=False 表示
        # 这是一次全新的战斗入口，旧会话即使没有经过普通路线分支也必须清表。
        state.combat_lock_started_at = now
        state.combat_lock_last_progress_at = now
        state.combat_lock_last_attack_completed_at = getattr(
            state.combat, "last_attack_completed_at", 0.0
        )
        return False
    elif (
        getattr(state.combat, "last_attack_completed_at", 0.0)
        > state.combat_lock_last_attack_completed_at
    ):
        state.combat_lock_last_attack_completed_at = getattr(
            state.combat, "last_attack_completed_at", 0.0
        )
        state.combat_lock_last_progress_at = now

    no_progress_seconds = now - state.combat_lock_last_progress_at
    if no_progress_seconds >= RECORDED_ROUTE_COMBAT_NO_PROGRESS_SECONDS:
        _release_stalled_combat(
            runtime,
            state,
            snapshot,
            position,
            reason="no_successful_attack",
        )
        return True
    return False


def _clear_smart_seek_target_loss(state) -> None:
    """清除短暂丢目标宽限，避免旧时间戳影响下一次清怪。"""
    state.smart_seek_target_loss_started_at = 0.0


def _apply_target_loss_direction_coast(
    runtime,
    state,
    loss_elapsed,
) -> bool:
    """目标短暂漏检时继续原追怪方向，避免JSON路线立刻反向抢键。"""
    chase_direction = state.last_monster_direction
    route_direction = state.route_command_direction
    if (
        loss_elapsed >= RECORDED_ROUTE_TARGET_LOSS_DIRECTION_COAST_SECONDS
        or chase_direction not in ("left", "right")
        or route_direction not in ("left", "right")
        or chase_direction == route_direction
    ):
        return False

    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="smart_seek_target_loss_direction_coast",
    )
    if state.combat.applied_direction != chase_direction:
        runtime.切换持续移动(
            chase_direction,
            reason="target_loss_chase_coast",
        )
        state.combat.applied_direction = chase_direction
    state.last_control_intent_at = time.monotonic()
    state.last_control_intent_kind = "target_loss_chase_coast"
    state.smart_seek_mode = "target_loss_direction_coast"
    return True


def _apply_smart_seek_priority(runtime, state, variant, position=None) -> bool:
    """使用参考版公共策略优先处理怪物，再把控制权交回当前JSON路线。"""
    # 本函数不使用路线变体；保留旧的三参数内部调用兼容性，避免测试和历史
    # 调用方只传 position 时失效。
    if position is None:
        position = variant
        variant = None
    snapshot = combat_logic.read_monster_snapshot(runtime)
    intent = combat_logic.build_action_intent(state.combat, snapshot)
    now = time.monotonic()
    real_target_visible = bool(getattr(snapshot, "fresh", False)) and (
        getattr(snapshot, "attackable_count", 0) > 0
        or getattr(snapshot, "chase_count", 0) > 0
    )
    combat_or_chase_intent = intent.source in (
        "combat",
        "combat_hold",
        "chase",
        "chase_hold",
    )
    # combat_hold 只表示公共战斗状态仍在做结束防抖，并不代表当前帧仍有
    # 可攻击或可追击目标。没有真实目标时不能让它再次刹停 JSON 路线。
    if combat_or_chase_intent and real_target_visible:
        # 攻击目标立即抢占；远距离追怪换边确认已在公共战斗策略中完成，
        # 这里直接执行最终稳定方向，不再由路线层重复改写。
        state.smart_seek_target_active = True
        if state.smart_seek_target_loss_started_at > 0:
            runtime.trace_event(
                "recorded_route_smart_seek_target_loss_recovered",
                position=position,
                loss_ms=round(
                    (now - state.smart_seek_target_loss_started_at) * 1000.0,
                    1,
                ),
                next_source=intent.source,
                action="resume_combat_or_chase_priority",
            )
        _clear_smart_seek_target_loss(state)
        state.smart_seek_scan_until = 0.0
        state.smart_seek_mode = "target"
        state.loot_approach_direction = None
        state.loot_approach_until = 0.0
        if intent.target_direction in ("left", "right"):
            state.last_monster_direction = intent.target_direction
        _apply_vertical(runtime, state, "none")
        decision_changed = combat_logic.trace_action_decision(
            runtime,
            state.combat,
            intent,
            position,
        )
        combat_logic.apply_action_intent(
            runtime,
            state.combat,
            intent,
            position,
            decision_changed,
        )
        state.last_ignored_chase_signature = None
        return True

    if state.smart_seek_target_active:
        if state.smart_seek_target_loss_started_at <= 0:
            snapshot_age_ms = getattr(snapshot, "age_ms", None)
            observed_target_loss_seconds = (
                max(0.0, float(snapshot_age_ms)) / 1000.0
                if snapshot_age_ms is not None
                else 0.0
            )
            observed_target_loss_seconds = min(
                RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS,
                observed_target_loss_seconds,
            )
            # 沿用最后目标快照的年龄，只保留一个短暂的“可重新接战”窗口。
            # 窗口期间路线照常移动，不再把公共 combat_hold 变成二次停顿。
            state.smart_seek_target_loss_started_at = (
                now - observed_target_loss_seconds
            )
            runtime.trace_event(
                "recorded_route_smart_seek_target_loss_route_continued",
                position=position,
                snapshot_fresh=snapshot.fresh,
                target_age_ms=snapshot.age_ms,
                chase_count=getattr(snapshot, "chase_count", 0),
                attackable_count=getattr(snapshot, "attackable_count", 0),
                grace_ms=round(
                    RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS
                    * 1000.0,
                    1,
                ),
                elapsed_loss_ms=round(observed_target_loss_seconds * 1000.0, 1),
                remaining_grace_ms=round(
                    max(
                        0.0,
                        RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS
                        - observed_target_loss_seconds,
                    )
                    * 1000.0,
                    1,
                ),
                last_chase_direction=state.last_monster_direction,
                route_direction=state.route_command_direction,
                direction_coast_ms=round(
                    RECORDED_ROUTE_TARGET_LOSS_DIRECTION_COAST_SECONDS
                    * 1000.0,
                    1,
                ),
                action="keep_chase_direction_if_route_opposes_while_target_recovers",
            )
        loss_elapsed = now - state.smart_seek_target_loss_started_at
        if loss_elapsed < RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS:
            if _apply_target_loss_direction_coast(
                runtime,
                state,
                loss_elapsed,
            ):
                return True
            # 短暂续走期结束后仍保留完整重获窗口；确认漏检持续存在时，
            # 才允许JSON路线接管一次，不再在每个漏检帧立即左右翻转。
            state.smart_seek_mode = "route_during_target_loss"
        else:
            runtime.trace_event(
                "recorded_route_smart_seek_target_loss_grace_expired",
                position=position,
                snapshot_fresh=snapshot.fresh,
                target_age_ms=snapshot.age_ms,
                chase_count=getattr(snapshot, "chase_count", 0),
                attackable_count=getattr(snapshot, "attackable_count", 0),
                loss_ms=round(loss_elapsed * 1000.0, 1),
                action="keep_recorded_route_running",
            )
            state.smart_seek_target_active = False
            state.smart_seek_mode = "patrol"
            state.combat.chase_active = False
            state.combat.chase_direction = None
            state.combat.chase_direction_nearest_dx = None
            state.combat.chase_switch_pending_direction = None
            state.combat.chase_switch_pending_count = 0
            state.combat.chase_switch_pending_frame_at = 0.0
            _clear_smart_seek_target_loss(state)

    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="smart_seek_no_target",
    )
    # 无怪时不再追加原地扫描。检测线程会继续运行；若目标重新出现，循环
    # 顶部及路线起步前的最终复核仍会立即把控制权交回攻击/追怪。
    state.smart_seek_scan_until = 0.0
    state.loot_approach_direction = None
    state.loot_approach_until = 0.0
    if not state.smart_seek_target_active and state.smart_seek_mode != "patrol":
        state.smart_seek_mode = "patrol"
    return False


def _apply_final_route_combat_handoff_guard(
    runtime,
    state,
    variant,
    position,
) -> bool:
    """路线真正起步前复核一次最新目标，避免刚走几十毫秒又被战斗打断。"""
    if not _apply_smart_seek_priority(runtime, state, variant, position):
        return False
    runtime.trace_event(
        "recorded_route_movement_preempted_by_latest_target",
        position=position,
        smart_seek_mode=state.smart_seek_mode,
        action="keep_combat_priority_before_route_key_down",
    )
    return True


def _apply_post_combat_loot_approach(runtime, state, variant, position) -> bool:
    """清怪后在当前平台内短暂朝最后怪物方向移动，优先拾取该侧掉落物。"""
    direction = state.loot_approach_direction
    now = time.monotonic()
    if direction not in ("left", "right") or now >= state.loot_approach_until:
        state.loot_approach_direction = None
        state.loot_approach_until = 0.0
        return False

    minimum_x = maximum_x = None
    if variant.platform_patrol:
        minimum_x = int(variant.platform_min_x)
        maximum_x = int(variant.platform_max_x)
    elif variant.platform_ranges:
        platform_index = _current_platform_range_index(variant, position)
        if platform_index is not None:
            platform = variant.platform_ranges[platform_index]
            minimum_x = int(platform.minimum_x)
            maximum_x = int(platform.maximum_x)
    if minimum_x is None or maximum_x is None:
        state.loot_approach_direction = None
        state.loot_approach_until = 0.0
        return False

    position_x = int(position[0])
    boundary_reached = (
        direction == "left"
        and position_x <= minimum_x + RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X
    ) or (
        direction == "right"
        and position_x >= maximum_x - RECORDED_ROUTE_LOOT_BOUNDARY_MARGIN_X
    )
    if boundary_reached:
        state.loot_approach_direction = None
        state.loot_approach_until = 0.0
        runtime.trace_event(
            "recorded_route_loot_approach_skipped",
            position=position,
            direction=direction,
            platform_x_range=[minimum_x, maximum_x],
            reason="platform_boundary",
        )
        return False

    combat_logic.clear_combat_for_movement(runtime, state.combat)
    combat_logic.reset_attack_direction_lock(
        runtime,
        state.combat,
        reason="post_combat_loot_approach",
    )
    _apply_vertical(runtime, state, "none")
    _apply_horizontal(runtime, state, direction)
    return True


def run_recorded_route(runtime) -> None:
    """运行自定义路线；有怪优先追击，清怪复查后恢复平台拾取巡逻。"""
    plan = _load_recorded_route(runtime)
    state = RecordedRouteState()
    state.ignored_platform_numbers = tuple(
        sorted(
            {
                int(number)
                for number in getattr(
                    runtime,
                    "recorded_route_ignored_platform_numbers",
                    (),
                )
                if int(number) > 0
            }
        )
    )
    state.active_variant_index = _choose_variant_index(plan)
    variant = plan.variants[state.active_variant_index]
    state.recorded_rest_point_enabled = plan.rest_point is not None
    if plan.rest_point is None:
        _configure_rope_middle_fallback(state, variant)
    else:
        state.recorded_rest_point = plan.rest_point
        recorded_rest_rope_geometry = _find_recorded_rest_rope_geometry(plan)
        if recorded_rest_rope_geometry is not None:
            (
                state.recorded_rest_rope_x,
                state.recorded_rest_rope_top_y,
                state.recorded_rest_rope_bottom_y,
            ) = recorded_rest_rope_geometry
            state.recorded_rest_on_rope = True
    recorded_rope_segment_count = _count_recorded_rope_rest_segments(plan)
    if plan.rest_point is not None:
        if plan.rest_point.interval_minutes is not None:
            state.recorded_rest_interval_seconds = max(
                0.0,
                float(plan.rest_point.interval_minutes) * 60.0,
            )
        if plan.rest_point.duration_minutes is not None:
            state.recorded_rest_duration_seconds = max(
                0.0,
                float(plan.rest_point.duration_minutes) * 60.0,
            )
    platform_count = len(
        {
            (
                platform.minimum_x,
                platform.maximum_x,
                platform.minimum_y,
                platform.maximum_y,
            )
            for platform in variant.platform_ranges
        }
    )
    single_active_platform = _single_unignored_platform_patrol_variant(
        variant,
        state.ignored_platform_numbers,
    )
    if single_active_platform is not None:
        state.single_active_platform_number = int(single_active_platform[0])
        state.single_active_platform_patrol_enabled = True
    ignored_platform_set = set(int(number) for number in state.ignored_platform_numbers)
    active_platform_numbers = {
        int(platform.platform_number)
        for platform in variant.platform_ranges
        if platform.platform_number is not None
        and int(platform.platform_number) not in ignored_platform_set
    }
    state.single_platform_stationary_enabled = bool(
        variant.platform_patrol
        or len(active_platform_numbers) == 1
        or platform_count == 1
    )
    set_bidirectional_chase = getattr(
        runtime,
        "设置智能追怪双向模式",
        None,
    )
    if callable(set_bidirectional_chase):
        set_bidirectional_chase(state.single_platform_stationary_enabled)
    if (
        state.single_active_platform_number is None
        and len(active_platform_numbers) == 1
    ):
        state.single_active_platform_number = int(next(iter(active_platform_numbers)))
    _initialize_rope_rest_schedule(runtime, state)
    rope_rest_interval_seconds, rope_rest_duration_seconds = _rope_rest_settings(
        runtime,
        state,
    )
    runtime.trace_event(
        "recorded_route_started",
        route_path=str(plan.path),
        route_profile="strict_recorded",
        route_points=len(variant.points),
        route_variants=len(plan.variants),
        initial_variant=variant.name,
        closed_loop=variant.closed_loop,
        movement_source="monster_first_combat_with_json_route_fallback",
        control_chain_revision="reference_combat_strategy_20260809",
        chase_enabled=True,
        combat_strategy_source=r"D:\PythonProject4-2\PythonProject4",
        target_snapshot_ttl_seconds=combat_logic.V2_TARGET_SNAPSHOT_TTL_SECONDS,
        idle_when_no_target=state.single_platform_stationary_enabled,
        post_combat_scan_seconds=[
            RECORDED_ROUTE_POST_COMBAT_SCAN_MIN_SECONDS,
            RECORDED_ROUTE_POST_COMBAT_SCAN_MAX_SECONDS,
        ],
        continue_route_when_no_target=True,
        target_loss_reacquire_window_seconds=(
            RECORDED_ROUTE_TRANSIENT_TARGET_LOSS_GRACE_SECONDS
        ),
        post_combat_route_grace_seconds=0.0,
        route_recovery_grace_seconds=RECORDED_ROUTE_RESUME_GRACE_SECONDS,
        post_combat_loot_override_enabled=state.single_platform_stationary_enabled,
        single_platform_loot_target_mode="last_monster_estimated_x",
        single_platform_idle_after_loot=state.single_platform_stationary_enabled,
        rope_rest_overrides_single_platform_idle=True,
        connection_priority_enabled=True,
        connection_priority_timeout_seconds=RECORDED_ROUTE_CONNECTION_MAX_SECONDS,
        route_liveness_watchdog=True,
        route_liveness_target_heartbeat=True,
        route_liveness_pause_gap_reset_seconds=0.50,
        route_control_silence_seconds=RECORDED_ROUTE_CONTROL_SILENCE_SECONDS,
        route_move_no_progress_seconds=RECORDED_ROUTE_MOVE_NO_PROGRESS_SECONDS,
        combat_starvation_seconds=RECORDED_ROUTE_COMBAT_STARVATION_SECONDS,
        platform_route_navigation_enabled=True,
        platform_stall_recovery=True,
        platform_stall_right_jump_first=True,
        multi_platform_navigation=(
            platform_count > 1
            and not state.single_active_platform_patrol_enabled
        ),
        platform_count=platform_count,
        active_platform_count=len(active_platform_numbers),
        multi_platform_max_dwell_seconds=(
            RECORDED_ROUTE_MULTI_PLATFORM_MAX_DWELL_SECONDS
        ),
        multi_platform_soft_rotation=True,
        walk_off_hold_until_target_platform=True,
        platform_route_replanning=True,
        active_brush_platform_numbers=sorted(active_platform_numbers),
        single_active_platform_patrol=state.single_active_platform_patrol_enabled,
        single_active_platform_number=state.single_active_platform_number,
        single_platform_stationary_enabled=state.single_platform_stationary_enabled,
        single_platform_bidirectional_chase=state.single_platform_stationary_enabled,
        single_platform_chase_boundary_guard=True,
        dynamic_rope_geometry=True,
        adaptive_rope_entry=True,
        adaptive_rope_entry_offsets=[1, 2, 3],
        adaptive_rope_entry_retries_per_offset=ROUTE_ROPE_ENTRY_RETRIES_PER_OFFSET,
        adaptive_rope_entry_confirm_progress_y=ROUTE_ROPE_ENTRY_CONFIRM_PROGRESS_Y,
        rope_contact_up_repress=True,
        rope_contact_up_release_ms=round(
            ROUTE_ROPE_CONTACT_UP_REPRESS_RELEASE_SECONDS * 1000.0,
            1,
        ),
        rope_entry_early_fall_ms=round(
            ROUTE_ROPE_ENTRY_EARLY_FALL_SECONDS * 1000.0,
            1,
        ),
        rope_entry_horizontal_total_hold_ms=[
            round(
                (
                    ROUTE_JUMP_HOLD_SECONDS
                    + ROUTE_ROPE_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS
                ) * 1000.0,
                1,
            ),
            round(
                (
                    ROUTE_ROPE_ENTRY_MAX_JUMP_HOLD_SECONDS
                    + ROUTE_ROPE_ENTRY_MAX_HORIZONTAL_AFTER_JUMP_HOLD_SECONDS
                ) * 1000.0,
                1,
            ),
        ],
        adaptive_rope_entry_max_commit_ms=round(
            (
                ROUTE_ROPE_ENTRY_COMMIT_SECONDS
                + ROUTE_ROPE_ENTRY_MAX_COMMIT_BONUS_SECONDS
            ) * 1000.0,
            1,
        ),
        rope_body_startup_detection=True,
        stalled_rope_body_reacquisition=True,
        rope_rest_enabled=(
            (
                state.recorded_rest_point_enabled
                or state.rope_rest_fallback_enabled
            )
            and rope_rest_interval_seconds > 0
            and rope_rest_duration_seconds > 0
        ),
        rope_rest_interval_minutes=round(rope_rest_interval_seconds / 60.0, 2),
        rope_rest_duration_minutes=round(rope_rest_duration_seconds / 60.0, 2),
        rope_rest_position=(
            [state.recorded_rest_point.x, state.recorded_rest_point.y]
            if state.recorded_rest_point is not None
            else None
        ),
        rope_rest_fallback_enabled=state.rope_rest_fallback_enabled,
        recorded_rope_segment_count=recorded_rope_segment_count,
        recorded_rest_point_mode=(
            "rope_point"
            if state.recorded_rest_on_rope
            else (
                "platform_point"
                if plan.rest_point is not None
                else (
                    "rope_middle"
                    if state.rope_rest_fallback_enabled
                    else None
                )
            )
        ),
        recorded_rest_rope_geometry=(
            [
                state.recorded_rest_rope_x,
                state.recorded_rest_rope_top_y,
                state.recorded_rest_rope_bottom_y,
            ]
            if state.recorded_rest_on_rope or state.rope_rest_fallback_enabled
            else None
        ),
        recorded_rest_point=(
            [state.recorded_rest_point.x, state.recorded_rest_point.y]
            if state.recorded_rest_point is not None
            else None
        ),
        platform_ai_actions=True,
        platform_ai_interval_seconds=[
            ROUTE_AI_MIN_INTERVAL_SECONDS,
            ROUTE_AI_MAX_INTERVAL_SECONDS,
        ],
        platform_ai_actions_available=["pause", "jump"],
        ignored_platform_numbers=list(state.ignored_platform_numbers),
        platform_patrol=variant.platform_patrol,
        platform_x_range=(
            [variant.platform_min_x, variant.platform_max_x]
            if variant.platform_patrol
            else None
        ),
    )
    print(
        "[自定义录制路线] 已加载：{}；平台启用智能寻怪："
        "有怪优先追击攻击，清怪后短暂复查，再恢复平台巡逻拾取。".format(
            plan.path.name
        )
    )
    if state.ignored_platform_numbers:
        print(
            "[忽略平台] 已启用：{}；到达这些平台后将跳过寻怪、"
            "拾取和平台巡逻，直接按录制路线前往下一平台。".format(
                "/".join(str(number) for number in state.ignored_platform_numbers)
            )
        )
    if state.single_active_platform_patrol_enabled:
        print(
            "[忽略平台] 过滤后仅剩平台{}：有怪时优先战斗，清怪后走到"
            "最后怪物死亡位置拾取并原地驻守；休息点到期会恢复跨平台路线。"
            "若意外掉到其他平台，会重新规划连接路线直到回到平台{}。".format(
                state.single_active_platform_number,
                state.single_active_platform_number,
            )
        )
    elif state.single_platform_stationary_enabled:
        print(
            "[单平台] 有怪时优先战斗，清怪后走到最后怪物死亡位置拾取并"
            "原地驻守；若配置了绳子休息点，到期会按录制路线跳上对应绳子，"
            "爬到休息点执行倒计时。"
        )
    if plan.rest_point is not None and state.recorded_rest_on_rope:
        print(
            "[定时休息] JSON休息点位于绳子上：X={}，Y={}；"
            "对应绳子X={}，顶部Y={}，底部Y={}；每{:.1f}分钟进入一次，"
            "每次休息{:.1f}分钟；到期后走录制路线跳上对应绳子，"
            "爬到录制Y后休息。".format(
                plan.rest_point.x,
                plan.rest_point.y,
                state.recorded_rest_rope_x,
                state.recorded_rest_rope_top_y,
                state.recorded_rest_rope_bottom_y,
                rope_rest_interval_seconds / 60.0,
                rope_rest_duration_seconds / 60.0,
            )
        )
    elif plan.rest_point is not None:
        print(
            "[定时休息] 使用JSON休息点：X={}，Y={}，起跳平台Y={}；"
            "每{:.1f}分钟进入一次，每次休息{:.1f}分钟；"
            "到期后先走到对应平台，再对齐X并跳跃进入。".format(
                plan.rest_point.x,
                plan.rest_point.y,
                plan.rest_point.approach_y,
                rope_rest_interval_seconds / 60.0,
                rope_rest_duration_seconds / 60.0,
            )
        )
    elif (
        state.rope_rest_fallback_enabled
        and rope_rest_interval_seconds > 0
        and rope_rest_duration_seconds > 0
    ):
        print(
            "[定时休息] 当前JSON未录制休息点，已找到{}段有效绳子；"
            "到期后在下一段绳子的动态中点休息。".format(
                recorded_rope_segment_count
            )
        )
    elif rope_rest_interval_seconds > 0 and rope_rest_duration_seconds > 0:
        print("[定时休息] 当前JSON未录制休息点，也没有有效绳子段，本路线不安排定时休息。")
    if variant.platform_patrol:
        print(
            "[自定义录制路线] 已识别单平台往返范围：X={}~{}；录制方向不影响左右循环。".format(
                variant.platform_min_x,
                variant.platform_max_x,
            )
        )
    elif state.single_active_platform_patrol_enabled:
        print(
            "[自定义录制路线] 已关闭平台轮换：当前唯一有效平台为平台{}。".format(
                state.single_active_platform_number
            )
        )
    elif platform_count > 1:
        print(
            "[自定义录制路线] 已启用多平台智能路线：{}个平台；按完整上行/逐层回程连接刷图，边缘停滞会自动切换连接或转回平台。".format(
                platform_count
            )
        )
    elif not variant.closed_loop:
        print("[自定义录制路线] 当前JSON未形成闭环，到达最后录制点后会停止移动。")
    if not combat_logic.wait_for_runtime_ready(runtime):
        return
    try:
        while not runtime.已请求停止():
            if not runtime.可中断等待(ROUTE_LOOP_SECONDS, interval=0.01):
                break
            _sync_focus_pause_clock(runtime, state)
            position = runtime.读取人物位置()
            if position is None:
                if state.rope_resting:
                    _release_rope_rest_keys(runtime, state)
                else:
                    _release_all_keys(runtime, state, "position_missing")
                _handle_missing_position(runtime, state, variant)
                continue
            if state.position_missing_started_at > 0:
                missing_seconds = max(
                    0.0,
                    time.monotonic() - state.position_missing_started_at,
                )
                state.position_missing_started_at = 0.0
                state.position_missing_last_relocate_at = 0.0
                state.last_route_progress_at = time.monotonic()
                state.route_command_progress_at = state.last_route_progress_at
                runtime.trace_event(
                    "recorded_route_position_restored",
                    position=position,
                    missing_ms=round(missing_seconds * 1000.0, 1),
                    action="resume_route_after_relocation",
                )
            _consume_rope_rest_test_request(runtime, state)
            _update_rope_rest_schedule(runtime, state)
            _maintain_rest_point_test_control(runtime, state)
            _maintain_rest_navigation_control(runtime, state)
            if state.rest_point_test_parked:
                # 手动测试成功后继续独占控制并释放所有动作键，让人物保持在
                # 当前绳子/休息点，直到用户主动停止任务。
                _release_rope_rest_keys(runtime, state)
                if _rest_point_test_park_is_valid(state, position):
                    continue
                runtime.trace_event(
                    "recorded_route_rest_point_test_park_interrupted",
                    position=position,
                    reason="left_parked_rest_point_after_success",
                    action="replan_and_retry_test_rest_point",
                )
                _rearm_recorded_rest_navigation(
                    runtime,
                    state,
                    position,
                    reason="test_park_interrupted",
                    remaining_seconds=0.0,
                )
                continue
            runtime.记录人物位置(
                position,
                interval=0.2,
                route="recorded_route",
                route_file=plan.path.name,
                route_index=state.route_index,
                route_points=len(variant.points),
                route_variant=variant.name,
                closed_loop=variant.closed_loop,
                phase=state.combat.phase,
                platform_replan_active=state.platform_replan_active,
                platform_replan_source_number=state.platform_replan_source_number,
                platform_replan_target_number=state.platform_replan_target_number,
                platform_rotation_active=state.platform_rotation_active,
                platform_rotation_source_key=state.platform_rotation_source_key,
                walk_off_active=state.walk_off_active,
                walk_off_direction=state.walk_off_direction,
                walk_off_target_platform_number=(
                    state.walk_off_target_platform_number
                ),
                rope_rest_pending=state.rope_rest_pending,
                rope_resting=state.rope_resting,
            )
            if (
                plan.rest_point is not None
                and not state.recorded_rest_on_rope
                and state.recorded_rest_phase in ("jumping", "resting")
                and _apply_recorded_rest_point(
                    runtime,
                    state,
                    plan.rest_point,
                    position,
                )
            ):
                continue
            if (
                _single_platform_exit_blocked(state, variant, position)
                and (
                    state.active_rope_x is not None
                    or state.rope_top_exit_pending
                    or state.walk_off_active
                    or state.connection_priority_active
                )
            ):
                _pin_single_platform_route(
                    runtime,
                    state,
                    variant,
                    position,
                    (
                        state.connection_priority_route_index
                        if state.connection_priority_route_index is not None
                        else state.route_index
                    ),
                )
                continue
            if _apply_rope_rest(runtime, state, position):
                continue
            if _apply_rope_top_exit(runtime, state, variant, position):
                continue
            if (
                state.rope_rest_test_active
                and plan.rest_point is not None
                and not state.recorded_rest_on_rope
                and _apply_recorded_rest_point(
                    runtime,
                    state,
                    plan.rest_point,
                    position,
                )
            ):
                continue
            if state.active_rope_x is None:
                # 支持人物启动时已经挂在绳身上，也支持卡绳重定位清空状态后，
                # 直接根据人物坐标和JSON绳子几何重新接管，无需回到底部再按C。
                if not _single_platform_exit_blocked(state, variant, position):
                    _latch_rope_from_current_position(
                        runtime,
                        state,
                        variant,
                        position,
                    )
            if state.active_rope_x is not None:
                # 一旦起跳进入绳子承诺阶段，就锁定挂绳和爬绳流程，避免战斗帧
                # 释放方向/上键导致已经靠近绳子的角色再次掉回平台。
                combat_logic.clear_combat_for_movement(runtime, state.combat)
                if _apply_active_rope(runtime, state, position):
                    continue
            if _maintain_walk_off(runtime, state, variant, position):
                continue
            _emit_route_monitor(runtime, state, variant, position)
            if _apply_route_liveness_watchdog(
                runtime,
                state,
                variant,
                position,
            ):
                continue
            if (
                not state.rope_rest_test_active
                and _apply_ignored_platform_route(
                    runtime,
                    state,
                    variant,
                    position,
                    preferred_route_index=state.route_index,
                )
            ):
                continue
            if not _rest_route_navigation_active(state) and runtime.zant == 2:
                _release_all_keys(runtime, state, "combat_wait")
                runtime.等待战斗恢复()
                continue
            if (
                not _rest_route_navigation_active(state)
                and runtime.攻击移动仍锁定()
            ):
                _release_all_keys(runtime, state, "move_lock")
                continue
            platform_rotation_active = _update_platform_rotation(
                runtime,
                state,
                variant,
                position,
            )
            _emit_route_monitor(runtime, state, variant, position)
            previous_index = state.route_index
            selected_index = _select_route_index(runtime, state, variant, position)
            variant, selected_index = _switch_variant_after_cycle(
                runtime,
                plan,
                state,
                variant,
                previous_index,
                selected_index,
                position,
            )
            connection_priority = False
            if (
                not state.rope_rest_test_active
                and not state.rope_rest_pending
                and not _rest_route_navigation_active(state)
            ):
                connection_priority = _update_connection_priority(
                    runtime,
                    state,
                    variant,
                    position,
                    selected_index,
                )
            if _maintain_walk_off(runtime, state, variant, position):
                continue
            route_resume_grace = _route_resume_grace_active(state)
            if (
                RECORDED_ROUTE_SMART_SEEK_MODE
                and not state.rope_rest_pending
                and not connection_priority
                and not platform_rotation_active
                and not route_resume_grace
                and _apply_smart_seek_priority(
                    runtime,
                    state,
                    variant,
                    position,
                )
            ):
                continue
            # 智能寻怪会在确认无怪后立即归还路线；卡死恢复流程仍可能开启
            # 临时路线保护，因此后续分支必须读取本轮最新状态。
            route_resume_grace = _route_resume_grace_active(state)
            if (
                plan.rest_point is not None
                and not state.recorded_rest_on_rope
                and _apply_recorded_rest_point(
                    runtime,
                    state,
                    plan.rest_point,
                    position,
                )
            ):
                continue
            if _rest_route_navigation_active(state):
                # 测试及正常定时休息途中都只允许JSON路线把人物送回目标平台/
                # 绳子。跳过战斗、战后拾取、平台全覆盖和随机AI动作；若受击
                # 掉层，则使用全图重定位继续同一轮休息任务。
                state.last_ignored_chase_signature = None
                if variant.platform_patrol:
                    _apply_platform_patrol(runtime, state, variant, position)
                else:
                    _apply_recorded_command(
                        runtime,
                        state,
                        variant,
                        position,
                        selected_index,
                    )
                continue
            if not connection_priority and not route_resume_grace:
                snapshot = combat_logic.read_monster_snapshot(runtime)
                intent = combat_logic.build_action_intent(state.combat, snapshot)
                combat_timed_out = _combat_lock_timed_out(
                    runtime,
                    state,
                    snapshot,
                    intent,
                    position,
                )
                if intent.source == "combat" and intent.attackable_count > 0:
                    if combat_timed_out:
                        intent = combat_logic.build_action_intent(
                            state.combat,
                            snapshot,
                        )
                    else:
                        _remember_single_platform_monster_target(
                            state,
                            variant,
                            position,
                            intent,
                        )
                        state.single_platform_idle_active = False
                        state.single_platform_loot_target_x = None
                        state.single_platform_loot_started_at = 0.0
                        state.route_command_direction = None
                        state.route_command_observed_direction = None
                        state.route_command_anchor_x = None
                        _apply_vertical(runtime, state, "none")
                        decision_changed = combat_logic.trace_action_decision(
                            runtime,
                            state.combat,
                            intent,
                            position,
                        )
                        combat_logic.apply_action_intent(
                            runtime,
                            state.combat,
                            intent,
                            position,
                            decision_changed,
                        )
                        continue
                if intent.source in ("chase", "chase_hold"):
                    ignored_signature = (
                        intent.source,
                        intent.target_direction,
                        round(float(intent.nearest_dx), 1) if intent.nearest_dx is not None else None,
                    )
                    if ignored_signature != state.last_ignored_chase_signature:
                        state.last_ignored_chase_signature = ignored_signature
                        runtime.trace_event(
                            "recorded_route_chase_ignored",
                            position=position,
                            source=intent.source,
                            target_direction=intent.target_direction,
                            nearest_dx=intent.nearest_dx,
                            action="continue_json_route",
                        )
                    state.combat.chase_active = False
                    state.combat.chase_direction = None
                    state.combat.chase_direction_nearest_dx = None
                    state.combat.near_chase_pending_at = 0.0
                    state.combat.near_chase_pending_direction = None
                else:
                    state.last_ignored_chase_signature = None
            else:
                state.last_ignored_chase_signature = None
            # 上面的战斗判断与真正下发路线方向键之间，检测线程可能刚好发布
            # 新目标。起步前再复核一次，避免日志里出现 attack_pending/nearby
            # 已非零，路线仍先走 40~200ms 再被战斗立即刹停的短促抽动。
            if (
                RECORDED_ROUTE_SMART_SEEK_MODE
                and not state.rope_rest_pending
                and not connection_priority
                and not platform_rotation_active
                and not route_resume_grace
                and _apply_final_route_combat_handoff_guard(
                    runtime,
                    state,
                    variant,
                    position,
                )
            ):
                continue
            if _apply_single_platform_loot_or_idle(
                runtime,
                state,
                variant,
                position,
            ):
                continue
            if _apply_single_unignored_platform_patrol(
                runtime,
                state,
                variant,
                position,
            ):
                continue
            if _apply_platform_full_coverage(
                runtime,
                state,
                variant,
                position,
            ):
                continue
            if variant.platform_patrol:
                if not _apply_single_platform_loot_or_idle(
                    runtime,
                    state,
                    variant,
                    position,
                ):
                    _apply_platform_patrol(runtime, state, variant, position)
            else:
                _apply_recorded_command(runtime, state, variant, position, selected_index)
                _maybe_apply_platform_ai_action(
                    runtime,
                    state,
                    variant,
                    position,
                    selected_index=selected_index,
                )
    finally:
        if state.rope_rest_test_active or getattr(
            runtime,
            "休息点测试进行中",
            False,
        ):
            _leave_rest_point_test_control(
                runtime,
                state,
                reason="recorded_route_stopped_during_rest_point_test",
            )
            state.rope_rest_test_active = False
        combat_logic.finish_combat(runtime, state.combat)
        _release_all_keys(runtime, state, "recorded_route_stopped")
        runtime.trace_event("recorded_route_stopped", route_path=str(plan.path))


__all__ = ("run_recorded_route",)

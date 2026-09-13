"""多个地图路线可以复用的移动、跳跃和攀爬动作。

原版和 V2 共用同一套爬绳按键时序，避免两个入口的绳子行为
再次分叉。
"""


def run_mushroom_legacy_climb(runtime, flow="mushroom_v1"):
    """执行“蘑菇地图1”已验证的 C+上、左移对齐和持续爬绳时序。"""
    entry_position = runtime.读取人物位置()
    runtime.trace_event(
        "mushroom_shared_climb_started",
        flow=flow,
        position=entry_position,
    )
    runtime.释放水平移动键(reason="climb")
    try:
        for attempt in range(1, 21):
            if runtime.已请求停止():
                return False
            before_position = runtime.读取人物位置()
            runtime.pydirectinput.keyDown("c")
            runtime.pydirectinput.keyUp("c")
            runtime.pydirectinput.keyDown("up")
            if not runtime.可中断等待(0.3, interval=0.02):
                return False
            runtime.pydirectinput.keyUp("up")
            if not runtime.可中断等待(0.1, interval=0.02):
                return False

            current_position = runtime.读取人物位置()
            runtime.trace_event(
                "mushroom_shared_climb_attempt",
                flow=flow,
                attempt=attempt,
                before_position=before_position,
                after_position=current_position,
            )
            if current_position is None:
                continue
            runtime.记录人物位置(
                current_position,
                interval=0.1,
                route=flow,
                phase="climb",
                climb_attempt=attempt,
                rope_zone=True,
            )
            if runtime.is_within_x_range(185, current_position[1], error=2):
                runtime.pydirectinput.keyDown("left")
                if not runtime.可中断等待(0.2, interval=0.02):
                    runtime.pydirectinput.keyUp("left")
                    return False
                runtime.pydirectinput.keyUp("left")
            else:
                runtime.trace_event(
                    "mushroom_shared_climb_aligned",
                    flow=flow,
                    attempt=attempt,
                    position=current_position,
                )
                break

        runtime.pydirectinput.keyDown("up")
        if not runtime.可中断等待(1.5, interval=0.05):
            return False
        runtime.pydirectinput.keyUp("up")

        蓝条x = int(623 + 30 / 100 * 100)
        try:
            if runtime.is_white_pixel(蓝条x, 826):
                runtime.trace_event(
                    "mushroom_shared_mana_recovery_started",
                    flow=flow,
                )
                if not runtime.可中断等待(60, interval=0.1):
                    return False
        except Exception as exc:
            runtime.trace_event(
                "mushroom_shared_mana_check_error",
                flow=flow,
                message=str(exc),
            )

        runtime.pydirectinput.keyDown("up")
        if not runtime.可中断等待(3, interval=0.05):
            return False
        runtime.pydirectinput.keyUp("up")
        runtime.trace_event(
            "mushroom_shared_climb_completed",
            flow=flow,
            entry_position=entry_position,
            final_position=runtime.读取人物位置(),
        )
        return True
    finally:
        runtime.pydirectinput.keyUp("left")
        runtime.pydirectinput.keyUp("right")
        runtime.pydirectinput.keyUp("up")
        runtime.pydirectinput.keyUp("c")


__all__ = ("run_mushroom_legacy_climb",)

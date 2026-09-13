"""各地图的移动路线定义。

本模块只保存路线函数源码。兼容引擎加载时会把这些函数重新绑定到引擎的
全局运行上下文，使旧路线仍能共享人物坐标、停止事件、按键和战斗状态。
"""
import pydirectinput

from v3.legacy_engine import *


def 蘑菇地图V2():
    """调用独立的蘑菇 V2 巡逻、战斗和爬绳流程。"""
    return run_mushroom_v2(sys.modules[__name__])


def 蘑菇地图V3():
    """调用按有序录制坐标回放的蘑菇V3路线和丝滑战斗流程。"""
    return run_mushroom_v3(sys.modules[__name__])


def 自定义录制路线():
    """调用独立的通用JSON路线回放器，不套用任何具体地图规则。"""
    return run_recorded_route(sys.modules[__name__])


def 蘑菇地图1():
    """执行带战斗抗抖的“蘑菇”左右巡逻和平台动作。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线 = 1
    已应用移动方向 = None
    上次爬绳时间 = 0.0

    print("[蘑菇] 路线启动，战斗期间保持原地。")
    trace_event("mushroom_route_started")
    if not 可中断等待(2):
        return
    try:
        while not 已请求停止():
            if not 可中断等待(0.03, interval=0.01):
                break

            r_pos = 读取人物位置()
            if r_pos is not None:
                记录人物位置(r_pos)

            if zant == 1:
                if 已应用移动方向 is not None:
                    释放水平移动键()
                    已应用移动方向 = None
                处理战斗暂停()
                _最近近怪时间, 最近怪物方向, _当前近怪数量 = 读取战斗感知()
                if 最近怪物方向 == "left":
                    路线 = 1
                elif 最近怪物方向 == "right":
                    路线 = 2
                trace_event(
                    "mushroom_combat_released",
                    resume_direction=("left" if 路线 == 1 else "right"),
                )
                # 战斗方法返回后必须进入下一轮重新判定，禁止同一轮立刻按移动键。
                continue

            if zant == 2:
                等待战斗恢复()
                continue

            if 攻击移动仍锁定():
                if 已应用移动方向 is not None:
                    释放水平移动键()
                    已应用移动方向 = None
                continue

            if r_pos is not None:
                if r_pos[0] <= 25 and 路线 != 2:
                    路线 = 2
                    trace_event("mushroom_boundary", side="left", position=r_pos)
                elif r_pos[0] >= 169 and 路线 != 1:
                    路线 = 1
                    trace_event("mushroom_boundary", side="right", position=r_pos)

            目标移动方向 = "left" if 路线 == 1 else "right"
            if 目标移动方向 != 已应用移动方向:
                切换持续移动(目标移动方向)
                已应用移动方向 = 目标移动方向

            if r_pos is None:
                continue

            # 人物经过绳子附近时就允许进入原版爬绳动作；动作内部会用
            # 小步左移完成最终对齐，因此这里不能额外限制巡逻方向。
            if (
                25 <= r_pos[0] <= 50
                and 168 <= r_pos[1] <= 202
                and time.monotonic() - 上次爬绳时间 >= 5.0
            ):
                上次爬绳时间 = time.monotonic()
                trace_event(
                    "mushroom_climb_started",
                    position=r_pos,
                    route=路线,
                    movement_direction=已应用移动方向,
                )
                已应用移动方向 = None
                爬绳完成 = run_mushroom_legacy_climb(
                    sys.modules[__name__],
                    flow="mushroom_v1",
                )
                if not 爬绳完成 and 已请求停止():
                    break

    except KeyboardInterrupt:
        if stop_event is not None:
            stop_event.set()
    finally:
        # 无论是用户停止还是爬绳等待被中断，都不留下按住的纵向键。
        pydirectinput.keyUp('up')
        pydirectinput.keyUp('c')
        释放水平移动键()
        trace_event("mushroom_route_stopped")


def 蘑菇地图1半图():
    """执行“蘑菇半层”地图的半图往返路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:

            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()

            if 路线==1:
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==2:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                target_center_x = 20 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("左边到达目标的X坐标在范围内！")
                    路线=2
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)
                target_center_x = 174# 目标的中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("右边到达目标的X坐标在范围内！")
                    路线=1
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 蘑菇地图1半图上():
    """执行“蘑菇半层上层”及模板备用模式共用路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    print("蘑菇地图1半图上")
    time.sleep(1)
    try:
        while True:

            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()

            if 路线==1:
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==2:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                target_center_x = 60 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("左边到达目标的X坐标在范围内！")
                    路线=2
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)
                target_center_x = 126# 目标的中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("右边到达目标的X坐标在范围内！")
                    路线=1
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 自定义路线(左边位置, 右边位置):
    """使用公共V2优化识别与攻击运行用户记录的左右边界路线。"""
    return run_custom_boundary_route(
        sys.modules[__name__],
        左边位置,
        右边位置,
    )


def 木面2地图2():
    """执行“木面2”地图的分层循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global didd闪烁
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.1)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant==1:
                # 木面2原先在这里维护了一套独立的轮询攻击循环，容易绕过攻击锁、
                # 冷却和旧意图清理；统一走公共战斗处理后，死亡动画过滤对该图也生效。
                处理战斗暂停()

            if 路线==22:
                print('-----------------------------------------继续左走2',zant)
                pydirectinput.press('left')
            if 路线==222:
                pydirectinput.press('right')
            if 路线==2222:
                pydirectinput.press('right')
            if 路线==22222:
                pydirectinput.press('left')
            if 路线==8:
                print('-----------------------------------------继续左走',zant)
                pydirectinput.press('left')
            if 路线==1:
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==88:
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:

                target_center = (46, 170)  # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("目标在范围内！")
                    for dsfj in range(1):
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')

                target_center = (133, 163)  # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("目标在范围内！")
                    for dsfj in range(1):
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')

                target_center = (61, 165)  # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("目标在范围内！")
                    for dsfj in range(1):
                        if didd闪烁==1:
                            pydirectinput.keyDown('v')
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('v')
                        pydirectinput.keyUp('c')




                target_center_x = 15 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("左边到达目标的X坐标在范围内！")
                    路线=2
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)
                target_center_x = 180# 目标的中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("右边到达目标的X坐标在范围内！")
                    路线=1
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)

                target_center = (153, 163)  # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("右边到达目标的X坐标在范围内！")
                    路线=8
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)
                target_center = (88,183)  # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("右边到达目标的X坐标在范围内！")
                    路线 = 88
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)

                if 路线==222:

                    target_center = (48, 230) # 目标的中心点
                    if is_within_range(target_center, renwu_pos, error=4):
                        print("2222222222222222222目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(2)
                            pydirectinput.keyUp('up')
                            路线=2222

                target_center = (150, 217) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("右边到达目标的X坐标在范围内！")
                    路线 = 22222
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)
                if 路线==1:
                    target_center = (76, 248) # 目标的中心点
                    if is_within_range(target_center, renwu_pos, error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)

                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(2)
                            pydirectinput.keyUp('up')
                            路线=22
                if 路线==22:
                    target_center_x = 24  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("右边到达目标的X坐标在范围内！")
                        路线 = 222
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.press('right')
                        time.sleep(0.1)
                target_center = (140, 186) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=3):
                    print("目标在范围内！")
                    pydirectinput.keyDown('right')
                    time.sleep(0.2)
                    pydirectinput.keyDown('c')
                    time.sleep(0.1)
                    pydirectinput.keyUp('c')

                if 路线==2:
                    target_center = (20, 248) # 目标的中心点
                    if is_within_range(target_center, renwu_pos, error=4):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(4)
                            pydirectinput.keyUp('up')
                            路线=2
                    target_center = (20, 209) # 目标的中心点
                    if is_within_range(target_center, renwu_pos, error=4):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(4)
                            pydirectinput.keyUp('up')
                            路线=2
                        pydirectinput.keyDown('right')
                        time.sleep(0.2)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')





            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 天使猴子():
    """执行“天使猴子”地图的循环路线。"""

    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global 发现轮
    global renwu_pos
    zant = 0
    路线=1
    zhuanquancishu=0
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.1)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==3:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('right')
            if 路线==112:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('right')
            if 路线==111:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==113:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==1:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==2:
                print('-----------------------------------------继续右走',zant)
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                pydirectinput.keyDown('right')
            if 路线==11:
                print('-----------------------------------------继续右走',zant)
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:
                print("发现轮路线次数 ----------------------------------------",zhuanquancishu)
                target_center_x = 81 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("-------------左边到达目标的X坐标在范围内！")

                    路线 = 2
                    if zhuanquancishu>4:
                        if 发现轮==1:
                            路线=111
                            print("-------------左边到达目标的X坐标在范围内！2222")
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            pydirectinput.keyDown('left')
                        else:
                            zhuanquancishu =0
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            pydirectinput.keyDown('right')
                    else:
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')



                    time.sleep(0.5)
                if 路线==111:
                    target_center_x =43  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        zhuanquancishu = zhuanquancishu + 1
                        for dsfj in range(1):
                            pydirectinput.keyDown('down')
                            time.sleep(0.1)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyUp('down')
                            time.sleep(1.1)
                        路线 = 112

                        pydirectinput.keyDown('right')
                        time.sleep(0.2)
                if 路线==112:
                    target_center_x =79  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        zhuanquancishu = zhuanquancishu + 1
                        for dsfj in range(1):
                            pydirectinput.keyDown('down')
                            time.sleep(0.1)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyUp('down')
                            time.sleep(1.1)
                        路线 = 1
                        zhuanquancishu =0
                        pydirectinput.keyDown('left')
                        time.sleep(0.2)
                target_center_x = 175# 目标的中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("右边到达目标的X坐标在范围内！")
                    路线=1
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('left')
                    time.sleep(0.1)




                target_center = (81, 187) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=6):
                    print("记录点目标在范围内！")
                    zhuanquancishu=zhuanquancishu+1




                target_center = (20, 268) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=7):
                    print("跳跃点目标在范围内！")
                    路线=3
                    zhuanquancishu = 0
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)


                target_center = (81, 234) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=5):
                    print("跳跃点目标在范围内！")
                    路线=3
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('right')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)

                target_center = (131, 265) # 目标的中心点
                if is_within_range(target_center, renwu_pos, error=5):
                    print("跳跃点目标在范围内！")
                    for dsfj in range(1):
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(0.1)
                        pydirectinput.keyDown('up')
                        time.sleep(0.2)
                        pydirectinput.keyUp('up')
                        time.sleep(0.1)
                        print("任务位置是", renwu_pos)
                        pydirectinput.keyDown('up')
                        time.sleep(11)
                        pydirectinput.keyUp('up')
                        time.sleep(0.2)
                        pydirectinput.keyDown('right')
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(1.2)
                        pydirectinput.keyUp('right')
                        路线 = 11





                if 路线==3:
                    target_center_x = 128  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("直接起跳=======目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(6.6)
                            pydirectinput.keyUp('up')
                            time.sleep(0.2)
                            pydirectinput.keyDown('right')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(1.2)
                            pydirectinput.keyUp('right')
                            路线=11

                if r_pos[1]<140:
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("直接起跳=======目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            print("任务位置是",renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(6.6)
                            pydirectinput.keyUp('up')
                            time.sleep(0.2)
                            pydirectinput.keyDown('right')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(1.2)
                            pydirectinput.keyUp('right')
                            路线=1


            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 军营2():
    """执行“军营2”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global didd闪烁
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线 = 1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()

            if 路线 == 8:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('right')
            if 路线 == 7:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('left')
            if 路线 == 6:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('left')
            if 路线 == 5:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('right')
            if 路线 == 4:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('left')
            if 路线 == 3:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('right')
            if 路线 == 1:
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                print('-----------------------------------------继续左走', zant)
                pydirectinput.keyDown('left')
            if 路线 == 2:
                print('-----------------------------------------继续右走', zant)
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                if 路线 == 1:
                    target_center_x =59 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("直接起跳路线1=======目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.7)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.7)
                            pydirectinput.keyDown('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            print("任务位置是", renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(2.8)
                            pydirectinput.keyUp('up')
                            time.sleep(0.2)
                            路线 = 2

                if 路线 == 2:
                    target_center_x =76  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("直接起跳路线1=======目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            print("任务位置是", renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.2)
                            pydirectinput.keyDown('right')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(1.2)
                            pydirectinput.keyUp('right')
                            路线 = 3

                if 路线 == 3:
                    target_center_x =110  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.2)
                        路线 = 4
                if 路线 == 4:
                    target_center_x =83  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("直接起跳路线1=======目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            print("任务位置是", renwu_pos)
                            pydirectinput.keyDown('up')
                            time.sleep(4.8)
                            pydirectinput.keyUp('up')
                            time.sleep(0.2)
                            路线 = 5


                if 路线 == 5:
                    target_center_x =110  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.2)
                        路线 =6

                if 路线 == 6:
                    target_center_x =79  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=1):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(0.2)
                        if didd闪烁==1:
                            pydirectinput.keyDown('up')
                            time.sleep(0.1)
                            pydirectinput.keyDown('v')
                            time.sleep(0.1)
                            pydirectinput.keyUp('v')
                            pydirectinput.keyUp('up')
                            time.sleep(0.5)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(5.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                        路线 = 7


                if 路线 == 7:
                    target_center_x =50  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.2)
                        路线 =8

                if 路线 == 8:
                    target_center_x =110  # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(0.2)
                        for dsfj in range(4):
                            pydirectinput.keyDown('down')
                            time.sleep(0.1)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyUp('down')
                            time.sleep(0.1)
                        路线 =1





            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()
    pass


def 通话妙月兔():
    """执行“通话妙月兔2”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break



            time.sleep(0.1)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()

            if 路线==1:
                print('-----------------------------------------继续左走',zant)
                pydirectinput.keyDown('left')
            if 路线==2:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                if 路线 == 2:
                    target_center_x = 225 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=1
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
                target_center_x = 20 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("左边到达目标的X坐标在范围内！")
                    路线=2
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyUp('left')
                    pydirectinput.keyDown('right')
                    time.sleep(0.1)
                if 路线 == 1:
                    target_center_x = 201 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("右边到达目标的X坐标在范围内！")
                        路线=1
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        pydirectinput.keyUp('c')
                        time.sleep(0.1)
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 林中石头人():
    """执行“林中石头人”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==22:
                pydirectinput.press('left')
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.press('right')
            if 路线==4:
                pydirectinput.press('left')
            if 路线==5:
                pydirectinput.keyDown('left')
            if 路线 == 6:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                if 路线 == 2:
                    target_center_x = 77 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                        pydirectinput.keyDown('up')
                        time.sleep(3.2)
                        pydirectinput.keyUp('up')
                        路线=22

                if 路线 == 22:
                    target_center_x = 46  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线 = 3
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)
                if 路线 == 3:
                    target_center_x = 153  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线 = 4
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
                if 路线 ==4:
                    target_center_x = 108  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                        pydirectinput.keyDown('up')
                        time.sleep(3.2)
                        pydirectinput.keyUp('up')
                        路线=5

                if 路线 ==5:
                    target_center_x = 82  # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(0.1)
                            pydirectinput.keyDown('up')
                            time.sleep(0.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                        pydirectinput.keyDown('up')
                        time.sleep(3.2)
                        pydirectinput.keyUp('up')
                        路线=6

                if 路线==1:
                    target_center_x = 20 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=2
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)
                if 路线 == 6:
                    target_center_x = 173 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("右边到达目标的X坐标在范围内！")
                        路线=1
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 林中怪猫():
    """执行“林中怪猫”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:

            if 已请求停止():
                释放水平移动键()
                break
            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==22:
                pydirectinput.press('left')
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.keyDown('left')
            if 路线==4:
                pydirectinput.keyDown('right')


            if zant == 2:
                等待战斗恢复()
            try:
                if 路线==2:
                    target_center_x = 140 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=3
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
                if 路线==1:
                    target_center_x = 21 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=2
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.press('right')
                        time.sleep(0.1)

                if 路线 == 2:
                    target_center_x = 32 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            time.sleep(0.2)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.2)


                            pydirectinput.keyDown('up')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(2.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(2.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                if 路线 == 3:
                    target_center_x = 123 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            pydirectinput.keyDown('up')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(2.5)
                            pydirectinput.keyUp('up')
                            pydirectinput.keyDown('left')
                            time.sleep(1.1)


                if 路线 == 3:
                    target_center_x = 21 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("右边到达目标的X坐标在范围内！")
                        路线=4
                        for dsfj in range(1):
                            pydirectinput.keyDown('down')
                            time.sleep(0.1)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyUp('down')
                            time.sleep(2.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('right')
                if 路线==4:
                    target_center_x = 140 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=1
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 林中青龙():
    """执行“林中青龙”模板检测地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==22:
                pydirectinput.press('left')
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.press('left')
            if 路线==4:
                pydirectinput.press('right')
            if 路线==5:
                pydirectinput.keyDown('left')
            if 路线==6:
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:
                if 路线==6:
                    target_center_x = 155 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=1
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)

                if 路线==1:
                    target_center_x = 18 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=2
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)

                if 路线 == 2:
                    target_center_x = 85 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)



                if 路线 == 5:
                    target_center_x = 132 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("右边到达目标的X坐标在范围内！")
                        路线=6
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                if 路线 == 4:
                    target_center_x = 135 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("路线4右边到达目标的X坐标在范围内！")
                        路线=5
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                        time.sleep(1.1)
                if 路线 == 2:
                    target_center_x = 135 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("路线2右边到达目标的X坐标在范围内！")
                        路线=3
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                if 路线==3:
                    target_center_x = 97 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=4
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)

                        pydirectinput.keyDown('right')
                        time.sleep(0.3)
                if 路线==5:
                    target_center_x = 100 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=6
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)

            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 天空蓝狮子():
    """执行“天空蓝狮子”模板检测地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.1)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.keyDown('left')
            if 路线==4:
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:

                if 路线==1:
                    target_center_x = 20# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=2
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)

                if 路线 == 2:
                    target_center_x = 69 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(6.2)
                            pydirectinput.keyUp('up')
                            time.sleep(0.1)
                        路线=3

                if 路线 == 4:
                    target_center_x = 197 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("路线4右边到达目标的X坐标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                        time.sleep(1.1)
                        路线=1

                if 路线 == 4:
                    target_center_x = 102 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("路线4右边到达目标的X坐标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.7)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.7)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.7)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.7)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.7)
                            pydirectinput.keyUp('c')

                if 路线==3:
                    target_center_x = 27 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=4
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)


            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 雪域冰原2大灰狼():
    """执行“雪域冰原2大灰狼”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.keyDown('left')
            if 路线==4:
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:
                if 路线==2:
                    target_center_x = 106# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=3
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('left')
                        time.sleep(0.1)
                if 路线==1:
                    target_center_x = 20# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("左边到达目标的X坐标在范围内！")
                        路线=2
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)

                if 路线 == 2:
                    target_center_x = 47 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("目标在范围内！")
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')




                if 路线 == 4:
                    target_center_x = 102 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("路线4右边到达目标的X坐标在范围内！")
                        路线 = 4
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.2)
                            pydirectinput.keyUp('up')


                if 路线==3:
                    target_center_x = 62 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("左边到达目标的X坐标在范围内！")
                        路线=4
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(3.2)
                            pydirectinput.keyUp('up')


                if 路线 == 4:
                    target_center_x = 265 # 目标的中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("路线4右边到达目标的X坐标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyDown('left')
                        time.sleep(1.1)
                        路线=1
            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 雪域冰原2黑雪人():
    """执行“雪域冰原2黑雪人”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==44:
                pydirectinput.press('left')
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.press('left')
            if 路线==4:
                pydirectinput.press('right')
            if 路线==5:
                pydirectinput.keyDown('left')
            if 路线==6:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:

                if 路线==1:
                    target_center = (108, 256) # 目标的中心点
                    if is_within_range(target_center, renwu_pos, error=3):
                        print("左边到达目标的X坐标在范围内！")

                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.2)
                            pydirectinput.keyUp('up')
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.5)
                            pydirectinput.keyUp('up')

                        pydirectinput.keyDown('right')
                        time.sleep(0.7)
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.9)
                            pydirectinput.keyUp('up')
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.5)
                            pydirectinput.keyUp('up')
                        pydirectinput.keyDown('left')
                        time.sleep(0.8)
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.5)
                            pydirectinput.keyUp('up')
                        pydirectinput.keyDown('right')
                        time.sleep(0.1)
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                        for dsfj in range(1):
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(1.2)
                            pydirectinput.keyUp('up')
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            time.sleep(2.2)
                            pydirectinput.keyUp('up')



                        路线=1




                if 路线 == 1:
                    target_center_x = 40 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=2
                if 路线 == 2:
                    target_center_x = 127 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=3
                if 路线 == 3:
                    target_center_x =40 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=4

                if 路线 == 4:
                    target_center_x =40 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=44

                if 路线 == 44:
                    target_center_x =126 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=5

                if 路线 == 6:
                    target_center_x =126# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=1

                if 路线 == 5:
                    target_center_x =40 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=6


            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 武陵迷雾森林():
    """执行“武陵迷雾森林”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break


            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)

            if zant == 1:
                处理战斗暂停()

            if 路线==44:
                pydirectinput.press('left')
            if 路线==1:
                pydirectinput.press('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==22:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.press('left')
            if 路线==33:
                pydirectinput.press('left')
            if 路线==4:
                pydirectinput.press('right')
            if 路线==5:
                pydirectinput.keyDown('left')
            if 路线==6:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:



                if 路线 == 1:
                    target_center_x = 20 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("线路1目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=2
                if 路线 == 2:
                    target_center_x = 73 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("线路2目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyDown('up')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(2.2)
                        pydirectinput.keyUp('up')
                        pydirectinput.keyDown('right')
                        路线=22

                if 路线 == 22:
                    target_center_x =145 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=3

                if 路线 == 33:
                    target_center_x =81 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=4
                if 路线 == 3:
                    target_center_x =127# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyDown('up')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(1.5)
                        pydirectinput.keyUp('up')
                        路线=33

                if 路线 == 4:
                    target_center_x = 107 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyDown('up')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(2.2)
                        pydirectinput.keyUp('up')
                        路线=5

                if 路线 == 5:
                    target_center_x =94# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=6


                target_center_x =165 # 目标的0中心x坐标
                if is_within_x_range(target_center_x, r_pos[0], error=5):
                    print("目标在范围内！")
                    pydirectinput.keyUp('right')
                    pydirectinput.keyUp('left')
                    路线=1


            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 武陵海盗船2():
    """执行“武陵海盗船2”模板检测地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    try:
        while True:
            if 已请求停止():
                释放水平移动键()
                break

            time.sleep(0.1)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==1:
                pydirectinput.keyDown('left')
            if 路线==2:
                pydirectinput.press('right')
            if 路线==22:
                pydirectinput.keyDown('right')
            if zant == 2:
                等待战斗恢复()
            try:
                if 路线 == 1:
                    target_center_x = 20 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("线路1目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=2
                if 路线 == 2:
                    target_center_x = 54# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        print("=============================================------------------------========线路2目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(0.5)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(0.5)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(0.5)

                        路线=22

                if 路线 == 22:
                    target_center_x =81 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("==================================================目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(0.5)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        time.sleep(0.5)
                        路线=2

                if 路线 == 2:
                    target_center_x =97# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyDown('down')
                        time.sleep(0.1)
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyUp('down')
                        time.sleep(1.1)
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=22

                if 路线 == 22:
                    target_center_x =186# 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=1


            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()


def 时间之路1():
    """执行“时间之路1”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    zant = 0
    路线=1
    while True:
        if 已请求停止():
            释放水平移动键()
            break
        time.sleep(0.1)
        r_pos = 读取人物位置()
        if zant == 1:
            处理战斗暂停()
        if 路线 == 44:
            pydirectinput.press('right')
        if 路线 == 1:
            pydirectinput.press('left')
        if 路线 == 2:
            pydirectinput.press('right')
        if 路线 == 22:
            pydirectinput.press('right')
        if 路线 == 3:
            pydirectinput.press('left')
        if 路线 == 4:
            pydirectinput.press('right')
        if 路线 == 44:
            pydirectinput.press('right')
        if 路线 == 5:
            pydirectinput.press('left')
        if 路线 == 6:
            pydirectinput.press('right')
        if 路线 == 7:
            pydirectinput.keyDown('left')
        if 路线 == 8:
            pydirectinput.press('right')
        if 路线 == 88:
            pydirectinput.keyDown('right')

        if zant == 2:
            等待战斗恢复()
        if 路线 == 5:
            target_center_x = 20  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                print("线路1目标在范围内！")
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 88
        if 路线 == 1:
            target_center_x = 20  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                print("线路1目标在范围内！")
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 2
        if 路线 == 2:
            target_center_x = 50  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=4):
                print("线路2目标在范围内！")
                pydirectinput.keyDown('c')
                time.sleep(0.1)
                pydirectinput.keyUp('c')
                pydirectinput.keyDown('up')
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                time.sleep(2.2)
                pydirectinput.keyUp('up')
                pydirectinput.keyDown('right')
                路线 = 22

        if 路线 == 22:
            target_center_x = 115  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=5):
                print("目标在范围内！")
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 3
        if 路线 == 7:
            target_center_x = 20  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 8
        if 路线 == 3:
            target_center_x = 20  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 4

        target_center_x = 165  # 目标的0中心x坐标
        if is_within_x_range(target_center_x, r_pos[0], error=2):
            pydirectinput.keyUp('right')
            pydirectinput.keyUp('left')
            路线 = 1
        if 路线 == 44:
            target_center_x = 95  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=2):
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 5
        if 路线 == 4:
            target_center_x = 28  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                print("目标在范围内！")
                pydirectinput.keyDown('c')
                time.sleep(0.1)
                pydirectinput.keyUp('c')
                pydirectinput.keyDown('up')
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                time.sleep(2.2)
                pydirectinput.keyUp('up')
                路线 = 44
        if 路线 == 8:
            target_center_x = 28  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                print("目标在范围内！")
                pydirectinput.keyDown('c')
                time.sleep(0.1)
                pydirectinput.keyUp('c')
                pydirectinput.keyDown('up')
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                time.sleep(2.2)
                pydirectinput.keyUp('up')
                路线 = 88
        if 路线 == 5:
            target_center_x = 50  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=3):
                print("目标在范围内！")
                pydirectinput.keyDown('c')
                time.sleep(0.1)
                pydirectinput.keyUp('c')
                pydirectinput.keyDown('up')
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                time.sleep(2.2)
                pydirectinput.keyUp('up')
                路线 = 6

        if 路线 == 6:
            target_center_x = 75  # 目标的0中心x坐标
            if is_within_x_range(target_center_x, r_pos[0], error=5):
                print("目标在范围内！")
                pydirectinput.keyUp('right')
                pydirectinput.keyUp('left')
                路线 = 7


def 火野猪1():
    """执行“火野猪1”地图的循环路线。"""
    global 单体按键, 群攻按键, 闪现按键
    global zant
    global 攻击
    global renwu_pos
    global stop_event2
    global 轮来了
    轮来了=1
    zant = 0
    路线=1
    try:
        while not stop_event.is_set():
            if 已请求停止():
                释放水平移动键()
                break


            time.sleep(0.05)
            r_pos = 读取人物位置()
            if r_pos:
                记录人物位置(r_pos)
            if zant == 1:
                处理战斗暂停()
            if 路线==1:
                pydirectinput.press('left')
                print("=============================================左走")
            if 路线==2:
                pydirectinput.press('right')
            if 路线==3:
                pydirectinput.keyDown('left')
            if 路线==4:
                pydirectinput.keyDown('right')

            if zant == 2:
                等待战斗恢复()
            try:
                if 路线 == 1:
                    if 轮来了==1:
                        target_center_x = 246  # 目标的0中心x坐标
                        if is_within_x_range(target_center_x, r_pos[0], error=5):
                            print("线路2目标在范围内！")
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            pydirectinput.keyDown('up')
                            time.sleep(2)
                            pydirectinput.keyUp('up')
                            路线 = 1
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)
                            pydirectinput.keyDown('c')
                            time.sleep(0.1)
                            pydirectinput.keyUp('c')
                            time.sleep(0.6)

                if 路线 == 1:
                    target_center_x = 18 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=2):
                        print("线路1目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=2
                if 路线 == 1:
                    target_center_x = 61 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("线路2目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.5)
                        pydirectinput.keyUp('c')



                        路线=1
                if 路线 == 2:
                    target_center_x = 45 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=3):
                        print("线路2目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.1)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyDown('up')
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        time.sleep(5.2)
                        pydirectinput.keyUp('up')
                        路线=3
                if 路线 == 3:
                    target_center_x =30 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线=4
                if 路线 == 4:
                    target_center_x = 263 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("目标在范围内！")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        路线 = 1

                if 路线 == 4:
                    target_center_x = 219 # 目标的0中心x坐标
                    if is_within_x_range(target_center_x, r_pos[0], error=5):
                        print("线路2目标在范围内！")
                        pydirectinput.keyDown('c')
                        time.sleep(0.5)
                        pydirectinput.keyUp('c')
                        pydirectinput.keyDown('c')
                        time.sleep(0.5)
                        pydirectinput.keyUp('c')



            except:
                pass

    except KeyboardInterrupt:
        stop_event.set()

ROUTE_HANDLER_NAMES = (
    '蘑菇地图V2',
    '蘑菇地图V3',
    '自定义录制路线',
    '蘑菇地图1',
    '蘑菇地图1半图',
    '蘑菇地图1半图上',
    '自定义路线',
    '木面2地图2',
    '天使猴子',
    '军营2',
    '通话妙月兔',
    '林中石头人',
    '林中怪猫',
    '林中青龙',
    '天空蓝狮子',
    '雪域冰原2大灰狼',
    '雪域冰原2黑雪人',
    '武陵迷雾森林',
    '武陵海盗船2',
    '时间之路1',
    '火野猪1',
)

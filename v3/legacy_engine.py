"""3.0 地图动作兼容引擎。

本模块保留 1.9 的地图坐标、路线和动作时序，并集中维护屏幕捕获、模型缓存、
模板加载、停止判断、人物位置读取、战斗暂停和按键释放等公共逻辑。

维护约定：
1. 地图函数只描述该地图独有的路线和坐标判断。
2. 跨地图重复的运行行为优先放进“地图运行公共方法”区域。
3. 检测循环必须检查停止信号，不允许使用没有休眠的忙等待。
4. 所有资源路径通过 get_base_dir() 解析，兼容源码和 PyInstaller。
"""

import base64
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import FunctionType

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ULTRALYTICS_CONFIG_DIR = os.path.join(PROJECT_ROOT, ".v3_runtime", "ultralytics")
MATPLOTLIB_CONFIG_DIR = os.path.join(PROJECT_ROOT, ".v3_runtime", "matplotlib")
os.makedirs(ULTRALYTICS_CONFIG_DIR, exist_ok=True)
os.makedirs(MATPLOTLIB_CONFIG_DIR, exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", ULTRALYTICS_CONFIG_DIR)
os.environ.setdefault("MPLCONFIGDIR", MATPLOTLIB_CONFIG_DIR)

import mss
import numpy as np
from ultralytics import YOLO
import cv2
import pydirectinput
import win32gui
# 测试 GET 方法
import requests
from PIL import ImageGrab

from .detection_diagnostics import log_template_detection_snapshot
from .public.monster_detection import (
    ATTACK_MOVE_LOCK_SECONDS,
    ATTACK_INTENT_TTL_SECONDS,
    COMBAT_RELEASE_GRACE_SECONDS,
    COMBAT_TARGET_LOST_CONFIRMATION_FRAMES,
    MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET,
    MONSTER_TEMPLATE_CONFIDENCE,
    POST_ATTACK_RECHECK_SECONDS,
    TEMPLATE_TARGET_FRAME_SECONDS,
    TEST_YOLO_TARGET_FRAME_SECONDS,
    YOLO_MAX_DETECTIONS,
    YOLO_MONSTER_CONFIDENCE,
    YOLO_TARGET_FRAME_SECONDS,
    custom_template_files,
    detect_and_associate_monster_health_bars,
    match_monster_templates_parallel,
    prepare_monster_template,
)
from .public.minimap_tracking import (
    CharacterTrajectoryRecorder,
    request_minimap_relocation,
)
from .facing_direction import CharacterFacingTracker
from .custom_route import next_custom_route_direction, run_custom_boundary_route
from .public.movement_actions import run_mushroom_legacy_climb
from .public import combat_controller
from .public.recorded_route_player import run_recorded_route
from .map.mushroom_v2 import run_mushroom_v2
from .map.mushroom_v3 import run_mushroom_v3
from .map import routes as map_routes
from .person_locator import (
    CharacterTracker,
    PERSON_MATCH_THRESHOLD,
    character_attack_reference_y,
    evaluate_attack_target,
    require_character_templates,
    select_character_template,
)
from .runtime_trace import trace_event


_capture_local = threading.local()
_model_cache = {}
_model_cache_lock = threading.Lock()
PYDIRECTINPUT_PAUSE_SECONDS = 0.01
ATTACK_DIRECTION_SETTLE_SECONDS = 0.04
ATTACK_KEY_HOLD_SECONDS = 0.05
# 自动群攻只看人物与怪物的屏幕 X 距离；左右各 150 像素内按运行阈值触发。
AUTO_GROUP_ATTACK_HORIZONTAL_RANGE = 150.0
CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE = 50.0
pydirectinput.PAUSE = PYDIRECTINPUT_PAUSE_SECONDS


def get_base_dir():
    """返回资源根目录，兼容源码运行与 PyInstaller 打包。"""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return PROJECT_ROOT


def 统计横向群攻怪物数量(character_x, monster_centers):
    """统计人物 X 左右自动群攻范围内的已识别怪物数量。"""
    return 统计指定横向范围怪物数量(
        character_x,
        monster_centers,
        AUTO_GROUP_ATTACK_HORIZONTAL_RANGE,
    )


def 统计指定横向范围怪物数量(character_x, monster_centers, horizontal_range):
    """统计人物 X 左右指定像素范围内的已识别怪物数量。"""
    character_x = float(character_x)
    horizontal_range = max(0.0, float(horizontal_range))
    return sum(
        1
        for center_x, _center_y in monster_centers
        if abs(float(center_x) - character_x)
        <= horizontal_range
    )


def 构建紧凑怪物坐标日志(person_x, person_y, monster_centers, limit=12):
    """记录与目标判定同坐标系的人物、怪物及相对坐标，限制日志体积。"""
    if person_x is None or person_y is None:
        absolute = [
            [int(round(float(center_x))), int(round(float(center_y)))]
            for center_x, center_y in list(monster_centers)[: max(0, int(limit))]
        ]
        return None, absolute, []
    px = float(person_x)
    py = float(person_y)
    ranked = sorted(
        monster_centers,
        key=lambda center: (
            abs(float(center[0]) - px),
            abs(float(center[1]) - py),
        ),
    )[: max(0, int(limit))]
    absolute = [
        [int(round(float(center_x))), int(round(float(center_y)))]
        for center_x, center_y in ranked
    ]
    relative = [
        [
            int(round(float(center_x) - px)),
            int(round(float(center_y) - py)),
        ]
        for center_x, center_y in ranked
    ]
    return [int(round(px)), int(round(py))], absolute, relative


def grab_screen(monitor_area):
    """每个工作线程复用一个 MSS 实例，避免每帧重复创建截图上下文。"""
    capture = getattr(_capture_local, "capture", None)
    if capture is None:
        capture = mss.mss()
        _capture_local.capture = capture
    return capture.grab(monitor_area)


def close_thread_capture():
    """关闭当前工作线程复用的 MSS/GDI 截图上下文。

    MSS 的 Windows 实现持有屏幕 DC、内存 DC 和兼容位图。线程退出后仅等待
    Python/线程局部对象回收，在虚拟机中可能让这些图形对象长时间滞留；每个
    工作线程结束时显式关闭，避免反复启停后累积虚拟显卡对象。
    """
    capture = getattr(_capture_local, "capture", None)
    if capture is not None:
        try:
            capture.close()
        except Exception:
            pass
        finally:
            try:
                delattr(_capture_local, "capture")
            except AttributeError:
                pass
    try:
        delattr(_capture_local, "full_preview_captured_at")
    except AttributeError:
        pass


def 获取怪物检测区域(map_name=None):
    """返回怪物检测区域；蘑菇V2/V3按游戏真实宽度扩展到最右侧。"""
    width = (
        max(游戏客户区宽度 + 游戏客户区屏幕左, 游戏窗口外框宽度)
        if map_name in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
        else 1280
    )
    return {"top": 300, "left": 0, "width": int(width), "height": 330}


def 捕获检测与预览画面(detection_monitor):
    """高频抓取检测区，并在截图模式下按较低频率补充完整窗口预览。"""
    if 测试截图模式:
        # Windows 虚拟机中的 MSS/GDI 全窗口抓取可能超过100ms。若每个识别帧
        # 都抓完整窗口，即使模板匹配只需几毫秒，检测也会被硬性限制在约9FPS。
        # 完整大图只服务页面预览；路线、人物和怪物识别始终可以使用下方检测区。
        当前时间 = time.monotonic()
        上次完整预览截图时间 = float(
            getattr(_capture_local, "full_preview_captured_at", 0.0)
        )
        需要完整预览 = (
            检测预览回调 is not None
            and 当前时间 - 上次完整预览截图时间
            >= 完整检测预览截图间隔
        )
        if not 需要完整预览:
            screenshot = grab_screen(detection_monitor)
            detection_img = cv2.cvtColor(
                np.asarray(screenshot),
                cv2.COLOR_BGRA2BGR,
            )
            return (
                detection_img,
                None,
                0,
                0,
                "left={left}, top={top}, width={width}, height={height} "
                "(检测区高频；完整预览限频)".format(**detection_monitor),
            )
        preview_width = max(
            int(detection_monitor["width"]),
            int(游戏窗口外框宽度),
        )
        preview_height = max(
            int(detection_monitor["top"] + detection_monitor["height"]),
            int(游戏窗口外框高度),
            int(游戏客户区屏幕顶 + 游戏客户区高度),
        )
        preview_monitor = {
            "top": 0,
            "left": 0,
            "width": preview_width,
            "height": preview_height,
        }
        screenshot = grab_screen(preview_monitor)
        preview_img = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)
        _capture_local.full_preview_captured_at = time.monotonic()
        offset_x = int(detection_monitor["left"] - preview_monitor["left"])
        offset_y = int(detection_monitor["top"] - preview_monitor["top"])
        detection_img = preview_img[
            offset_y:offset_y + int(detection_monitor["height"]),
            offset_x:offset_x + int(detection_monitor["width"]),
        ].copy()
        return (
            detection_img,
            preview_img,
            offset_x,
            offset_y,
            "left=0, top=0, width={}, height={}".format(
                preview_width,
                preview_height,
            ),
        )
    screenshot = grab_screen(detection_monitor)
    detection_img = cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)
    return (
        detection_img,
        detection_img,
        0,
        0,
        "left={left}, top={top}, width={width}, height={height}".format(
            **detection_monitor
        ),
    )


def 发布检测预览(img, info, 人物局部坐标=None, 怪物框列表=None):
    """限频转发检测线程已经截取的画面，避免为页面预览重复截图和重复推理。"""
    global 上次检测预览时间
    callback = 检测预览回调
    if callback is None or img is None:
        return
    当前时间 = time.monotonic()
    with 检测预览锁:
        if 当前时间 - 上次检测预览时间 < 检测预览帧间隔:
            return
        上次检测预览时间 = 当前时间
    try:
        preview = img.copy()
        (红量检测点, 蓝量检测点) = 获取红蓝检测坐标()
        for label, point, color in (
            ("HP", 红量检测点, (0, 0, 255)),
            ("MP", 蓝量检测点, (255, 120, 0)),
        ):
            if 0 <= point[0] < preview.shape[1] and 0 <= point[1] < preview.shape[0]:
                cv2.circle(preview, point, 7, color, 2)
                cv2.putText(
                    preview,
                    label,
                    (point[0] + 9, max(18, point[1] - 7)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    color,
                    2,
                )
        for box in 怪物框列表 or ():
            cv2.rectangle(
                preview,
                (int(box[0]), int(box[1])),
                (int(box[2]), int(box[3])),
                (0, 255, 0),
                2,
            )
        if 人物局部坐标 is not None:
            cv2.circle(
                preview,
                (int(人物局部坐标[0]), int(人物局部坐标[1])),
                8,
                (255, 220, 40),
                2,
            )
            cv2.putText(
                preview,
                "SELF",
                (int(人物局部坐标[0]) + 10, max(18, int(人物局部坐标[1]) - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 220, 40),
                2,
            )
        rgb_frame = cv2.cvtColor(preview, cv2.COLOR_BGR2RGB)
        # rgb_frame 已由 cvtColor 新建且随后不再修改。预览服务会限制为最多一帧
        # 在途，无需再复制一张完整窗口大图。
        callback(rgb_frame, dict(info))
    except Exception as exc:
        print("[测试] 实时截图转发失败：{}".format(exc))


def get_yolo_model(model_path):
    """按模型绝对路径缓存 YOLO 实例，加快停止后再次启动的速度。"""
    normalized_path = os.path.normcase(os.path.abspath(model_path))
    with _model_cache_lock:
        model = _model_cache.get(normalized_path)
        if model is None:
            print(f"[模型] 正在加载 {os.path.basename(model_path)}")
            model = YOLO(model_path)
            _model_cache[normalized_path] = model
        else:
            print(f"[模型] 复用已加载模型 {os.path.basename(model_path)}")
        return model


didd红 = "50"
didd蓝 = "5"

global DRAW_DETECTION_BOXES
DRAW_DETECTION_BOXES = False
测试截图模式 = False
启用轮检测 = False
蘑菇V3路线文件 = "蘑菇V3路线.json"
自定义录制路线文件 = "蘑菇V3路线.json"
recorded_route_ignored_platform_numbers = ()
# 通用录制路线和蘑菇V2共用的绳中定时休息配置；任意一项为0时关闭。
绳子休息间隔分钟 = 20.0
绳子休息时长分钟 = 1.0
游戏客户区宽度 = 1280
游戏客户区高度 = 800
游戏客户区屏幕左 = 0
游戏客户区屏幕顶 = 31
游戏窗口外框宽度 = 1280
游戏窗口外框高度 = 831
检测预览回调 = None
检测预览锁 = threading.Lock()
检测预览帧间隔 = 1.0 / 15.0
# 完整窗口只用于页面大图预览。4FPS足够观察识别框，同时避免虚拟机抓屏
# 占满检测线程；非预览帧仍按原有节奏抓取下方检测区域。
完整检测预览截图间隔 = 0.25
上次检测预览时间 = 0.0

global zant
zant=0
# 一键测试休息点期间由路线线程独占移动控制；检测线程继续识别，但不再发布
# 攻击意图或占用战斗暂停状态，避免附近持续刷怪时永远无法前往休息点。
休息点测试进行中 = False
global didd闪烁
didd闪烁 = 0
stop_event2 = 0
# 用户暂停与停止相互独立：事件为 set 时允许路线、战斗和按键继续执行，
# clear 时所有使用公共等待的动作停在当前位置，恢复后继续原流程。
用户运行许可事件 = threading.Event()
用户运行许可事件.set()


class _PauseAwareDirectInput:
    """暂停期间屏蔽新的按下/点击，但始终允许 keyUp 安全释放。"""

    def __init__(self, backend):
        self._backend = backend

    def keyDown(self, key, *args, **kwargs):
        if not 用户运行许可事件.is_set():
            return None
        return self._backend.keyDown(key, *args, **kwargs)

    def keyUp(self, key, *args, **kwargs):
        return self._backend.keyUp(key, *args, **kwargs)

    def press(self, key, *args, **kwargs):
        if not 用户运行许可事件.is_set():
            return None
        return self._backend.press(key, *args, **kwargs)

    def click(self, *args, **kwargs):
        if not 用户运行许可事件.is_set():
            return None
        return self._backend.click(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._backend, name)


pydirectinput = _PauseAwareDirectInput(pydirectinput)
YOLO怪物置信度 = YOLO_MONSTER_CONFIDENCE
单体按键 = "x"
群攻按键 = "f"
闪现按键 = "c"
# 运行设置中的 N；横向范围内怪物数量严格大于 N 时自动群攻。
自动群攻大于数量 = 2
monitor = {'top': 10, 'left': 0, 'width': 1280, 'height': 720}
global 攻击
攻击=0
# 检测线程发布攻击意图，路线线程消费意图。单独的锁避免“读到方向后刚好被
# 另一帧覆盖”，冷却截止时间则用于过滤死亡动画期间的重复攻击。
攻击状态锁 = threading.Lock()
攻击状态条件 = threading.Condition(攻击状态锁)
攻击冷却截止时间 = 0.0
攻击移动锁截止时间 = 0.0
攻击意图版本 = 0
攻击意图生成时间 = 0.0
# 保存最近一次攻击按键完成的单调时间，用于丢弃“攻击完成前已经截图、完成后才
# 发布”的旧检测帧。这样无需恢复固定复检 sleep，也能避免死亡动画补一刀。
最近攻击完成时间 = 0.0
# 路线线程读取最近一次近距离怪物方向，避免检测短暂漏帧时立刻继续巡逻。
战斗感知锁 = threading.Lock()
最近近怪时间 = 0.0
最近怪物方向 = None
当前近怪数量 = 0
# 录制路线判定当前命中长期没有攻击进展时，会短暂忽略战斗。检测线程仍继续
# 刷新识别快照，但在截止时间前不再发布攻击意图或重复占用 zant。
战斗忽略截止时间 = 0.0
上次移动方向 = None
# V2 智能追怪使用独立快照；其他地图仍只读取上面的公共战斗感知。
智能追怪额外横向范围 = 200
智能追怪前方额外横向范围 = combat_controller.V2_FORWARD_CHASE_EXTRA_RANGE
# 录制路线发布的稳定前进方向。攻击前转身、受击转身和战斗阶段释放方向键都不
# 改写它；这样“身后怪”不会因为人物刚转身攻击而在下一帧变成可追目标。
智能追怪前进方向锁 = threading.Lock()
智能追怪前进方向 = None
智能追怪双向模式 = False
# 模板只在人物攻击范围、额外追怪范围及少量边缘余量内匹配。人物暂时定位失败时
# 复用上一可靠位置；只有启动后尚未定位成功时才扫描完整检测区。
# 自定义模板最大宽度约28px、高度约15px；保留20px边缘即可覆盖完整目标，
# 比原40/30px余量少扫描约14%的像素，提升模板检测频率而不缩短追怪200px范围。
# 蘑菇V2攻击后对位置基本不变的目标连续确认两张新画面；若目标明显位移、
# 换边或新增目标，则算法判定怪物仍存活并快速补刀。识别线程继续全速运行，
# 不增加固定sleep，静止死亡残影仍不会直接转换成攻击。
V2攻击后存活确认帧数 = (
    combat_controller.V2_POST_ATTACK_ALIVE_CONFIRMATION_FRAMES
)
# 怪物受击后中心横向变化达到该值，视为击退/移动而不是静止死亡残影。
V2存活目标最小位移像素 = combat_controller.V2_POST_ATTACK_MOVEMENT_THRESHOLD
# 只在攻击后的短时间内把模板命中视为可能的死亡动画；超过窗口后出现的目标
# 是新怪或存活怪，立即恢复正常攻击，不能一直重复确认造成停走抽动。
V2死亡动画确认窗口秒数 = (
    combat_controller.V2_POST_ATTACK_DEATH_ANIMATION_WINDOW_SECONDS
)
# 自定义怪物五官模板在技能、受击特效遮挡时可能连续漏掉一两帧。所有模板
# 检测入口统一连续三帧无目标后才发布空快照，避免追怪与JSON路线频繁互抢。
V2目标丢失确认帧数 = 3
# 高帧率模板检测中三帧可能只有约0.10秒，技能特效或受击遮挡很容易超过
# 该时长。空帧必须同时满足帧数和持续时间才算真正清怪。
V2目标丢失确认最短秒数 = 0.28
# 严格攻击距离外再给80px近身补刀带。怪物受击后轻微击退到攻击边缘外时，
# V2保持原地并按怪物左右方向继续攻击，不插入一次追怪移动。
V2近身补刀额外横向范围 = 80
追怪感知锁 = threading.Lock()
最近追怪时间 = 0.0
最近追怪方向 = None
当前追怪数量 = 0
当前可攻击数量 = 0
当前追怪最近距离 = None
当前追怪左边数量 = 0
当前追怪右边数量 = 0
当前可攻击左边数量 = 0
当前可攻击右边数量 = 0
当前血条追怪左边数量 = 0
当前血条追怪右边数量 = 0
当前血条可攻击左边数量 = 0
当前血条可攻击右边数量 = 0
当前追怪连续无目标发布帧数 = 0
当前追怪丢失确认中 = False
当前追怪无目标开始时间 = 0.0
当前攻击确认中 = False
当前自动群攻怪物数量 = 0
当前近身群攻怪物数量 = 0
当前自动群攻大于数量 = 2
# 战斗感知的唯一真实状态由公共战斗模块保存。下面的旧模块级变量暂时保留，
# 仅用于兼容历史脚本直接取属性；生产读写全部通过该存储器。
战斗感知存储 = combat_controller.CombatPerceptionStore(
    trace=trace_event,
    recent_attack_completed_at=lambda: 最近攻击完成时间,
)
# 人物朝向由检测线程发布、蘑菇V2路线线程读取。低置信度或受击黑闪帧不会
# 覆盖最近可靠方向，避免角色被击退转身后一直沿旧方向攻击。
人物朝向感知锁 = threading.Lock()
人物朝向检测时间 = 0.0
人物当前可靠朝向 = None
人物本帧检测朝向 = None
人物朝向左置信度 = 0.0
人物朝向右置信度 = 0.0
人物朝向置信度差 = 0.0
人物朝向检测耗时 = 0.0
人物朝向黑闪疑似 = False
人物朝向本帧可靠 = False
# 模板路径
global didd
didd=1
global renwu_pos
# 共享变量
renwu_pos = None
lock = threading.Lock()
# 由 AutomationService 在每次启动任务时注入新的 Event。
stop_event = None
人物轨迹记录器 = CharacterTrajectoryRecorder()
上次小地图人物发现时间 = 0.0
# 路线卡住时由路线线程置位，人物模板线程在下一帧清除局部ROI并执行全图定位。
人物全图重定位事件 = threading.Event()
# V2 路线等待怪物检测线程完成首帧后再开始移动。
怪物检测就绪事件 = threading.Event()
# 自动化服务统一维护游戏窗口焦点。焦点丢失时清除此事件，所有复用公共等待
# 的地图、录制路线和战斗动作都会暂停，避免把方向键或攻击键发送给其他弹窗。
游戏窗口焦点事件 = threading.Event()
游戏窗口焦点事件.set()

# 参数
distance_threshold = 100  # 距离阈值
search_range = 200        # 在检测目标范围内找

# 输出模式配置: "gui"=仅界面, "console"=仅控制台, "both"=界面+控制台
OUTPUT_MODE = "both"

import ctypes















药水状态锁 = threading.Lock()
药水连续空白次数 = {"health": 0, "mana": 0}
药水上次按下时间 = {"health": 0.0, "mana": 0.0}
药水空白确认帧数 = 2
药水最短冷却秒数 = 2.0
药水按键持续秒数 = 0.10
# 药水判定属于检测线程，输入动作则由唯一的守护线程串行执行。这样红蓝同帧
# 触发时仍不会同时按住，也不会把 100ms 按键时长阻塞到下一次怪物检测。
药水输入队列 = queue.Queue()
药水输入线程 = None
药水输入线程锁 = threading.Lock()
药水输入世代 = 0
# 完整原图显示HP/MP槽内部中心约为屏幕Y=787；当前客户区底部为799，
# 因此检测点应距客户区底部12px。完整图中的槽位扣除客户区屏幕左偏移8后，
# HP内槽相对X约496～599，MP内槽相对X约604～708。
药水检测距客户区底部 = 12
血量槽客户区起点X = 496
血量槽有效宽度 = 104
蓝量槽客户区起点X = 604
蓝量槽有效宽度 = 105


def 重置药水检测状态():
    """每次启动任务时清空红蓝空白计数和按药冷却时间。"""
    global 药水输入世代
    with 药水状态锁:
        for potion in 药水连续空白次数:
            药水连续空白次数[potion] = 0
            药水上次按下时间[potion] = 0.0
    # 队列可能还保留上一次任务的未执行药水。递增世代可让工作线程安全丢弃
    # 它们，而不需要竞争性地清空 Queue 内部对象。
    with 药水输入线程锁:
        药水输入世代 += 1


def _药水输入已停止():
    """在模块初始化和测试替身下安全读取全局停止状态。"""
    try:
        return bool(已请求停止())
    except NameError:
        return False


def _执行药水按键(key, *, input_driver=None, should_stop=None, wait=None):
    """执行一次可中断的药水短按；供唯一输入线程和聚焦测试复用。"""
    input_driver = input_driver or pydirectinput
    should_stop = should_stop or _药水输入已停止
    wait = wait or time.sleep
    if should_stop():
        # keyUp 可重复调用；停止前没有 keyDown 时也保持安全。
        input_driver.keyUp(key)
        return False

    pressed = False
    try:
        # 工作线程出队后再次确认停止，防止停止期间的陈旧任务发起新的 keyDown。
        if should_stop():
            return False
        input_driver.keyDown(key)
        pressed = True
        deadline = time.monotonic() + 药水按键持续秒数
        while True:
            if should_stop():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            wait(min(0.01, remaining))
    finally:
        if pressed:
            input_driver.keyUp(key)


def _药水输入工作线程():
    """按队列顺序串行执行药水短按；该线程绝不占用怪物检测循环。"""
    while True:
        generation, key = 药水输入队列.get()
        try:
            with 药水输入线程锁:
                current_generation = 药水输入世代
            if generation != current_generation or _药水输入已停止():
                # 已停止或任务已切换时只做安全 keyUp，不会迟到地 keyDown。
                pydirectinput.keyUp(key)
                continue
            _执行药水按键(key)
        finally:
            药水输入队列.task_done()


def _排队药水按键(key):
    """快速提交药水输入；返回后检测线程可以立即继续处理下一帧。"""
    global 药水输入线程
    if _药水输入已停止():
        pydirectinput.keyUp(key)
        return False
    with 药水输入线程锁:
        if 药水输入线程 is None or not 药水输入线程.is_alive():
            药水输入线程 = threading.Thread(
                target=_药水输入工作线程,
                name="potion-input-worker",
                daemon=True,
            )
            药水输入线程.start()
        generation = 药水输入世代
        药水输入队列.put((generation, key))
    return True


def 获取红蓝检测坐标():
    """按完整原图中的HP/MP槽真实范围和页面百分比计算检测点。"""
    检测y = int(
        游戏客户区屏幕顶
        + 游戏客户区高度
        - 药水检测距客户区底部
    )
    红量百分比 = max(0, min(100, int(didd红))) / 100.0
    蓝量百分比 = max(0, min(100, int(didd蓝))) / 100.0
    return (
        (
            int(round(
                游戏客户区屏幕左
                + 血量槽客户区起点X
                + 红量百分比 * 血量槽有效宽度
            )),
            检测y,
        ),
        (
            int(round(
                游戏客户区屏幕左
                + 蓝量槽客户区起点X
                + 蓝量百分比 * 蓝量槽有效宽度
            )),
            检测y,
        ),
    )


def 药水检测点位于客户区(x, y):
    """判断药水取色点是否位于当前游戏客户区，防止对桌面空白区域按药。"""
    return (
        游戏客户区屏幕左 <= x < 游戏客户区屏幕左 + 游戏客户区宽度
        and 游戏客户区屏幕顶 <= y < 游戏客户区屏幕顶 + 游戏客户区高度
    )


def 尝试按下药水(potion, key, label, x, y):
    """连续确认取色点为空白并满足冷却后，安全按下一次对应药水键。"""
    if not 药水检测点位于客户区(x, y):
        trace_event(
            "potion_probe_invalid",
            potion=potion,
            position=(x, y),
            client_rect=(
                游戏客户区屏幕左,
                游戏客户区屏幕顶,
                游戏客户区宽度,
                游戏客户区高度,
            ),
        )
        return False

    blank = is_white_pixel(x, y)
    now = time.monotonic()
    with 药水状态锁:
        药水连续空白次数[potion] = (
            药水连续空白次数[potion] + 1 if blank else 0
        )
        confirmation_count = 药水连续空白次数[potion]
        remaining_cooldown = 药水最短冷却秒数 - (
            now - 药水上次按下时间[potion]
        )
        if (
            confirmation_count < 药水空白确认帧数
            or remaining_cooldown > 0
        ):
            return False
        药水连续空白次数[potion] = 0
        药水上次按下时间[potion] = now

    print("[药水] {}检测点=({}, {})，连续确认空白，按下 {}。".format(
        label,
        x,
        y,
        key,
    ))
    trace_event(
        "potion_action",
        potion=potion,
        key=key,
        position=(x, y),
        confirmation_frames=药水空白确认帧数,
        cooldown_seconds=药水最短冷却秒数,
        combat_state=zant,
    )
    return _排队药水按键(key)


def 检查并补充红蓝():
    """按实际游戏客户区计算取色点，并经连续确认与冷却后补充红蓝。

    怪物使用 YOLO 还是图片模板只决定怪物坐标来源，不能改变药水判断方式。
    所有地图统一按界面百分比计算检测点，并在该点连续变成白色时分别按1、2。
    """
    (红量检测x, 检测y), (蓝量检测x, _) = 获取红蓝检测坐标()
    尝试按下药水("health", "1", "红量", 红量检测x, 检测y)
    尝试按下药水("mana", "2", "蓝量", 蓝量检测x, 检测y)


def load_monster_templates(directory):
    """加载当前目录中所有以 ``guai`` 开头的怪物图片。

    旧逻辑用文件总数推算模板编号，会漏掉最后一张图，也会被其他文件干扰。
    现在不要求数字连续，并支持PNG/JPG/JPEG/BMP/WebP。
    """
    templates = []
    paths = custom_template_files(Path(directory))
    for template_path in paths:
        template = cv2.imdecode(np.fromfile(str(template_path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if template is None:
            print("[模板] 无法读取：{}".format(template_path))
            continue
        templates.append(prepare_monster_template(cv2, template))
    print("[模板] 已加载 {} 张怪物模板".format(len(templates)))
    return templates


def detection_thread(
    名字,
    search_range,
    vertical_range=70,
    template_directory=None,
    map_name=None,
):
    """使用多模板匹配检测怪物，并保留页面所选地图的战斗上下文。"""
    basedir = get_base_dir()
    dituming = map_name or 名字
    path = str(template_directory) if template_directory else basedir+"\\img\\"+名字
    model2 = None
    if dituming == "武陵海盗船2" and not template_directory:
        model2 = get_yolo_model(basedir + "\\moxing\\血.pt")




    global 攻击
    global zant
    global stop_event2
    global didd
    search_range, vertical_range = int(search_range), int(vertical_range)
    模板搜索横向半径 = max(
        300,
        search_range + int(智能追怪额外横向范围),
    )
    print("监控怪物：横向距离<{}，纵向高度<{}，模板目录={}".format(search_range, vertical_range, path))
    monster_templates = load_monster_templates(path)
    if template_directory and not monster_templates:
        raise ValueError(
            "已开启自定义怪物模板，但没有可读取的 guai* 图片：{}；"
            "为避免误用YOLO，本次任务已停止。".format(template_directory)
        )





    basedir = get_base_dir()
    path = basedir+"\\resres\\"
    luntup=path+'lun.png'
    lun_template = cv2.imread(luntup) if 启用轮检测 else None
    # 人物和怪物只检测游戏下半区，减少模板匹配面积并避开顶部 UI 的红色干扰。
    monitor = 获取怪物检测区域(dituming)
    selected_character_template = None
    character_tracker = None
    if dituming != "武陵海盗船2":
        character_templates = require_character_templates(cv2, basedir)
        selected_character_template = select_character_template(
            cv2,
            np,
            grab_screen,
            character_templates,
            monitor=monitor,
            stop_requested=已请求停止,
            wait=可中断等待,
            log_prefix="[模板人物]",
        )
        if selected_character_template is None:
            print("[模板人物][警告] 未找到可用人物模板，本次无法触发攻击。")
        else:
            character_tracker = CharacterTracker(
                selected_character_template,
                monitor,
            )
            print(
                "[模板人物] 本次固定使用 {}，阈值={:.2f}。".format(
                    selected_character_template[0],
                    PERSON_MATCH_THRESHOLD,
                )
            )
    facing_tracker = (
        CharacterFacingTracker(cv2, np)
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
        else None
    )
    模板朝向上次日志状态 = None
    模板朝向上次日志时间 = 0.0
    if facing_tracker is not None:
        print(
            "[朝向识别] 模板检测线程已启用：左={}x{}，右={}x{}，来源={}。".format(
                facing_tracker.left_template.shape[1],
                facing_tracker.left_template.shape[0],
                facing_tracker.right_template.shape[1],
                facing_tracker.right_template.shape[0],
                facing_tracker.template_source,
            )
        )
        trace_event(
            "character_facing_config",
            detector="template",
            template_source=facing_tracker.template_source,
            left_size=[
                facing_tracker.left_template.shape[1],
                facing_tracker.left_template.shape[0],
            ],
            right_size=[
                facing_tracker.right_template.shape[1],
                facing_tracker.right_template.shape[0],
            ],
            aux_source="user_left_star+user_right_star",
            aux_left_size=[
                facing_tracker.left_aux_template.shape[1],
                facing_tracker.left_aux_template.shape[0],
            ],
            aux_right_size=[
                facing_tracker.right_aux_template.shape[1],
                facing_tracker.right_aux_template.shape[0],
            ],
            aux_confidence_threshold=facing_tracker.aux_confidence_threshold,
            aux_margin_threshold=facing_tracker.aux_margin_threshold,
            aux_search_relative=[
                -facing_tracker.aux_radius_x,
                facing_tracker.aux_top_offset,
                facing_tracker.aux_radius_x,
                facing_tracker.aux_bottom_offset,
            ],
        )
    if 已请求停止():
        return

    没怪数量 = 0
    诊断帧编号 = 0
    上次详细日志时间 = 0.0
    详细日志间隔 = 1.0
    上一帧开始时间 = 0.0
    print("[模板诊断] 已启用，每 {:.1f} 秒输出一次完整攻击判定。".format(详细日志间隔))
    print("[模板诊断] 人物/怪物采集范围：{}".format(monitor))
    print(
        "[模板诊断] 怪物搜索范围：人物左右各{}px，上下各{}px；"
        "人物定位失败时200ms内沿用上一可靠坐标参与战斗，超时后仅用于裁剪搜索区。".format(
            模板搜索横向半径,
            vertical_range
        )
    )
    模板匹配线程数 = max(1, min(4, len(monster_templates)))
    模板匹配线程池 = ThreadPoolExecutor(
        max_workers=模板匹配线程数,
        thread_name_prefix="monster-template",
    )
    print(
        "[模板诊断] 怪物模板并行匹配已启用：模板={}，线程={}。".format(
            len(monster_templates),
            模板匹配线程数,
        )
    )
    启用怪物血条检测 = bool(monster_templates)
    血条纵向偏移估计 = MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET
    攻击后目标保护 = combat_controller.PostAttackTargetGuard(
        trace=trace_event,
        alive_confirmation_frames=V2攻击后存活确认帧数,
        movement_threshold=V2存活目标最小位移像素,
        death_animation_window_seconds=V2死亡动画确认窗口秒数,
    )
    if 启用怪物血条检测:
        print("[模板诊断] 已启用绿色怪物血条检测，并参与活怪中心补充。")
    trace_event(
        "template_detector_config",
        map=dituming,
        template_count=len(monster_templates),
        template_workers=模板匹配线程数,
        search_horizontal_radius=模板搜索横向半径,
        search_vertical_radius=vertical_range,
        attack_range_x=search_range,
        attack_range_y=vertical_range,
        health_bar_detection=启用怪物血条检测,
        target_frame_seconds=TEMPLATE_TARGET_FRAME_SECONDS,
    )
    while True:
        if stop_event2 == 0:
            break
        帧开始时间 = time.perf_counter()
        帧间隔耗时 = (
            (帧开始时间 - 上一帧开始时间) * 1000
            if 上一帧开始时间 > 0
            else None
        )
        上一帧开始时间 = 帧开始时间
        截图开始时间 = time.perf_counter()
        img, 预览画面, 预览偏移X, 预览偏移Y, 截图区域说明 = 捕获检测与预览画面(
            monitor
        )
        截图耗时 = (time.perf_counter() - 截图开始时间) * 1000
        诊断帧编号 += 1
        当前时间 = time.monotonic()
        输出详细日志 = 当前时间 - 上次详细日志时间 >= 详细日志间隔
        if 输出详细日志:
            上次详细日志时间 = 当前时间
        共享坐标检测前 = 读取人物位置() if 输出详细日志 else None
        判定前状态 = (zant, 攻击, 没怪数量)
        人物定位说明 = "尚未执行人物定位"
        攻击参考点 = None
        人物局部坐标 = None
        目标判定详情 = []
        ccx = ccy = match_y = 0
        lun_center_x = lun_center_y = 0
        轮子置信度 = 0.0
        centers=[]
        目标置信度列表=[]
        预览怪物框 = []
        人物置信度 = 0.0
        # 用多模板匹配检测“怪物”
        total_targets = 0
        threshold = MONSTER_TEMPLATE_CONFIDENCE  # 模板匹配阈值

        # 先定位人物，再把怪物模板搜索限制在人物附近。当前帧人物定位无效时，
        # 只在200ms内沿用上一可靠坐标参与战斗判定；更旧的位置仅用于裁剪ROI。
        人物匹配开始时间 = time.perf_counter()
        人物屏幕坐标 = None
        人物定位有效 = False
        模板ROI人物屏幕坐标 = None
        模板ROI攻击参考Y = None
        模板ROI使用上一人物位置 = False
        战斗人物屏幕坐标 = None
        人物定位可用于战斗 = False
        人物定位短暂保留 = False
        人物定位保留年龄毫秒 = None
        if dituming != "武陵海盗船2" and character_tracker is not None:
            if 人物全图重定位事件.is_set():
                character_tracker.reset()
                人物全图重定位事件.clear()
                trace_event("character_full_relocation_applied", detector="template")
            人物置信度, 人物屏幕坐标, 人物局部坐标 = character_tracker.locate(
                cv2,
                img,
            )
            人物定位有效 = (
                人物屏幕坐标 is not None
                and 人物置信度 >= PERSON_MATCH_THRESHOLD
            )
            人物定位短暂保留 = False
            人物定位保留年龄毫秒 = None
            战斗人物屏幕坐标 = 人物屏幕坐标 if 人物定位有效 else None
            if not 人物定位有效:
                战斗人物屏幕坐标 = character_tracker.recent_screen_center(
                    now=当前时间,
                )
                if 战斗人物屏幕坐标 is not None:
                    人物定位短暂保留 = True
                    人物定位保留年龄毫秒 = (
                        character_tracker.recent_screen_center_age_ms(
                            now=当前时间,
                        )
                    )
            人物定位可用于战斗 = 战斗人物屏幕坐标 is not None
            if 人物定位可用于战斗:
                ccx, ccy = 战斗人物屏幕坐标
                人物模板名称 = selected_character_template[0]
                match_y = character_attack_reference_y(人物模板名称, ccy)
                攻击参考点 = (ccx, match_y)
                模板ROI人物屏幕坐标 = 战斗人物屏幕坐标
                模板ROI攻击参考Y = match_y
                模板ROI使用上一人物位置 = 人物定位短暂保留
            elif character_tracker.last_local_center is not None:
                上一人物局部X, 上一人物局部Y = character_tracker.last_local_center
                模板ROI人物屏幕坐标 = (
                    monitor['left'] + 上一人物局部X,
                    monitor['top'] + 上一人物局部Y,
                )
                模板ROI攻击参考Y = character_attack_reference_y(
                    selected_character_template[0],
                    模板ROI人物屏幕坐标[1],
                )
                模板ROI使用上一人物位置 = True
        人物匹配耗时 = (
            character_tracker.last_match_ms
            if character_tracker is not None
            else (time.perf_counter() - 人物匹配开始时间) * 1000
        )
        人物朝向观测 = None
        if facing_tracker is not None:
            人物朝向观测 = facing_tracker.observe(
                cv2,
                np,
                img,
                monitor,
                人物屏幕坐标 if 人物定位有效 else None,
                detected_at=time.monotonic(),
            )
            更新人物朝向感知(人物朝向观测)
            模板朝向日志状态 = (
                "reliable-{}-{}".format(
                    人物朝向观测.detected_direction,
                    facing_tracker.last_decision_source,
                )
                if 人物朝向观测.reliable
                else (
                    "black-flicker-fallback"
                    if 人物朝向观测.black_flicker_suspected
                    else "low-confidence-fallback"
                )
            )
            if (
                模板朝向日志状态 != 模板朝向上次日志状态
                or 帧开始时间 - 模板朝向上次日志时间 >= 5.0
            ):
                trace_event(
                    "character_facing_frame",
                    detected_direction=人物朝向观测.detected_direction,
                    reliable_direction=人物朝向观测.reliable_direction,
                    left_confidence=round(人物朝向观测.left_confidence, 4),
                    right_confidence=round(人物朝向观测.right_confidence, 4),
                    confidence_margin=round(人物朝向观测.confidence_margin, 4),
                    black_flicker_suspected=人物朝向观测.black_flicker_suspected,
                    reliable=人物朝向观测.reliable,
                    match_ms=round(人物朝向观测.match_ms, 3),
                    template_source=facing_tracker.template_source,
                    decision_source=facing_tracker.last_decision_source,
                    aux_direction=facing_tracker.last_aux_direction,
                    aux_left_confidence=round(
                        facing_tracker.last_aux_left_confidence,
                        4,
                    ),
                    aux_right_confidence=round(
                        facing_tracker.last_aux_right_confidence,
                        4,
                    ),
                    aux_confidence_margin=round(
                        facing_tracker.last_aux_confidence_margin,
                        4,
                    ),
                    aux_reliable=facing_tracker.last_aux_reliable,
                    aux_roi=facing_tracker.last_aux_roi,
                    action=模板朝向日志状态,
                )
                模板朝向上次日志状态 = 模板朝向日志状态
                模板朝向上次日志时间 = 帧开始时间

        ROI构建开始时间 = time.perf_counter()
        模板搜索画面, 模板搜索偏移X, 模板搜索偏移Y, 模板搜索模式 = (
            获取人物固定范围模板搜索画面(
                img,
                monitor,
                模板ROI人物屏幕坐标,
                模板ROI攻击参考Y,
                horizontal_radius=模板搜索横向半径,
                vertical_radius=vertical_range,
            )
        )
        if 模板ROI使用上一人物位置:
            模板搜索模式 = (
                "person-fixed-held"
                if 人物定位短暂保留
                else "person-fixed-stale-no-attack"
            )
        ROI构建耗时 = (time.perf_counter() - ROI构建开始时间) * 1000

        怪物匹配开始时间 = time.perf_counter()
        血条目标数量 = 0
        血条关联目标数量 = 0
        血条推断目标数量 = 0
        血条最大绿色比例 = None
        血条活怪中心索引 = set()
        血条检测耗时 = 0.0
        血条命中 = ()
        模板命中 = match_monster_templates_parallel(
            cv2,
            np,
            模板搜索画面,
            monster_templates,
            executor=模板匹配线程池,
            threshold=threshold,
            x_offset=monitor['left'] + 模板搜索偏移X,
            y_offset=monitor['top'] + 模板搜索偏移Y,
        )
        for 命中 in 模板命中:
            centers.append(命中.center)
            目标置信度列表.append(命中.confidence)
            本地框 = (
                命中.output_box[0] - monitor['left'],
                命中.output_box[1] - monitor['top'],
                命中.output_box[2] - monitor['left'],
                命中.output_box[3] - monitor['top'],
            )
            预览怪物框.append(本地框)
            if DRAW_DETECTION_BOXES:
                cv2.rectangle(img, 本地框[:2], 本地框[2:], (0, 255, 0), 2)
        怪物匹配耗时 = (time.perf_counter() - 怪物匹配开始时间) * 1000
        if 启用怪物血条检测:
            血条检测开始时间 = time.perf_counter()
            原模板中心数量 = len(centers)
            血条检测结果 = detect_and_associate_monster_health_bars(
                cv2,
                np,
                模板搜索画面,
                centers,
                血条纵向偏移估计,
                x_offset=monitor['left'] + 模板搜索偏移X,
                y_offset=monitor['top'] + 模板搜索偏移Y,
            )
            血条命中 = 血条检测结果.matches
            血条目标数量 = len(血条命中)
            血条最大绿色比例 = 血条检测结果.max_green_ratio
            centers = list(血条检测结果.centers)
            血条活怪中心索引 = set(血条检测结果.alive_center_indices)
            血条关联目标数量 = 血条检测结果.linked_count
            血条推断目标数量 = 血条检测结果.inferred_count
            血条纵向偏移估计 = 血条检测结果.y_offset_estimate
            新增血条中心数量 = len(centers) - 原模板中心数量
            if 新增血条中心数量 > 0:
                目标置信度列表.extend(
                    [float(血条最大绿色比例 or 1.0)] * 新增血条中心数量
                )
            for 血条 in 血条命中:
                血条本地框 = (
                    血条.output_box[0] - monitor['left'],
                    血条.output_box[1] - monitor['top'],
                    血条.output_box[2] - monitor['left'],
                    血条.output_box[3] - monitor['top'],
                )
                预览怪物框.append(血条本地框)
                if DRAW_DETECTION_BOXES:
                    cv2.rectangle(
                        img,
                        血条本地框[:2],
                        血条本地框[2:],
                        (255, 255, 0),
                        1,
                    )
            血条检测耗时 = (
                time.perf_counter() - 血条检测开始时间
            ) * 1000
        total_targets = len(centers)
        自动群攻怪物数量 = (
            统计横向群攻怪物数量(ccx, centers)
            if 人物定位可用于战斗
            else 0
        )
        近身群攻怪物数量 = (
            统计指定横向范围怪物数量(
                ccx,
                centers,
                CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE,
            )
            if 人物定位可用于战斗
            else 0
        )

        # 以下是你原来的检测和操作逻辑（示意）
        # 例如：检测“轮子”，检测任务中心、距离、左右
        # 这里只提供检测“轮子”以及其他逻辑的主要框架
        left_right_counts = {'左边': 0, '右边': 0}
        total_nearby = 0
        最近目标方向 = None
        最近候选横向距离 = float("inf")
        最近攻击目标横向距离 = float("inf")
        最近候选纵向距离 = None
        战斗可见数量 = 0
        战斗可见左边 = 0
        战斗可见右边 = 0
        战斗可见方向 = None
        战斗可见最近距离 = float("inf")
        追怪可见数量 = 0
        追怪可见左边 = 0
        追怪可见右边 = 0
        追怪可见方向 = None
        追怪可见最近距离 = float("inf")
        血条可攻击活怪数量 = 0
        血条追怪活怪数量 = 0
        目标判定开始时间 = time.perf_counter()
        if dituming!="武陵海盗船2":
            if 人物定位可用于战斗:
                人物定位说明 = (
                    "{}：conf={:.3f}，搜索={}，匹配={:.2f}ms，局部中心={}，屏幕中心={}，1.9攻击参考Y={}{}".format(
                        人物模板名称,
                        人物置信度,
                        character_tracker.last_mode,
                        character_tracker.last_match_ms,
                        人物局部坐标,
                        战斗人物屏幕坐标,
                        match_y,
                        (
                            "，短时沿用上一坐标 {:.1f}ms".format(
                                人物定位保留年龄毫秒 or 0.0
                            )
                            if 人物定位短暂保留
                            else ""
                        ),
                    )
                )
                for 目标序号, (cx, cy) in enumerate(centers, start=1):
                    是血条活怪 = 目标序号 - 1 in 血条活怪中心索引
                    目标置信度 = 目标置信度列表[目标序号 - 1]
                    横向距离, 纵向距离, 进入攻击范围 = evaluate_attack_target(
                        ccx,
                        match_y,
                        (cx, cy), search_range, vertical_range,
                    )
                    if 横向距离 < 最近候选横向距离:
                        最近候选横向距离 = 横向距离
                        最近候选纵向距离 = 纵向距离
                    if (
                        横向距离 < search_range + 80
                        and 纵向距离 < vertical_range + 30
                    ):
                        战斗可见数量 += 1
                        可见方向 = "left" if ccx > cx else "right"
                        if 可见方向 == "left":
                            战斗可见左边 += 1
                        else:
                            战斗可见右边 += 1
                        if 横向距离 < 战斗可见最近距离:
                            战斗可见最近距离 = 横向距离
                            战斗可见方向 = 可见方向
                    if (
                        横向距离 < search_range + 智能追怪额外横向范围
                        and 纵向距离 < vertical_range
                    ):
                        追怪可见数量 += 1
                        if 是血条活怪:
                            血条追怪活怪数量 += 1
                        追怪方向 = "left" if ccx > cx else "right"
                        if 追怪方向 == "left":
                            追怪可见左边 += 1
                        else:
                            追怪可见右边 += 1
                        if 横向距离 < 追怪可见最近距离:
                            追怪可见最近距离 = 横向距离
                            追怪可见方向 = 追怪方向
                    if 进入攻击范围:
                        total_nearby += 1
                        if 是血条活怪:
                            血条可攻击活怪数量 += 1
                        if ccx - cx > 0:
                            left_right_counts['左边'] += 1
                            目标方向 = "left"
                            判定结果 = "通过：左侧攻击目标"
                        else:
                            left_right_counts['右边'] += 1
                            目标方向 = "right"
                            判定结果 = "通过：右侧攻击目标"
                        if 横向距离 < 最近攻击目标横向距离:
                            最近攻击目标横向距离 = 横向距离
                            最近目标方向 = 目标方向
                    elif 纵向距离 >= vertical_range:
                        判定结果 = "过滤：纵向距离 {:.1f} >= {}".format(
                            纵向距离, vertical_range,
                        )
                    else:
                        判定结果 = "过滤：横向距离 {:.1f} >= {}".format(
                            横向距离,
                            search_range,
                        )
                    if 输出详细日志:
                        目标判定详情.append(
                            (
                                目标序号, cx, cy, 目标置信度,
                                横向距离, 纵向距离, 判定结果,
                            )
                        )
            else:
                已选模板名称 = (
                    selected_character_template[0]
                    if selected_character_template is not None
                    else "未选择"
                )
                人物定位说明 = (
                    "{} 未达到阈值：conf={:.3f} < {:.2f}，无法建立攻击参考点".format(
                        已选模板名称,
                        人物置信度,
                        PERSON_MATCH_THRESHOLD,
                    )
                )
                if 输出详细日志:
                    for 目标序号, (cx, cy) in enumerate(centers, start=1):
                        目标判定详情.append(
                            (
                                目标序号,
                                cx,
                                cy,
                                目标置信度列表[目标序号 - 1],
                                None,
                                None,
                                "未判定：人物模板未达到阈值",
                            )
                        )
        else:
            if zant == 0:
                results = model2(
                    img,
                    verbose=False,
                    conf=YOLO怪物置信度,
                )
                for result in results:
                    for box in result.boxes:
                        if int(box.cls[0]) == 0:
                            xyxy = box.xyxy[0].cpu().numpy()
                            人物置信度 = float(box.conf[0].cpu().numpy())
                            if DRAW_DETECTION_BOXES:
                                cv2.rectangle(
                                    img,
                                    (int(xyxy[0]), int(xyxy[1])),
                                    (int(xyxy[2]), int(xyxy[3])),
                                    (0, 255, 0),
                                    2,
                                )

                            # 计算中心点
                            ccx = monitor['left'] + (xyxy[0] + xyxy[2]) / 2
                            ccy = monitor['top'] + (xyxy[1] + xyxy[3]) / 2
                            人物局部坐标 = (
                                int(ccx - monitor['left']),
                                int(ccy - monitor['top']),
                            )
                            print(ccx,ccy)
                            match_x =ccx
                            match_y = ccy+ 80
                            for cx, cy in centers:
                                chacc = abs(match_y - cy)
                                if chacc < vertical_range:

                                    chacc=abs(ccx-cx)

                                    if chacc<search_range:
                                        print(chacc)
                                        total_nearby += 1
                                        # 判断左右
                                        chacc = ccx - cx
                                        if chacc>0:
                                            left_right_counts['左边'] += 1
                                        else:
                                            left_right_counts['右边'] += 1
        目标判定耗时 = (time.perf_counter() - 目标判定开始时间) * 1000
        药水检测开始时间 = time.perf_counter()
        检查并补充红蓝()
        药水检测耗时 = (time.perf_counter() - 药水检测开始时间) * 1000

        轮子检测耗时 = 0.0
        if 启用轮检测 and lun_template is not None:
            轮子检测开始时间 = time.perf_counter()
            res_lun = cv2.matchTemplate(img, lun_template, cv2.TM_CCOEFF_NORMED)
            _, 轮子置信度, _, 轮子位置 = cv2.minMaxLoc(res_lun)
            if 轮子置信度 >= 0.7:
                lun_center_x = monitor['left'] + 轮子位置[0] + lun_template.shape[1] // 2
                lun_center_y = monitor['top'] + 轮子位置[1] + lun_template.shape[0] // 2

                cha2=abs(ccy-lun_center_y)
                if cha2<100:
                    cha=abs(ccx-lun_center_x)
                    if 输出详细日志:
                        print("[模板诊断][轮子] 人物与轮子横向距离：{:.1f}".format(cha))
                    if cha<60:
                        if 输出详细日志:
                            print("[模板诊断][轮子] 距离小于 60，进入解轮流程。")
                        pydirectinput.keyUp('right')
                        pydirectinput.keyUp('left')
                        pydirectinput.keyDown('up')
                        time.sleep(0.1)
                        pydirectinput.keyUp('up')
                        zant=0
                        zant=2
                        pydirectinput.keyDown('up')
                        time.sleep(0.1)
                        pydirectinput.keyUp('up')
                        pydirectinput.keyDown('up')
                        time.sleep(0.1)
                        pydirectinput.keyUp('up')
                        time.sleep(2)
                        jielun()
                        time.sleep(1)
                        zant=0
                        time.sleep(1)


                else:
                    if 输出详细日志:
                        print("[模板诊断][轮子] 纵向距离过大：{:.1f} >= 100。".format(cha2))
            轮子检测耗时 = (time.perf_counter() - 轮子检测开始时间) * 1000
        可发布攻击数量 = total_nearby
        攻击确认中 = False
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线"):
            with 攻击状态锁:
                当前最近攻击完成时间 = 最近攻击完成时间
            可发布攻击数量 = 攻击后目标保护.filter_attackable_count(
                frame_detected_at=当前时间,
                recent_attack_completed_at=当前最近攻击完成时间,
                decision_count=total_nearby,
                decision_direction=最近目标方向,
                chase_nearest_distance=(
                    追怪可见最近距离
                    if 追怪可见最近距离 != float("inf")
                    else None
                ),
                health_bar_attackable_count=血条可攻击活怪数量,
                health_bar_chase_count=血条追怪活怪数量,
                health_bar_green_max_ratio=血条最大绿色比例,
                close_range_promoted=False,
            )
            攻击确认中 = 攻击后目标保护.confirmation_pending

        公共快照开始时间 = time.perf_counter()
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线"):
            # 模板检测必须与YOLO检测发布同一份公共快照，否则录制路线虽然
            # 能看到attack_pending，却无法生成公共combat攻击意图。
            更新追怪感知(
                可发布攻击数量,
                追怪可见数量,
                追怪可见左边,
                追怪可见右边,
                attackable_left_count=(
                    left_right_counts['左边'] if 可发布攻击数量 > 0 else 0
                ),
                attackable_right_count=(
                    left_right_counts['右边'] if 可发布攻击数量 > 0 else 0
                ),
                preferred_direction=追怪可见方向,
                nearest_distance=(
                    追怪可见最近距离
                    if 追怪可见最近距离 != float("inf")
                    else None
                ),
                detected_at=当前时间,
                attack_confirmation_pending=攻击确认中,
            )
        公共快照耗时 = (time.perf_counter() - 公共快照开始时间) * 1000
        攻击结果开始时间 = time.perf_counter()
        战斗前状态 = zant
        没怪数量 = 处理怪物检测结果(
            可发布攻击数量,
            left_right_counts['左边'] if 可发布攻击数量 > 0 else 0,
            left_right_counts['右边'] if 可发布攻击数量 > 0 else 0,
            没怪数量,
            allow_group_attack=(dituming == "蘑菇半图上层备用" and didd == 1),
            preferred_direction=最近目标方向,
            visible_nearby=战斗可见数量,
            visible_left_count=战斗可见左边,
            visible_right_count=战斗可见右边,
            visible_preferred_direction=战斗可见方向,
            hold_visible_combat=(dituming not in ("蘑菇V2", "蘑菇V3", "自定义录制路线")),
            detected_at=当前时间,
        )
        if 战斗前状态 != 1 and zant == 1:
            print("[战斗] 锁定近距离怪物，路线保持原地。")
        elif 战斗前状态 == 1 and zant == 0:
            print("[战斗] 连续 {} 帧未发现近怪，恢复路线。".format(
                COMBAT_TARGET_LOST_CONFIRMATION_FRAMES
            ))
        攻击结果耗时 = (time.perf_counter() - 攻击结果开始时间) * 1000
        基础追踪日志开始时间 = time.perf_counter()
        日志人物坐标, 日志怪物坐标, 日志怪物相对坐标 = 构建紧凑怪物坐标日志(
            (ccx if 人物定位可用于战斗 else None),
            (match_y if 人物定位可用于战斗 else None),
            centers,
        )
        trace_event(
            "detection_frame",
            detector="template",
            map=dituming,
            p=日志人物坐标,
            m=日志怪物坐标,
            mr=日志怪物相对坐标,
            targets=total_targets,
            nearby=total_nearby,
            combat_visible=战斗可见数量,
            decision_direction=最近目标方向,
            decision_attackable=可发布攻击数量,
            left=left_right_counts['左边'],
            right=left_right_counts['右边'],
            capture_ms=round(截图耗时, 3),
            roi_ms=round(ROI构建耗时, 3),
            monster_ms=round(怪物匹配耗时, 3),
            health_bar_ms=round(血条检测耗时, 3),
            health_bar_targets=血条目标数量,
            health_bar_attackable=血条可攻击活怪数量,
            health_bar_chase_targets=血条追怪活怪数量,
            health_bar_linked_targets=血条关联目标数量,
            health_bar_inferred_targets=血条推断目标数量,
            health_bar_green_max_ratio=(
                round(血条最大绿色比例, 4)
                if 血条最大绿色比例 is not None
                else None
            ),
            health_bar_y_offset_estimate=(
                round(血条纵向偏移估计, 2)
                if 启用怪物血条检测
                else None
            ),
            person_ms=round(人物匹配耗时, 3),
            target_evaluation_ms=round(目标判定耗时, 3),
            person_confidence=round(float(人物置信度), 4),
            person_mode=(character_tracker.last_mode if character_tracker else "yolo-person"),
            person_position_held=人物定位短暂保留,
            person_position_age_ms=(
                round(人物定位保留年龄毫秒, 1)
                if 人物定位保留年龄毫秒 is not None
                else None
            ),
            search_mode=模板搜索模式,
            search_left=monitor['left'] + 模板搜索偏移X,
            search_top=monitor['top'] + 模板搜索偏移Y,
            search_width=int(模板搜索画面.shape[1]),
            search_height=int(模板搜索画面.shape[0]),
            search_horizontal_radius=模板搜索横向半径,
            search_vertical_radius=vertical_range,
            used_stale_person_position=模板ROI使用上一人物位置,
            fallback_full_frame=模板搜索模式.startswith("full-"),
            template_count=len(monster_templates),
            template_workers=模板匹配线程数,
            nearest_dx=(
                round(最近候选横向距离, 2)
                if 最近候选横向距离 != float("inf")
                else None
            ),
            nearest_dy=(
                round(最近候选纵向距离, 2)
                if 最近候选纵向距离 is not None
                else None
            ),
            attack_range_x=search_range,
            attack_range_y=vertical_range,
            capture_left=monitor['left'],
            capture_top=monitor['top'],
            capture_width=monitor['width'],
            capture_height=monitor['height'],
            chase_range_x=search_range + 智能追怪额外横向范围,
            chase_extra_x=智能追怪额外横向范围,
            chase_forward_direction=None,
            rear_chase_enabled=读取智能追怪双向模式(),
            chase_targets=追怪可见数量,
            lost_frames=没怪数量,
            combat_state=zant,
        )
        基础追踪日志耗时 = (time.perf_counter() - 基础追踪日志开始时间) * 1000
        帧处理耗时 = (time.perf_counter() - 帧开始时间) * 1000
        预览发布开始时间 = time.perf_counter()
        发布检测预览(
            预览画面,
            {
                "map": dituming,
                "detector": "template",
                "monster_count": total_targets,
                "max_monster_confidence": (
                    max(目标置信度列表) if 目标置信度列表 else None
                ),
                "yellow_position": None,
                "self_position": (
                    (round(float(ccx), 1), round(float(ccy), 1))
                    if 人物局部坐标 is not None
                    else None
                ),
                "self_template": (
                    selected_character_template[0]
                    if selected_character_template is not None
                    else "yolo-person"
                ),
                "self_search_mode": (
                    character_tracker.last_mode
                    if character_tracker is not None
                    else "yolo-person"
                ),
                "self_match_ms": 人物匹配耗时,
                "monster_match_ms": 怪物匹配耗时,
                "health_bar_count": 血条目标数量,
                "health_bar_green_ratios": [
                    round(float(命中.green_ratio), 4)
                    for 命中 in 血条命中
                ],
                "fps": 1000.0 / max(帧处理耗时, 0.001),
                "capture_region": 截图区域说明,
            },
            (
                (人物局部坐标[0] + 预览偏移X, 人物局部坐标[1] + 预览偏移Y)
                if 人物局部坐标 is not None
                else None
            ),
            [
                (
                    box[0] + 预览偏移X,
                    box[1] + 预览偏移Y,
                    box[2] + 预览偏移X,
                    box[3] + 预览偏移Y,
                )
                for box in 预览怪物框
            ],
        )
        预览发布耗时 = (time.perf_counter() - 预览发布开始时间) * 1000
        if not 怪物检测就绪事件.is_set():
            怪物检测就绪事件.set()
            trace_event("monster_detector_ready", detector="template")

        诊断日志开始时间 = time.perf_counter()
        if 输出详细日志:
            log_template_detection_snapshot(
                诊断帧编号, dituming, search_range, vertical_range, 共享坐标检测前, 读取人物位置(),
                人物定位说明, (ccx, ccy), 攻击参考点, total_targets, 目标判定详情,
                total_nearby, left_right_counts, 获取红蓝检测坐标(), 轮子置信度,
                (lun_center_x, lun_center_y), 判定前状态, (zant, 攻击, 没怪数量),
                {
                    "thread": threading.current_thread().name,
                    "template_count": len(monster_templates),
                    "capture_ms": 截图耗时,
                    "monster_ms": 怪物匹配耗时,
                    "health_bar_ms": 血条检测耗时,
                    "health_bar_count": 血条目标数量,
                    "person_ms": 人物匹配耗时,
                    "frame_ms": 帧处理耗时,
                },
            )
        诊断日志耗时 = (time.perf_counter() - 诊断日志开始时间) * 1000
        完整帧耗时 = (time.perf_counter() - 帧开始时间) * 1000
        已分类耗时 = sum((
            截图耗时,
            人物匹配耗时,
            ROI构建耗时,
            怪物匹配耗时,
            血条检测耗时,
            目标判定耗时,
            药水检测耗时,
            轮子检测耗时,
            公共快照耗时,
            攻击结果耗时,
            基础追踪日志耗时,
            预览发布耗时,
            诊断日志耗时,
        ))
        未分类耗时 = max(0.0, 完整帧耗时 - 已分类耗时)
        trace_event(
            "template_detection_performance",
            frame_number=诊断帧编号,
            map=dituming,
            frame_interval_ms=(
                round(帧间隔耗时, 3)
                if 帧间隔耗时 is not None
                else None
            ),
            capture_ms=round(截图耗时, 3),
            full_preview_capture=bool(
                测试截图模式 and 预览画面 is not None
            ),
            person_ms=round(人物匹配耗时, 3),
            roi_ms=round(ROI构建耗时, 3),
            monster_ms=round(怪物匹配耗时, 3),
            health_bar_ms=round(血条检测耗时, 3),
            health_bar_targets=血条目标数量,
            health_bar_attackable=血条可攻击活怪数量,
            health_bar_chase_targets=血条追怪活怪数量,
            health_bar_linked_targets=血条关联目标数量,
            health_bar_inferred_targets=血条推断目标数量,
            health_bar_green_max_ratio=(
                round(血条最大绿色比例, 4)
                if 血条最大绿色比例 is not None
                else None
            ),
            target_evaluation_ms=round(目标判定耗时, 3),
            potion_ms=round(药水检测耗时, 3),
            wheel_ms=round(轮子检测耗时, 3),
            snapshot_ms=round(公共快照耗时, 3),
            attack_result_ms=round(攻击结果耗时, 3),
            trace_ms=round(基础追踪日志耗时, 3),
            preview_ms=round(预览发布耗时, 3),
            diagnostic_ms=round(诊断日志耗时, 3),
            unaccounted_ms=round(未分类耗时, 3),
            frame_total_ms=round(完整帧耗时, 3),
            search_mode=模板搜索模式,
            search_left=monitor['left'] + 模板搜索偏移X,
            search_top=monitor['top'] + 模板搜索偏移Y,
            search_width=int(模板搜索画面.shape[1]),
            search_height=int(模板搜索画面.shape[0]),
            search_horizontal_radius=模板搜索横向半径,
            search_vertical_radius=vertical_range,
            used_stale_person_position=模板ROI使用上一人物位置,
            fallback_full_frame=模板搜索模式.startswith("full-"),
            template_count=len(monster_templates),
            template_workers=模板匹配线程数,
            targets=total_targets,
            nearby=total_nearby,
        )


        if not 等待下一检测帧(帧开始时间, TEMPLATE_TARGET_FRAME_SECONDS):
            break
    模板匹配线程池.shutdown(wait=False)

def 获取人物固定范围模板搜索画面(
    frame,
    monitor,
    person_screen_position,
    attack_reference_y,
    horizontal_radius=300,
    vertical_radius=70,
):
    """按人物中心裁剪固定怪物搜索区，人物丢失或区域无效时返回全图。

    调用方应传入覆盖攻击范围和智能追怪额外范围的横向半径；纵向半径直接使用
    页面设置的攻击高度，使模板计算量和用户看到的配置保持一致。
    """
    if person_screen_position is None or attack_reference_y is None:
        return frame, 0, 0, "full-person-missing"

    frame_height, frame_width = frame.shape[:2]
    local_x = int(round(person_screen_position[0] - monitor["left"]))
    local_y = int(round(attack_reference_y - monitor["top"]))
    radius_x = max(1, int(horizontal_radius))
    radius_y = max(1, int(vertical_radius))
    left = max(0, local_x - radius_x)
    right = min(frame_width, local_x + radius_x)
    top = max(0, local_y - radius_y)
    bottom = min(frame_height, local_y + radius_y)
    if right <= left or bottom <= top:
        return frame, 0, 0, "full-invalid-fixed-roi"
    return frame[top:bottom, left:right], left, top, "person-fixed"


# 目标检测线程
def detection_threadyolo(
    名字,
    search_range,
    vertical_range=70,
    template_directory=None,
    map_name=None,
):
    """保留原地图战斗上下文，怪物位置可来自YOLO或自定义图片模板。"""
    global renwu_pos
    global 攻击
    global 发现轮
    global stop_event2
    global zant
    zant=0
    global didd
    print("群体是", didd)
    resource_name = 名字
    dituming = map_name or resource_name
    basedir = get_base_dir()
    path2 = basedir+"\\moxing\\"+resource_name
    search_range, vertical_range = int(search_range), int(vertical_range)
    模板搜索横向半径 = max(
        300,
        search_range + int(智能追怪额外横向范围),
    )


    monitor2 = {'top':163 , 'left':479 , 'width': 280, 'height': 100}

    monitor = 获取怪物检测区域(dituming)
    没怪数量=0
    名字=path2+".pt"
    custom_template_requested = bool(template_directory)
    monster_templates = (
        load_monster_templates(template_directory)
        if custom_template_requested
        else []
    )
    if custom_template_requested and not monster_templates:
        raise ValueError(
            "已开启自定义怪物模板，但没有可读取的 guai* 图片：{}；"
            "为避免回退YOLO，本次任务已停止。".format(template_directory)
        )
    model = None if custom_template_requested else get_yolo_model(名字)
    if custom_template_requested:
        print("[自定义模板] 保留原地图攻击逻辑，怪物识别改用：{}".format(template_directory))
        print("[自定义模板] YOLO已禁用，本次只使用guai*图片识别怪物。")

    basedir = get_base_dir()
    path = basedir + "\\resres\\"
    if 启用轮检测:
        luntup = path + 'lun.png'
        lun_template = cv2.imread(luntup)
        luntishi = cv2.imread(path + 'fuwentishi.png')
    else:
        lun_template = None
        luntishi = None
    # 人物图片模板启动时只选择一次，后续由 CharacterTracker 局部跟踪。
    character_templates = require_character_templates(cv2, basedir)
    selected_character_template = select_character_template(
        cv2,
        np,
        grab_screen,
        character_templates,
        monitor=monitor,
        stop_requested=已请求停止,
        wait=可中断等待,
        log_prefix=("[模板人物]" if custom_template_requested else "[YOLO人物]"),
    )
    if 已请求停止():
        return
    if selected_character_template is None:
        raise RuntimeError("未能选择人物定位模板")
    图片名, xue_template = selected_character_template
    character_tracker = CharacterTracker(selected_character_template, monitor)
    facing_tracker = (
        CharacterFacingTracker(cv2, np)
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
        else None
    )
    if facing_tracker is not None:
        print(
            "[朝向识别] 左模板={}x{}，右模板={}x{}，来源={}。".format(
                facing_tracker.left_template.shape[1],
                facing_tracker.left_template.shape[0],
                facing_tracker.right_template.shape[1],
                facing_tracker.right_template.shape[0],
                facing_tracker.template_source,
            )
        )
        trace_event(
            "character_facing_config",
            template_source=facing_tracker.template_source,
            left_size=[
                facing_tracker.left_template.shape[1],
                facing_tracker.left_template.shape[0],
            ],
            right_size=[
                facing_tracker.right_template.shape[1],
                facing_tracker.right_template.shape[0],
            ],
            confidence_threshold=facing_tracker.confidence_threshold,
            margin_threshold=facing_tracker.margin_threshold,
            aux_source="user_left_star+user_right_star",
            aux_left_size=[
                facing_tracker.left_aux_template.shape[1],
                facing_tracker.left_aux_template.shape[0],
            ],
            aux_right_size=[
                facing_tracker.right_aux_template.shape[1],
                facing_tracker.right_aux_template.shape[0],
            ],
            aux_confidence_threshold=facing_tracker.aux_confidence_threshold,
            aux_margin_threshold=facing_tracker.aux_margin_threshold,
            aux_search_relative=[
                -facing_tracker.aux_radius_x,
                facing_tracker.aux_top_offset,
                facing_tracker.aux_radius_x,
                facing_tracker.aux_bottom_offset,
            ],
        )
    print(
        "[加载] 人物图片模板：{}，尺寸={}x{}，局部搜索={}x{}。".format(
            图片名,
            xue_template.shape[1],
            xue_template.shape[0],
            character_tracker.radius_x * 2,
            character_tracker.radius_y * 2,
        )
    )
    发现轮 = 1 if 启用轮检测 else 0
    YOLO帧序号 = 0
    上一帧开始时间 = None
    频率窗口开始时间 = time.monotonic()
    频率窗口帧数 = 0
    频率窗口间隔总毫秒 = 0.0
    频率窗口间隔数 = 0
    频率窗口推理总毫秒 = 0.0
    频率窗口完整帧总毫秒 = 0.0
    # 以下状态与参考工程保持一致；只服务于参考版攻击后的目标确认节奏。
    V2上次确认攻击完成时间 = 0.0
    V2攻击后近怪连续帧数 = 0
    V2本次攻击已确认存活 = False
    V2连续无目标帧数 = 0
    V2最近攻击目标方向 = None
    V2最近攻击目标距离 = None
    V2最近攻击目标数量 = 0
    V2攻击基准方向 = None
    V2攻击基准距离 = None
    V2攻击基准数量 = 0
    V2血条纵向偏移估计 = MONSTER_HEALTH_BAR_DEFAULT_Y_OFFSET
    V2上次朝向日志状态 = None
    V2上次朝向日志时间 = 0.0
    模板匹配线程数 = (
        max(1, min(4, len(monster_templates)))
        if monster_templates
        else 0
    )
    模板匹配线程池 = (
        ThreadPoolExecutor(
            max_workers=模板匹配线程数,
            thread_name_prefix="monster-template",
        )
        if monster_templates
        else None
    )
    启用怪物血条检测 = bool(monster_templates) or dituming in (
        "蘑菇V2",
        "蘑菇V3",
        "自定义录制路线",
    )
    if monster_templates:
        trace_event(
            "template_detector_config",
            map=dituming,
            template_count=len(monster_templates),
            template_workers=模板匹配线程数,
            search_horizontal_radius=模板搜索横向半径,
            search_vertical_radius=vertical_range,
            attack_range_x=search_range,
            attack_range_y=vertical_range,
            target_frame_seconds=YOLO_TARGET_FRAME_SECONDS,
        )
    while True:
        if stop_event2 == 0:
            break
        当前帧开始时间 = time.monotonic()
        帧间隔毫秒 = (
            None
            if 上一帧开始时间 is None
            else (当前帧开始时间 - 上一帧开始时间) * 1000
        )
        上一帧开始时间 = 当前帧开始时间
        YOLO帧序号 += 1
        帧开始时间 = time.perf_counter()
        img, 预览画面, 预览偏移X, 预览偏移Y, 截图区域说明 = 捕获检测与预览画面(
            monitor
        )
        img2 = None
        if 启用轮检测 and 发现轮 == 0:
            # 只有等待轮子提示时才抓取顶部小区域，正常战斗帧不做额外截图。
            sct_img2 = grab_screen(monitor2)
            img2 = np.array(sct_img2)
            img2 = cv2.cvtColor(img2, cv2.COLOR_BGRA2BGR)
        centers = []
        怪物置信度列表 = []
        预览怪物框 = []
        # 先定位人物，再把多模板匹配限制在人物攻击范围和额外追怪范围附近。
        # CharacterTracker 正常使用上一帧局部搜索，日志实测通常只需约1～2ms。
        if 人物全图重定位事件.is_set():
            character_tracker.reset()
            人物全图重定位事件.clear()
            trace_event("character_full_relocation_applied", detector="main")
        人物置信度, 人物屏幕坐标, _人物局部坐标 = character_tracker.locate(
            cv2,
            img,
        )
        ccx = 0
        ccy = 0
        match_y = 0
        人物定位有效 = (
            人物屏幕坐标 is not None
            and 人物置信度 >= PERSON_MATCH_THRESHOLD
        )
        人物定位短暂保留 = False
        人物定位保留年龄毫秒 = None
        战斗人物屏幕坐标 = 人物屏幕坐标 if 人物定位有效 else None
        if not 人物定位有效:
            战斗人物屏幕坐标 = character_tracker.recent_screen_center(
                now=当前帧开始时间,
            )
            if 战斗人物屏幕坐标 is not None:
                人物定位短暂保留 = True
                人物定位保留年龄毫秒 = (
                    character_tracker.recent_screen_center_age_ms(
                        now=当前帧开始时间,
                    )
                )
        人物定位可用于战斗 = 战斗人物屏幕坐标 is not None
        if 人物定位可用于战斗:
            ccx, ccy = 战斗人物屏幕坐标
            match_y = character_attack_reference_y(图片名, ccy)

        # 人物模板偶发低分时，200ms内可沿用上一可靠坐标计算方向和距离；超时后
        # 旧坐标只用于裁剪怪物搜索区，不能再发布攻击。没有历史位置才回退全图。
        模板ROI人物屏幕坐标 = (
            (ccx, ccy) if 人物定位可用于战斗 else None
        )
        模板ROI攻击参考Y = match_y
        模板ROI使用上一人物位置 = 人物定位短暂保留
        if (
            not 人物定位可用于战斗
            and character_tracker.last_local_center is not None
        ):
            上一人物局部X, 上一人物局部Y = character_tracker.last_local_center
            模板ROI人物屏幕坐标 = (
                monitor['left'] + 上一人物局部X,
                monitor['top'] + 上一人物局部Y,
            )
            模板ROI攻击参考Y = character_attack_reference_y(
                图片名,
                模板ROI人物屏幕坐标[1],
            )
            模板ROI使用上一人物位置 = True

        人物朝向观测 = None
        if facing_tracker is not None:
            人物朝向观测 = facing_tracker.observe(
                cv2,
                np,
                img,
                monitor,
                人物屏幕坐标 if 人物定位有效 else None,
                detected_at=当前帧开始时间,
            )
            更新人物朝向感知(人物朝向观测)
            朝向日志状态 = (
                "reliable-{}-{}".format(
                    人物朝向观测.detected_direction,
                    facing_tracker.last_decision_source,
                )
                if 人物朝向观测.reliable
                else (
                    "black-flicker-fallback"
                    if 人物朝向观测.black_flicker_suspected
                    else "low-confidence-fallback"
                )
            )
            if (
                朝向日志状态 != V2上次朝向日志状态
                or 当前帧开始时间 - V2上次朝向日志时间 >= 5.0
            ):
                trace_event(
                    "character_facing_frame",
                    detected_direction=人物朝向观测.detected_direction,
                    reliable_direction=人物朝向观测.reliable_direction,
                    left_confidence=round(人物朝向观测.left_confidence, 4),
                    right_confidence=round(人物朝向观测.right_confidence, 4),
                    confidence_margin=round(人物朝向观测.confidence_margin, 4),
                    black_flicker_suspected=(
                        人物朝向观测.black_flicker_suspected
                    ),
                    reliable=人物朝向观测.reliable,
                    match_ms=round(人物朝向观测.match_ms, 3),
                    template_source=facing_tracker.template_source,
                    decision_source=facing_tracker.last_decision_source,
                    aux_direction=facing_tracker.last_aux_direction,
                    aux_left_confidence=round(
                        facing_tracker.last_aux_left_confidence,
                        4,
                    ),
                    aux_right_confidence=round(
                        facing_tracker.last_aux_right_confidence,
                        4,
                    ),
                    aux_confidence_margin=round(
                        facing_tracker.last_aux_confidence_margin,
                        4,
                    ),
                    aux_reliable=facing_tracker.last_aux_reliable,
                    aux_roi=facing_tracker.last_aux_roi,
                    action=朝向日志状态,
                )
                V2上次朝向日志状态 = 朝向日志状态
                V2上次朝向日志时间 = 当前帧开始时间

        怪物检测开始时间 = time.perf_counter()
        模板搜索模式 = None
        模板搜索偏移X = 0
        模板搜索偏移Y = 0
        模板搜索画面 = img
        V2血条目标数量 = 0
        V2血条关联目标数量 = 0
        V2血条推断目标数量 = 0
        V2血条最大绿色比例 = None
        V2血条活怪中心索引 = set()
        if monster_templates:
            模板搜索画面, 模板搜索偏移X, 模板搜索偏移Y, 模板搜索模式 = (
                获取人物固定范围模板搜索画面(
                    img,
                    monitor,
                    模板ROI人物屏幕坐标,
                    模板ROI攻击参考Y,
                    horizontal_radius=模板搜索横向半径,
                    vertical_radius=vertical_range,
                )
            )
            if 模板ROI使用上一人物位置:
                模板搜索模式 = (
                    "person-fixed-held"
                    if 人物定位短暂保留
                    else "person-fixed-stale-no-attack"
                )
            模板命中 = match_monster_templates_parallel(
                cv2,
                np,
                模板搜索画面,
                monster_templates,
                executor=模板匹配线程池,
                threshold=MONSTER_TEMPLATE_CONFIDENCE,
                x_offset=monitor['left'] + 模板搜索偏移X,
                y_offset=monitor['top'] + 模板搜索偏移Y,
            )
            for 命中 in 模板命中:
                centers.append(命中.center)
                怪物置信度列表.append(命中.confidence)
                本地框 = (
                    命中.output_box[0] - monitor['left'],
                    命中.output_box[1] - monitor['top'],
                    命中.output_box[2] - monitor['left'],
                    命中.output_box[3] - monitor['top'],
                )
                预览怪物框.append(本地框)
                if DRAW_DETECTION_BOXES:
                    cv2.rectangle(img, 本地框[:2], 本地框[2:], (0, 255, 0), 2)
            if 启用怪物血条检测:
                血条检测结果 = detect_and_associate_monster_health_bars(
                    cv2,
                    np,
                    模板搜索画面,
                    centers,
                    V2血条纵向偏移估计,
                    x_offset=monitor['left'] + 模板搜索偏移X,
                    y_offset=monitor['top'] + 模板搜索偏移Y,
                )
                血条命中 = 血条检测结果.matches
                V2血条目标数量 = len(血条命中)
                V2血条最大绿色比例 = 血条检测结果.max_green_ratio
                centers = list(血条检测结果.centers)
                V2血条活怪中心索引 = set(
                    血条检测结果.alive_center_indices
                )
                V2血条关联目标数量 = 血条检测结果.linked_count
                V2血条推断目标数量 = 血条检测结果.inferred_count
                V2血条纵向偏移估计 = 血条检测结果.y_offset_estimate
                for 血条 in 血条命中:
                    血条本地框 = (
                        血条.output_box[0] - monitor['left'],
                        血条.output_box[1] - monitor['top'],
                        血条.output_box[2] - monitor['left'],
                        血条.output_box[3] - monitor['top'],
                    )
                    预览怪物框.append(血条本地框)
                    if DRAW_DETECTION_BOXES:
                        cv2.rectangle(
                            img,
                            血条本地框[:2],
                            血条本地框[2:],
                            (255, 255, 0),
                            1,
                        )
        else:
            results = model(
                img,
                verbose=False,
                conf=YOLO怪物置信度,
                classes=[0],
                max_det=YOLO_MAX_DETECTIONS,
            )
            for result in results:
                for box in result.boxes:
                    if int(box.cls[0]) == 0:
                        xyxy = box.xyxy[0].cpu().numpy()
                        conf = float(box.conf[0].cpu().numpy())
                        怪物置信度列表.append(conf)
                        预览怪物框.append(
                            (
                                int(xyxy[0]),
                                int(xyxy[1]),
                                int(xyxy[2]),
                                int(xyxy[3]),
                            )
                        )
                        if DRAW_DETECTION_BOXES:
                            cv2.rectangle(
                                img,
                                (int(xyxy[0]), int(xyxy[1])),
                                (int(xyxy[2]), int(xyxy[3])),
                                (0, 255, 0),
                                2,
                            )
                            cv2.putText(
                                img,
                                "{:.2f}".format(conf),
                                (int(xyxy[0]), max(15, int(xyxy[1]) - 5)),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.5,
                                (0, 255, 0),
                                1,
                            )

                        cx = (xyxy[0] + xyxy[2]) / 2
                        cy = (xyxy[1] + xyxy[3]) / 2 + monitor['top']
                        centers.append((cx, cy))
        怪物检测耗时 = (time.perf_counter() - 怪物检测开始时间) * 1000
        left_right_counts = {'左边': 0, '右边': 0}
        total_nearby = 0
        自动群攻怪物数量 = 0
        近身群攻怪物数量 = 0
        最近目标方向 = None
        最近候选横向距离 = float("inf")
        最近攻击目标横向距离 = float("inf")
        最近候选纵向距离 = None
        战斗可见数量 = 0
        战斗可见左边 = 0
        战斗可见右边 = 0
        战斗可见方向 = None
        战斗可见最近距离 = float("inf")
        追怪可见数量 = 0
        追怪可见左边 = 0
        追怪可见右边 = 0
        追怪可见方向 = None
        追怪可见最近距离 = float("inf")
        V2血条可攻击活怪数量 = 0
        V2血条追怪活怪数量 = 0
        V2血条可攻击左边数量 = 0
        V2血条可攻击右边数量 = 0
        V2血条追怪左边数量 = 0
        V2血条追怪右边数量 = 0
        V2血条可攻击方向 = None
        V2血条追怪方向 = None

        # 完全采用参考工程的目标分层规则：人物左右两侧都可进入追怪区，
        # 最近目标决定方向；不再按JSON路线前进方向过滤后方怪物。
        if 人物定位可用于战斗:
            for 中心索引, (cx, cy) in enumerate(centers):
                是血条活怪 = 中心索引 in V2血条活怪中心索引
                横向距离, 纵向距离, 进入攻击范围 = evaluate_attack_target(
                    ccx,
                    match_y,
                    (cx, cy),
                    search_range,
                    vertical_range,
                )
                if 横向距离 <= AUTO_GROUP_ATTACK_HORIZONTAL_RANGE:
                    自动群攻怪物数量 += 1
                if 横向距离 <= CLOSE_GROUP_ATTACK_HORIZONTAL_RANGE:
                    近身群攻怪物数量 += 1
                if 横向距离 < 最近候选横向距离:
                    最近候选横向距离 = 横向距离
                    最近候选纵向距离 = 纵向距离
                目标方向 = "left" if ccx > cx else "right"
                if (
                    横向距离 < search_range + 80
                    and 纵向距离 < vertical_range + 30
                ):
                    战斗可见数量 += 1
                    if 目标方向 == "left":
                        战斗可见左边 += 1
                    else:
                        战斗可见右边 += 1
                    if 横向距离 < 战斗可见最近距离:
                        战斗可见最近距离 = 横向距离
                        战斗可见方向 = 目标方向
                if (
                    横向距离 < search_range + 智能追怪额外横向范围
                    and 纵向距离 < vertical_range
                ):
                    追怪可见数量 += 1
                    if 目标方向 == "left":
                        追怪可见左边 += 1
                    else:
                        追怪可见右边 += 1
                    if 是血条活怪:
                        V2血条追怪活怪数量 += 1
                        if 目标方向 == "left":
                            V2血条追怪左边数量 += 1
                        else:
                            V2血条追怪右边数量 += 1
                    if 横向距离 < 追怪可见最近距离:
                        追怪可见最近距离 = 横向距离
                        追怪可见方向 = 目标方向
                    if 是血条活怪 and V2血条追怪方向 is None:
                        V2血条追怪方向 = 目标方向
                if not 进入攻击范围:
                    continue
                total_nearby += 1
                side = '左边' if 目标方向 == "left" else '右边'
                left_right_counts[side] += 1
                if 是血条活怪:
                    V2血条可攻击活怪数量 += 1
                    if 目标方向 == "left":
                        V2血条可攻击左边数量 += 1
                    else:
                        V2血条可攻击右边数量 += 1
                    if V2血条可攻击方向 is None:
                        V2血条可攻击方向 = 目标方向
                if 横向距离 < 最近攻击目标横向距离:
                    最近攻击目标横向距离 = 横向距离
                    最近目标方向 = 目标方向



        检查并补充红蓝()











        if 启用轮检测 and lun_template is not None and luntishi is not None:
            if 发现轮==0:
                res_lun = cv2.matchTemplate(img2, luntishi, cv2.TM_CCOEFF_NORMED)
                _, max_val_lun, _, max_loc_lun = cv2.minMaxLoc(res_lun)
                if max_val_lun >= 0.5:
                    发现轮 = 1
                    print("-------------------------------------------------------------------------------发现轮")
            if 发现轮 >0:
                res_lun = cv2.matchTemplate(img, lun_template, cv2.TM_CCOEFF_NORMED)
                _, max_val_lun, _, max_loc_lun = cv2.minMaxLoc(res_lun)
                if max_val_lun >= 0.65:
                    lun_center_x = max_loc_lun[0] + lun_template.shape[1] // 2
                    lun_center_y = max_loc_lun[1] + lun_template.shape[0] // 2
                    lun_center_y = lun_center_y + 300
                    print("轮坐标", lun_center_x, lun_center_y)
                    print("人坐标", ccx, ccy)
                    cha = abs(ccx - lun_center_x)
                    print("2个间距", cha)
                    cha2 = abs(ccy - lun_center_y)
                    if cha2 < 100:
                        if cha <50:
                            发现轮 = 22
                            print("轮左边100")
                            pydirectinput.keyUp('right')
                            pydirectinput.keyUp('left')
                            pydirectinput.keyUp('z')
                            pydirectinput.press(单体按键)
                            pydirectinput.keyDown('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('up')
                            zant = 0
                            zant = 2
                            pydirectinput.keyDown('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('up')
                            pydirectinput.keyDown('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('up')
                            pydirectinput.keyDown('up')
                            time.sleep(0.1)
                            pydirectinput.keyUp('up')
                            time.sleep(4)
                            jielun()
                            zant = 0
                            time.sleep(1)
                    else:
                        print(f"差值过大: {cha2} > 50")  # 添加else分支验证
                else:
                    if 发现轮==22:
                        发现轮=0
        V2决策可攻击数量 = total_nearby
        V2决策攻击方向 = 最近目标方向
        V2近身补刀提升 = False
        if (
            dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
            and total_nearby <= 0
            and 追怪可见数量 > 0
            and 追怪可见最近距离 != float("inf")
            and 追怪可见最近距离
            <= search_range + V2近身补刀额外横向范围
        ):
            # 怪物仍与人物同层且只被轻微击退时，不切成追怪走路；直接按照
            # 最近怪物方向发布单体补刀意图，背后的怪会先转身再攻击。
            V2决策可攻击数量 = 1
            V2决策攻击方向 = 追怪可见方向
            V2近身补刀提升 = True

        发布追怪快照 = True
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线"):
            if 追怪可见数量 > 0:
                V2连续无目标帧数 = 0
            else:
                V2连续无目标帧数 += 1
                if V2连续无目标帧数 < V2目标丢失确认帧数:
                    发布追怪快照 = False
                    trace_event(
                        "mushroom_v2_target_loss_confirmation",
                        missing_frame=V2连续无目标帧数,
                        confirmation_required=V2目标丢失确认帧数,
                        action="keep_previous_snapshot",
                    )
        可发布攻击数量 = V2决策可攻击数量
        攻击确认中 = False
        if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线"):
            with 攻击状态锁:
                当前最近攻击完成时间 = 最近攻击完成时间
            if 当前最近攻击完成时间 > V2上次确认攻击完成时间:
                # 新一次攻击完成时冻结“触发该次攻击的最后目标”，后续新截图
                # 与它比较位置、方向和数量，区分活怪移动与静止死亡残影。
                V2攻击基准方向 = V2最近攻击目标方向
                V2攻击基准距离 = V2最近攻击目标距离
                V2攻击基准数量 = V2最近攻击目标数量
                V2上次确认攻击完成时间 = 当前最近攻击完成时间
                V2攻击后近怪连续帧数 = 0
                V2本次攻击已确认存活 = False
            攻击后经过秒数 = 当前帧开始时间 - 当前最近攻击完成时间
            if (
                当前最近攻击完成时间 > 0
                and 0 < 攻击后经过秒数 <= V2死亡动画确认窗口秒数
            ):
                目标距离变化 = (
                    abs(追怪可见最近距离 - V2攻击基准距离)
                    if (
                        追怪可见最近距离 != float("inf")
                        and V2攻击基准距离 is not None
                    )
                    else 0.0
                )
                绿色血条明确存活 = (
                    V2血条可攻击活怪数量 > 0
                    or (
                        V2近身补刀提升
                        and V2血条追怪活怪数量 > 0
                    )
                )
                if V2决策可攻击数量 > 0 and 绿色血条明确存活:
                    V2攻击后近怪连续帧数 = V2攻击后存活确认帧数
                    if not V2本次攻击已确认存活:
                        V2本次攻击已确认存活 = True
                        trace_event(
                            "monster_health_bar_alive_confirmed",
                            baseline_direction=V2攻击基准方向,
                            current_direction=V2决策攻击方向,
                            baseline_distance=V2攻击基准距离,
                            current_distance=(
                                round(追怪可见最近距离, 2)
                                if 追怪可见最近距离 != float("inf")
                                else None
                            ),
                            distance_delta=round(目标距离变化, 2),
                            baseline_count=V2攻击基准数量,
                            current_count=V2决策可攻击数量,
                            health_bar_attackable=V2血条可攻击活怪数量,
                            health_bar_chase_targets=V2血条追怪活怪数量,
                            health_bar_green_max_ratio=(
                                round(V2血条最大绿色比例, 4)
                                if V2血条最大绿色比例 is not None
                                else None
                            ),
                            movement_threshold=V2存活目标最小位移像素,
                            action="fast_reattack",
                        )
                elif V2决策可攻击数量 > 0:
                    # 攻击动画会让人物模板横向漂移，多只怪之间的最近目标
                    # 也会切换。没有血条证据时，这些相对距离变化不能证明怪物
                    # 仍存活；整个死亡动画窗口保持停手，窗口结束后再按新目标处理。
                    if V2攻击后近怪连续帧数 <= 0:
                        trace_event(
                            "post_attack_target_confirmation",
                            detected_targets=V2决策可攻击数量,
                            confirmation_frame=1,
                            confirmation_required=V2攻击后存活确认帧数,
                            frame_after_attack_ms=round(
                                攻击后经过秒数 * 1000,
                                3,
                            ),
                            confirmation_window_ms=round(
                                V2死亡动画确认窗口秒数 * 1000,
                                1,
                            ),
                            baseline_direction=V2攻击基准方向,
                            current_direction=V2决策攻击方向,
                            baseline_distance=V2攻击基准距离,
                            current_distance=(
                                round(追怪可见最近距离, 2)
                                if 追怪可见最近距离 != float("inf")
                                else None
                            ),
                            distance_delta=round(目标距离变化, 2),
                            health_bar_evidence=False,
                            reason="wait_out_death_animation_without_health_bar",
                            action="hold_without_attack",
                        )
                    V2攻击后近怪连续帧数 = 1
                    可发布攻击数量 = 0
                    攻击确认中 = True
                else:
                    V2攻击后近怪连续帧数 = 0
            elif 攻击后经过秒数 > V2死亡动画确认窗口秒数:
                # 超过死亡动画窗口后遇到的是新目标，不再沿用上次攻击的确认计数。
                V2攻击后近怪连续帧数 = V2攻击后存活确认帧数
            if (
                V2决策可攻击数量 > 0
                and (
                    当前最近攻击完成时间 <= 0
                    or 当前帧开始时间 > 当前最近攻击完成时间
                )
            ):
                V2最近攻击目标方向 = V2决策攻击方向
                V2最近攻击目标距离 = (
                    追怪可见最近距离
                    if 追怪可见最近距离 != float("inf")
                    else None
                )
                V2最近攻击目标数量 = V2决策可攻击数量
        if 发布追怪快照:
            # 必须在攻击后死亡动画过滤完成后再发布给路线线程。
            # 若先发布未过滤的数量，路线线程会在本帧已经决定
            # ``可发布攻击数量=0`` 时仍消费旧的可攻击快照，形成持续空打。
            更新追怪感知(
                可发布攻击数量,
                追怪可见数量,
                追怪可见左边,
                追怪可见右边,
                attackable_left_count=(
                    left_right_counts['左边'] if 可发布攻击数量 > 0 else 0
                ),
                attackable_right_count=(
                    left_right_counts['右边'] if 可发布攻击数量 > 0 else 0
                ),
                preferred_direction=追怪可见方向,
                nearest_distance=(
                    追怪可见最近距离
                    if 追怪可见最近距离 != float("inf")
                    else None
                ),
                detected_at=当前帧开始时间,
                automatic_group_count=自动群攻怪物数量,
                close_group_count=近身群攻怪物数量,
                configured_group_over_count=自动群攻大于数量,
                health_bar_chase_left_count=V2血条追怪左边数量,
                health_bar_chase_right_count=V2血条追怪右边数量,
                health_bar_attackable_left_count=V2血条可攻击左边数量,
                health_bar_attackable_right_count=V2血条可攻击右边数量,
                attack_confirmation_pending=攻击确认中,
            )
        战斗前状态 = zant
        没怪数量 = 处理怪物检测结果(
            可发布攻击数量,
            left_right_counts['左边'],
            left_right_counts['右边'],
            没怪数量,
            allow_group_attack=(didd == 1),
            preferred_direction=V2决策攻击方向,
            visible_nearby=战斗可见数量,
            visible_left_count=战斗可见左边,
            visible_right_count=战斗可见右边,
            visible_preferred_direction=战斗可见方向,
            hold_visible_combat=(dituming not in ("蘑菇V2", "蘑菇V3", "自定义录制路线")),
            detected_at=当前帧开始时间,
        )
        if 战斗前状态 != 1 and zant == 1:
            if monster_templates:
                print(
                    "[战斗] 锁定近距离怪物，模板匹配={:.1f}ms，"
                    "人物匹配={:.2f}ms，模板阈值=0.80。".format(
                        怪物检测耗时,
                        character_tracker.last_match_ms,
                    )
                )
            else:
                print(
                    "[战斗] 锁定近距离怪物，YOLO={:.1f}ms，"
                    "人物匹配={:.2f}ms，置信度阈值={:.2f}。".format(
                        怪物检测耗时,
                        character_tracker.last_match_ms,
                        YOLO怪物置信度,
                    )
                )
        elif 战斗前状态 == 1 and zant == 0:
            print("[战斗] 连续 {} 帧未发现近怪，恢复路线。".format(
                COMBAT_TARGET_LOST_CONFIRMATION_FRAMES
            ))
        完整帧耗时 = (time.perf_counter() - 帧开始时间) * 1000
        日志人物坐标, 日志怪物坐标, 日志怪物相对坐标 = 构建紧凑怪物坐标日志(
            (ccx if 人物定位可用于战斗 else None),
            (match_y if 人物定位可用于战斗 else None),
            centers,
        )
        trace_event(
            "detection_frame",
            detector=("template" if monster_templates else "yolo"),
            map=dituming,
            p=日志人物坐标,
            m=日志怪物坐标,
            mr=日志怪物相对坐标,
            targets=len(centers),
            nearby=total_nearby,
            combat_visible=战斗可见数量,
            left=left_right_counts['左边'],
            right=left_right_counts['右边'],
            max_monster_confidence=(
                round(max(怪物置信度列表), 4) if 怪物置信度列表 else None
            ),
            yolo_threshold=(None if monster_templates else round(YOLO怪物置信度, 3)),
            monster_ms=round(怪物检测耗时, 3),
            person_ms=round(character_tracker.last_match_ms, 3),
            person_confidence=round(float(人物置信度), 4),
            person_mode=character_tracker.last_mode,
            person_position_held=人物定位短暂保留,
            person_position_age_ms=(
                round(人物定位保留年龄毫秒, 1)
                if 人物定位保留年龄毫秒 is not None
                else None
            ),
            nearest_dx=(
                round(最近候选横向距离, 2)
                if 最近候选横向距离 != float("inf")
                else None
            ),
            nearest_dy=(
                round(最近候选纵向距离, 2)
                if 最近候选纵向距离 is not None
                else None
            ),
            attack_range_x=search_range,
            attack_range_y=vertical_range,
            capture_left=monitor['left'],
            capture_top=monitor['top'],
            capture_width=monitor['width'],
            capture_height=monitor['height'],
            chase_range_x=search_range + 智能追怪额外横向范围,
            chase_extra_x=智能追怪额外横向范围,
            chase_forward_direction=None,
            rear_chase_enabled=读取智能追怪双向模式(),
            chase_targets=追怪可见数量,
            chase_direction=追怪可见方向,
            chase_left=追怪可见左边,
            chase_right=追怪可见右边,
            decision_direction=V2决策攻击方向,
            chase_nearest_dx=(
                round(追怪可见最近距离, 2)
                if 追怪可见最近距离 != float("inf")
                else None
            ),
            decision_attackable=V2决策可攻击数量,
            health_bar_targets=V2血条目标数量,
            health_bar_attackable=V2血条可攻击活怪数量,
            health_bar_chase_targets=V2血条追怪活怪数量,
            health_bar_linked_targets=V2血条关联目标数量,
            health_bar_inferred_targets=V2血条推断目标数量,
            health_bar_green_max_ratio=(
                round(V2血条最大绿色比例, 4)
                if V2血条最大绿色比例 is not None
                else None
            ),
            health_bar_y_offset_estimate=(
                round(V2血条纵向偏移估计, 2)
                if 启用怪物血条检测
                else None
            ),
            facing_direction=(
                人物朝向观测.detected_direction if 人物朝向观测 else None
            ),
            facing_reliable_direction=(
                人物朝向观测.reliable_direction if 人物朝向观测 else None
            ),
            facing_left_confidence=(
                round(人物朝向观测.left_confidence, 4)
                if 人物朝向观测 else None
            ),
            facing_right_confidence=(
                round(人物朝向观测.right_confidence, 4)
                if 人物朝向观测 else None
            ),
            facing_black_flicker=(
                人物朝向观测.black_flicker_suspected
                if 人物朝向观测 else None
            ),
            near_finish_promoted=V2近身补刀提升,
            near_finish_range_x=(
                search_range + V2近身补刀额外横向范围
                if dituming in ("蘑菇V2", "蘑菇V3", "自定义录制路线")
                else None
            ),
            lost_frames=没怪数量,
            combat_state=zant,
            frame_sequence=YOLO帧序号,
            frame_interval_ms=(
                round(帧间隔毫秒, 3) if 帧间隔毫秒 is not None else None
            ),
            instant_fps=(
                round(1000.0 / 帧间隔毫秒, 3)
                if 帧间隔毫秒 and 帧间隔毫秒 > 0
                else None
            ),
            frame_ms=round(完整帧耗时, 3),
            template_search_mode=模板搜索模式,
            template_search_left=(
                monitor['left'] + 模板搜索偏移X if monster_templates else None
            ),
            template_search_top=(
                monitor['top'] + 模板搜索偏移Y if monster_templates else None
            ),
            template_search_width=(
                int(模板搜索画面.shape[1]) if monster_templates else None
            ),
            template_search_height=(
                int(模板搜索画面.shape[0]) if monster_templates else None
            ),
            template_search_horizontal_radius=(
                模板搜索横向半径 if monster_templates else None
            ),
            template_search_vertical_radius=(vertical_range if monster_templates else None),
            template_count=(len(monster_templates) if monster_templates else None),
            template_workers=(模板匹配线程数 if monster_templates else None),
        )
        发布检测预览(
            预览画面,
            {
                "map": dituming,
                "detector": "template/custom" if monster_templates else "yolo",
                "monster_count": len(centers),
                "health_bar_count": V2血条目标数量,
                "health_bar_green_max_ratio": V2血条最大绿色比例,
                "max_monster_confidence": (
                    max(怪物置信度列表) if 怪物置信度列表 else None
                ),
                "yellow_position": None,
                "self_position": (
                    (round(float(ccx), 1), round(float(ccy), 1))
                    if 人物屏幕坐标 is not None
                    and 人物置信度 >= PERSON_MATCH_THRESHOLD
                    else None
                ),
                "self_template": 图片名,
                "self_search_mode": character_tracker.last_mode,
                "self_match_ms": character_tracker.last_match_ms,
                "facing_direction": (
                    人物朝向观测.detected_direction if 人物朝向观测 else None
                ),
                "facing_reliable_direction": (
                    人物朝向观测.reliable_direction if 人物朝向观测 else None
                ),
                "facing_left_confidence": (
                    人物朝向观测.left_confidence if 人物朝向观测 else None
                ),
                "facing_right_confidence": (
                    人物朝向观测.right_confidence if 人物朝向观测 else None
                ),
                "facing_black_flicker": (
                    人物朝向观测.black_flicker_suspected
                    if 人物朝向观测 else None
                ),
                "monster_match_ms": 怪物检测耗时,
                "fps": (
                    1000.0 / 帧间隔毫秒
                    if 帧间隔毫秒 and 帧间隔毫秒 > 0
                    else 1000.0 / max(完整帧耗时, 0.001)
                ),
                "capture_region": 截图区域说明,
            },
            (
                (
                    _人物局部坐标[0] + 预览偏移X,
                    _人物局部坐标[1] + 预览偏移Y,
                )
                if 人物屏幕坐标 is not None
                and 人物置信度 >= PERSON_MATCH_THRESHOLD
                else None
            ),
            [
                (
                    box[0] + 预览偏移X,
                    box[1] + 预览偏移Y,
                    box[2] + 预览偏移X,
                    box[3] + 预览偏移Y,
                )
                for box in 预览怪物框
            ],
        )
        if not monster_templates:
            频率窗口帧数 += 1
            频率窗口推理总毫秒 += 怪物检测耗时
            频率窗口完整帧总毫秒 += 完整帧耗时
            if 帧间隔毫秒 is not None:
                频率窗口间隔总毫秒 += 帧间隔毫秒
                频率窗口间隔数 += 1
            频率窗口耗时 = time.monotonic() - 频率窗口开始时间
            if 频率窗口耗时 >= 1.0:
                trace_event(
                    "yolo_frequency_summary",
                    frames=频率窗口帧数,
                    window_ms=round(频率窗口耗时 * 1000, 3),
                    fps=round(频率窗口帧数 / 频率窗口耗时, 3),
                    avg_interval_ms=(
                        round(频率窗口间隔总毫秒 / 频率窗口间隔数, 3)
                        if 频率窗口间隔数
                        else None
                    ),
                    avg_yolo_ms=round(
                        频率窗口推理总毫秒 / 频率窗口帧数,
                        3,
                    ),
                    avg_frame_ms=round(
                        频率窗口完整帧总毫秒 / 频率窗口帧数,
                        3,
                    ),
                )
                频率窗口开始时间 = time.monotonic()
                频率窗口帧数 = 0
                频率窗口间隔总毫秒 = 0.0
                频率窗口间隔数 = 0
                频率窗口推理总毫秒 = 0.0
                频率窗口完整帧总毫秒 = 0.0
        if not 怪物检测就绪事件.is_set():
            怪物检测就绪事件.set()
            trace_event(
                "monster_detector_ready",
                detector=("template" if monster_templates else "yolo"),
            )

        检测目标帧间隔 = (
            TEST_YOLO_TARGET_FRAME_SECONDS
            if 测试截图模式 and not monster_templates
            else YOLO_TARGET_FRAME_SECONDS
        )
        if not 等待下一检测帧(帧开始时间, 检测目标帧间隔):
            break
    if 模板匹配线程池 is not None:
        模板匹配线程池.shutdown(wait=False)


def is_within_x_range(target_x, ref_x, error=2):
    """
    判断目标的x坐标是否在参考点x的误差范围内
    """
    return abs(target_x - ref_x) <= error
def is_within_range(target, reference, error=2):
    """
    判断目标点是否在参考点误差范围内
    target: (cx, cy)
    reference: (rx, ry)
    error: 允许误差
    """
    dist = np.sqrt((target[0] - reference[0])**2 + (target[1] - reference[1])**2)
    return dist <= error
def find_window_by_title(keyword):
    """
    查找标题中包含keyword的窗口句柄
    """
    def enum_windows_callback(hwnd, result):
        """收集标题中包含关键字的顶层窗口句柄。"""
        title = win32gui.GetWindowText(hwnd)
        if keyword in title:
            result.append(hwnd)

    handles = []
    win32gui.EnumWindows(enum_windows_callback, handles)
    return handles
def move_window_to_origin(hwnd):
    """
    移动窗口到（0, 0）
    """
    # 获取当前窗口大小
    rect = win32gui.GetWindowRect(hwnd)
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]
    # 移动窗口到 (0, 0)，宽高保持不变
    win32gui.MoveWindow(hwnd, 4, 38, width, height, True)


def set_window_size_1280x800(window_title_keyword="冒险岛"):
    """
    查找标题包含 window_title_keyword 的窗口，把它的**客户区**设置为 1280x800，并把窗口左上角移动到屏幕 (0,0)。

    为什么要强调"客户区"：
      - 游戏内的点击/截图/血条坐标（如 monitor={'top':300,'left':0,'width':1280,'height':300}、血条x=353,y=787）
        都是基于客户区坐标系的，和窗口外框（标题栏/边框）无关。
      - 如果直接把外框设成1280x800，扣掉标题栏+边框后客户区会偏小，所有写死的坐标都会偏移。

    用法示例：
      set_window_size_1280x800()               # 默认找标题含"冒险岛"的窗口
      set_window_size_1280x800("冒险岛阿尔泰")  # 指定标题关键字

    返回值：成功返回 (True, hwnd)，失败返回 (False, None)
    """
    # 1. 找窗口
    handles = find_window_by_title(window_title_keyword)
    if not handles:
        print(f"[错误] 没找到标题含 '{window_title_keyword}' 的窗口")
        return False, None
    hwnd = handles[0]
    if len(handles) > 1:
        print(f"[提示] 找到 {len(handles)} 个匹配窗口，使用第一个：句柄={hwnd} 标题='{win32gui.GetWindowText(hwnd)}'")

    # 2. 先把窗口还原（不从最小化/最大化状态调整会失败）
    try:
        import win32con
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.1)
    except Exception:
        pass

    # 3. 反推外框大小：先假设客户区1280x800，用AdjustWindowRect算出外框应有的宽高
    try:
        import win32con
        # 取当前窗口的样式，和AdjustWindowRect配合算边框
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        # Win32的RECT结构：(left, top, right, bottom)
        rect = win32gui.AdjustWindowRectEx((0, 0, 1280, 800), style, False, ex_style)
        outer_w = rect[2] - rect[0]
        outer_h = rect[3] - rect[1]
    except Exception:
        # 如果AdjustWindowRect失败（比如缺少pywin32完整模块），用经验值兜底：外框≈客户区+边框(16px宽, 59px高)
        outer_w = 1280 + 16
        outer_h = 800 + 59

    # 4. MoveWindow：一次调用完成"移到(0,0)+改变外框大小"
    #    参数 (hwnd, x, y, width, height, bRepaint=True)
    win32gui.MoveWindow(hwnd, 0, 0, outer_w, outer_h, True)
    time.sleep(0.1)

    # 5. 验证客户区实际大小（给用户看，方便排查）
    try:
        client_rect = win32gui.GetClientRect(hwnd)
        client_w = client_rect[2] - client_rect[0]
        client_h = client_rect[3] - client_rect[1]
        win_rect = win32gui.GetWindowRect(hwnd)
        win_w = win_rect[2] - win_rect[0]
        win_h = win_rect[3] - win_rect[1]
        print(f"[OK] 窗口已定位到 (0,0)")
        print(f"     外框大小: {win_w}x{win_h}  |  客户区大小: {client_w}x{client_h}")
        if client_w != 1280 or client_h != 800:
            print(f"[警告] 客户区不是 1280x800，可能是DPI缩放或边框样式导致；如需完全匹配可手动调 outer_w/outer_h。")
    except Exception:
        print(f"[OK] 窗口已移动并调整大小 (句柄 {hwnd})，但无法读取客户区尺寸做验证。")

    return True, hwnd
def is_white_pixel(x, y):
    """
    判断屏幕坐标(x, y)处的像素是否为白色
    返回True表示是白色，False表示不是白色
    """
    # 获取设备上下文
    hdc = ctypes.windll.user32.GetDC(0)

    # 获取指定坐标的像素颜色
    pixel = ctypes.windll.gdi32.GetPixel(hdc, x, y)

    # 释放设备上下文
    ctypes.windll.user32.ReleaseDC(0, hdc)

    # 提取RGB值
    r = pixel & 0xFF
    g = (pixel >> 8) & 0xFF
    b = (pixel >> 16) & 0xFF

    return r>=170 and g>=170 and b>=170

def get_img(x1, y1, ex, ey, question_text):
    """截取验证区域，复用同一份 JPEG 数据进行本地匹配和网络识别。"""
    screen = ImageGrab.grab(bbox=(x1, y1, ex, ey))
    buffer = BytesIO()
    screen.save(buffer, format="JPEG", quality=100)
    image_bytes = buffer.getvalue()
    Path("1.png").write_bytes(image_bytes)
    img_base64 = base64.b64encode(image_bytes).decode("utf-8")
    return not_robot(img_base64, question_text)
def template_match(big_image, small_image, threshold=0.8):
    """
    在大图中查找小图，返回匹配的中心点坐标和是否找到匹配。

    参数：
    big_image: 大图的图像数组（numpy.ndarray）
    small_image: 小图的模板图像数组（numpy.ndarray）
    threshold: 匹配的相似度阈值（默认0.8，越接近1越严格）

    返回：
    match_found: 是否找到匹配（布尔值）
    center: 匹配区域的中心点坐标（元组(x, y)），未找到时为None
    """
    big_img = cv2.imread(big_image)
    small_img = cv2.imread(small_image)
    if big_img is None or small_img is None:
        return False, None

    result = cv2.matchTemplate(big_img, small_img, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val >= threshold:
        h, w = small_img.shape[:2]
        center_x = max_loc[0] + w // 2
        center_y = max_loc[1] + h // 2
        return True, (center_x, center_y)
    return False, None


def not_robot(img_base64, question_text):
    """调用视觉接口识别符文箭头方向并返回精简文本。"""
    api_key = os.environ.get("GLM_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 GLM_API_KEY，无法调用在线符文识别")
    api_url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": "glm-4v-flash",
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}
                },
                {
                    "type": "text",
                    "text": question_text
                }
            ]
        }]
    }
    response = requests.post(
        url=api_url,
        headers=headers,
        json=payload,
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    return data['choices'][0]['message']['content'].replace(" ", "")


def jielun():
    """识别四个方向提示并依次发送对应按键。"""
    basedir = get_base_dir()
    path = basedir+"\\resres\\"
    print("解轮进行")
    regions = (
        (356, 395, 438, 471),
        (525, 394, 607, 474),
        (695, 398, 778, 477),
        (858, 388, 945, 479),
    )
    question = "告诉我图片彩色的箭头方向,只有上下左右4种方向发给我,请回答我一个字即可(上,下,左,右)"

    for index, region in enumerate(regions, start=1):
        print("第几个方向", index)
        result = ""
        while not 已请求停止():
            try:
                result = get_img(*region, question)
                print("识别方向：", result)
                break
            except Exception as e:
                print("[解轮] 方向识别失败，准备重试：{}".format(e))
                if not 可中断等待(1):
                    return
        if 已请求停止():
            return

        # 本地模板作为网络识别结果的兜底校正。
        found, coord = template_match("1.png",  path+'zuo.png', threshold=0.7)
        if found:
            print(f"匹配到目标，中心坐标：{coord}")
            print("是左")
            result = "左"
        else:
            print("没有找到匹配的目标。")
        found, coord = template_match("1.png",  path+'shang.png', threshold=0.7)
        if found:
            print(f"匹配到目标，中心坐标：{coord}")
            print("是上")
            result = "上"
        else:
            print("没有找到匹配的目标。")
        found, coord = template_match("1.png",  path+'you.png', threshold=0.7)
        if found:
            print(f"匹配到目标，中心坐标：{coord}")
            print("是右")
            result = "右"
        else:
            print("没有找到匹配的目标。")



        found, coord = template_match("1.png",  path+'xia.png', threshold=0.7)
        if found:
            print(f"匹配到目标，中心坐标：{coord}")
            print("是下")
            result = "下"
        else:
            print("没有找到匹配的目标。")




        if result =="上":
            print("按上")
            pydirectinput.keyUp('up')
            pydirectinput.keyDown('up')
            pydirectinput.keyUp('up')
            if not 可中断等待(1.7):
                return
        if result == "下":
            print("按下")
            pydirectinput.keyUp('down')
            pydirectinput.keyDown('down')
            pydirectinput.keyUp('down')
            if not 可中断等待(1.7):
                return
        if result == "左":
            print("按左")
            pydirectinput.keyUp('left')
            pydirectinput.keyDown('left')
            pydirectinput.keyUp('left')
            if not 可中断等待(1.7):
                return
        if result == "右":
            print("按右")
            pydirectinput.keyUp('right')
            pydirectinput.keyDown('right')
            pydirectinput.keyUp('right')
            if not 可中断等待(1.7):
                return


# ---------------------------------------------------------------------------
# 地图运行公共方法
# ---------------------------------------------------------------------------
# 地图函数只保留各自的路线与坐标判断；停止、坐标读取、战斗处理等通用行为
# 统一放在这里。这样修改按键策略或停止机制时，不需要同步改十几张地图。


def 已请求停止():
    """返回当前自动化任务是否已经收到停止信号。"""
    return stop_event2 == 0 or (stop_event is not None and stop_event.is_set())


def 用户已暂停():
    """返回页面是否已暂停当前任务。"""
    return not 用户运行许可事件.is_set()


def 等待运行许可(interval=0.05):
    """暂停时等待继续；停止仍立即返回 False。"""
    while not 用户运行许可事件.is_set():
        if 已请求停止():
            return False
        用户运行许可事件.wait(timeout=max(0.01, float(interval)))
    return not 已请求停止()


def 可中断等待(seconds, interval=0.05):
    """按小时间片等待；停止时退出，失焦或用户暂停时冻结动作计时。"""
    deadline = time.monotonic() + max(0, seconds)
    while True:
        if 已请求停止():
            return False
        if not 游戏窗口焦点事件.is_set() or 用户已暂停():
            paused_at = time.monotonic()
            while not 游戏窗口焦点事件.is_set() or 用户已暂停():
                if 已请求停止():
                    return False
                if 用户已暂停():
                    用户运行许可事件.wait(timeout=min(0.05, max(0.01, interval)))
                else:
                    time.sleep(min(0.05, max(0.01, interval)))
            # 暂停/失焦时间不计入方向键、跳跃键或攻击键的动作时长。
            deadline += time.monotonic() - paused_at
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(interval, remaining))


def 等待下一检测帧(frame_started, target_seconds):
    """按目标帧周期补足剩余时间，避免在推理耗时之外再叠加固定休眠。"""
    elapsed = time.perf_counter() - frame_started
    remaining = max(0.0, float(target_seconds) - elapsed)
    if remaining <= 0:
        return not 已请求停止()
    return 可中断等待(remaining, interval=0.01)


def 读取人物位置():
    """在线程锁保护下读取定位线程发布的最新人物坐标。"""
    with lock:
        return renwu_pos


def 获取运行诊断快照():
    """返回攻击意图、移动锁、近怪方向和坐标新鲜度快照。"""
    now = time.monotonic()
    with 攻击状态锁:
        attack_snapshot = {
            "user_paused": 用户已暂停(),
            "attack_pending": 攻击,
            "attack_intent_age_ms": (
                round((now - 攻击意图生成时间) * 1000, 3)
                if 攻击意图生成时间 > 0
                else None
            ),
            "attack_recheck_remaining_ms": round(
                max(0.0, 攻击冷却截止时间 - now) * 1000,
                3,
            ),
            "move_lock_remaining_ms": round(
                max(0.0, 攻击移动锁截止时间 - now) * 1000,
                3,
            ),
        }
    最近近怪时间快照, 最近怪物方向快照, 当前近怪数量快照 = (
        读取战斗感知()
    )
    combat_snapshot = {
        "nearby_targets": 当前近怪数量快照,
        "last_target_direction": 最近怪物方向快照,
        "last_target_age_ms": (
            round((now - 最近近怪时间快照) * 1000, 3)
            if 最近近怪时间快照 > 0
            else None
        ),
    }
    attack_snapshot.update(combat_snapshot)
    attack_snapshot["combat_state"] = zant
    attack_snapshot["movement_direction"] = 上次移动方向
    attack_snapshot["detector_ready"] = 怪物检测就绪事件.is_set()
    attack_snapshot["minimap_stale_ms"] = (
        round((now - 上次小地图人物发现时间) * 1000, 3)
        if 上次小地图人物发现时间 > 0
        else None
    )
    return attack_snapshot


def 记录人物位置(position, interval=0.25, **context):
    """把人物坐标交给公共轨迹记录器统一限频、计算速度并写入日志。"""
    return 人物轨迹记录器.record(
        sys.modules[__name__],
        position,
        interval=interval,
        **context,
    )


def 重置人物轨迹记录():
    """开始新任务时清空公共轨迹记录器的上一轮采样状态。"""
    人物轨迹记录器.reset()


def 请求人物重新定位(reason="route_recovery"):
    """清空小地图缓存并要求人物模板线程下一帧执行完整区域重定位。"""
    previous_position = request_minimap_relocation(
        sys.modules[__name__],
        reason=reason,
    )
    人物全图重定位事件.set()
    人物轨迹记录器.reset()
    trace_event(
        "character_relocation_requested",
        reason=reason,
        previous_position=previous_position,
    )
    return previous_position


def 清除攻击意图():
    """清空尚未被路线线程消费的攻击方向。"""
    global 攻击, 攻击意图生成时间
    with 攻击状态条件:
        previous = 攻击
        攻击 = 0
        攻击意图生成时间 = 0.0
        if previous:
            trace_event("attack_intent_cleared", direction=previous)
        攻击状态条件.notify_all()


def 重置攻击状态():
    """开始新任务时原子地清空攻击意图、版本和死亡动画冷却。"""
    global 攻击, 攻击冷却截止时间, 攻击移动锁截止时间
    global 攻击意图版本, 攻击意图生成时间, 最近攻击完成时间
    with 攻击状态条件:
        攻击 = 0
        攻击冷却截止时间 = 0.0
        攻击移动锁截止时间 = 0.0
        攻击意图版本 = 0
        攻击意图生成时间 = 0.0
        最近攻击完成时间 = 0.0
        攻击状态条件.notify_all()


def 重置战斗感知():
    """开始新任务时清空公共战斗感知、V2追怪快照和人物朝向。"""
    global 最近近怪时间, 最近怪物方向, 当前近怪数量, 战斗忽略截止时间, 上次移动方向
    global 智能追怪前进方向, 智能追怪双向模式
    global 最近追怪时间, 最近追怪方向, 当前追怪数量
    global 当前可攻击数量, 当前追怪最近距离
    global 当前追怪左边数量, 当前追怪右边数量
    global 当前可攻击左边数量, 当前可攻击右边数量
    global 当前血条追怪左边数量, 当前血条追怪右边数量
    global 当前血条可攻击左边数量, 当前血条可攻击右边数量
    global 当前追怪连续无目标发布帧数
    global 当前追怪丢失确认中
    global 当前追怪无目标开始时间
    global 当前攻击确认中
    global 当前自动群攻怪物数量, 当前近身群攻怪物数量
    global 当前自动群攻大于数量
    global 人物朝向检测时间, 人物当前可靠朝向, 人物本帧检测朝向
    global 人物朝向左置信度, 人物朝向右置信度, 人物朝向置信度差
    global 人物朝向检测耗时, 人物朝向黑闪疑似, 人物朝向本帧可靠
    战斗感知存储.reset(configured_group_over_count=自动群攻大于数量)
    with 战斗感知锁:
        最近近怪时间 = 0.0
        最近怪物方向 = None
        当前近怪数量 = 0
        战斗忽略截止时间 = 0.0
        上次移动方向 = None
    with 追怪感知锁:
        最近追怪时间 = 0.0
        最近追怪方向 = None
        当前追怪数量 = 0
        当前可攻击数量 = 0
        当前追怪最近距离 = None
        当前追怪左边数量 = 0
        当前追怪右边数量 = 0
        当前可攻击左边数量 = 0
        当前可攻击右边数量 = 0
        当前血条追怪左边数量 = 0
        当前血条追怪右边数量 = 0
        当前血条可攻击左边数量 = 0
        当前血条可攻击右边数量 = 0
        当前追怪连续无目标发布帧数 = 0
        当前追怪丢失确认中 = False
        当前追怪无目标开始时间 = 0.0
        当前攻击确认中 = False
        当前自动群攻怪物数量 = 0
        当前近身群攻怪物数量 = 0
        当前自动群攻大于数量 = max(0, int(自动群攻大于数量))
    with 人物朝向感知锁:
        人物朝向检测时间 = 0.0
        人物当前可靠朝向 = None
        人物本帧检测朝向 = None
        人物朝向左置信度 = 0.0
        人物朝向右置信度 = 0.0
        人物朝向置信度差 = 0.0
        人物朝向检测耗时 = 0.0
        人物朝向黑闪疑似 = False
        人物朝向本帧可靠 = False
    with 智能追怪前进方向锁:
        智能追怪前进方向 = None
        智能追怪双向模式 = False


def 更新智能追怪前进方向(direction):
    """记录路线稳定前进方向；战斗临时转身和松键不能调用本入口。"""
    global 智能追怪前进方向
    normalized = direction if direction in ("left", "right") else None
    if normalized is None:
        return False
    with 智能追怪前进方向锁:
        changed = 智能追怪前进方向 != normalized
        智能追怪前进方向 = normalized
        bidirectional = bool(智能追怪双向模式)
    if changed:
        trace_event(
            "smart_chase_forward_direction_changed",
            direction=normalized,
            forward_extra_range_x=智能追怪前方额外横向范围,
            rear_chase_enabled=bidirectional,
            action=(
                "update_route_direction_keep_bidirectional"
                if bidirectional
                else "chase_forward_only"
            ),
        )
    return changed


def 设置智能追怪双向模式(enabled):
    """单平台允许左右智能追怪；多平台仍只追录制路线正前方。"""
    global 智能追怪双向模式
    normalized = bool(enabled)
    with 智能追怪前进方向锁:
        changed = 智能追怪双向模式 != normalized
        智能追怪双向模式 = normalized
    if changed:
        trace_event(
            "smart_chase_bidirectional_mode_changed",
            enabled=normalized,
            forward_extra_range_x=智能追怪前方额外横向范围,
            platform_boundary_guard=True,
            action=(
                "chase_both_sides_inside_single_platform"
                if normalized
                else "chase_forward_only"
            ),
        )
    return changed


def 读取智能追怪双向模式():
    """返回当前路线是否允许在单平台内向左右两侧智能追怪。"""
    with 智能追怪前进方向锁:
        return bool(智能追怪双向模式)


def 读取智能追怪前进方向():
    """返回最近一次录制路线实际下发的水平前进方向。"""
    with 智能追怪前进方向锁:
        return 智能追怪前进方向


def 更新人物朝向感知(observation):
    """原子发布一帧人物左右朝向；不可靠帧仅保留诊断值和旧可靠方向。"""
    global 人物朝向检测时间, 人物当前可靠朝向, 人物本帧检测朝向
    global 人物朝向左置信度, 人物朝向右置信度, 人物朝向置信度差
    global 人物朝向检测耗时, 人物朝向黑闪疑似, 人物朝向本帧可靠
    with 人物朝向感知锁:
        人物朝向检测时间 = float(observation.detected_at or 0.0)
        人物本帧检测朝向 = observation.detected_direction
        if observation.reliable_direction in ("left", "right"):
            人物当前可靠朝向 = observation.reliable_direction
        人物朝向左置信度 = float(observation.left_confidence)
        人物朝向右置信度 = float(observation.right_confidence)
        人物朝向置信度差 = float(observation.confidence_margin)
        人物朝向检测耗时 = float(observation.match_ms)
        人物朝向黑闪疑似 = bool(observation.black_flicker_suspected)
        人物朝向本帧可靠 = bool(observation.reliable)


def 读取人物朝向感知():
    """返回V2攻击前校验所需的朝向、置信度、黑闪和采集时间快照。"""
    with 人物朝向感知锁:
        return (
            人物朝向检测时间,
            人物本帧检测朝向,
            人物当前可靠朝向,
            人物朝向左置信度,
            人物朝向右置信度,
            人物朝向置信度差,
            人物朝向检测耗时,
            人物朝向黑闪疑似,
            人物朝向本帧可靠,
        )


def 更新追怪感知(
    attackable_count,
    chase_count,
    chase_left_count,
    chase_right_count,
    attackable_left_count=0,
    attackable_right_count=0,
    preferred_direction=None,
    nearest_distance=None,
    detected_at=None,
    automatic_group_count=0,
    close_group_count=0,
    configured_group_over_count=None,
    health_bar_chase_left_count=0,
    health_bar_chase_right_count=0,
    health_bar_attackable_left_count=0,
    health_bar_attackable_right_count=0,
    attack_confirmation_pending=False,
):
    """发布V2两侧目标数量、最近方向及截图真实采集时间。"""
    global 最近追怪时间, 最近追怪方向, 当前追怪数量
    global 当前可攻击数量, 当前追怪最近距离
    global 当前追怪左边数量, 当前追怪右边数量
    global 当前可攻击左边数量, 当前可攻击右边数量
    global 当前血条追怪左边数量, 当前血条追怪右边数量
    global 当前血条可攻击左边数量, 当前血条可攻击右边数量
    global 当前自动群攻怪物数量, 当前近身群攻怪物数量
    global 当前自动群攻大于数量
    global 当前攻击确认中
    frame_detected_at = float(detected_at or time.monotonic())
    with 攻击状态锁:
        stale_after_attack = frame_detected_at <= 最近攻击完成时间
    attackable_count = int(attackable_count)
    chase_count = int(chase_count)
    chase_left_count = int(chase_left_count)
    chase_right_count = int(chase_right_count)
    attackable_left_count = int(attackable_left_count)
    attackable_right_count = int(attackable_right_count)
    if stale_after_attack:
        # 攻击完成前截到的旧画面既不能补刀，也不应把追怪快照强制清零，否则
        # 路线会在两张有效画面之间突然走一步。保留上一快照，等待下一张新图。
        return
    with 追怪感知锁:
        当前可攻击数量 = attackable_count
        当前追怪数量 = chase_count
        当前追怪左边数量 = chase_left_count
        当前追怪右边数量 = chase_right_count
        当前可攻击左边数量 = attackable_left_count
        当前可攻击右边数量 = attackable_right_count
        当前血条追怪左边数量 = max(
            0, int(health_bar_chase_left_count)
        )
        当前血条追怪右边数量 = max(
            0, int(health_bar_chase_right_count)
        )
        当前血条可攻击左边数量 = max(
            0, int(health_bar_attackable_left_count)
        )
        当前血条可攻击右边数量 = max(
            0, int(health_bar_attackable_right_count)
        )
        当前自动群攻怪物数量 = max(0, int(automatic_group_count))
        当前近身群攻怪物数量 = max(0, int(close_group_count))
        当前自动群攻大于数量 = max(
            0,
            int(
                自动群攻大于数量
                if configured_group_over_count is None
                else configured_group_over_count
            ),
        )
        当前攻击确认中 = bool(attack_confirmation_pending)
        当前追怪最近距离 = (
            float(nearest_distance) if nearest_distance is not None else None
        )
        if chase_count <= 0:
            最近追怪方向 = None
            return
        最近追怪时间 = frame_detected_at
        if preferred_direction in ("left", "right"):
            最近追怪方向 = preferred_direction
        elif chase_left_count > chase_right_count:
            最近追怪方向 = "left"
        elif chase_right_count > chase_left_count:
            最近追怪方向 = "right"


def 读取追怪感知():
    """原子读取V2最近目标方向、距离以及攻击区和追怪区两侧数量。"""
    with 追怪感知锁:
        return (
            最近追怪时间,
            最近追怪方向,
            当前追怪数量,
            当前可攻击数量,
            当前追怪最近距离,
            当前追怪左边数量,
            当前追怪右边数量,
            当前可攻击左边数量,
            当前可攻击右边数量,
            当前自动群攻怪物数量,
            当前近身群攻怪物数量,
            当前自动群攻大于数量,
            当前血条追怪左边数量,
            当前血条追怪右边数量,
            当前血条可攻击左边数量,
            当前血条可攻击右边数量,
            当前追怪丢失确认中,
            当前攻击确认中,
            0,
        )


def 保持追怪感知有效(detected_at=None):
    """第一张无目标确认帧到来时刷新已有目标时间，不改变方向和数量。"""
    return 战斗感知存储.keep_chase_fresh(detected_at=detected_at)


def 更新战斗感知(total_nearby, left_count, right_count, preferred_direction=None):
    """发布当前近怪数量，并在命中时记住最近怪物方向和时间。"""
    global 最近近怪时间, 最近怪物方向, 当前近怪数量
    total_nearby = int(total_nearby)
    left_count = int(left_count)
    right_count = int(right_count)
    with 战斗感知锁:
        当前近怪数量 = total_nearby
        if total_nearby <= 0:
            return
        最近近怪时间 = time.monotonic()
        if preferred_direction in ("left", "right"):
            最近怪物方向 = preferred_direction
        elif left_count > right_count:
            最近怪物方向 = "left"
        elif right_count > left_count:
            最近怪物方向 = "right"


def 读取战斗感知():
    """原子读取最近怪物时间、方向和当前近怪数量。"""
    with 战斗感知锁:
        return 最近近怪时间, 最近怪物方向, 当前近怪数量


def 暂时忽略战斗(seconds, reason=None):
    """在指定时间内阻止检测线程重新锁住路线，供卡死恢复流程调用。"""
    global 战斗忽略截止时间, zant
    seconds = max(0.0, float(seconds))
    now = time.monotonic()
    战斗忽略截止时间 = max(战斗忽略截止时间, now + seconds)
    清除攻击意图()
    zant = 0
    trace_event(
        "combat_detection_suppressed",
        reason=reason,
        suppress_ms=round(seconds * 1000.0, 1),
    )


def 处理怪物检测结果(
    total_nearby,
    left_count,
    right_count,
    lost_frames,
    allow_group_attack=False,
    automatic_group_count=0,
    close_group_count=0,
    preferred_direction=None,
    visible_nearby=0,
    visible_left_count=0,
    visible_right_count=0,
    visible_preferred_direction=None,
    hold_visible_combat=True,
    detected_at=None,
):
    """用连续丢失帧确认战斗结束，并发布本帧唯一攻击方向。

    单帧人物模板失败或 YOLO 漏检只会让人物继续原地等待，不会立即恢复路线；
    连续达到确认帧数后才清空攻击并允许继续移动。
    """
    global zant, 休息点测试进行中
    if 休息点测试进行中:
        清除攻击意图()
        zant = 0
        return 0
    total_nearby = int(total_nearby)
    left_count = int(left_count)
    right_count = int(right_count)
    visible_nearby = int(visible_nearby)
    awareness_total = max(total_nearby, visible_nearby)
    awareness_left = visible_left_count if visible_nearby else left_count
    awareness_right = visible_right_count if visible_nearby else right_count
    awareness_direction = (
        visible_preferred_direction
        if visible_nearby
        else preferred_direction
    )
    更新战斗感知(
        awareness_total,
        awareness_left,
        awareness_right,
        preferred_direction=awareness_direction,
    )

    if total_nearby <= 0:
        # 怪物受击跑动后短暂离开严格攻击范围时仍保持战斗，但不盲目发送按键；
        # 回到严格范围后下一帧即可继续攻击，不会先插入一步巡逻移动。
        if visible_nearby > 0 and hold_visible_combat:
            zant = 1
            return 0
        if visible_nearby > 0:
            # 蘑菇V2会通过独立追怪快照向目标移动；攻击范围外的可见怪不能
            # 再占用旧战斗暂停通道，否则路线会停止但又没有攻击意图。
            清除攻击意图()
            zant = 0
            return 0
        if zant != 1:
            return 0
        lost_frames += 1
        last_seen, _last_direction, _nearby = 读取战斗感知()
        recent_target = (
            last_seen > 0
            and time.monotonic() - last_seen < COMBAT_RELEASE_GRACE_SECONDS
        )
        if (
            lost_frames < COMBAT_TARGET_LOST_CONFIRMATION_FRAMES
            or recent_target
            or 攻击移动仍锁定()
        ):
            return min(lost_frames, COMBAT_TARGET_LOST_CONFIRMATION_FRAMES - 1)
        清除攻击意图()
        zant = 0
        return 0

    lost_frames = 0
    zant = 1
    if allow_group_attack and total_nearby >= 2:
        发布攻击意图(3, detected_at=detected_at)
    elif preferred_direction == "right":
        发布攻击意图(2, detected_at=detected_at)
    elif preferred_direction == "left":
        发布攻击意图(1, detected_at=detected_at)
    elif right_count > left_count:
        发布攻击意图(2, detected_at=detected_at)
    elif left_count > right_count:
        发布攻击意图(1, detected_at=detected_at)
    else:
        _last_seen, last_direction, _nearby = 读取战斗感知()
        发布攻击意图(
            2 if last_direction == "right" else 1,
            detected_at=detected_at,
        )
    return lost_frames


def 发布攻击意图(
    direction,
    detected_at=None,
    reason=None,
    monster_count=None,
    horizontal_range=None,
    configured_over_count=None,
):
    """在复检冷却结束后发布一次攻击方向，返回是否发布成功。

    ``detected_at`` 是截图开始时间。若该截图早于最近一次攻击完成时间，说明
    画面反映的是攻击前状态，即使模板匹配稍后才完成也不能再触发补刀。
    """
    global 攻击, 攻击意图版本, 攻击意图生成时间
    if 用户已暂停():
        return False
    direction = int(direction)
    now = time.monotonic()
    frame_detected_at = float(detected_at or now)
    with 攻击状态条件:
        if frame_detected_at <= 最近攻击完成时间:
            trace_event(
                "attack_intent_stale_frame_dropped",
                direction=direction,
                frame_age_ms=round((now - frame_detected_at) * 1000, 3),
                completed_after_capture_ms=round(
                    (最近攻击完成时间 - frame_detected_at) * 1000,
                    3,
                ),
            )
            return False
        if now < 攻击冷却截止时间:
            攻击 = 0
            return False
        # 同一帧附近的重复检测不需要不断改写共享状态；保留最新方向变化即可。
        if 攻击 == direction and now - 攻击意图生成时间 <= ATTACK_INTENT_TTL_SECONDS:
            return True
        攻击 = direction
        攻击意图版本 += 1
        攻击意图生成时间 = now
        trace_fields = {
            "direction": direction,
            "version": 攻击意图版本,
        }
        if reason is not None:
            trace_fields["reason"] = reason
        if monster_count is not None:
            trace_fields["monster_count"] = int(monster_count)
        if horizontal_range is not None:
            trace_fields["horizontal_range"] = float(horizontal_range)
        if configured_over_count is not None:
            trace_fields["configured_over_count"] = int(
                configured_over_count
            )
        trace_event("attack_intent_published", **trace_fields)
        攻击状态条件.notify()
        return True


def 领取攻击意图(wait_seconds=0.05):
    """等待并原子取走一个未过期攻击方向，避免轮询和旧帧重复执行。"""
    global 攻击, 攻击意图生成时间
    with 攻击状态条件:
        if 用户已暂停():
            攻击 = 0
            攻击意图生成时间 = 0.0
            return 0
        if 攻击 == 0 and wait_seconds > 0 and not 已请求停止():
            攻击状态条件.wait(timeout=wait_seconds)
        if (
            攻击 != 0
            and time.monotonic() - 攻击意图生成时间 > ATTACK_INTENT_TTL_SECONDS
        ):
            攻击 = 0
            攻击意图生成时间 = 0.0
            return 0
        direction = 攻击
        攻击 = 0
        攻击意图生成时间 = 0.0
        if direction:
            trace_event(
                "attack_intent_consumed",
                direction=direction,
                version=攻击意图版本,
            )
        return direction


def 标记攻击完成(
    recheck_seconds=POST_ATTACK_RECHECK_SECONDS,
    move_lock_seconds=ATTACK_MOVE_LOCK_SECONDS,
):
    """开启攻击后复检和移动锁，并允许特定路线传入专用节奏。"""
    global 攻击, 攻击冷却截止时间, 攻击移动锁截止时间, 攻击意图生成时间
    global 最近攻击完成时间
    recheck_seconds = max(0.0, float(recheck_seconds))
    move_lock_seconds = max(0.0, float(move_lock_seconds))
    with 攻击状态条件:
        # 攻击执行期间检测线程可能已经排队了新意图；完成时一并清掉，正是避免
        # 死亡动画触发“补一刀”的关键。
        攻击 = 0
        攻击意图生成时间 = 0.0
        最近攻击完成时间 = time.monotonic()
        攻击冷却截止时间 = 最近攻击完成时间 + recheck_seconds
        攻击移动锁截止时间 = 最近攻击完成时间 + move_lock_seconds
        trace_event(
            "attack_completed",
            recheck_ms=round(recheck_seconds * 1000, 1),
            move_lock_ms=round(move_lock_seconds * 1000, 1),
        )
        攻击状态条件.notify_all()


def 攻击移动仍锁定():
    """返回攻击动作后的短暂禁止移动时间是否尚未结束。"""
    with 攻击状态锁:
        return time.monotonic() < 攻击移动锁截止时间


def 释放水平移动键(reason=None):
    """释放左右方向键，并记录停止前方向、位置和路线原因。"""
    global 上次移动方向
    pydirectinput.keyUp('right')
    pydirectinput.keyUp('left')
    if 上次移动方向 is not None:
        previous_direction = 上次移动方向
        上次移动方向 = None
        trace_event(
            "movement_stopped",
            previous_direction=previous_direction,
            reason=reason,
            position=读取人物位置(),
            **获取运行诊断快照(),
        )


def 切换持续移动(direction, reason=None):
    """只保持一个水平方向键按下，并记录开始移动时的运行快照。"""
    global 上次移动方向
    if direction == 上次移动方向:
        return
    opposite = 'right' if direction == 'left' else 'left'
    pydirectinput.keyUp(opposite)
    pydirectinput.keyDown(direction)
    上次移动方向 = direction
    trace_event(
        "movement_started",
        direction=direction,
        reason=reason,
        position=读取人物位置(),
        **获取运行诊断快照(),
    )


def 释放攻击键():
    """释放用户配置的单体、群体攻击键。"""
    pydirectinput.keyUp(单体按键)
    pydirectinput.keyUp(群攻按键)


def 轻点方向(direction):
    """停止水平移动后轻点指定方向，用于攻击前校正角色朝向。"""
    释放水平移动键()
    pydirectinput.keyDown(direction)
    pydirectinput.keyUp(direction)


def 等待战斗恢复():
    """等待检测线程解除暂停，同时避免旧版忙循环持续占用 CPU。"""
    释放水平移动键()
    while zant != 0:
        if 已请求停止():
            return
        可中断等待(0.02, interval=0.01)


def 处理战斗暂停():
    """执行所有常规地图共用的朝向攻击逻辑。

    ``攻击`` 的含义保持 1.9 不变：1=左侧单体、2=右侧单体、3=群攻。
    攻击意图通过条件变量即时唤醒本线程，并丢弃超过有效期的旧检测结果。
    """
    上次攻击方向 = 0
    while True:
        if 已请求停止():
            释放攻击键()
            return

        当前攻击方向 = 领取攻击意图()
        if 当前攻击方向 in (1, 2, 3):
            if 上次攻击方向 == 0:
                上次攻击方向 = 当前攻击方向
            elif 上次攻击方向 != 当前攻击方向:
                释放攻击键()
                if not 可中断等待(ATTACK_DIRECTION_SETTLE_SECONDS, interval=0.01):
                    return
                上次攻击方向 = 当前攻击方向

            if 当前攻击方向 == 1:
                trace_event("attack_key", attack_type="single", direction="left")
                轻点方向('left')
                if not 可中断等待(ATTACK_DIRECTION_SETTLE_SECONDS, interval=0.01):
                    return
                pydirectinput.press(单体按键)
            elif 当前攻击方向 == 2:
                trace_event("attack_key", attack_type="single", direction="right")
                轻点方向('right')
                if not 可中断等待(ATTACK_DIRECTION_SETTLE_SECONDS, interval=0.01):
                    return
                pydirectinput.press(单体按键)
            else:
                trace_event("attack_key", attack_type="group", direction="area")
                释放水平移动键()
                pydirectinput.keyDown(群攻按键)
                if not 可中断等待(ATTACK_KEY_HOLD_SECONDS, interval=0.01):
                    pydirectinput.keyUp(群攻按键)
                    return
                pydirectinput.keyUp(群攻按键)
            标记攻击完成()

        if zant == 0:
            释放攻击键()
            return


def 安装地图路线():
    """把 map/routes.py 中的路线函数绑定到当前兼容引擎上下文。

    路线代码仍使用旧版模块级状态。重新绑定函数的全局命名空间后，外部继续通过
    ``legacy_engine.<handler>()`` 调用，同时路线源码可以独立维护在 map 目录。
    """
    for name in map_routes.ROUTE_HANDLER_NAMES:
        source = getattr(map_routes, name)
        rebound = FunctionType(
            source.__code__,
            globals(),
            name=source.__name__,
            argdefs=source.__defaults__,
            closure=source.__closure__,
        )
        rebound.__doc__ = source.__doc__
        rebound.__kwdefaults__ = source.__kwdefaults__
        rebound.__annotations__ = dict(source.__annotations__)
        rebound.__qualname__ = source.__qualname__
        globals()[name] = rebound


def 更新游戏客户区尺寸(hwnd):
    """读取游戏真实客户区尺寸，供蘑菇V2/V3扩展最右侧截图范围。"""
    global 游戏客户区宽度
    global 游戏客户区高度
    global 游戏客户区屏幕左
    global 游戏客户区屏幕顶
    global 游戏窗口外框宽度
    global 游戏窗口外框高度
    try:
        client_rect = win32gui.GetClientRect(hwnd)
        width = int(client_rect[2] - client_rect[0])
        height = int(client_rect[3] - client_rect[1])
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        window_rect = win32gui.GetWindowRect(hwnd)
        outer_width = int(window_rect[2] - window_rect[0])
        outer_height = int(window_rect[3] - window_rect[1])
        if 800 <= width <= 3840:
            游戏客户区宽度 = width
        if 600 <= height <= 2160:
            游戏客户区高度 = height
        游戏客户区屏幕左 = max(0, int(client_left))
        游戏客户区屏幕顶 = max(0, int(client_top))
        if 800 <= outer_width <= 4096:
            游戏窗口外框宽度 = outer_width
        if 600 <= outer_height <= 2300:
            游戏窗口外框高度 = outer_height
    except Exception as exc:
        print("[窗口] 无法读取游戏客户区尺寸，继续使用 {}x{}：{}".format(
            游戏客户区宽度,
            游戏客户区高度,
            exc,
        ))
    return 游戏客户区宽度, 游戏客户区高度


def 将游戏窗口贴齐截图原点(hwnd):
    """保持游戏当前分辨率不变，只还原、置前并移动到屏幕左上角。"""
    try:
        import win32con
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    rect = win32gui.GetWindowRect(hwnd)
    width = int(rect[2] - rect[0])
    height = int(rect[3] - rect[1])
    win32gui.MoveWindow(hwnd, 0, 0, width, height, True)
    time.sleep(0.1)


def prepare_game_window(keyword="冒险岛怀旧服", capture_mode=False):
    """启动前定位游戏窗口，并保持分辨率贴齐屏幕左上角。"""
    handles = find_window_by_title(keyword)
    if not handles:
        if capture_mode:
            trace_event(
                "game_window_capture_aligned",
                success=False,
                hwnd=None,
                client_width=游戏客户区宽度,
                client_height=游戏客户区高度,
                client_left=游戏客户区屏幕左,
                client_top=游戏客户区屏幕顶,
                outer_width=游戏窗口外框宽度,
                outer_height=游戏窗口外框高度,
                preserve_resolution=True,
            )
        print("没有找到包含 '{}' 的窗口".format(keyword))
        return False

    # 截图模式和正常模式都必须使用同一套还原、置前、保持尺寸并贴齐原点的流程。
    # 正常模式继续逐个处理所有匹配窗口；截图模式沿用只对首个游戏窗口采集尺寸的行为。
    target_handles = handles[:1] if capture_mode else handles
    client_width = 游戏客户区宽度
    client_height = 游戏客户区高度
    for hwnd in target_handles:
        if not capture_mode:
            print(f"找到窗口句柄: {hwnd}，移动到 (0,0)")
        将游戏窗口贴齐截图原点(hwnd)
        client_width, client_height = 更新游戏客户区尺寸(hwnd)

    if capture_mode:
        hwnd = target_handles[0]
        trace_event(
            "game_window_capture_aligned",
            success=True,
            hwnd=hwnd,
            client_width=client_width,
            client_height=client_height,
            client_left=游戏客户区屏幕左,
            client_top=游戏客户区屏幕顶,
            outer_width=游戏窗口外框宽度,
            outer_height=游戏窗口外框高度,
            preserve_resolution=True,
        )
        print("[测试] 游戏窗口已固定到左上角，客户区={}x{}，外框={}x{}。".format(
            client_width,
            client_height,
            游戏窗口外框宽度,
            游戏窗口外框高度,
        ))
    return True



# 在模块导入结束前恢复旧的公开入口，AutomationService 无需感知路线已拆分。
安装地图路线()

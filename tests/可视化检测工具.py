"""
可视化检测测试工具 - 从 detection_threadyolo 提取
功能：屏幕截图 + YOLO怪物识别 + 人物颜色检测 + 轮子模板匹配
按 Q 退出，按 S 保存截图
"""

import os
import sys
import time

# 工具已移动到 tests；第三方库缓存固定到项目内，避免用户目录权限影响导入。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "YOLO_CONFIG_DIR",
    os.path.join(PROJECT_ROOT, ".v3_runtime", "ultralytics"),
)
os.environ.setdefault(
    "MPLCONFIGDIR",
    os.path.join(PROJECT_ROOT, ".v3_runtime", "matplotlib"),
)

import mss
import numpy as np
import cv2
from ultralytics import YOLO


# ==================== 配置区域 ====================

# 选择地图模型（修改这里切换）
MAP_NAME = "蘑菇"  # 可选: 蘑菇/蘑菇半层/木面2/天使猴子/军营2/林中石头人/林中怪猫/林中青龙/雪域冰原2大灰狼/武陵迷雾森林/时间之路1/火野猪1 等

# 搜索范围（像素）
SEARCH_RANGE = 2

# 人物颜色检测 - 支持任意颜色十六进制
# 示例: "ee0000"(红), "4a7c9f"(蓝灰), "ffffff"(白), "808080"(灰)
PERSON_COLOR_HEX = "ff0000"

# 颜色容差(0-255)，越大检测范围越广
COLOR_TOLERANCE = 1

# 轮/绳子模板检测开关
ENABLE_LUN_DETECT = True

# 人物颜色检测开关
ENABLE_PERSON_DETECT = False

# 血量图片检测开关（使用模板匹配）
ENABLE_XUE_DETECT = True
# 血量图片文件名（放在执行路径下）
XUE_IMG_NAME = "xueliang.png"
# 血量图片匹配阈值（0-1，越高越严格）
XUE_MATCH_THRESHOLD = 0.45

# ==================== 代码区域 ====================

def get_base_dir():
    """返回项目根目录，使工具迁入 tests 后仍能找到模型和图片资源。"""
    if getattr(sys, 'frozen', None):
        return sys._MEIPASS
    return PROJECT_ROOT


def cv_imread(file_path):
    """
    支持中文路径的imread - cv2.imread在Windows中文路径下会返回None
    改用 np.fromfile + cv2.imdecode 读取
    """
    if not os.path.exists(file_path):
        return None
    try:
        data = np.fromfile(file_path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"[错误] 读取图片失败 {file_path}: {e}")
        return None


def hex_to_rgb(hex_str):
    """把六位十六进制颜色文本转换为 RGB 元组。"""
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))


def detect_color_contours(frame, hex_color, tolerance=30):
    """
    通用颜色检测 - 自动判断颜色类型使用最佳检测方式
    低饱和度颜色(白/灰/黑)用BGR直接比较
    彩色(红/绿/蓝等)用HSV范围检测
    """
    r, g, b = hex_to_rgb(hex_color)
    bgr_target = np.uint8([[[b, g, r]]])
    hsv_target = cv2.cvtColor(bgr_target, cv2.COLOR_BGR2HSV)[0][0]
    hue, sat, val = int(hsv_target[0]), int(hsv_target[1]), int(hsv_target[2])

    # 判断颜色类型：sat < 30 为无色系
    is_achromatic = sat < 30

    if is_achromatic:
        # 无色系：用BGR欧几里得距离直接比较
        target_bgr = np.array([b, g, r], dtype=np.int16)
        frame_int = frame.astype(np.int16)
        diff = np.sqrt(np.sum((frame_int - target_bgr) ** 2, axis=2))
        mask = (diff <= tolerance).astype(np.uint8) * 255
    else:
        # 彩色：用HSV范围
        lower_hsv = np.array([max(hue - 10, 0), max(sat - 40, 50), max(val - 40, 50)])
        upper_hsv = np.array([min(hue + 10, 179), 255, 255])
        hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv_frame, lower_hsv, upper_hsv)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    color_type = "BGR" if is_achromatic else "HSV"
    return contours, color_type


def main():
    """持续显示 YOLO、人物、血量和符文模板的可视化检测结果。"""
    basedir = get_base_dir()

    # 加载地图模型
    model_path = os.path.join(basedir, "moxing", f"{MAP_NAME}.pt")
    if not os.path.exists(model_path):
        print(f"[错误] 找不到模型文件: {model_path}")
        print(f"可用地图: 蘑菇, 蘑菇半层, 木面2, 天使猴子, 军营2, 林中石头人, 林中怪猫,")
        print(f"         林中青龙, 雪域冰原2大灰狼, 雪域冰原2黑雪人, 武陵迷雾森林,")
        print(f"         武陵海盗船2, 时间之路1, 火野猪1")
        return
    print(f"[加载] 地图模型: {model_path}")
    model = YOLO(model_path)

    # 加载血量图片模板（从执行路径下加载）
    xue_img_path = os.path.join(basedir, XUE_IMG_NAME)
    # 兼容：如果执行路径下没有，去resres目录找
    if not os.path.exists(xue_img_path):
        xue_img_path = os.path.join(basedir, "resres", XUE_IMG_NAME)
    if os.path.exists(xue_img_path):
        xue_template = cv_imread(xue_img_path)
        if xue_template is not None:
            print(f"[加载] 血量图片: {xue_img_path} 尺寸:{xue_template.shape[1]}x{xue_template.shape[0]}")
        else:
            print(f"[警告] 血量图片存在但读取失败: {xue_img_path}")
    else:
        xue_template = None
        print(f"[警告] 找不到血量图片: {XUE_IMG_NAME}（请放在执行路径下或resres目录）")

    # 加载轮子模板
    resres_dir = os.path.join(basedir, "resres")
    lun_path = os.path.join(resres_dir, "lun.png")
    if os.path.exists(lun_path):
        lun_template = cv_imread(lun_path)
        if lun_template is not None:
            print(f"[加载] 轮子模板: {lun_path} 尺寸:{lun_template.shape[1]}x{lun_template.shape[0]}")
        else:
            print(f"[警告] 轮子模板读取失败: {lun_path}")
    else:
        lun_template = None
        print("[警告] 找不到轮子模板，跳过轮子检测")

    # 加载符文提示模板
    luntishi_path = os.path.join(resres_dir, "fuwentishi.png")
    if os.path.exists(luntishi_path):
        luntishi_template = cv_imread(luntishi_path)
        if luntishi_template is not None:
            print(f"[加载] 符文提示模板: {luntishi_path}")
        else:
            print(f"[警告] 符文提示模板读取失败: {luntishi_path}")
    else:
        luntishi_template = None

    # 计算目标颜色信息
    rgb = hex_to_rgb(PERSON_COLOR_HEX)
    bgr_test = np.uint8([[[rgb[2], rgb[1], rgb[0]]]])
    hsv_test = cv2.cvtColor(bgr_test, cv2.COLOR_BGR2HSV)[0][0]
    color_type = "BGR" if int(hsv_test[1]) < 30 else "HSV"
    print(f"[颜色] 目标: #{PERSON_COLOR_HEX} RGB:{rgb} HSV:({int(hsv_test[0])},{int(hsv_test[1])},{int(hsv_test[2])}) 模式:{color_type}")

    # 屏幕截图区域（与正式版一致）
    monitor_game = {'top': 300, 'left': 0, 'width': 1280, 'height': 300}
    monitor_tishi = {'top': 163, 'left': 479, 'width': 280, 'height': 100}

    print(f"\n{'='*50}")
    print(f"  可视化检测测试工具")
    print(f"  地图: {MAP_NAME}")
    print(f"  模型: {model_path}")
    print(f"  按 Q 退出 | 按 S 保存截图")
    print(f"{'='*50}\n")

    window_name = f"YOLO检测结果 - {MAP_NAME} (按Q退出, 按S保存)"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    frame_count = 0
    last_time = time.time()
    fps = 0

    try:
        while True:
            # 截图
            with mss.mss() as sct:
                sct_img = sct.grab(monitor_game)
            img = np.array(sct_img)
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            # 克隆一份用于绘制
            display = img.copy()

            # ========== 1. YOLO怪物检测 ==========
            results = model(img, verbose=False,conf=0.75)
            monster_count = 0
            monster_centers = []

            for result in results:
                for box in result.boxes:
                    cls_id = int(box.cls[0])
                    xyxy = box.xyxy[0].cpu().numpy()
                    conf = box.conf[0].cpu().numpy()

                    # 画矩形框
                    x1, y1 = int(xyxy[0]), int(xyxy[1])
                    x2, y2 = int(xyxy[2]), int(xyxy[3])

                    if cls_id == 0:
                        # 怪物 - 绿色框
                        color = (0, 255, 0)
                        label = f"guai {conf:.2f}"
                        monster_count += 1
                        cx = (x1 + x2) // 2
                        cy = y2
                        monster_centers.append((cx, cy))
                    else:
                        # 其他类别 - 蓝色框
                        color = (255, 0, 0)
                        label = f"cls{cls_id} {conf:.2f}"

                    cv2.rectangle(display, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(display, label, (x1, y1 - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

            # ========== 2. 人物颜色检测 ==========
            if ENABLE_PERSON_DETECT:
                frame = img.copy()
                contours, detect_mode = detect_color_contours(frame, PERSON_COLOR_HEX, COLOR_TOLERANCE)

                max_area = 0
                max_cnt = None
                for cnt in contours:
                    area = cv2.contourArea(cnt)
                    if area > max_area and area > 0:
                        max_area = area
                        max_cnt = cnt

                if max_cnt is not None:
                    x, y, w, h = cv2.boundingRect(max_cnt)
                    if w > 0:  # 最小宽度过滤
                        cv2.rectangle(display, (x, y), (x + w, y + h), (0, 0, 255), 2)
                        person_cx = x + w // 2
                        person_cy = y + h // 2
                        cv2.circle(display, (person_cx, person_cy), 5, (0, 0, 255), -1)
                        cv2.putText(display, f"person ({person_cx},{person_cy}) [{detect_mode}]",
                                    (x, y - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

                        # 检测与怪物的距离关系
                        for mcx, mcy in monster_centers:
                            dy = abs(person_cy + 300 - mcy)
                            if dy < 70:
                                dx = abs(person_cx - mcx)
                                side = "L" if person_cx > mcx else "R"
                                cv2.line(display, (person_cx, person_cy + 300),
                                         (mcx, mcy), (0, 165, 255), 1)
                                cv2.putText(display, f"{side} dx={int(dx)}",
                                            (mcx, mcy + 15), cv2.FONT_HERSHEY_SIMPLEX,
                                            0.4, (0, 165, 255), 1)

            # ========== 3. 血量图片模板匹配 ==========
            if ENABLE_XUE_DETECT and xue_template is not None:
                # 在全屏截图中匹配血量图片
                res_xue = cv2.matchTemplate(img, xue_template, cv2.TM_CCOEFF_NORMED)
                _, max_xue_val, _, max_xue_loc = cv2.minMaxLoc(res_xue)
                if max_xue_val >= XUE_MATCH_THRESHOLD:
                    # 画框标识血量位置
                    tx, ty = max_xue_loc
                    tw, th = xue_template.shape[1], xue_template.shape[0]
                    cv2.rectangle(display, (tx, ty), (tx + tw, ty + th), (255, 255, 0), 2)
                    cv2.putText(display, f"xue {max_xue_val:.2f}",
                                (tx, ty - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
                    # 中心点
                    cx_xue = tx + tw // 2
                    cy_xue = ty + th // 2
                    cv2.circle(display, (cx_xue, cy_xue), 4, (255, 255, 0), -1)
                else:
                    cv2.putText(display, f"xue conf={max_xue_val:.2f} (below {XUE_MATCH_THRESHOLD})",
                                (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (128, 128, 128), 1)

            # ========== 4. 轮子/绳子模板匹配 ==========
            if ENABLE_LUN_DETECT and lun_template is not None:
                # 先检测符文提示（判断是否需要检测轮子）
                lun_enabled = True
                if luntishi_template is not None:
                    with mss.mss() as sct:
                        sct_tishi = sct.grab(monitor_tishi)
                    img_tishi = np.array(sct_tishi)
                    img_tishi = cv2.cvtColor(img_tishi, cv2.COLOR_BGRA2BGR)
                    res_tishi = cv2.matchTemplate(img_tishi, luntishi_template, cv2.TM_CCOEFF_NORMED)
                    _, max_tishi_val, _, _ = cv2.minMaxLoc(res_tishi)
                    lun_enabled = max_tishi_val >= 0.5
                    if not lun_enabled:
                        cv2.putText(display, f"no-fuwen ({max_tishi_val:.2f})",
                                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (128, 128, 128), 1)

                if lun_enabled:
                    res_lun = cv2.matchTemplate(img, lun_template, cv2.TM_CCOEFF_NORMED)
                    _, max_lun_val, _, max_loc = cv2.minMaxLoc(res_lun)
                    lun_thresh = 0.65
                    if max_lun_val >= lun_thresh:
                        lx = max_loc[0] + lun_template.shape[1] // 2
                        ly = max_loc[1] + lun_template.shape[0] // 2
                        cv2.circle(display, (lx, ly), 8, (255, 255, 0), 2)
                        cv2.drawMarker(display, (lx, ly), (255, 255, 0),
                                       cv2.MARKER_CROSS, 20, 2)
                        cv2.putText(display, f"lun ({lx},{ly}) conf={max_lun_val:.2f}",
                                    (lx + 10, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
                    else:
                        cv2.putText(display, f"lun conf={max_lun_val:.2f} (below {lun_thresh})",
                                    (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (128, 128, 128), 1)

            # ========== 5. 绘制信息面板 ==========
            frame_count += 1
            now = time.time()
            if now - last_time >= 1.0:
                fps = frame_count
                frame_count = 0
                last_time = now

            info_lines = [
                f"Map: {MAP_NAME}",
                f"Monsters: {monster_count}",
                f"Person: #{PERSON_COLOR_HEX} tol={COLOR_TOLERANCE}",
                f"Xue img: {XUE_IMG_NAME} thr={XUE_MATCH_THRESHOLD}",
                f"Search Range: {SEARCH_RANGE}px",
                f"FPS: {fps}",
            ]
            for i, line in enumerate(info_lines):
                cv2.putText(display, line, (10, 20 + i * 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

            # ========== 6. 显示画面 ==========
            cv2.imshow(window_name, display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                print("[退出] 收到Q键")
                break
            elif key == ord('s'):
                save_path = os.path.join(basedir, f"detect_screenshot_{int(time.time())}.png")
                cv2.imwrite(save_path, display)
                print(f"[保存] 截图已保存: {save_path}")

    except KeyboardInterrupt:
        print("[退出] 键盘中断")
    finally:
        cv2.destroyAllWindows()
        print("[退出] 测试结束")


if __name__ == "__main__":
    main()

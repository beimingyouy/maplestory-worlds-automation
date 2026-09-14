from pathlib import Path
import ctypes
import os
import time
from typing import Callable

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig, ConfigStore
from .branding import display_name
from .monster_atlas_dialog import MonsterAtlasDialog
from .public.monster_detection import (
    custom_template_directory,
    ensure_custom_template_directory,
)
from .public.minimap_tracking import MINIMAP_MONITOR
from .map_registry import map_names
from .public.route_recording import (
    SEGMENT_DOWN_JUMP,
    SEGMENT_PLATFORM,
    SEGMENT_PLATFORM_JUMP_LEFT,
    SEGMENT_PLATFORM_JUMP_NEUTRAL,
    SEGMENT_PLATFORM_JUMP_RIGHT,
    SEGMENT_PLATFORM_TO_ROPE_LEFT,
    SEGMENT_PLATFORM_TO_ROPE_RIGHT,
    SEGMENT_RIGHT_RETURN,
    SEGMENT_ROPE,
    SEGMENT_ROPE_TO_PLATFORM_LEFT,
    SEGMENT_ROPE_TO_PLATFORM_RIGHT,
    SEGMENT_TRIAL_WALK_V2,
    SEGMENT_WALK_OFF_LEFT,
    SEGMENT_WALK_OFF_RIGHT,
    RouteRecorderService,
    mushroom_v3_recordings_directory,
    mushroom_v3_preview_path,
    read_route_rest_point,
    resolve_mushroom_v3_route_path,
    save_route_rest_point,
)


FIXED_KEY_OPTIONS = (
    "x",
    "f",
    "c",
    "z",
    "a",
    "s",
    "d",
    "q",
    "w",
    "e",
    "r",
    "ctrl",
    "shift",
    "alt",
    "space",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "0",
    "b",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "t",
    "u",
    "y",
    "f1",
    "f2",
    "f3",
    "f4",
    "f5",
    "f6",
    "f7",
    "f8",
    "f9",
    "tab",
    "enter",
)


class _ProcessMemoryCounters(ctypes.Structure):
    """Windows GetProcessMemoryInfo 使用的精简进程内存结构。"""

    _fields_ = (
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


class FixedComboBox(QComboBox):
    """禁止滚轮修改选项，只允许用户展开并点击固定值。"""

    def __init__(self, parent=None):
        """创建只能通过鼠标展开选择、不会接收键盘输入的下拉框。"""
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)

    def wheelEvent(self, event):
        """忽略控件自身的滚轮切换，并把滚动机会留给外层页面。"""
        event.ignore()


class FixedSpinBox(QSpinBox):
    """允许自定义整数，只禁止滚轮误改并固定显示单位。"""

    def __init__(self, parent=None):
        """创建可输入数值、可点击上下按钮且单位不可编辑的整数控件。"""
        super().__init__(parent)
        self.lineEdit().setReadOnly(False)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        """忽略滚轮改值，避免页面滚动时误改参数。"""
        event.ignore()


class FixedDoubleSpinBox(QDoubleSpinBox):
    """允许自定义小数，只禁止滚轮误改并固定显示单位。"""

    def __init__(self, parent=None):
        """创建可输入数值、可点击上下按钮且单位不可编辑的小数控件。"""
        super().__init__(parent)
        self.lineEdit().setReadOnly(False)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        """忽略滚轮改值，避免页面滚动时误改参数。"""
        event.ignore()


class MainWindow(QMainWindow):
    """展示运行配置、动态模式切换、检测预览和任务日志。"""

    def __init__(self, service, store: ConfigStore, test_mode: bool = False):
        """装配服务、配置仓库和主窗口控件。"""
        super().__init__()
        self.service = service
        self.store = store
        self.test_mode = test_mode
        self.detection_only_mode = False
        self._close_callbacks = []
        self._pending_log = ""
        self._custom_left_position = None
        self._custom_right_position = None
        self._last_route_preview_path = None
        self._last_route_data = None
        self._rest_snapshot_count = 0
        self._rest_countdown_status = "stopped"
        self._rest_countdown_deadline = 0.0
        self._rest_countdown_remaining = 0.0
        self._rest_countdown_number = 1
        self._route_monitor_payload = {}
        self._service_state = "idle"
        self._pending_rest_point_test = False
        self._route_platform_count = 1
        self._updating_route_probability = False
        self._pending_route_segment_type = None
        self._resource_baseline = None
        self._resource_last_sample = None
        self._resource_monotonic_growth = 0
        self._route_preview_resize_timer = QTimer(self)
        self._route_preview_resize_timer.setSingleShot(True)
        self._route_preview_resize_timer.timeout.connect(self._render_route_preview)
        self.scheduled_key_rows = []
        self.route_recorder = RouteRecorderService(self.store.path.parent)
        self.add_close_callback(lambda: self.route_recorder.stop(save=False))

        title = display_name()
        if self.test_mode:
            title += " - 截图测试模式"
        self.setWindowTitle(title)
        # 运行配置与路线录制分页显示，缩小默认窗口也不会挤压按钮。
        self.setMinimumSize(1080, 720)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            initial_width = max(1080, min(1320, available.width() - 40))
            initial_height = max(720, min(820, available.height() - 40))
            self.resize(initial_width, initial_height)
        else:
            self.resize(1280, 800)
        self._build_ui()
        self._rest_countdown_timer = QTimer(self)
        self._rest_countdown_timer.setInterval(500)
        self._rest_countdown_timer.timeout.connect(self._refresh_rest_countdown)
        self._rest_countdown_timer.start()
        self._resource_monitor_timer = QTimer(self)
        self._resource_monitor_timer.setInterval(1000)
        self._resource_monitor_timer.timeout.connect(self._refresh_resource_monitor)
        self._resource_monitor_timer.start()
        self._connect_service()
        self._connect_route_recorder()
        QTimer.singleShot(0, self._load_last_route_preview)
        initial_config = self.store.load()
        if test_mode:
            # 保留旧TEST_MODE和--test入口：它们只负责页面首次勾选，用户仍可
            # 在任务启动前直接从页面切回正常模式。
            initial_config.test_mode = True
        self.apply_config(initial_config)
        self._refresh_rest_point_test_button()

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """注册窗口关闭时需要执行的资源清理回调。"""
        self._close_callbacks.append(callback)

    def _build_ui(self) -> None:
        """构建主窗口的顶栏、设置区和监控区。"""
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        outer.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._build_settings_panel(), 46)
        body.addWidget(self._build_monitor_panel(), 54)
        outer.addLayout(body, 1)

    def _build_header(self) -> QFrame:
        """创建标题、版本和当前运行状态区域。"""
        frame = QFrame()
        frame.setObjectName("header")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(20, 15, 20, 15)

        title_layout = QVBoxLayout()
        title = QLabel(display_name())
        title.setObjectName("appTitle")
        subtitle_text = "兼容 1.9 全部地图逻辑 · 分层架构 · 安全启停"
        if self.test_mode:
            subtitle_text = "截图测试模式 · 实时预览 · 移动攻击和地图路线正常运行"
        self.subtitle_label = QLabel(subtitle_text)
        self.subtitle_label.setObjectName("subtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(self.subtitle_label)
        layout.addLayout(title_layout)
        layout.addStretch()

        self.version_label = QLabel("V3.0 TEST" if self.test_mode else "V3.0")
        self.version_label.setObjectName("versionBadge")
        self.status_badge = QLabel("● 已停止")
        self.status_badge.setObjectName("statusBadge")
        self.status_badge.setProperty("state", "idle")
        layout.addWidget(self.version_label)
        layout.addSpacing(8)
        layout.addWidget(self.status_badge)
        return frame

    def _build_settings_panel(self) -> QWidget:
        """创建紧凑的运行配置与路线录制分页面板。"""
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(4)

        # 核心启停按钮固定在分页上方，任何页面都可直接操作。
        panel_layout.addWidget(self._build_action_frame())
        self.settings_tabs = QTabWidget()

        runtime_scroll = QScrollArea()
        runtime_scroll.setWidgetResizable(True)
        runtime_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("settingsRuntimeContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 3, 0)
        layout.setSpacing(4)

        recording_page = QWidget()
        recording_page.setObjectName("settingsRecordingPage")
        recording_layout = QVBoxLayout(recording_page)
        recording_layout.setContentsMargins(4, 4, 4, 4)
        recording_layout.setSpacing(4)

        map_group = QGroupBox("运行配置")
        form = QGridLayout(map_group)
        form.setContentsMargins(6, 6, 6, 6)
        form.setHorizontalSpacing(5)
        form.setVerticalSpacing(3)
        self.map_combo = FixedComboBox()
        self.map_combo.setEditable(False)
        self.map_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.map_combo.setMinimumContentsLength(8)
        self.map_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.map_combo.addItems(map_names())
        self.map_combo.currentTextChanged.connect(self._on_map_selection_changed)
        self.route_map_file_label = QLabel(self.route_recorder.route_path.name)
        self.route_map_file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.route_map_file_label.setToolTip(str(self.route_recorder.route_path))
        self.route_map_file_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.route_map_load_button = QPushButton("选JSON")
        self.route_map_load_button.setToolTip(
            "选择后会自动切换到“自定义录制路线”，运行时只执行该JSON中的路线动作。"
        )
        self.route_map_load_button.clicked.connect(self._load_route_json)
        route_map_file_widget = QWidget()
        route_map_file_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        route_map_file_layout = QHBoxLayout(route_map_file_widget)
        route_map_file_layout.setContentsMargins(0, 0, 0, 0)
        route_map_file_layout.setSpacing(3)
        route_map_file_layout.addWidget(self.route_map_file_label, 1)
        route_map_file_layout.addWidget(self.route_map_load_button)
        self.test_mode_enabled = QCheckBox("截图运行")
        self.test_mode_enabled.setToolTip(
            "开启后自动把游戏窗口固定到左上角并显示实时检测截图；地图路线、移动、攻击和日志保持正常。"
        )
        self.detection_only_enabled = QCheckBox("纯识别测试")
        self.detection_only_enabled.setToolTip(
            "只截图并绘制怪物识别框和置信度；不会运行路线、移动、攻击、补药或发送任何游戏按键。"
        )
        self.runtime_logging_enabled = QCheckBox("记录日志")
        self.runtime_logging_enabled.setChecked(True)
        self.runtime_logging_enabled.setToolTip(
            "开启时在 logs 目录创建运行轨迹日志；关闭后不写入运行轨迹文件，页面即时输出仍会显示。"
        )
        self.wheel_detection_enabled = QCheckBox("启用轮检测")
        self.wheel_detection_enabled.setChecked(False)
        self.wheel_detection_enabled.setToolTip(
            "默认关闭；勾选后才检测符文提示和轮，并执行解轮流程。"
        )
        self.test_mode_enabled.toggled.connect(
            self._on_screenshot_mode_toggled
        )
        self.detection_only_enabled.toggled.connect(
            self._on_detection_only_mode_toggled
        )
        self.attack_distance = FixedDoubleSpinBox()
        self.attack_distance.setRange(20, 1000)
        self.attack_distance.setDecimals(0)
        self.attack_distance.setSingleStep(10)
        self.attack_distance.setSuffix(" px")
        self.attack_height = FixedDoubleSpinBox()
        self.attack_height.setRange(10, 1000)
        self.attack_height.setDecimals(0)
        self.attack_height.setSingleStep(5)
        self.attack_height.setSuffix(" px")
        self.yolo_confidence = FixedDoubleSpinBox()
        self.yolo_confidence.setRange(0.30, 0.95)
        self.yolo_confidence.setDecimals(2)
        self.yolo_confidence.setSingleStep(0.05)
        self.yolo_confidence.setToolTip("数值越高越不容易把死亡动画识别成怪物，但过高可能漏怪。")
        self.rope_rest_interval = FixedDoubleSpinBox()
        self.rope_rest_interval.setRange(0, 1440)
        self.rope_rest_interval.setDecimals(1)
        self.rope_rest_interval.setSingleStep(1)
        self.rope_rest_interval.setSuffix(" 分钟")
        self.rope_rest_interval.setToolTip(
            "连续运行到该时长后前往JSON录制的休息点；未录制休息点时回退到下一条绳子中点。设为0关闭。"
        )
        self.rope_rest_duration = FixedDoubleSpinBox()
        self.rope_rest_duration.setRange(0, 120)
        self.rope_rest_duration.setDecimals(1)
        self.rope_rest_duration.setSingleStep(0.5)
        self.rope_rest_duration.setSuffix(" 分钟")
        self.rope_rest_duration.setToolTip("到达录制休息点后的停留时长；设为0关闭定时休息。")
        self.ignored_platform_numbers_input = QLineEdit()
        self.ignored_platform_numbers_input.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Fixed,
        )
        self.ignored_platform_numbers_input.setPlaceholderText("例如：1/2/3")
        self.ignored_platform_numbers_input.setToolTip(
            "填写要忽略的平台数字，支持多个，用 / 隔开。"
            "人物到达这些平台后不停留寻怪，直接按录制路线前往下一平台。"
        )
        compact_runtime_controls = (
            self.route_map_load_button,
        )
        for button in compact_runtime_controls:
            button.setMinimumHeight(24)
            button.setMaximumHeight(26)
        for control in (
            self.attack_distance,
            self.attack_height,
            self.yolo_confidence,
            self.rope_rest_interval,
            self.rope_rest_duration,
        ):
            control.setMaximumWidth(85)
        attack_range_widget = QWidget()
        attack_range_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        attack_range_layout = QHBoxLayout(attack_range_widget)
        attack_range_layout.setContentsMargins(0, 0, 0, 0)
        attack_range_layout.setSpacing(2)
        attack_range_layout.addWidget(QLabel("横"))
        attack_range_layout.addWidget(self.attack_distance, 1)
        attack_range_layout.addWidget(QLabel("纵"))
        attack_range_layout.addWidget(self.attack_height, 1)
        self.attack_distance.setMinimumWidth(72)
        self.attack_height.setMinimumWidth(72)
        yolo_widget = QWidget()
        yolo_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        yolo_layout = QHBoxLayout(yolo_widget)
        yolo_layout.setContentsMargins(0, 0, 0, 0)
        yolo_layout.setSpacing(2)
        yolo_layout.addWidget(QLabel("YOLO"))
        yolo_layout.addWidget(self.yolo_confidence, 1)
        rest_time_widget = QWidget()
        rest_time_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        rest_time_layout = QHBoxLayout(rest_time_widget)
        rest_time_layout.setContentsMargins(0, 0, 0, 0)
        rest_time_layout.setSpacing(2)
        rest_time_layout.addWidget(QLabel("隔"))
        rest_time_layout.addWidget(self.rope_rest_interval, 1)
        rest_time_layout.addWidget(QLabel("停"))
        rest_time_layout.addWidget(self.rope_rest_duration, 1)
        form.addWidget(self.test_mode_enabled, 0, 0)
        form.addWidget(self.detection_only_enabled, 0, 1)
        form.addWidget(self.runtime_logging_enabled, 0, 2)
        form.addWidget(self.wheel_detection_enabled, 0, 3)
        form.addWidget(QLabel("地图"), 1, 0)
        form.addWidget(self.map_combo, 1, 1)
        form.addWidget(QLabel("路线JSON"), 1, 2)
        form.addWidget(route_map_file_widget, 1, 3)
        form.addWidget(QLabel("攻击范围"), 2, 0)
        form.addWidget(attack_range_widget, 2, 1, 1, 2)
        form.addWidget(yolo_widget, 2, 3)
        form.addWidget(QLabel("休息"), 3, 0)
        form.addWidget(rest_time_widget, 3, 1)
        form.addWidget(QLabel("忽略平台"), 3, 2)
        form.addWidget(self.ignored_platform_numbers_input, 3, 3)
        for column in (1, 3):
            form.setColumnStretch(column, 1)
        layout.addWidget(map_group)

        key_group = QGroupBox("按键设置")
        key_form = QFormLayout(key_group)
        key_form.setContentsMargins(6, 6, 6, 6)
        key_form.setHorizontalSpacing(5)
        key_form.setVerticalSpacing(3)
        self.single_key = self._key_input("x")
        self.group_key = self._key_input("f")
        self.flash_key = self._key_input("c")
        key_form.addRow("单体攻击", self.single_key)
        key_form.addRow("群体攻击", self.group_key)
        key_form.addRow("闪现", self.flash_key)

        custom_group = QGroupBox("怪物模板与旧版边界")
        custom_layout = QVBoxLayout(custom_group)
        custom_layout.setContentsMargins(6, 6, 6, 6)
        custom_layout.setSpacing(3)
        self.custom_mode = QCheckBox("旧边界")
        self.custom_mode.setToolTip("使用记录的左右边界路线，并自动使用img\\自定义怪物模板。")
        self.use_custom_templates = QCheckBox("换怪物模板")
        self.use_custom_templates.setToolTip(
            "地图路线、爬绳和平台逻辑保持不变，只加载img\\自定义内以guai开头的图片。"
        )
        self.custom_mode.toggled.connect(self._on_custom_mode_toggled)
        self.use_custom_templates.toggled.connect(
            self._on_custom_template_override_toggled
        )
        self.custom_description = QLabel(
            "可选择自定义左右路线，或只替换怪物识别并保留原地图路线。"
        )
        self.custom_description.setWordWrap(True)
        self.custom_description.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.custom_left_label = QLabel("左边：未记录")
        self.custom_right_label = QLabel("右边：未记录")
        self.custom_left_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.custom_right_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        record_left = QPushButton("记录左边")
        record_right = QPushButton("记录右边")
        record_left.clicked.connect(lambda: self._record_custom_position("left"))
        record_right.clicked.connect(lambda: self._record_custom_position("right"))
        left_record_row = QHBoxLayout()
        left_record_row.addWidget(self.custom_left_label, 1)
        left_record_row.addWidget(record_left)
        right_record_row = QHBoxLayout()
        right_record_row.addWidget(self.custom_right_label, 1)
        right_record_row.addWidget(record_right)
        full_custom_path = str(custom_template_directory(self.store.path.parent))
        self.custom_template_path = QLabel("固定目录：img\\自定义")
        self.custom_template_path.setWordWrap(True)
        self.custom_template_path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.custom_template_path.setToolTip(full_custom_path)
        self.custom_template_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        open_custom_folder = QPushButton("打开模板文件夹")
        open_custom_folder.clicked.connect(self._open_custom_template_folder)
        monster_atlas_button = QPushButton("怪物图鉴")
        monster_atlas_button.setToolTip(
            "按 img\\monsters 的等级目录多选怪物，并自动拆成 img\\自定义\\guai* 识图模板。"
        )
        monster_atlas_button.clicked.connect(self._open_monster_atlas)
        for button in (record_left, record_right, open_custom_folder, monster_atlas_button):
            button.setMinimumHeight(24)
            button.setMaximumHeight(26)
        custom_mode_row = QHBoxLayout()
        custom_mode_row.setSpacing(3)
        custom_mode_row.addWidget(self.custom_mode)
        custom_mode_row.addWidget(self.use_custom_templates)
        custom_layout.addLayout(custom_mode_row)
        custom_layout.addLayout(left_record_row)
        custom_layout.addLayout(right_record_row)
        custom_path_row = QHBoxLayout()
        custom_path_row.setSpacing(4)
        custom_path_row.addWidget(self.custom_template_path, 1)
        custom_path_row.addWidget(monster_atlas_button)
        custom_path_row.addWidget(open_custom_folder)
        custom_layout.addLayout(custom_path_row)

        key_and_custom = QHBoxLayout()
        key_and_custom.setSpacing(4)
        key_and_custom.addWidget(key_group, 1)
        key_and_custom.addWidget(custom_group, 1)
        layout.addLayout(key_and_custom)

        scheduled_key_group = QGroupBox("定时按键")
        scheduled_key_layout = QVBoxLayout(scheduled_key_group)
        scheduled_key_layout.setContentsMargins(6, 6, 6, 6)
        scheduled_key_layout.setSpacing(3)
        scheduled_key_description = QLabel(
            "可设置多个按键及执行周期；启动后等待一个完整周期再首次按下。"
        )
        scheduled_key_description.setWordWrap(True)
        scheduled_key_description.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.scheduled_key_rows_layout = QVBoxLayout()
        self.scheduled_key_rows_layout.setSpacing(3)
        self.add_scheduled_key_button = QPushButton("新增定时按键")
        self.add_scheduled_key_button.setMinimumHeight(24)
        self.add_scheduled_key_button.setMaximumHeight(26)
        self.add_scheduled_key_button.clicked.connect(
            lambda: self._add_scheduled_key_row()
        )
        scheduled_key_header = QHBoxLayout()
        scheduled_key_header.setSpacing(4)
        scheduled_key_header.addWidget(scheduled_key_description, 1)
        scheduled_key_header.addWidget(self.add_scheduled_key_button)
        scheduled_key_layout.addLayout(scheduled_key_header)
        scheduled_key_layout.addLayout(self.scheduled_key_rows_layout)
        layout.addWidget(scheduled_key_group)

        route_record_group = QGroupBox("自定义地图路线录制")
        route_record_layout = QVBoxLayout(route_record_group)
        route_record_layout.setContentsMargins(6, 6, 6, 6)
        route_record_layout.setSpacing(3)
        route_record_description = QLabel(
            "V2：先完整试走一圈，再录平台、绳子和过渡；不点试走按钮仍按V1录制。"
        )
        route_record_description.setWordWrap(True)
        route_record_description.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        route_file_row = QHBoxLayout()
        route_file_row.addWidget(QLabel("当前路线"))
        self.route_file_label = QLabel(self.route_recorder.route_path.name)
        self.route_file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.route_file_label.setToolTip(str(self.route_recorder.route_path))
        self.route_file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.route_new_file_button = QPushButton("新建路线录制")
        self.route_new_file_button.setObjectName("primaryButton")
        self.route_new_file_button.setToolTip(
            "创建一份新的路线JSON录制任务；输入文件名后即可开始录制平台、绳子和过渡段。"
        )
        self.route_load_file_button = QPushButton("加载路线 JSON")
        self.route_load_file_button.setToolTip(
            "加载已有路线JSON，继续查看、追加录制或直接用于自定义录制路线。"
        )
        for route_file_button in (
            self.route_new_file_button,
            self.route_load_file_button,
        ):
            route_file_button.setMinimumHeight(30)
            route_file_button.setMaximumHeight(32)
        self.route_new_file_button.clicked.connect(self._new_route_file)
        self.route_load_file_button.clicked.connect(self._load_route_json)
        route_file_row.addWidget(self.route_file_label, 1)
        route_file_actions = QHBoxLayout()
        route_file_actions.setSpacing(4)
        route_file_actions.addWidget(self.route_new_file_button, 1)
        route_file_actions.addWidget(self.route_load_file_button, 1)
        platform_select_row = QHBoxLayout()
        platform_select_row.addWidget(QLabel("当前平台"))
        self.route_platform_combo = FixedComboBox()
        self.route_platform_combo.addItem("平台1")
        self.route_platform_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.route_platform_combo.setMinimumContentsLength(6)
        self.route_platform_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.route_platform_combo.currentTextChanged.connect(
            self._on_route_platform_selected
        )
        self.route_add_platform_button = QPushButton("新增平台")
        self.route_add_platform_button.setToolTip(
            "在当前路线中新增下一个平台，并切换到新平台继续录制。"
        )
        self.route_add_platform_button.clicked.connect(self._add_route_platform)
        self.route_delete_platform_button = QPushButton("删除当前平台")
        self.route_delete_platform_button.setToolTip(
            "删除当前选中的平台及其尚未保存的分段配置；平台1不能删除。"
        )
        self.route_delete_platform_button.setEnabled(False)
        self.route_delete_platform_button.clicked.connect(self._delete_route_platform)
        platform_select_row.addWidget(self.route_platform_combo, 1)
        platform_action_row = QHBoxLayout()
        platform_action_row.setSpacing(4)
        platform_action_row.addWidget(self.route_add_platform_button, 1)
        platform_action_row.addWidget(self.route_delete_platform_button, 1)
        for platform_action_button in (
            self.route_add_platform_button,
            self.route_delete_platform_button,
        ):
            platform_action_button.setMinimumHeight(28)
            platform_action_button.setMaximumHeight(30)

        probability_row = QGridLayout()
        probability_row.setHorizontalSpacing(3)
        probability_row.setVerticalSpacing(2)
        probability_row.addWidget(QLabel("权重"), 0, 0)
        self.route_down_jump_probability = FixedSpinBox()
        self.route_down_jump_probability.setRange(0, 100)
        self.route_down_jump_probability.setSuffix(" %")
        self.route_down_jump_probability.setValue(34)
        self.route_left_walk_off_probability = FixedSpinBox()
        self.route_left_walk_off_probability.setRange(0, 100)
        self.route_left_walk_off_probability.setSuffix(" %")
        self.route_left_walk_off_probability.setValue(33)
        self.route_right_return_probability = FixedSpinBox()
        self.route_right_return_probability.setRange(0, 100)
        self.route_right_return_probability.setSuffix(" %")
        self.route_right_return_probability.setValue(33)
        self.route_down_jump_probability.valueChanged.connect(
            self._on_down_jump_probability_changed
        )
        self.route_left_walk_off_probability.valueChanged.connect(
            self._on_left_walk_off_probability_changed
        )
        self.route_right_return_probability.valueChanged.connect(
            self._on_right_return_probability_changed
        )
        for probability_control in (
            self.route_down_jump_probability,
            self.route_left_walk_off_probability,
            self.route_right_return_probability,
        ):
            probability_control.setMaximumWidth(85)
            probability_control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        probability_row.addWidget(QLabel("下"), 0, 1)
        probability_row.addWidget(self.route_down_jump_probability, 0, 2)
        probability_row.addWidget(QLabel("左"), 0, 3)
        probability_row.addWidget(self.route_left_walk_off_probability, 0, 4)
        probability_row.addWidget(QLabel("右"), 0, 5)
        probability_row.addWidget(self.route_right_return_probability, 0, 6)
        probability_row.setColumnStretch(7, 1)
        rest_point_row = QHBoxLayout()
        self.route_rest_point_label = QLabel("休息点：未录制")
        self.route_rest_point_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.route_rest_point_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.route_record_rest_point_button = QPushButton("录制休息点")
        self.route_record_rest_point_button.setToolTip(
            "先让人物站到真正用于休息的位置，再点击本按钮；同时保存页面当前的休息间隔和休息时长。"
        )
        self.route_record_rest_point_button.clicked.connect(
            self._record_route_rest_point
        )
        rest_point_row.addWidget(self.route_rest_point_label, 1)
        self.route_record_status = QLabel(
            "V2请先点击“开始试走V2”；V1可直接选择分段并按F5开始。"
        )
        self.route_record_status.setWordWrap(True)
        self.route_record_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.route_record_status.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.route_record_platform_button = QPushButton("平台")
        self.route_record_rope_button = QPushButton("绳子")
        self.route_record_down_jump_button = QPushButton("下跳")
        self.route_record_left_walk_off_button = QPushButton("左走出")
        self.route_record_right_return_button = QPushButton("右走出")
        self.route_record_trial_start_button = QPushButton("开始试走 V2")
        self.route_record_trial_finish_button = QPushButton("结束试走 V2")
        self.route_record_trial_finish_button.setEnabled(False)
        self.route_record_platform_jump_left_button = QPushButton("平台左跳")
        self.route_record_platform_jump_right_button = QPushButton("平台右跳")
        self.route_record_platform_jump_neutral_button = QPushButton("平台原地跳")
        self.route_record_platform_to_rope_left_button = QPushButton("左跳上绳")
        self.route_record_platform_to_rope_right_button = QPushButton("右跳上绳")
        self.route_record_rope_to_platform_left_button = QPushButton("左跳落台")
        self.route_record_rope_to_platform_right_button = QPushButton("右跳落台")
        v2_transition_buttons = (
            (
                self.route_record_platform_jump_left_button,
                SEGMENT_PLATFORM_JUMP_LEFT,
                "当前平台是起点平台；站在左跳起点，F5后完成跳跃，落稳后F6。",
            ),
            (
                self.route_record_platform_jump_right_button,
                SEGMENT_PLATFORM_JUMP_RIGHT,
                "当前平台是起点平台；站在右跳起点，F5后完成跳跃，落稳后F6。",
            ),
            (
                self.route_record_platform_jump_neutral_button,
                SEGMENT_PLATFORM_JUMP_NEUTRAL,
                "当前平台是起点平台；站在原地跳点，F5后完成跳跃，落稳后F6。",
            ),
            (
                self.route_record_platform_to_rope_left_button,
                SEGMENT_PLATFORM_TO_ROPE_LEFT,
                "当前平台是起点平台；F5后左+C+上，稳定挂绳并上移几像素后F6。",
            ),
            (
                self.route_record_platform_to_rope_right_button,
                SEGMENT_PLATFORM_TO_ROPE_RIGHT,
                "当前平台是起点平台；F5后右+C+上，稳定挂绳并上移几像素后F6。",
            ),
            (
                self.route_record_rope_to_platform_left_button,
                SEGMENT_ROPE_TO_PLATFORM_LEFT,
                "当前平台是目标平台；挂在离绳点，F5后向左跳，落稳后F6。",
            ),
            (
                self.route_record_rope_to_platform_right_button,
                SEGMENT_ROPE_TO_PLATFORM_RIGHT,
                "当前平台是目标平台；挂在离绳点，F5后向右跳，落稳后F6。",
            ),
        )
        self._route_v2_transition_buttons = []
        for button, segment_type, tooltip in v2_transition_buttons:
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, selected_type=segment_type: self.request_route_segment(
                    selected_type
                )
            )
            self._route_v2_transition_buttons.append(button)
        self.route_record_start_button = QPushButton("开始 F5")
        self.route_record_finish_button = QPushButton("结束 F6")
        self.route_record_save_button = QPushButton("保存 F7")
        self.route_record_cancel_button = QPushButton("取消")
        self.route_record_summary_button = QPushButton("路线汇总")
        self.route_record_open_folder_button = QPushButton("打开文件夹")
        self.route_record_left_walk_off_button.setToolTip(
            "从当前平台向左走出，落到已录制的下层平台后自动结束。"
        )
        self.route_record_right_return_button.setToolTip(
            "从当前平台向右走出，落到已录制的下层平台后自动结束。"
        )
        self.route_record_start_button.setEnabled(False)
        self.route_record_finish_button.setEnabled(False)
        self.route_record_save_button.setEnabled(False)
        self.route_record_cancel_button.setEnabled(False)
        self.route_record_platform_button.clicked.connect(
            lambda: self.request_route_segment(SEGMENT_PLATFORM)
        )
        self.route_record_rope_button.clicked.connect(
            lambda: self.request_route_segment(SEGMENT_ROPE)
        )
        self.route_record_down_jump_button.clicked.connect(
            lambda: self.request_route_segment(SEGMENT_DOWN_JUMP)
        )
        self.route_record_left_walk_off_button.clicked.connect(
            lambda: self.request_route_segment(SEGMENT_WALK_OFF_LEFT)
        )
        self.route_record_right_return_button.clicked.connect(
            lambda: self.request_route_segment(SEGMENT_WALK_OFF_RIGHT)
        )
        self.route_record_trial_start_button.clicked.connect(self.start_route_trial_walk_v2)
        self.route_record_trial_finish_button.clicked.connect(self.finish_route_trial_walk_v2)
        self.route_record_start_button.clicked.connect(self.start_pending_route_segment)
        self.route_record_finish_button.clicked.connect(self.finish_route_segment)
        self.route_record_save_button.clicked.connect(self.save_route_recording)
        self.route_record_cancel_button.clicked.connect(self.cancel_route_recording)
        self.route_record_summary_button.clicked.connect(self._show_route_summary)
        self.route_record_open_folder_button.clicked.connect(
            self._open_route_recording_folder
        )
        compact_route_buttons = (
            self.route_record_trial_start_button,
            self.route_record_trial_finish_button,
            self.route_record_rest_point_button,
            self.route_record_platform_button,
            self.route_record_rope_button,
            self.route_record_down_jump_button,
            self.route_record_left_walk_off_button,
            self.route_record_right_return_button,
            self.route_record_start_button,
            self.route_record_finish_button,
            self.route_record_save_button,
            self.route_record_cancel_button,
            self.route_record_summary_button,
            self.route_record_open_folder_button,
            *self._route_v2_transition_buttons,
        )
        for button in compact_route_buttons:
            button.setMinimumHeight(24)
            button.setMaximumHeight(26)
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        route_record_buttons = QGridLayout()
        route_record_buttons.setHorizontalSpacing(3)
        route_record_buttons.setVerticalSpacing(2)
        route_record_buttons.addWidget(self.route_record_trial_start_button, 0, 0, 1, 2)
        route_record_buttons.addWidget(self.route_record_trial_finish_button, 0, 2, 1, 2)
        route_record_buttons.addWidget(self.route_record_rest_point_button, 0, 4, 1, 2)
        route_record_buttons.addWidget(self.route_record_platform_button, 1, 0)
        route_record_buttons.addWidget(self.route_record_rope_button, 1, 1)
        route_record_buttons.addWidget(self.route_record_down_jump_button, 1, 2)
        route_record_buttons.addWidget(self.route_record_left_walk_off_button, 1, 3)
        route_record_buttons.addWidget(self.route_record_right_return_button, 1, 4)
        route_record_buttons.addWidget(self.route_record_start_button, 2, 0)
        route_record_buttons.addWidget(self.route_record_finish_button, 2, 1)
        route_record_buttons.addWidget(self.route_record_save_button, 2, 2)
        route_record_buttons.addWidget(self.route_record_cancel_button, 2, 3)
        route_record_buttons.addWidget(self.route_record_summary_button, 2, 4)
        route_record_buttons.addWidget(self.route_record_open_folder_button, 2, 5)
        for column in range(6):
            route_record_buttons.setColumnStretch(column, 1)
        v2_transition_group = QGroupBox("V2过渡（F5开始 / F6结束）")
        v2_transition_layout = QGridLayout(v2_transition_group)
        v2_transition_layout.setContentsMargins(4, 4, 4, 4)
        v2_transition_layout.setHorizontalSpacing(3)
        v2_transition_layout.setVerticalSpacing(2)
        v2_transition_layout.addWidget(self.route_record_platform_jump_left_button, 0, 0)
        v2_transition_layout.addWidget(self.route_record_platform_jump_right_button, 0, 1)
        v2_transition_layout.addWidget(self.route_record_platform_jump_neutral_button, 0, 2)
        v2_transition_layout.addWidget(self.route_record_platform_to_rope_left_button, 1, 0)
        v2_transition_layout.addWidget(self.route_record_platform_to_rope_right_button, 1, 1)
        v2_transition_layout.addWidget(self.route_record_rope_to_platform_left_button, 1, 2)
        v2_transition_layout.addWidget(self.route_record_rope_to_platform_right_button, 1, 3)
        for column in range(4):
            v2_transition_layout.setColumnStretch(column, 1)
        route_record_layout.addWidget(route_record_description)
        route_record_layout.addLayout(route_file_actions)
        route_record_layout.addLayout(route_file_row)
        route_record_layout.addLayout(platform_select_row)
        route_record_layout.addLayout(platform_action_row)
        route_record_layout.addLayout(probability_row)
        route_record_layout.addWidget(v2_transition_group)
        route_record_layout.addWidget(self.route_record_status)
        route_record_layout.addLayout(route_record_buttons)
        route_record_layout.addLayout(rest_point_row)
        recording_layout.addWidget(route_record_group)

        option_group = QGroupBox("战斗与检测")
        option_layout = QGridLayout(option_group)
        option_layout.setContentsMargins(6, 6, 6, 6)
        option_layout.setHorizontalSpacing(5)
        option_layout.setVerticalSpacing(3)
        self.group_attack = QCheckBox("群攻模式")
        self.flash_enabled = QCheckBox("启用闪现")
        self.draw_boxes = QCheckBox("显示检测框")
        option_layout.addWidget(self.group_attack, 0, 0)
        option_layout.addWidget(self.flash_enabled, 0, 1)
        option_layout.addWidget(self.draw_boxes, 0, 2)

        self.auto_group_attack_over_count = FixedSpinBox()
        self.auto_group_attack_over_count.setRange(0, 99)
        self.auto_group_attack_over_count.setValue(2)
        self.auto_group_attack_over_count.setSuffix(" 只")
        self.auto_group_attack_over_count.setToolTip(
            "人物X左右150像素内识别到的怪物数量严格大于此值时，自动使用群攻键；"
            "怪物进入人物X正负50像素时仍按近身规则直接群攻；运行中修改会立即生效"
        )
        self.auto_group_attack_over_count.valueChanged.connect(
            self._on_auto_group_attack_threshold_changed
        )
        self.red_percent = FixedSpinBox()
        self.red_percent.setRange(0, 100)
        self.red_percent.setSuffix(" %")
        self.blue_percent = FixedSpinBox()
        self.blue_percent.setRange(0, 100)
        self.blue_percent.setSuffix(" %")
        self.rope_delay = FixedDoubleSpinBox()
        self.rope_delay.setRange(0, 30)
        self.rope_delay.setDecimals(1)
        self.rope_delay.setSingleStep(0.1)
        self.rope_delay.setSuffix(" 秒")
        for control in (
            self.auto_group_attack_over_count,
            self.red_percent,
            self.blue_percent,
            self.rope_delay,
        ):
            control.setMaximumWidth(85)
        option_layout.addWidget(QLabel("群攻 >"), 1, 0)
        option_layout.addWidget(self.auto_group_attack_over_count, 1, 1)
        option_layout.addWidget(QLabel("红量阈值"), 1, 2)
        option_layout.addWidget(self.red_percent, 1, 3)
        option_layout.addWidget(QLabel("蓝量阈值"), 2, 0)
        option_layout.addWidget(self.blue_percent, 2, 1)
        option_layout.addWidget(QLabel("绳子延迟"), 2, 2)
        option_layout.addWidget(self.rope_delay, 2, 3)
        option_layout.setColumnStretch(1, 1)
        option_layout.setColumnStretch(3, 1)
        layout.addWidget(option_group)

        layout.addStretch()
        recording_layout.addStretch()
        runtime_scroll.setWidget(content)
        self.settings_tabs.addTab(runtime_scroll, "运行设置")
        recording_tab_index = self.settings_tabs.addTab(recording_page, "路线录制")
        self.settings_tabs.setTabToolTip(
            recording_tab_index,
            "新建路线录制、加载路线JSON，以及录制平台、绳子和V2过渡动作",
        )
        panel_layout.addWidget(self.settings_tabs, 1)
        return panel

    def _build_monitor_panel(self) -> QFrame:
        """创建检测预览、日志和热键状态面板。"""
        frame = QFrame()
        frame.setObjectName("panel")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self.monitor_title = QLabel("检测预览与日志" if self.test_mode else "运行日志")
        self.monitor_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        clear_button = QPushButton("清空")
        clear_button.setFixedWidth(78)
        clear_button.clicked.connect(self._clear_log)
        top.addWidget(self.monitor_title)
        top.addStretch()
        top.addWidget(clear_button)
        layout.addLayout(top)

        self.monitor_tabs = QTabWidget()
        monitor_page = QWidget()
        monitor_layout = QVBoxLayout(monitor_page)
        monitor_layout.setContentsMargins(0, 8, 0, 0)
        monitor_layout.setSpacing(10)

        # 预览控件始终创建，页面切换TEST_MODE时直接显示或隐藏，无需重启窗口。
        self.preview_label = QLabel("点击“启动（截图）”后显示实时识别画面")
        self.preview_label.setObjectName("previewView")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(500, 330)
        self.preview_label.setStyleSheet(
            "background: #070c17; border: 1px solid #2b3a57; "
            "border-radius: 10px; color: #7f8ca4;"
        )
        monitor_layout.addWidget(self.preview_label, 4)

        self.preview_info = QLabel("尚未收到检测画面")
        self.preview_info.setWordWrap(True)
        self.preview_info.setStyleSheet(
            "color: #9fb3cf; background: #0d1527; border: 1px solid #263652; "
            "border-radius: 8px; padding: 8px;"
        )
        monitor_layout.addWidget(self.preview_info)

        self.route_runtime_status_label = QLabel(
            "角色平台：等待路线启动  ·  下一平台：--  ·  换台倒计时：--"
        )
        self.route_runtime_status_label.setWordWrap(True)
        self.route_runtime_status_label.setStyleSheet(
            "color: #d9e8ff; background: #132038; border: 1px solid #35517d; "
            "border-radius: 8px; padding: 8px 10px; font-weight: 700;"
        )
        monitor_layout.addWidget(self.route_runtime_status_label)

        self.resource_monitor_label = QLabel(
            "图形资源：正在读取 GDI / USER 对象、句柄和内存……"
        )
        self.resource_monitor_label.setWordWrap(True)
        self.resource_monitor_label.setToolTip(
            "虚拟机中若 GDI/USER 对象或进程句柄持续只增不减，可能存在截图、"
            "预览帧或图形对象积压。开始运行时会重置增量基线。"
        )
        self.resource_monitor_label.setStyleSheet(
            "color: #b9cbe5; background: #101a2d; border: 1px solid #304566; "
            "border-radius: 8px; padding: 8px 10px;"
        )
        monitor_layout.addWidget(self.resource_monitor_label)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(1800)
        placeholder = "启动后，检测、地图动作和异常信息会显示在这里。"
        self.log_view.setPlaceholderText(placeholder)
        monitor_layout.addWidget(self.log_view, 1)
        self.monitor_tabs.addTab(monitor_page, "运行监控")

        self.route_preview_page = QWidget()
        route_preview_layout = QVBoxLayout(self.route_preview_page)
        route_preview_layout.setContentsMargins(0, 8, 0, 0)
        route_preview_layout.setSpacing(10)
        self.route_preview_label = QLabel("尚未保存分段路线")
        self.route_preview_label.setAlignment(Qt.AlignCenter)
        self.route_preview_label.setMinimumSize(500, 320)
        self.route_preview_label.setStyleSheet(
            "background: #070c17; border: 1px solid #2b3a57; "
            "border-radius: 10px; color: #7f8ca4;"
        )
        route_preview_legend = QLabel(
            "颜色：<span style='color:#ff5555'>左=红色</span>　"
            "<span style='color:#5599ff'>右=蓝色</span>　"
            "<span style='color:#55dd77'>跳跃/绳子入口=绿色点</span>　"
            "<span style='color:#ffd166'>绳子=黄色</span>　"
            "<span style='color:#ff50ff'>绳顶离绳=紫色箭头</span>"
        )
        route_preview_legend.setAlignment(Qt.AlignCenter)
        route_preview_legend.setWordWrap(True)
        route_summary_scope_row = QHBoxLayout()
        route_summary_scope_row.addWidget(QLabel("汇总范围"))
        self.route_summary_platform_combo = FixedComboBox()
        self.route_summary_platform_combo.addItem("全部平台")
        self.route_summary_platform_combo.currentTextChanged.connect(
            self._on_route_summary_scope_changed
        )
        route_summary_scope_row.addWidget(self.route_summary_platform_combo, 1)
        self.route_summary_view = QTextEdit()
        self.route_summary_view.setReadOnly(True)
        self.route_summary_view.setMaximumHeight(190)
        self.route_summary_view.setPlaceholderText("保存路线后显示平台、绳子和回程概率汇总。")
        route_preview_layout.addWidget(self.route_preview_label, 1)
        route_preview_layout.addWidget(route_preview_legend)
        route_preview_layout.addLayout(route_summary_scope_row)
        route_preview_layout.addWidget(self.route_summary_view)
        self.monitor_tabs.addTab(self.route_preview_page, "最后录制路线")
        self.rest_snapshot_page = QWidget()
        rest_snapshot_layout = QVBoxLayout(self.rest_snapshot_page)
        rest_snapshot_layout.setContentsMargins(0, 8, 0, 0)
        rest_snapshot_layout.setSpacing(10)
        rest_countdown_row = QHBoxLayout()
        rest_countdown_row.setSpacing(8)
        self.rest_countdown_label = QLabel("下次休息：任务未运行")
        self.rest_countdown_label.setAlignment(Qt.AlignCenter)
        self.rest_countdown_label.setStyleSheet(
            "background: #132038; border: 1px solid #35517d; "
            "border-radius: 8px; color: #8fc5ff; font-size: 16px; "
            "font-weight: 700; padding: 9px 12px;"
        )
        self.rest_point_test_button = QPushButton("立即测试休息点")
        self.rest_point_test_button.setEnabled(False)
        self.rest_point_test_button.setToolTip(
            "无需先点启动；空闲时会自动启动当前自定义路线，定位完成后立即返回休息点并验证人物能否停在绳子上。"
        )
        self.rest_point_test_button.clicked.connect(self._test_rest_point)
        rest_countdown_row.addWidget(self.rest_countdown_label, 1)
        rest_countdown_row.addWidget(self.rest_point_test_button)
        self.rest_snapshot_label = QLabel("尚未到达录制休息点")
        self.rest_snapshot_label.setAlignment(Qt.AlignCenter)
        self.rest_snapshot_label.setMinimumSize(500, 320)
        self.rest_snapshot_label.setStyleSheet(
            "background: #070c17; border: 1px solid #2b3a57; "
            "border-radius: 10px; color: #7f8ca4;"
        )
        self.rest_snapshot_info = QLabel(
            "到达录制休息点后，会在这里保留当时的游戏截图、时间和本次运行的次数。"
        )
        self.rest_snapshot_info.setWordWrap(True)
        self.rest_snapshot_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rest_snapshot_layout.addLayout(rest_countdown_row)
        rest_snapshot_layout.addWidget(self.rest_snapshot_label, 1)
        rest_snapshot_layout.addWidget(self.rest_snapshot_info)
        self.monitor_tabs.addTab(self.rest_snapshot_page, "休息记录")
        self.monitor_tabs.currentChanged.connect(self._on_monitor_tab_changed)
        layout.addWidget(self.monitor_tabs, 1)

        footer = QFrame()
        footer.setObjectName("panel")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 9, 12, 9)
        hotkey_title = QLabel("全局热键")
        hotkey_title.setStyleSheet("font-weight: 700; color: #c9d5e8;")
        self.hotkey_label = QLabel("正在注册……")
        self.hotkey_label.setObjectName("hotkeyText")
        footer_layout.addWidget(hotkey_title)
        footer_layout.addSpacing(8)
        footer_layout.addWidget(self.hotkey_label)
        footer_layout.addStretch()
        layout.addWidget(footer)
        return frame

    def _build_action_frame(self) -> QFrame:
        """创建始终位于设置区顶部的启动、停止和配置操作按钮。"""
        action_frame = QFrame()
        action_frame.setObjectName("panel")
        action_layout = QGridLayout(action_frame)
        action_layout.setContentsMargins(6, 6, 6, 6)
        action_layout.setHorizontalSpacing(4)
        action_layout.setVerticalSpacing(2)
        self.start_button = QPushButton(
            "▶ 启动（截图）" if self.test_mode else "▶ 启动"
        )
        self.start_button.setObjectName("primaryButton")
        self.pause_button = QPushButton("Ⅱ 暂停")
        self.pause_button.setObjectName("pauseButton")
        self.pause_button.setEnabled(False)
        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        save_button = QPushButton("保存配置")
        reset_button = QPushButton("恢复默认")
        self.start_button.clicked.connect(self.request_start)
        self.pause_button.clicked.connect(self.request_toggle_pause)
        self.stop_button.clicked.connect(self.request_stop)
        save_button.clicked.connect(self.save_config)
        reset_button.clicked.connect(self.reset_config)
        for button in (
            self.start_button,
            self.pause_button,
            self.stop_button,
            save_button,
            reset_button,
        ):
            button.setMinimumHeight(26)
            button.setMaximumHeight(30)
        action_layout.addWidget(self.start_button, 0, 0)
        action_layout.addWidget(self.pause_button, 0, 1)
        action_layout.addWidget(self.stop_button, 0, 2)
        action_layout.addWidget(save_button, 0, 3)
        action_layout.addWidget(reset_button, 0, 4)
        for column in range(5):
            action_layout.setColumnStretch(column, 1)
        return action_frame

    @staticmethod
    def _key_input(default_key: str) -> FixedComboBox:
        """创建不可输入、不可滚轮切换的固定按键选择框。"""
        widget = FixedComboBox()
        widget.setEditable(False)
        widget.addItems(FIXED_KEY_OPTIONS)
        widget.setCurrentText(default_key)
        return widget

    @staticmethod
    def _set_fixed_key_value(widget: FixedComboBox, key: str) -> None:
        """回填历史配置；旧值不在固定列表时追加为只读选项。"""
        normalized = str(key).strip().lower()
        if not normalized:
            return
        index = widget.findText(normalized)
        if index < 0:
            widget.addItem(normalized)
            index = widget.findText(normalized)
        widget.setCurrentIndex(index)

    def _add_scheduled_key_row(self, key: str = "q", interval: float = 60.0) -> None:
        """新增一行定时按键选择和秒数设置，最多允许十二行。"""
        if len(self.scheduled_key_rows) >= 12:
            QMessageBox.information(self, "数量限制", "定时按键最多只能设置12个。")
            return
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(3)
        key_input = self._key_input(key)
        interval_input = FixedDoubleSpinBox()
        interval_input.setRange(1.0, 86400.0)
        interval_input.setDecimals(1)
        interval_input.setSingleStep(1.0)
        interval_input.setSuffix(" 秒")
        interval_input.setValue(float(interval))
        interval_input.setMaximumWidth(110)
        delete_button = QPushButton("删除")
        delete_button.setMinimumHeight(24)
        delete_button.setMaximumHeight(26)
        delete_button.clicked.connect(
            lambda _checked=False, widget=row_widget: self._remove_scheduled_key_row(widget)
        )
        row_layout.addWidget(QLabel("按键"))
        row_layout.addWidget(key_input, 1)
        row_layout.addWidget(QLabel("每隔"))
        row_layout.addWidget(interval_input, 2)
        row_layout.addWidget(delete_button)
        self.scheduled_key_rows_layout.addWidget(row_widget)
        self.scheduled_key_rows.append(
            {
                "widget": row_widget,
                "key": key_input,
                "interval": interval_input,
            }
        )
        self.add_scheduled_key_button.setEnabled(len(self.scheduled_key_rows) < 12)

    def _remove_scheduled_key_row(self, row_widget: QWidget) -> None:
        """删除指定定时按键行，并恢复新增按钮的可用状态。"""
        for index, row in enumerate(self.scheduled_key_rows):
            if row["widget"] is not row_widget:
                continue
            self.scheduled_key_rows.pop(index)
            self.scheduled_key_rows_layout.removeWidget(row_widget)
            row_widget.deleteLater()
            break
        self.add_scheduled_key_button.setEnabled(len(self.scheduled_key_rows) < 12)

    def _clear_scheduled_key_rows(self) -> None:
        """清空当前界面中的全部定时按键行。"""
        for row in tuple(self.scheduled_key_rows):
            self.scheduled_key_rows_layout.removeWidget(row["widget"])
            row["widget"].deleteLater()
        self.scheduled_key_rows.clear()
        self.add_scheduled_key_button.setEnabled(True)

    def _collect_scheduled_keys(self):
        """按界面显示顺序收集定时按键和执行间隔。"""
        return tuple(
            (
                row["key"].currentText().strip().lower(),
                float(row["interval"].value()),
            )
            for row in self.scheduled_key_rows
        )

    def _open_custom_template_folder(self) -> None:
        """确保自定义模板目录存在并用系统文件管理器打开。"""
        try:
            directory = ensure_custom_template_directory(self.store.path.parent)
        except OSError as exc:
            QMessageBox.warning(self, "创建失败", "无法创建自定义模板目录：{}".format(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))

    def _open_monster_atlas(self) -> None:
        """打开按素材目录分组的怪物图鉴多选窗口。"""
        dialog = MonsterAtlasDialog(
            self.store.path.parent,
            is_automation_running=lambda: bool(self.service.is_running),
            parent=self,
        )
        dialog.templates_generated.connect(self._on_monster_atlas_templates_generated)
        dialog.exec_()

    def _on_monster_atlas_templates_generated(self, report) -> None:
        """模板替换完成后启用对应识别入口并刷新界面提示。"""
        if not self.custom_mode.isChecked():
            self.use_custom_templates.setChecked(True)
        self.custom_template_path.setText(
            "固定目录：img\\自定义（图鉴 {} 张）".format(report.template_count)
        )
        self.custom_template_path.setToolTip(str(report.output_directory))
        self._update_custom_mode_description()

    def _open_route_recording_folder(self) -> None:
        """确保自定义路线录制目录存在并用系统文件管理器打开。"""
        directory = self.route_recorder.route_path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "创建失败", "无法创建录制目录：{}".format(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))

    def _record_route_rest_point(self) -> None:
        """把人物当前小地图位置写入已存在路线JSON，作为定时休息落点。"""
        if self.service.is_running:
            QMessageBox.warning(self, "请先停止", "请先停止当前刷图任务，再录制休息点。")
            return
        if self.route_recorder.is_running or self._pending_route_segment_type is not None:
            QMessageBox.warning(
                self,
                "录制进行中",
                "请先保存或取消当前路线分段录制，再添加休息点。",
            )
            return
        route_path = Path(self.route_recorder.route_path)
        if not route_path.is_file():
            QMessageBox.warning(
                self,
                "路线尚未保存",
                "休息点只能加入已有JSON；请先保存整条路线或加载一个已有JSON。",
            )
            return
        try:
            position = self.service.capture_yellow_position()
            if position is None:
                raise ValueError("没有识别到小地图黄色人物点")
            rest_point = save_route_rest_point(
                route_path,
                position,
                interval_minutes=self.rope_rest_interval.value(),
                duration_minutes=self.rope_rest_duration.value(),
            )
            self.route_recorder.set_rest_point(rest_point)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "休息点录制失败", str(exc))
            return
        self.route_rest_point_label.setText(
            "休息点：X={}，Y={}；平台Y={}；每{}分钟休息{}分钟".format(
                rest_point["x"],
                rest_point["y"],
                rest_point.get("approach_y"),
                rest_point.get("interval_minutes"),
                rest_point.get("duration_minutes"),
            )
        )
        self.route_record_status.setText(
            "已写入休息点：X={}，Y={}，起跳平台Y={}；每{}分钟前往一次，"
            "每次休息{}分钟。运行到周期后会先到该平台，再对齐X并跳跃一次。".format(
                rest_point["x"],
                rest_point["y"],
                rest_point.get("approach_y"),
                rest_point.get("interval_minutes"),
                rest_point.get("duration_minutes"),
            )
        )
        self._load_last_route_preview()
        QMessageBox.information(
            self,
            "休息点已保存",
            "已把当前人物位置保存到{}。\n\n"
            "运行时会先走到相同X坐标，跳跃一次，到达该Y附近后开始计算休息时长。".format(
                route_path.name
            ),
        )

    def _can_switch_route_file(self) -> bool:
        """检查当前是否可以安全新建或加载另一份路线文件。"""
        if self.service.is_running:
            QMessageBox.warning(self, "任务运行中", "请先停止当前刷图任务，再切换路线文件。")
            return False
        if self.route_recorder.is_running or self._pending_route_segment_type is not None:
            QMessageBox.warning(
                self,
                "录制尚未结束",
                "请先保存或取消当前录制，再切换路线文件。",
            )
            return False
        return True

    @staticmethod
    def _normalize_route_file_name(file_name: str) -> str:
        """清理用户输入并返回安全的JSON文件名。"""
        name = str(file_name).strip()
        if not name:
            raise ValueError("路线文件名不能为空")
        if any(character in name for character in '<>:"/\\|?*'):
            raise ValueError("路线文件名不能包含 <>:\"/\\|?* 等字符")
        if not name.lower().endswith(".json"):
            name += ".json"
        stem = Path(name).stem
        if stem.strip(" .") == "" or stem.endswith((" ", ".")):
            raise ValueError("路线文件名无效")
        reserved_names = {"CON", "PRN", "AUX", "NUL"}
        reserved_names.update("COM{}".format(index) for index in range(1, 10))
        reserved_names.update("LPT{}".format(index) for index in range(1, 10))
        if stem.upper() in reserved_names:
            raise ValueError("该名称是Windows保留文件名，请更换路线名称")
        return name

    def _next_route_file_name(self) -> str:
        """返回录制目录中尚未使用的路线名称建议。"""
        directory = mushroom_v3_recordings_directory(self.store.path.parent)
        for index in range(1, 10000):
            candidate = "自定义路线{}.json".format(index)
            if not (directory / candidate).exists():
                return candidate
        return "自定义路线_新建.json"

    def _set_active_route_path(self, route_path: Path, load_existing: bool) -> None:
        """切换当前路线文件并刷新文件名、预览和地图选择。"""
        path = Path(route_path).resolve()
        self.route_recorder.set_route_path(path)
        self.route_file_label.setText(path.name)
        self.route_file_label.setToolTip(str(path))
        self.route_map_file_label.setText(path.name)
        self.route_map_file_label.setToolTip(str(path))
        self.map_combo.setCurrentText("自定义录制路线")
        if load_existing:
            self._load_last_route_preview()
            rest_point = read_route_rest_point(path)
            self.route_rest_point_label.setText(
                "休息点：X={}，Y={}；平台Y={}；每{}分钟休息{}分钟".format(
                    rest_point["x"],
                    rest_point["y"],
                    rest_point.get("approach_y"),
                    rest_point.get("interval_minutes"),
                    rest_point.get("duration_minutes"),
                )
                if rest_point is not None
                else "休息点：未录制"
            )
        else:
            self._last_route_data = None
            self._last_route_preview_path = mushroom_v3_preview_path(path)
            self.route_preview_label.setPixmap(QPixmap())
            self.route_preview_label.setText("新路线尚未保存：{}".format(path.name))
            self.route_summary_view.setPlainText(
                "文件将在完成录制并按F7保存时创建。"
            )
            self._refresh_route_summary_platforms([])
            self.route_rest_point_label.setText("休息点：未录制")

    def _new_route_file(self) -> None:
        """输入新路线文件名，但直到F7保存时才实际创建文件。"""
        if not self._can_switch_route_file():
            return
        suggested_name = self._next_route_file_name()
        file_name, accepted = QInputDialog.getText(
            self,
            "新建自定义录制路线",
            "请输入路线文件名：",
            text=suggested_name,
        )
        if not accepted:
            return
        try:
            normalized_name = self._normalize_route_file_name(file_name)
        except ValueError as exc:
            QMessageBox.warning(self, "文件名无效", str(exc))
            return
        directory = mushroom_v3_recordings_directory(self.store.path.parent)
        route_path = directory / normalized_name
        if route_path.exists():
            QMessageBox.warning(
                self,
                "文件已存在",
                "{}已经存在，请使用“加载JSON”或输入其他文件名。".format(
                    normalized_name
                ),
            )
            return
        self._set_active_route_path(route_path, load_existing=False)
        self._route_platform_count = 1
        self.route_platform_combo.clear()
        self.route_platform_combo.addItem("平台1")
        self.route_delete_platform_button.setEnabled(False)
        self.route_record_status.setText(
            "已新建路线“{}”，尚未写入磁盘；请选择分段并按F5开始。".format(
                normalized_name
            )
        )

    def _load_route_json(self) -> None:
        """选择并校验已有JSON，然后切换到独立自定义录制路线模式。"""
        if not self._can_switch_route_file():
            return
        directory = mushroom_v3_recordings_directory(self.store.path.parent)
        directory.mkdir(parents=True, exist_ok=True)
        selected_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择自定义录制路线JSON",
            str(directory.resolve()),
            "JSON路线 (*.json)",
        )
        if not selected_path:
            return
        path = Path(selected_path)
        route_closed = False
        route_shape_message = ""
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            variants = data.get("route_variants") if isinstance(data, dict) else None
            legacy_points = data.get("points") if isinstance(data, dict) else None
            has_variant_points = isinstance(variants, list) and any(
                isinstance(variant, dict)
                and isinstance(variant.get("points"), list)
                and len(variant["points"]) >= 2
                for variant in variants
            )
            if not has_variant_points and not (
                isinstance(legacy_points, list) and len(legacy_points) >= 2
            ):
                raise ValueError("JSON中没有可运行的points或route_variants")
            selected_points = legacy_points
            selected_variant = None
            if has_variant_points:
                selected_variant = next(
                    variant
                    for variant in variants
                    if isinstance(variant, dict)
                    and isinstance(variant.get("points"), list)
                    and len(variant["points"]) >= 2
                )
                selected_points = selected_variant["points"]
            graph = data.get("route_graph") if isinstance(data, dict) else None
            transitions = graph.get("return_transitions") if isinstance(graph, dict) else None
            has_return_transition = bool(
                (isinstance(transitions, list) and transitions)
                or (
                    isinstance(selected_variant, dict)
                    and selected_variant.get("transition_segment_id") is not None
                )
            )
            first_point = selected_points[0]
            last_point = selected_points[-1]
            endpoint_distance = abs(int(first_point["x"]) - int(last_point["x"])) + abs(
                int(first_point["y"]) - int(last_point["y"])
            )
            route_closed = has_return_transition or endpoint_distance <= 18
            segments = data.get("segments") if isinstance(data, dict) else None
            rope_count = sum(
                1
                for segment in (segments or [])
                if isinstance(segment, dict) and segment.get("type") == SEGMENT_ROPE
            )
            platform_count = sum(
                1
                for segment in (segments or [])
                if isinstance(segment, dict) and segment.get("type") == SEGMENT_PLATFORM
            )
            platform_patrol = (
                not route_closed
                and platform_count == 1
                and rope_count == 0
                and not transitions
                and all(
                    str(point.get("segment_type", "")) == SEGMENT_PLATFORM
                    for point in selected_points
                    if isinstance(point, dict)
                )
            )
            if platform_patrol:
                xs = [int(point["x"]) for point in selected_points]
                route_shape_message = (
                    "已识别为单平台往返路线，运行时会在X={}~{}之间左右循环，"
                    "录制方向不影响巡逻。"
                ).format(min(xs), max(xs))
            elif route_closed:
                route_shape_message = "已识别为闭环路线。"
            else:
                route_shape_message = (
                    "该JSON未形成闭环（绳子段{}个、回程段{}个），"
                    "会严格走到最后录制点后停止，不会继续走出地图。"
                ).format(rope_count, len(transitions or []))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "加载失败", "路线JSON无效：{}".format(exc))
            return
        self._set_active_route_path(path, load_existing=True)
        self.route_record_status.setText(
            "已加载路线：{}；地图已切换为“自定义录制路线”。{}".format(
                path.name,
                route_shape_message,
            )
        )

    def _on_custom_mode_toggled(self, enabled: bool) -> None:
        """开启自定义路线时关闭“仅替换识别”，避免两种页面语义同时生效。"""
        if enabled and self.use_custom_templates.isChecked():
            self.use_custom_templates.blockSignals(True)
            self.use_custom_templates.setChecked(False)
            self.use_custom_templates.blockSignals(False)
        self._update_custom_mode_description()

    def _on_map_selection_changed(self, map_name: str) -> None:
        """选择JSON录制路线时禁用旧版左右边界模式，避免运行入口互相覆盖。"""
        recorded_route_selected = str(map_name) == "自定义录制路线"
        if recorded_route_selected and self.custom_mode.isChecked():
            self.custom_mode.blockSignals(True)
            self.custom_mode.setChecked(False)
            self.custom_mode.blockSignals(False)
        self.custom_mode.setEnabled(not recorded_route_selected)
        if recorded_route_selected:
            self.custom_mode.setToolTip(
                "当前使用独立JSON录制路线；旧版左右边界模式已禁用。"
            )
        else:
            self.custom_mode.setToolTip(
                "使用记录的左右边界路线，并自动使用img\\自定义怪物模板。"
            )
        self._update_custom_mode_description()
        self._refresh_rest_point_test_button()

    def _on_custom_template_override_toggled(self, enabled: bool) -> None:
        """开启仅替换识别时关闭自定义路线，确保继续使用下拉地图原路线。"""
        if enabled and self.custom_mode.isChecked():
            self.custom_mode.blockSignals(True)
            self.custom_mode.setChecked(False)
            self.custom_mode.blockSignals(False)
        self._update_custom_mode_description()

    def _update_custom_mode_description(self) -> None:
        """根据当前选择说明实际使用的路线和怪物检测来源。"""
        if self.map_combo.currentText() == "自定义录制路线":
            text = (
                "路线：使用上方所选JSON巡逻拾取；怪物：公共智能追击和攻击优先；"
                "清怪后短暂复查再继续路线；绳子：按JSON几何信息动态判断。"
            )
        elif self.custom_mode.isChecked():
            text = "路线：记录的左右边界往返；怪物识别：img\\自定义模板。"
        elif self.use_custom_templates.isChecked():
            text = "路线：下拉框所选地图原路线；怪物识别：img\\自定义模板。"
        else:
            text = "路线和怪物识别均使用下拉框所选地图的默认配置。"
        self.custom_description.setText(text)

    def _record_custom_position(self, side: str) -> None:
        """从小地图记录自定义路线的一侧边界坐标。"""
        if self.service.is_running:
            QMessageBox.warning(self, "请先停止", "请停止当前任务后再记录自定义边界坐标。")
            return
        capture = getattr(self.service, "capture_yellow_position", None)
        if capture is None:
            QMessageBox.warning(self, "记录失败", "当前服务不支持黄色坐标采集。")
            return
        try:
            position = capture()
        except Exception as exc:
            QMessageBox.warning(self, "记录失败", "读取小地图黄色坐标失败：{}".format(exc))
            return
        if position is None:
            QMessageBox.warning(self, "未识别", "没有识别到小地图黄色人物点，请确认游戏画面可见。")
            return
        position = (int(position[0]), int(position[1]))
        if side == "left":
            self._custom_left_position = position
            message = "已记录左边坐标：{}".format(position)
        else:
            self._custom_right_position = position
            message = "已记录右边坐标：{}".format(position)
        self._update_custom_position_labels()
        self.set_status("idle", message)
        QTimer.singleShot(1800, self._restore_idle_status)

    def _update_custom_position_labels(self) -> None:
        """刷新自定义左右边界的显示文本和完整坐标提示。"""
        left_text = "未记录" if self._custom_left_position is None else self._custom_left_position[0]
        right_text = "未记录" if self._custom_right_position is None else self._custom_right_position[0]
        self.custom_left_label.setText("左X：{}".format(left_text))
        self.custom_right_label.setText("右X：{}".format(right_text))
        self.custom_left_label.setToolTip("完整黄色坐标：{}".format(self._custom_left_position))
        self.custom_right_label.setToolTip("完整黄色坐标：{}".format(self._custom_right_position))

    def _connect_service(self) -> None:
        """把后台服务信号连接到界面状态和预览槽。"""
        self.service.status_changed.connect(self.set_status)
        self.service.started.connect(self._dispatch_pending_rest_point_test)
        self.service.failed.connect(self._show_error)
        if hasattr(self.service, "frame_ready"):
            self.service.frame_ready.connect(self.update_test_preview)
        if hasattr(self.service, "rest_snapshot_ready"):
            self.service.rest_snapshot_ready.connect(self.update_rest_snapshot)
        if hasattr(self.service, "rest_schedule_changed"):
            self.service.rest_schedule_changed.connect(self.update_rest_schedule)
        if hasattr(self.service, "route_monitor_changed"):
            self.service.route_monitor_changed.connect(self.update_route_monitor)

    def update_route_monitor(self, details) -> None:
        """Display current platform, focus/recovery state and platform countdown."""
        payload = dict(details or {})
        self._route_monitor_payload.update(payload)
        data = self._route_monitor_payload
        focus_status = str(data.get("focus_status", "focused"))
        status = str(data.get("status", "brushing"))
        platform = data.get("platform_number")
        next_platform = data.get("next_platform_number")
        remaining = data.get("switch_remaining_seconds")
        platform_text = "平台{}".format(platform) if platform is not None else "识别中"
        next_text = "平台{}".format(next_platform) if next_platform is not None else "--"
        if remaining is None:
            countdown_text = "切换中" if status in {"rotating", "transitioning"} else "--"
        else:
            total = max(0, int(float(remaining) + 0.999))
            minutes, seconds = divmod(total, 60)
            countdown_text = "{:02d}:{:02d}".format(minutes, seconds)
        message = str(data.get("message", "")).strip()
        if focus_status == "lost":
            text = "已切出游戏：等待窗口恢复  ·  路线与卡死计时已暂停"
        elif status == "relocalizing":
            text = message or "正在重新定位角色与所在平台"
        elif status == "position_missing":
            text = message or "角色坐标暂时丢失，等待重新识别"
        elif status == "replanning":
            text = "角色在{}  ·  正在重新规划到{}".format(platform_text, next_text)
        elif status in {"rotating", "transitioning", "rope"}:
            text = "角色在{}  ·  正在前往{}  ·  换台倒计时：切换中".format(
                platform_text, next_text
            )
        else:
            text = "角色在{}  ·  下一平台：{}  ·  换台倒计时：{}".format(
                platform_text, next_text, countdown_text
            )
        recovery_stage = int(data.get("recovery_stage", 0) or 0)
        if recovery_stage > 0:
            text += "  ·  卡住兜底：第{}级".format(recovery_stage)
        self.route_runtime_status_label.setText(text)

    def _connect_route_recorder(self) -> None:
        """把路线录制器的状态、进度、保存和错误信号连接到页面。"""
        self.route_recorder.state_changed.connect(self._on_route_record_state)
        self.route_recorder.progress_changed.connect(self._on_route_record_progress)
        self.route_recorder.route_saved.connect(self._on_route_saved)
        self.route_recorder.failed.connect(self._on_route_record_failed)

    def _add_route_platform(self) -> None:
        """按平台1、平台2、平台3的顺序新增可录制平台。"""
        self._route_platform_count += 1
        platform_name = "平台{}".format(self._route_platform_count)
        self.route_platform_combo.addItem(platform_name)
        self.route_platform_combo.setCurrentText(platform_name)
        self.route_delete_platform_button.setEnabled(True)

    def _delete_route_platform(self) -> None:
        """删除当前平台；录制会话中仅允许从最后一个平台向前删除。"""
        count = self.route_platform_combo.count()
        if count <= 1:
            QMessageBox.information(self, "无法删除", "至少需要保留平台1。")
            return
        current_index = self.route_platform_combo.currentIndex()
        if self.route_recorder.is_running and current_index != count - 1:
            QMessageBox.warning(
                self,
                "请删除最后平台",
                "录制会话已经开始。为避免平台编号错位，请先选择最后一个平台再删除。",
            )
            return
        platform_name = self.route_platform_combo.currentText()
        if self.route_recorder.is_running:
            self.route_recorder.discard_platform(platform_name)
            self.route_platform_combo.removeItem(current_index)
        else:
            remaining_count = count - 1
            selected_index = min(current_index, remaining_count - 1)
            self.route_platform_combo.blockSignals(True)
            self.route_platform_combo.clear()
            self.route_platform_combo.addItems(
                ["平台{}".format(index) for index in range(1, remaining_count + 1)]
            )
            self.route_platform_combo.setCurrentIndex(selected_index)
            self.route_platform_combo.blockSignals(False)
        self._route_platform_count = self.route_platform_combo.count()
        self.route_delete_platform_button.setEnabled(self._route_platform_count > 1)
        self._on_route_platform_selected(self.route_platform_combo.currentText())

    def _on_route_platform_selected(self, platform_name: str) -> None:
        """提示当前平台编号及其在预览图中的P编号。"""
        if self.route_recorder.is_running and not self.route_recorder.is_paused:
            return
        if self._pending_route_segment_type is not None:
            self._show_pending_route_segment()
            return
        platform_number = "".join(character for character in platform_name if character.isdigit())
        self.route_record_status.setText(
            "当前准备录制{}；保存后的地图标注为P{}。".format(
                platform_name,
                platform_number or "?",
            )
        )

    def _on_down_jump_probability_changed(self, value: int) -> None:
        """修改下跳分支权重后同步到录制器。"""
        if self._updating_route_probability:
            return
        self._sync_route_transition_probabilities()

    def _on_left_walk_off_probability_changed(self, value: int) -> None:
        """修改向左走出平台的分支权重。"""
        if self._updating_route_probability:
            return
        self._sync_route_transition_probabilities()

    def _on_right_return_probability_changed(self, value: int) -> None:
        """修改向右走出平台的分支权重。"""
        if self._updating_route_probability:
            return
        self._sync_route_transition_probabilities()

    def _sync_route_transition_probabilities(self) -> None:
        """把页面概率同步到当前录制会话。"""
        self.route_recorder.set_transition_probabilities(
            down_jump=self.route_down_jump_probability.value(),
            left_walk_off=self.route_left_walk_off_probability.value(),
            right_walk_off=self.route_right_return_probability.value(),
        )

    def request_route_segment(self, segment_type: str) -> None:
        """选择待录制的分段类型，等待F5在人物站好后开始采集。"""
        if self.service.is_running:
            QMessageBox.warning(self, "请先停止", "请先停止当前刷图任务，再开始录制路线。")
            return
        self._pending_route_segment_type = segment_type
        self.route_new_file_button.setEnabled(False)
        self.route_load_file_button.setEnabled(False)
        self.route_map_load_button.setEnabled(False)
        self.route_record_rest_point_button.setEnabled(False)
        self.route_record_trial_start_button.setEnabled(False)
        self._show_pending_route_segment()
        self.route_record_start_button.setEnabled(True)
        self.route_record_cancel_button.setEnabled(True)
        self.start_button.setEnabled(False)

    def start_route_trial_walk_v2(self) -> None:
        """开始V2逐帧试走，完整保留坐标、按键、停顿和回头路。"""
        if self.service.is_running:
            QMessageBox.warning(self, "请先停止", "请先停止当前刷图任务，再开始V2试走。")
            return
        if self.route_recorder.is_running:
            QMessageBox.warning(self, "已有录制", "V2试走必须在任何结构分段之前开始。")
            return
        self._pending_route_segment_type = None
        try:
            self.route_recorder.start_trial_walk_v2()
        except Exception as exc:
            QMessageBox.warning(self, "V2试走启动失败", str(exc))

    def finish_route_trial_walk_v2(self) -> None:
        """结束V2试走并进入结构标注阶段。"""
        self.route_recorder.finish_trial_walk_v2()

    def _show_pending_route_segment(self) -> None:
        """显示当前已选择但尚未按F5开始的路线分段。"""
        segment_type = self._pending_route_segment_type
        labels = {
            SEGMENT_PLATFORM: "{}平台段".format(self.route_platform_combo.currentText()),
            SEGMENT_ROPE: "绳子段",
            SEGMENT_DOWN_JUMP: "{}下跳回程".format(self.route_platform_combo.currentText()),
            SEGMENT_RIGHT_RETURN: "{}旧版向右起点".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_WALK_OFF_LEFT: "{}向左走出平台".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_WALK_OFF_RIGHT: "{}向右走出平台".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_PLATFORM_JUMP_LEFT: "{}向左跳到平台".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_PLATFORM_JUMP_RIGHT: "{}向右跳到平台".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_PLATFORM_JUMP_NEUTRAL: "{}原地跳到平台".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_PLATFORM_TO_ROPE_LEFT: "{}向左跳上绳".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_PLATFORM_TO_ROPE_RIGHT: "{}向右跳上绳".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_ROPE_TO_PLATFORM_LEFT: "绳子向左跳到{}".format(
                self.route_platform_combo.currentText()
            ),
            SEGMENT_ROPE_TO_PLATFORM_RIGHT: "绳子向右跳到{}".format(
                self.route_platform_combo.currentText()
            ),
        }
        label = labels.get(segment_type, "路线段")
        if segment_type in (SEGMENT_WALK_OFF_LEFT, SEGMENT_WALK_OFF_RIGHT):
            message = (
                "已选择{}。请把人物站在离开点前，按F5后实际走出平台；"
                "落到已录制的下层平台后会自动结束。".format(label)
            )
        else:
            message = (
                "已选择{}。请把人物站到正确起点，按F5开始录制，"
                "按F6结束当前段。".format(label)
            )
        self.route_record_status.setText(message)

    def start_pending_route_segment(self) -> None:
        """响应F5并开始采集页面已选择的平台、绳子或回程分段。"""
        if self.service.is_running:
            return
        segment_type = self._pending_route_segment_type
        if segment_type is None:
            return
        if self.route_recorder.is_running and not self.route_recorder.is_paused:
            return
        self._sync_route_transition_probabilities()
        platform_id = (
            None
            if segment_type == SEGMENT_ROPE
            else self.route_platform_combo.currentText()
        )
        if self.route_recorder.start_segment(segment_type, platform_id=platform_id):
            self._pending_route_segment_type = None
            self.route_record_start_button.setEnabled(False)
            self.start_button.setEnabled(False)

    def finish_route_segment(self) -> None:
        """结束当前分段并等待用户选择下一段类型。"""
        if not self.route_recorder.is_running or self.route_recorder.is_paused:
            return
        if self.route_recorder.active_segment_type == SEGMENT_TRIAL_WALK_V2:
            self.route_recorder.finish_trial_walk_v2()
            return
        self.route_recorder.finish_segment()

    def save_route_recording(self) -> None:
        """结束分段会话并保存路线图、连接分析和预览图。"""
        self._pending_route_segment_type = None
        self.route_record_start_button.setEnabled(False)
        self._sync_route_transition_probabilities()
        self.route_recorder.stop(save=True)

    def cancel_route_recording(self) -> None:
        """结束本次录制但保留旧路线文件，不保存当前不完整轨迹。"""
        self._pending_route_segment_type = None
        self.route_record_start_button.setEnabled(False)
        if self.route_recorder.is_running:
            self.route_recorder.stop(save=False)
        elif not self.service.is_running:
            self.route_record_status.setText("已取消待录制分段。")
            self.route_record_cancel_button.setEnabled(False)
            self.route_new_file_button.setEnabled(True)
            self.route_load_file_button.setEnabled(True)
            self.route_map_load_button.setEnabled(True)
            self.route_record_rest_point_button.setEnabled(True)
            self.route_record_trial_start_button.setEnabled(True)
            self.start_button.setEnabled(True)

    def _on_route_record_state(self, state: str, message: str) -> None:
        """根据录制会话和当前分段状态更新页面按钮。"""
        self.route_record_status.setText(message)
        session_active = state in {
            "recording",
            "segment_ready",
            "trial_recording",
            "trial_ready",
        }
        recording_segment = state == "recording"
        recording_trial = state == "trial_recording"
        can_start_segment = (
            not self.service.is_running
            and not recording_segment
            and not recording_trial
        )
        can_switch_route = not self.service.is_running and not session_active
        self.route_new_file_button.setEnabled(can_switch_route)
        self.route_load_file_button.setEnabled(can_switch_route)
        self.route_map_load_button.setEnabled(can_switch_route)
        self.route_record_rest_point_button.setEnabled(can_switch_route)
        self.route_record_platform_button.setEnabled(can_start_segment)
        self.route_record_rope_button.setEnabled(can_start_segment)
        self.route_record_down_jump_button.setEnabled(can_start_segment)
        self.route_record_left_walk_off_button.setEnabled(can_start_segment)
        self.route_record_right_return_button.setEnabled(can_start_segment)
        for button in self._route_v2_transition_buttons:
            button.setEnabled(can_start_segment)
        self.route_record_trial_start_button.setEnabled(can_switch_route)
        self.route_record_trial_finish_button.setEnabled(recording_trial)
        self.route_platform_combo.setEnabled(can_start_segment)
        self.route_add_platform_button.setEnabled(can_start_segment)
        self.route_delete_platform_button.setEnabled(
            can_start_segment and self.route_platform_combo.count() > 1
        )
        self.route_record_start_button.setEnabled(
            can_start_segment and self._pending_route_segment_type is not None
        )
        self.route_record_finish_button.setEnabled(recording_segment)
        self.route_record_save_button.setEnabled(
            state in {"recording", "segment_ready", "trial_ready"}
        )
        self.route_record_cancel_button.setEnabled(session_active)
        if not self.service.is_running:
            self.start_button.setEnabled(not session_active)

    def _on_route_record_progress(self, info: dict) -> None:
        """显示当前分段、分段点数、人物坐标和采样帧率。"""
        if info.get("recording_mode") == SEGMENT_TRIAL_WALK_V2:
            self.route_record_status.setText(
                "V2试走录制中：{trial_frames}帧，{seconds:.1f}秒 · "
                "位置={position} · 按键命令={command} · {fps}Hz\n"
                "请完整走一圈并回到平台1；回头路和重复节点会保留。".format(
                    seconds=int(info.get("trial_duration_ms", 0)) / 1000.0,
                    **info,
                )
            )
            return
        segment_names = {
            SEGMENT_PLATFORM: str(info.get("platform_id") or "平台"),
            SEGMENT_ROPE: "绳子",
            SEGMENT_DOWN_JUMP: "下跳回程",
            SEGMENT_RIGHT_RETURN: "旧版向右回程",
            SEGMENT_WALK_OFF_LEFT: "向左走出平台",
            SEGMENT_WALK_OFF_RIGHT: "向右走出平台",
            SEGMENT_PLATFORM_JUMP_LEFT: "平台向左跳",
            SEGMENT_PLATFORM_JUMP_RIGHT: "平台向右跳",
            SEGMENT_PLATFORM_JUMP_NEUTRAL: "平台原地跳",
            SEGMENT_PLATFORM_TO_ROPE_LEFT: "平台向左跳上绳",
            SEGMENT_PLATFORM_TO_ROPE_RIGHT: "平台向右跳上绳",
            SEGMENT_ROPE_TO_PLATFORM_LEFT: "绳子向左跳到平台",
            SEGMENT_ROPE_TO_PLATFORM_RIGHT: "绳子向右跳到平台",
        }
        segment_name = segment_names.get(info["segment_type"], "未知")
        finish_hint = (
            "落到已录制平台后自动结束，F7保存整条路线。"
            if info["segment_type"] in (
                SEGMENT_WALK_OFF_LEFT,
                SEGMENT_WALK_OFF_RIGHT,
            )
            else "F6结束当前段，F7保存整条路线。"
        )
        self.route_record_status.setText(
            "正在录制{segment_name}段#{segment_id}：本段{segment_points}点，"
            "总计{points}点 · 位置={position} · 命令={command} · {fps}Hz\n"
            "{finish_hint}".format(
                segment_name=segment_name,
                finish_hint=finish_hint,
                **info,
            )
        )

    def _on_route_saved(self, route_path: str, preview_path: str) -> None:
        """保存成功后选择自定义录制路线并切换到最后路线预览页。"""
        saved_path = Path(route_path)
        self.route_file_label.setText(saved_path.name)
        self.route_file_label.setToolTip(str(saved_path))
        self.route_map_file_label.setText(saved_path.name)
        self.route_map_file_label.setToolTip(str(saved_path))
        self.map_combo.setCurrentText("自定义录制路线")
        self.route_record_status.setText(
            "路线已保存：{}\n预览图：{}".format(route_path, preview_path)
        )
        self.monitor_tabs.setCurrentWidget(self.route_preview_page)
        self._load_last_route_preview(preview_path)
        self._schedule_route_preview_render()
        QMessageBox.information(
            self,
            "路线录制完成",
            "已保存分段路线、平台绳子连接关系和预览图。\n\n"
            "录制先后顺序不会影响平台与绳子的空间连接。",
        )

    def _load_last_route_preview(self, preview_path: str = None) -> None:
        """加载最后保存的路线PNG并显示在独立预览页。"""
        path = (
            Path(preview_path)
            if preview_path
            else mushroom_v3_preview_path(self.route_recorder.route_path)
        )
        self._last_route_preview_path = path
        route_path = self.route_recorder.route_path
        if not route_path.is_file():
            self._last_route_data = None
            self.route_preview_label.setPixmap(QPixmap())
            self.route_preview_label.setText("尚未保存分段路线")
            self._refresh_route_summary_platforms([])
            self.route_summary_view.setPlainText("尚未保存路线汇总。")
            return
        try:
            import json

            self._last_route_data = json.loads(route_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._last_route_data = None
            self.route_summary_view.setPlainText("路线汇总读取失败：{}".format(exc))
            return
        rest_point = read_route_rest_point(route_path)
        self.route_rest_point_label.setText(
            "休息点：X={}，Y={}；平台Y={}；每{}分钟休息{}分钟".format(
                rest_point["x"],
                rest_point["y"],
                rest_point.get("approach_y"),
                rest_point.get("interval_minutes"),
                rest_point.get("duration_minutes"),
            )
            if rest_point is not None
            else "休息点：未录制"
        )
        platforms = list(
            ((self._last_route_data or {}).get("route_graph") or {}).get("platforms")
            or []
        )
        self._refresh_route_summary_platforms(platforms)
        if path.is_file():
            self._render_route_preview()
        else:
            self.route_preview_label.setPixmap(QPixmap())
            self.route_preview_label.setText(
                "已加载{}，但没有同名PNG预览图".format(route_path.name)
            )
        self._load_route_summary()

    def _refresh_route_summary_platforms(self, platforms: list) -> None:
        """刷新汇总范围选项，并保留仍然存在的当前平台选择。"""
        current_text = self.route_summary_platform_combo.currentText() or "全部平台"
        names = sorted(
            {
                str(platform.get("platform_id", "平台{}".format(platform.get("id"))))
                for platform in platforms
            },
            key=self._platform_sort_key,
        )
        self.route_summary_platform_combo.blockSignals(True)
        self.route_summary_platform_combo.clear()
        self.route_summary_platform_combo.addItem("全部平台")
        self.route_summary_platform_combo.addItems(names)
        if current_text in {"全部平台", *names}:
            self.route_summary_platform_combo.setCurrentText(current_text)
        self.route_summary_platform_combo.blockSignals(False)

    @staticmethod
    def _platform_sort_key(platform_name: str) -> tuple:
        """按平台名称中的数字排序，保证平台2位于平台10之前。"""
        number_text = "".join(character for character in str(platform_name) if character.isdigit())
        return (int(number_text) if number_text else 10**9, str(platform_name))

    def _on_route_summary_scope_changed(self, _platform_name: str) -> None:
        """切换汇总平台时重绘高亮并刷新关联路线说明。"""
        self._render_route_preview()
        self._load_route_summary()

    def _on_monitor_tab_changed(self, _index: int) -> None:
        """路线页真正完成显示后再按最终布局尺寸重绘预览。"""
        if self.monitor_tabs.currentWidget() is self.route_preview_page:
            self._schedule_route_preview_render()

    def _schedule_route_preview_render(self, delay_ms: int = 0) -> None:
        """合并标签切换和窗口缩放产生的重绘请求，避免使用隐藏页旧尺寸。"""
        self._route_preview_resize_timer.start(max(0, int(delay_ms)))

    def _render_route_preview(self) -> None:
        """绘制整图预览，并用绿色半透明框高亮当前选择的平台。"""
        path = self._last_route_preview_path
        if path is None or not Path(path).is_file():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.route_preview_label.setText("路线预览图读取失败：{}".format(path))
            return
        selected_name = self.route_summary_platform_combo.currentText()
        if selected_name and selected_name != "全部平台" and self._last_route_data:
            graph = self._last_route_data.get("route_graph") or {}
            platform = next(
                (
                    item
                    for item in graph.get("platforms") or []
                    if str(item.get("platform_id")) == selected_name
                ),
                None,
            )
            if platform is not None:
                minimum_x, maximum_x = [int(value) for value in platform["x_range"]]
                platform_y = int(platform["representative_y"])
                painter = QPainter(pixmap)
                painter.setPen(QPen(QColor(80, 255, 120), 2))
                painter.setBrush(QBrush(QColor(80, 255, 120, 55)))
                painter.drawRect(
                    minimum_x - MINIMAP_MONITOR["left"],
                    platform_y - MINIMAP_MONITOR["top"] - 6,
                    max(2, maximum_x - minimum_x),
                    12,
                )
                painter.end()
        rest_point = (
            self._last_route_data.get("rest_point")
            if isinstance(self._last_route_data, dict)
            else None
        )
        if isinstance(rest_point, dict):
            try:
                rest_x = int(rest_point["x"]) - MINIMAP_MONITOR["left"]
                rest_y = int(rest_point["y"]) - MINIMAP_MONITOR["top"]
                painter = QPainter(pixmap)
                painter.setPen(QPen(QColor(255, 80, 220), 2))
                painter.setBrush(QBrush(QColor(255, 80, 220, 110)))
                painter.drawEllipse(rest_x - 5, rest_y - 5, 10, 10)
                painter.drawText(rest_x + 7, rest_y - 7, "休")
                painter.end()
            except (KeyError, TypeError, ValueError):
                pass
        target_size = self.route_preview_label.contentsRect().size()
        if target_size.width() <= 1 or target_size.height() <= 1:
            return
        self.route_preview_label.setText("")
        self.route_preview_label.setPixmap(
            pixmap.scaled(
                target_size,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def _load_route_summary(self) -> None:
        """读取最后路线JSON并汇总所有平台、绳子和概率分支。"""
        data = self._last_route_data
        if data is None:
            self.route_summary_view.setPlainText("尚未保存路线汇总。")
            return
        try:
            graph = data.get("route_graph") or {}
            platforms = list(graph.get("platforms") or [])
            connections = list(graph.get("connections") or [])
            transitions = list(graph.get("return_transitions") or [])
            selected_name = self.route_summary_platform_combo.currentText() or "全部平台"
            selected_platforms = platforms
            if selected_name != "全部平台":
                selected_platforms = [
                    platform
                    for platform in platforms
                    if str(platform.get("platform_id")) == selected_name
                ]
                connections = [
                    connection
                    for connection in connections
                    if selected_name
                    in {
                        str(connection.get("lower_platform_name")),
                        str(connection.get("upper_platform_name")),
                    }
                ]
                transitions = [
                    transition
                    for transition in transitions
                    if selected_name
                    in {
                        str(transition.get("from_platform_name")),
                        str(transition.get("to_platform_name")),
                    }
                ]
            lines = [
                "录制模式：{}".format(data.get("recording_mode", "旧版有序路线")),
                "平台：{}个　绳子：{}个　回程分支：{}个".format(
                    len(platforms),
                    len(graph.get("ropes") or []),
                    len(graph.get("return_transitions") or []),
                ),
                "当前查看：{}".format(selected_name),
                "休息点：{}".format(
                    "X={}，Y={}，起跳平台Y={}；每{}分钟休息{}分钟".format(
                        data["rest_point"].get("x"),
                        data["rest_point"].get("y"),
                        data["rest_point"].get("approach_y"),
                        data["rest_point"].get("interval_minutes"),
                        data["rest_point"].get("duration_minutes"),
                    )
                    if isinstance(data.get("rest_point"), dict)
                    else "未录制"
                ),
                "",
            ]
            if int(data.get("schema_version", 1)) >= 2:
                route_plans = list(data.get("route_plans") or [])
                main_plan = route_plans[0] if route_plans else {}
                calibration = data.get("calibration") or {}
                lines[1:1] = [
                    "V2有向图：{}节点，{}条边；有序步骤{}步；闭环={}".format(
                        len(data.get("nodes") or []),
                        len(data.get("edges") or []),
                        len(main_plan.get("steps") or []),
                        bool(main_plan.get("closed_loop")),
                    ),
                    "试走：{}帧，稳定访问{}次，未匹配标注={}，失败重试样本={}".format(
                        (data.get("trial_walk") or {}).get("frame_count", 0),
                        calibration.get("stable_visit_count", 0),
                        calibration.get("unmatched_annotation_segment_ids", []),
                        len(calibration.get("retry_samples") or []),
                    ),
                ]
            for platform in sorted(
                selected_platforms,
                key=lambda item: self._platform_sort_key(
                    str(item.get("platform_id", item.get("id")))
                ),
            ):
                lines.append(
                    "{}（P{}）：X={}，Y={}，{}点".format(
                        platform.get("platform_id", "平台{}".format(platform.get("id"))),
                        "".join(
                            character
                            for character in str(platform.get("platform_id", platform.get("id")))
                            if character.isdigit()
                        )
                        or platform.get("id"),
                        platform.get("x_range"),
                        platform.get("representative_y"),
                        platform.get("point_count"),
                    )
                )
            if connections:
                lines.append("")
                for connection in connections:
                    bottom_point = connection.get("bottom_point") or [
                        connection.get("rope_x"),
                        (connection.get("rope_y_range") or [None, None])[-1],
                    ]
                    top_point = connection.get("top_point") or [
                        connection.get("rope_x"),
                        (connection.get("rope_y_range") or [None, None])[0],
                    ]
                    lower_y = connection.get("lower_platform_y")
                    lower_gap = (
                        abs(int(lower_y) - int(bottom_point[1]))
                        if lower_y is not None and bottom_point[1] is not None
                        else "未知"
                    )
                    lines.append(
                        "绳子#{}：{} → {}，下端={}，上端={}，起跳Y={}，绳顶离绳={}，下层Y差={}，右跳={}，左跳={}".format(
                            connection.get("rope_segment_id"),
                            connection.get("lower_platform_name"),
                            connection.get("upper_platform_name"),
                            bottom_point,
                            top_point,
                            connection.get("lower_entry_y", bottom_point[1]),
                            (connection.get("top_exit") or {}).get("direction", "未生成"),
                            lower_gap,
                            connection.get("right_jump", {}).get("x_range"),
                            connection.get("left_jump", {}).get("x_range"),
                        )
                    )
            if transitions:
                lines.append("")
                transition_names = {
                    SEGMENT_DOWN_JUMP: "下跳",
                    SEGMENT_RIGHT_RETURN: "旧版向右走",
                    SEGMENT_WALK_OFF_LEFT: "向左走出平台",
                    SEGMENT_WALK_OFF_RIGHT: "向右走出平台",
                }
                for transition in transitions:
                    marker_text = (
                        "，单点标记={}".format(transition.get("start"))
                        if transition.get("marker_only")
                        else ""
                    )
                    lines.append(
                        "{}：{} → {}，概率{}%{}".format(
                            transition_names.get(transition.get("type"), transition.get("type")),
                            transition.get("from_platform_name"),
                            transition.get("to_platform_name"),
                            transition.get("probability", 0),
                            marker_text,
                        )
                    )
            self.route_summary_view.setPlainText("\n".join(lines))
        except Exception as exc:
            self.route_summary_view.setPlainText(
                "路线汇总读取失败：{}".format(exc)
            )

    def _show_route_summary(self) -> None:
        """切换到最后录制路线页并刷新所有平台汇总。"""
        self.monitor_tabs.setCurrentWidget(self.route_preview_page)
        self._load_last_route_preview()
        self._load_route_summary()
        self._schedule_route_preview_render()

    def _on_route_record_failed(self, message: str) -> None:
        """用对话框显示游戏窗口、黄色坐标或路线保存错误。"""
        QMessageBox.critical(self, "路线录制失败", message)

    def update_test_preview(self, rgb_frame, info) -> None:
        """把正常自动化检测线程复用的RGB截图显示到Qt界面。"""
        try:
            if not (self.test_mode or self.detection_only_mode) or rgb_frame is None:
                return
            height, width, channels = rgb_frame.shape
            bytes_per_line = channels * width
            image = QImage(
                rgb_frame.data,
                width,
                height,
                bytes_per_line,
                QImage.Format_RGB888,
            ).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                self.preview_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.preview_label.setPixmap(pixmap)

            self_position = info.get("self_position") or "未识别"
            self_template = info.get("self_template") or "未选择"
            detector = str(info.get("detector", "")).upper()
            self_search_mode = info.get("self_search_mode") or "未执行"
            capture_region = info.get("capture_region") or "未提供"
            max_confidence = info.get("max_monster_confidence")
            confidence_text = (
                "无"
                if max_confidence is None
                else "{:.3f}".format(float(max_confidence))
            )
            self.preview_info.setText(
                "地图：{map}  |  检测：{detector}  |  人物模板：{self_template}  |  怪物数量：{count}  |  最高置信度：{confidence}\n"
                "自己位置坐标：{self_pos}  |  人物搜索：{search}  |  人物匹配：{self_ms:.2f}ms\n"
                "截图区域：{capture_region}\n"
                "怪物检测/YOLO：{monster_ms:.1f}ms  |  FPS：{fps:.1f}".format(
                    map=info.get("map", ""), detector=detector,
                    self_template=self_template, count=info.get("monster_count", 0),
                    confidence=confidence_text,
                    self_pos=self_position, search=self_search_mode,
                    capture_region=capture_region,
                    self_ms=float(info.get("self_match_ms", 0.0)),
                    monster_ms=float(info.get("monster_match_ms", 0.0)),
                    fps=float(info.get("fps", 0.0)),
                )
            )
        finally:
            consumed = getattr(self.service, "mark_preview_consumed", None)
            if callable(consumed):
                consumed()

    def update_rest_snapshot(self, rgb_frame, details) -> None:
        """显示一次已确认抵达休息点时的截图、时间和会话内次数。"""
        details = dict(details or {})
        is_test = bool(details.get("test_mode", False))
        rest_count = int(details.get("rest_count", self._rest_snapshot_count + 1))
        if not is_test:
            self._rest_snapshot_count = max(self._rest_snapshot_count, rest_count)
        arrived_at = details.get("arrived_at", "未知时间")
        position = details.get("position") or "未知"
        rest_point = details.get("rest_point") or "未知"
        remaining = float(details.get("remaining_seconds", 0.0))
        if rgb_frame is not None:
            height, width, channels = rgb_frame.shape
            image = QImage(
                rgb_frame.data,
                width,
                height,
                channels * width,
                QImage.Format_RGB888,
            ).copy()
            self.rest_snapshot_label.setText("")
            self.rest_snapshot_label.setPixmap(
                QPixmap.fromImage(image).scaled(
                    self.rest_snapshot_label.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
            snapshot_text = "截图已显示"
        else:
            self.rest_snapshot_label.setPixmap(QPixmap())
            self.rest_snapshot_label.setText("已到达休息点，但当前截图失败")
            snapshot_text = "截图失败：{}".format(details.get("snapshot_error", "未知原因"))
        if is_test:
            self.rest_snapshot_info.setText(
                "休息点测试成功：已到达录制休息点\n"
                "测试到达时间：{}\n"
                "当前位置：{}  |  录制点：{}\n"
                "未执行铃铛、枫叶或商店操作；人物将保持在当前休息点，"
                "不会恢复后续路线\n{}".format(
                    arrived_at, position, rest_point, snapshot_text
                )
            )
        else:
            self.rest_snapshot_info.setText(
                "已到达录制休息点\n"
                "时间：{}\n"
                "本次运行第 {} 次休息\n"
                "当前位置：{}  |  录制点：{}\n"
                "本次预计休息：{:.1f} 秒\n{}".format(
                    arrived_at, rest_count, position, rest_point, remaining, snapshot_text
                )
            )
        self.monitor_tabs.setCurrentWidget(self.rest_snapshot_page)

    @staticmethod
    def _format_rest_countdown(seconds) -> str:
        """把秒数格式化为适合持续查看的 时:分:秒。"""
        total_seconds = max(0, int(float(seconds) + 0.999))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return "{:02d}:{:02d}:{:02d}".format(hours, minutes, secs)

    @staticmethod
    def _read_process_resources():
        """读取当前进程的 Windows 图形对象、句柄和工作集。"""
        if os.name != "nt":
            return None
        try:
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            process = kernel32.GetCurrentProcess()
            user32.GetGuiResources.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
            user32.GetGuiResources.restype = ctypes.c_ulong
            gdi_objects = int(user32.GetGuiResources(process, 0))
            user_objects = int(user32.GetGuiResources(process, 1))

            handle_count = ctypes.c_ulong()
            kernel32.GetProcessHandleCount.argtypes = (
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            )
            kernel32.GetProcessHandleCount.restype = ctypes.c_int
            if not kernel32.GetProcessHandleCount(process, ctypes.byref(handle_count)):
                handle_count.value = 0

            memory = _ProcessMemoryCounters()
            memory.cb = ctypes.sizeof(memory)
            psapi.GetProcessMemoryInfo.argtypes = (
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCounters),
                ctypes.c_ulong,
            )
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            if not psapi.GetProcessMemoryInfo(
                process, ctypes.byref(memory), memory.cb
            ):
                memory.WorkingSetSize = 0
            return {
                "gdi": gdi_objects,
                "user": user_objects,
                "handles": int(handle_count.value),
                "memory_mb": float(memory.WorkingSetSize) / (1024.0 * 1024.0),
            }
        except Exception:
            return None

    def _refresh_resource_monitor(self) -> None:
        """每秒刷新资源，并在持续单向增长时用颜色提示虚拟机风险。"""
        if not hasattr(self, "resource_monitor_label"):
            return
        sample = self._read_process_resources()
        if sample is None:
            self.resource_monitor_label.setText("图形资源：当前系统不支持读取")
            return
        if self._resource_baseline is None:
            self._resource_baseline = dict(sample)
        baseline = self._resource_baseline
        deltas = {
            key: sample[key] - baseline[key]
            for key in ("gdi", "user", "handles", "memory_mb")
        }
        previous = self._resource_last_sample
        if previous is not None and (
            sample["gdi"] > previous["gdi"]
            or sample["user"] > previous["user"]
            or sample["handles"] > previous["handles"]
        ):
            self._resource_monotonic_growth += 1
        else:
            self._resource_monotonic_growth = max(
                0, self._resource_monotonic_growth - 1
            )
        self._resource_last_sample = dict(sample)

        severe = (
            sample["gdi"] >= 8000
            or sample["user"] >= 8000
            or deltas["gdi"] >= 2000
            or deltas["user"] >= 2000
            or deltas["handles"] >= 5000
        )
        warning = severe or (
            deltas["gdi"] >= 400
            or deltas["user"] >= 400
            or deltas["handles"] >= 1000
            or self._resource_monotonic_growth >= 15
        )
        if severe:
            state_text, color, background, border = (
                "危险：对象持续累积", "#ff9b9b", "#321923", "#8d3d52"
            )
        elif warning:
            state_text, color, background, border = (
                "注意：资源上涨", "#ffd27d", "#302817", "#7b6230"
            )
        else:
            state_text, color, background, border = (
                "正常", "#9fe6c1", "#10261f", "#2e6e57"
            )
        self.resource_monitor_label.setText(
            "图形资源：{state}  ·  GDI {gdi} ({gdi_delta:+d})  ·  "
            "USER {user} ({user_delta:+d})  ·  句柄 {handles} ({handle_delta:+d})  ·  "
            "内存 {memory:.0f} MB ({memory_delta:+.0f})  ·  预览队列 ≤ 1".format(
                state=state_text,
                gdi=sample["gdi"], gdi_delta=int(deltas["gdi"]),
                user=sample["user"], user_delta=int(deltas["user"]),
                handles=sample["handles"], handle_delta=int(deltas["handles"]),
                memory=sample["memory_mb"], memory_delta=deltas["memory_mb"],
            )
        )
        self.resource_monitor_label.setStyleSheet(
            "color: {color}; background: {background}; border: 1px solid {border}; "
            "border-radius: 8px; padding: 8px 10px; font-weight: 700;".format(
                color=color, background=background, border=border
            )
        )

    def update_rest_schedule(self, details) -> None:
        """接收路线线程的真实休息阶段，并启动对应的页面倒计时。"""
        payload = dict(details or {})
        status = str(payload.get("status", "disabled"))
        remaining = max(0.0, float(payload.get("remaining_seconds", 0.0)))
        self._rest_countdown_status = status
        self._rest_countdown_remaining = remaining
        self._rest_countdown_number = max(1, int(payload.get("rest_count", 1)))
        self._rest_countdown_deadline = (
            time.monotonic() + remaining
            if status in {"scheduled", "resting"}
            else 0.0
        )
        self._refresh_rest_point_test_button()
        self._refresh_rest_countdown()

    def _refresh_rest_countdown(self) -> None:
        """每半秒刷新休息记录页，避免倒计时依赖日志或截图事件。"""
        status = self._rest_countdown_status
        remaining = self._rest_countdown_remaining
        if self._rest_countdown_deadline > 0:
            remaining = max(0.0, self._rest_countdown_deadline - time.monotonic())
        countdown = self._format_rest_countdown(remaining)
        rest_number = self._rest_countdown_number
        if status == "scheduled":
            text = "下次休息倒计时：{}  ·  第 {} 次".format(countdown, rest_number)
        elif status == "pending":
            text = "休息时间已到  ·  正在前往休息点（第 {} 次）".format(rest_number)
        elif status == "resting":
            text = "正在休息：剩余 {}  ·  第 {} 次".format(countdown, rest_number)
        elif status == "returning":
            text = "休息被中断，正在返回休息点  ·  保留 {}".format(countdown)
        elif status == "disabled":
            text = "下次休息：未启用或当前路线没有可用休息位置"
        elif status == "initializing":
            text = "下次休息：正在读取路线休息计划……"
        elif status == "test_requested":
            text = "休息点测试已发送，正在等待路线接管……"
        elif status == "test_unavailable":
            text = "无法测试：请先录制休息点或有效绳子，并启用休息间隔和时长"
        elif status == "test_parked":
            text = "休息点测试成功  ·  已停在当前绳子/休息点"
        else:
            text = "下次休息：任务未运行"
        self.rest_countdown_label.setText(text)

    def _test_rest_point(self) -> None:
        """空闲时一键启动路线，运行中则直接请求休息点测试。"""
        if self.map_combo.currentText() != "自定义录制路线":
            QMessageBox.warning(
                self,
                "无法测试休息点",
                "请先选择或加载一个“自定义录制路线”JSON。",
            )
            return
        if self.detection_only_mode:
            QMessageBox.warning(
                self,
                "无法测试休息点",
                "纯识别测试模式不会发送移动和跳跃按键，请先关闭纯识别测试。",
            )
            return
        if self.route_recorder.is_running or self._pending_route_segment_type is not None:
            QMessageBox.warning(
                self,
                "正在录制路线",
                "请先结束或保存当前路线录制，再测试休息点。",
            )
            return
        previous_state = (
            self._rest_countdown_status,
            self._rest_countdown_deadline,
            self._rest_countdown_remaining,
        )
        self._rest_countdown_status = "test_requested"
        self._rest_countdown_deadline = 0.0
        self._rest_countdown_remaining = 0.0
        self._refresh_rest_countdown()
        if self.service.is_running:
            if self.service.trigger_rest_point_test():
                self._refresh_rest_point_test_button()
                return
            (
                self._rest_countdown_status,
                self._rest_countdown_deadline,
                self._rest_countdown_remaining,
            ) = previous_state
            self._refresh_rest_countdown()
            QMessageBox.warning(
                self,
                "无法测试休息点",
                "当前运行任务尚未准备好，请停止后重新点击“立即测试休息点”。",
            )
            return

        try:
            config = self.collect_config()
            self.store.save(config)
            self._pending_rest_point_test = True
            self._rest_snapshot_count = 0
            self.rest_snapshot_label.setPixmap(QPixmap())
            self.rest_snapshot_label.setText("正在启动路线并前往休息点……")
            self.rest_snapshot_info.setText(
                "测试会自动启动当前路线；定位就绪后立即返回休息点，"
                "不操作铃铛、枫叶或便捷杂货店，到达后直接停在绳子/休息点。"
            )
            if not self.service.start(config, trace_suffix="rest"):
                self._pending_rest_point_test = False
                (
                    self._rest_countdown_status,
                    self._rest_countdown_deadline,
                    self._rest_countdown_remaining,
                ) = previous_state
                self._refresh_rest_countdown()
                QMessageBox.warning(self, "无法启动", "自动化任务仍在停止中，请稍后重试。")
        except (ValueError, OSError) as exc:
            self._pending_rest_point_test = False
            (
                self._rest_countdown_status,
                self._rest_countdown_deadline,
                self._rest_countdown_remaining,
            ) = previous_state
            self._refresh_rest_countdown()
            QMessageBox.warning(self, "无法测试休息点", str(exc))
        self._refresh_rest_point_test_button()

    def _dispatch_pending_rest_point_test(self) -> None:
        """一键测试自动启动完成后，把测试请求交给录制路线线程。"""
        if not self._pending_rest_point_test:
            return
        self._pending_rest_point_test = False
        if not self.service.trigger_rest_point_test():
            self._rest_countdown_status = "test_unavailable"
            self._refresh_rest_countdown()
            print("[定时休息] 一键测试启动成功，但休息点测试请求未能交给路线。")
        self._refresh_rest_point_test_button()

    def _refresh_rest_point_test_button(self) -> None:
        """空闲时允许一键启动测试；测试执行期间防止重复提交。"""
        if not hasattr(self, "rest_point_test_button"):
            return
        correct_map = self.map_combo.currentText() == "自定义录制路线"
        route_recording_idle = (
            not self.route_recorder.is_running
            and self._pending_route_segment_type is None
        )
        active_test = self._rest_countdown_status in {
            "test_requested",
            "test_parked",
            "pending",
            "resting",
            "returning",
        }
        state_allows_click = self._service_state in {"idle", "running", "error"}
        self.rest_point_test_button.setEnabled(
            correct_map
            and not self.detection_only_mode
            and route_recording_idle
            and state_allows_click
            and not self._pending_rest_point_test
            and not active_test
        )

    def collect_config(self) -> AppConfig:
        """从控件收集、构造并校验当前配置。"""
        return AppConfig(
            map_name=self.map_combo.currentText(),
            mushroom_v3_route_file=str(self.route_recorder.route_path),
            test_mode=self.test_mode_enabled.isChecked(),
            detection_only_mode=self.detection_only_enabled.isChecked(),
            runtime_logging_enabled=self.runtime_logging_enabled.isChecked(),
            wheel_detection_enabled=self.wheel_detection_enabled.isChecked(),
            attack_distance=self.attack_distance.value(),
            attack_height=self.attack_height.value(),
            yolo_confidence=self.yolo_confidence.value(),
            use_custom_templates=self.use_custom_templates.isChecked(),
            custom_mode=self.custom_mode.isChecked(),
            custom_left_position=self._custom_left_position,
            custom_right_position=self._custom_right_position,
            single_attack_key=self.single_key.currentText().strip(),
            group_attack_key=self.group_key.currentText().strip(),
            flash_key=self.flash_key.currentText().strip(),
            group_attack=self.group_attack.isChecked(),
            auto_group_attack_over_count=(
                self.auto_group_attack_over_count.value()
            ),
            flash_enabled=self.flash_enabled.isChecked(),
            draw_detection_boxes=self.draw_boxes.isChecked(),
            red_percent=self.red_percent.value(),
            blue_percent=self.blue_percent.value(),
            rope_delay=self.rope_delay.value(),
            rope_rest_interval_minutes=self.rope_rest_interval.value(),
            rope_rest_duration_minutes=self.rope_rest_duration.value(),
            ignored_platform_numbers=self.ignored_platform_numbers_input.text(),
            scheduled_keys=self._collect_scheduled_keys(),
        ).validate()

    def apply_config(self, config: AppConfig) -> None:
        """把配置值回填到全部界面控件。"""
        self.test_mode_enabled.blockSignals(True)
        self.detection_only_enabled.blockSignals(True)
        self.test_mode_enabled.setChecked(config.test_mode)
        self.detection_only_enabled.setChecked(config.detection_only_mode)
        self.runtime_logging_enabled.setChecked(config.runtime_logging_enabled)
        self.wheel_detection_enabled.setChecked(config.wheel_detection_enabled)
        self.test_mode_enabled.blockSignals(False)
        self.detection_only_enabled.blockSignals(False)
        self._update_mode_ui()
        self.map_combo.setCurrentText(config.map_name)
        configured_route_path = resolve_mushroom_v3_route_path(
            self.store.path.parent,
            config.mushroom_v3_route_file,
        )
        self.route_recorder.set_route_path(configured_route_path)
        self.route_file_label.setText(configured_route_path.name)
        self.route_file_label.setToolTip(str(configured_route_path))
        self.route_map_file_label.setText(configured_route_path.name)
        self.route_map_file_label.setToolTip(str(configured_route_path))
        self.attack_distance.setValue(config.attack_distance)
        self.attack_height.setValue(config.attack_height)
        self.yolo_confidence.setValue(config.yolo_confidence)
        self.use_custom_templates.setChecked(config.use_custom_templates)
        self.custom_mode.setChecked(config.custom_mode)
        self._update_custom_mode_description()
        self._custom_left_position = config.custom_left_position
        self._custom_right_position = config.custom_right_position
        self._update_custom_position_labels()
        self._set_fixed_key_value(self.single_key, config.single_attack_key)
        self._set_fixed_key_value(self.group_key, config.group_attack_key)
        self._set_fixed_key_value(self.flash_key, config.flash_key)
        self.group_attack.setChecked(config.group_attack)
        self.auto_group_attack_over_count.setValue(
            config.auto_group_attack_over_count
        )
        self.flash_enabled.setChecked(config.flash_enabled)
        self.draw_boxes.setChecked(config.draw_detection_boxes)
        self.red_percent.setValue(config.red_percent)
        self.blue_percent.setValue(config.blue_percent)
        self.rope_delay.setValue(config.rope_delay)
        self.rope_rest_interval.setValue(config.rope_rest_interval_minutes)
        self.rope_rest_duration.setValue(config.rope_rest_duration_minutes)
        self.ignored_platform_numbers_input.setText(
            "/".join(str(number) for number in config.ignored_platform_numbers)
        )
        self._clear_scheduled_key_rows()
        for key, interval in config.scheduled_keys:
            self._add_scheduled_key_row(key, interval)

    def _on_screenshot_mode_toggled(self, enabled: bool) -> None:
        """开启截图运行时自动关闭纯识别测试，并刷新页面模式。"""
        if enabled and self.detection_only_enabled.isChecked():
            self.detection_only_enabled.blockSignals(True)
            self.detection_only_enabled.setChecked(False)
            self.detection_only_enabled.blockSignals(False)
        self._update_mode_ui()

    def _on_detection_only_mode_toggled(self, enabled: bool) -> None:
        """开启纯识别测试时自动关闭截图运行，并刷新页面模式。"""
        if enabled and self.test_mode_enabled.isChecked():
            self.test_mode_enabled.blockSignals(True)
            self.test_mode_enabled.setChecked(False)
            self.test_mode_enabled.blockSignals(False)
        self._update_mode_ui()

    def _on_auto_group_attack_threshold_changed(self, value: int) -> None:
        """把页面中的自动群攻数量阈值即时同步给正在运行的识别引擎。"""
        if not self.service.is_running:
            return
        update_threshold = getattr(
            self.service,
            "update_auto_group_attack_over_count",
            None,
        )
        if callable(update_threshold):
            update_threshold(value)

    def _update_mode_ui(self) -> None:
        """根据两个互斥测试选项更新标题、按钮、日志和实时预览。"""
        self.test_mode = self.test_mode_enabled.isChecked()
        self.detection_only_mode = self.detection_only_enabled.isChecked()
        preview_enabled = self.test_mode or self.detection_only_mode
        if self.detection_only_mode:
            mode_name = "纯识别测试模式"
        elif self.test_mode:
            mode_name = "截图测试模式"
        else:
            mode_name = "正常模式"
        self.setWindowTitle("{} - {}".format(display_name(), mode_name))
        application = QApplication.instance()
        if application is not None:
            application.setApplicationName(
                "{} - {}".format(display_name(), mode_name)
            )

        if self.detection_only_mode:
            subtitle = "纯识别测试 · 只截图和识别怪物 · 不发送任何游戏按键"
            start_text = "▶ 启动识别测试"
        elif self.test_mode:
            subtitle = "截图测试模式 · 实时预览 · 移动攻击和地图路线正常运行"
            start_text = "▶ 启动（截图）"
        else:
            subtitle = "兼容 1.9 全部地图逻辑 · 分层架构 · 安全启停"
            start_text = "▶ 启动"
        self.subtitle_label.setText(subtitle)
        self.version_label.setText("V3.0 TEST" if preview_enabled else "V3.0")
        self.start_button.setText(start_text)
        self.monitor_title.setText("检测预览与日志" if preview_enabled else "运行日志")
        self.preview_label.setVisible(preview_enabled)
        self.preview_info.setVisible(preview_enabled)

        if preview_enabled:
            if not self.service.is_running:
                self.preview_label.clear()
                self.preview_label.setText("点击启动按钮后显示实时识别画面")
                self.preview_info.setText("尚未收到检测画面")
            self.log_view.setMaximumHeight(190)
            if self.detection_only_mode:
                self.log_view.setPlaceholderText(
                    "纯识别测试只显示怪物框、置信度和耗时，不会执行任何游戏动作。"
                )
            else:
                self.log_view.setPlaceholderText(
                    "截图测试模式仍会正常移动、攻击并执行地图路线，运行信息会完整记录。"
                )
        else:
            self.log_view.setMaximumHeight(16777215)
            self.log_view.setPlaceholderText(
                "启动后，检测、地图动作和异常信息会显示在这里。"
            )
        self._refresh_rest_point_test_button()

    def request_start(self) -> None:
        """保存当前配置并请求后台服务启动。"""
        if self.route_recorder.is_running or self._pending_route_segment_type is not None:
            QMessageBox.warning(
                self,
                "正在录制路线",
                "请先按F7保存、按F6结束当前段，或取消待录制分段，再启动刷图。",
            )
            return
        try:
            config = self.collect_config()
            self.store.save(config)
            self._rest_countdown_status = "initializing"
            self._rest_countdown_deadline = 0.0
            self._rest_countdown_remaining = 0.0
            self._rest_countdown_number = 1
            self._refresh_rest_countdown()
            if self.service.start(config):
                self._rest_snapshot_count = 0
                self.rest_snapshot_label.setPixmap(QPixmap())
                self.rest_snapshot_label.setText("本次运行尚未到达录制休息点")
                self.rest_snapshot_info.setText(
                    "到达录制休息点后，会在这里保留当时的游戏截图、时间和本次运行的次数。"
                )
                self.start_button.setEnabled(False)
                self.stop_button.setEnabled(True)
            else:
                self._rest_countdown_status = "stopped"
                self._refresh_rest_countdown()
        except ValueError as exc:
            self._rest_countdown_status = "stopped"
            self._refresh_rest_countdown()
            QMessageBox.warning(self, "配置有误", str(exc))
        except OSError as exc:
            self._rest_countdown_status = "stopped"
            self._refresh_rest_countdown()
            QMessageBox.warning(self, "保存失败", "无法保存配置：{}".format(exc))

    def request_stop(self) -> None:
        """请求后台服务安全停止。"""
        self.service.stop()

    def request_toggle_pause(self) -> None:
        """暂停或继续当前刷图/纯识别任务。"""
        if not self.service.is_running:
            return
        toggle = getattr(self.service, "toggle_pause", None)
        if toggle is None or not toggle():
            QMessageBox.warning(self, "暂停失败", "当前任务尚未完成初始化，请稍后再试。")

    def save_config(self) -> None:
        """仅保存配置，不启动任务。"""
        try:
            self.store.save(self.collect_config())
            self.set_status("idle", "配置已保存")
            QTimer.singleShot(1400, self._restore_idle_status)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def reset_config(self) -> None:
        """把控件恢复为程序默认配置。"""
        self.apply_config(AppConfig())
        self.set_status("idle", "已恢复默认")
        QTimer.singleShot(1400, self._restore_idle_status)

    def append_log(self, text: str) -> None:
        """合并零散输出并追加到有容量上限的日志视图。"""
        self._pending_log += text
        if "\n" not in self._pending_log and len(self._pending_log) < 400:
            return
        value, self._pending_log = self._pending_log, ""
        self.log_view.moveCursor(QTextCursor.End)
        self.log_view.insertPlainText(value)
        self.log_view.moveCursor(QTextCursor.End)

    def _clear_log(self) -> None:
        """清空日志缓存和显示内容。"""
        self._pending_log = ""
        self.log_view.clear()

    def set_hotkey_status(self, available: bool, message: str) -> None:
        """更新全局热键可用状态及对应颜色。"""
        self.hotkey_label.setText(message)
        color = "#78f0b3" if available else "#ff9b9b"
        self.hotkey_label.setStyleSheet("color: {};".format(color))

    def set_status(self, state: str, text: str) -> None:
        """更新状态徽标，并同步启停按钮的可用状态。"""
        if state == "starting":
            # 每次实际启动都以启动前一刻为资源基线，页面增量更容易判断本轮
            # 是否仍有虚拟显卡/GDI对象持续增长。
            self._resource_baseline = None
            self._resource_last_sample = None
            self._resource_monotonic_growth = 0
        self._service_state = state
        symbols = {
            "idle": "●",
            "starting": "◐",
            "running": "●",
            "paused": "Ⅱ",
            "stopping": "◐",
            "error": "●",
        }
        self.status_badge.setText("{} {}".format(symbols.get(state, "●"), text))
        self.status_badge.setProperty("state", state)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)
        busy = state in {"starting", "running", "paused", "stopping"}
        if state in {"idle", "error"}:
            if not self.service.is_running:
                self._pending_rest_point_test = False
            self._rest_countdown_status = "stopped"
            self._rest_countdown_deadline = 0.0
            self._rest_countdown_remaining = 0.0
            if hasattr(self, "rest_countdown_label"):
                self._refresh_rest_countdown()
            self._route_monitor_payload = {}
            if hasattr(self, "route_runtime_status_label"):
                self.route_runtime_status_label.setText(
                    "角色平台：等待路线启动  ·  下一平台：--  ·  换台倒计时：--"
                )
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.pause_button.setEnabled(state in {"running", "paused"})
        self.pause_button.setText("▶ 继续" if state == "paused" else "Ⅱ 暂停")
        self.test_mode_enabled.setEnabled(not busy)
        self.detection_only_enabled.setEnabled(not busy)
        self.runtime_logging_enabled.setEnabled(not busy)
        self.wheel_detection_enabled.setEnabled(not busy)
        self._refresh_rest_point_test_button()
        can_record_segment = not busy and (
            not self.route_recorder.is_running or self.route_recorder.is_paused
        )
        can_switch_route = (
            not busy
            and not self.route_recorder.is_running
            and self._pending_route_segment_type is None
        )
        self.route_new_file_button.setEnabled(can_switch_route)
        self.route_load_file_button.setEnabled(can_switch_route)
        self.route_map_load_button.setEnabled(can_switch_route)
        self.route_record_rest_point_button.setEnabled(can_switch_route)
        self.route_record_platform_button.setEnabled(can_record_segment)
        self.route_record_rope_button.setEnabled(can_record_segment)
        self.route_record_down_jump_button.setEnabled(can_record_segment)
        self.route_record_left_walk_off_button.setEnabled(can_record_segment)
        self.route_record_right_return_button.setEnabled(can_record_segment)
        for button in self._route_v2_transition_buttons:
            button.setEnabled(can_record_segment)
        self.route_record_trial_start_button.setEnabled(can_switch_route)
        self.route_record_trial_finish_button.setEnabled(
            not busy
            and self.route_recorder.active_segment_type == SEGMENT_TRIAL_WALK_V2
            and not self.route_recorder.is_paused
        )

    def _restore_idle_status(self) -> None:
        """在服务已停止时恢复默认空闲提示。"""
        if not self.service.is_running:
            if self.detection_only_mode:
                text = "纯识别测试已停止"
            elif self.test_mode:
                text = "截图运行已停止"
            else:
                text = "已停止"
            self.set_status("idle", text)

    def _show_error(self, message: str) -> None:
        """用对话框展示后台服务启动或运行错误。"""
        if self.detection_only_mode:
            title = "纯识别测试失败"
            description = "只读怪物识别服务未能启动"
        elif self.test_mode:
            title = "截图运行失败"
            description = "带截图的自动化未能启动"
        else:
            title = "运行失败"
            description = "自动化引擎未能启动"
        QMessageBox.critical(
            self,
            title,
            "{}：\n{}\n\n请检查依赖、模型/模板文件和屏幕捕获权限。".format(
                description, message
            ),
        )

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化后按路线预览控件的最终可用区域重新缩放原图。"""
        super().resizeEvent(event)
        if (
            hasattr(self, "monitor_tabs")
            and hasattr(self, "route_preview_page")
            and self.monitor_tabs.currentWidget() is self.route_preview_page
        ):
            self._schedule_route_preview_render(40)

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭窗口前停止任务、保存配置并释放外部资源。"""
        self.service.stop()
        try:
            self.store.save(self.collect_config())
        except (ValueError, OSError):
            pass
        for callback in self._close_callbacks:
            try:
                callback()
            except Exception:
                pass
        event.accept()

"""按素材目录分组、以五列懒加载缩略图展示的怪物图鉴。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .public.monster_atlas import (
    MAX_TEMPLATES_PER_MONSTER,
    MonsterEntry,
    MonsterTemplateReport,
    estimated_template_count,
    generate_monster_templates,
    monster_library_directory,
    scan_monster_library,
)


MONSTER_CARD_COLUMNS = 5
MONSTER_CARD_WIDTH = 140
MONSTER_CARD_HEIGHT = 126
MONSTER_ICON_WIDTH = 108
MONSTER_ICON_HEIGHT = 84
MONSTER_PREVIEW_VIEWPORT_MARGIN = 1


class MonsterCard(QToolButton):
    """一张只在进入滚动窗口附近时读取图片的怪物卡片。"""

    def __init__(self, entry: MonsterEntry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._preview_loaded = False
        self.setObjectName("monsterCard")
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setIconSize(QSize(MONSTER_ICON_WIDTH, MONSTER_ICON_HEIGHT))
        self.setFixedSize(MONSTER_CARD_WIDTH, MONSTER_CARD_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(
            "目录：{}\n怪物：{}\n素材：{}\n点击缩略图即可多选".format(
                entry.group,
                entry.name,
                entry.path,
            )
        )
        # 初始化时不读取图片。素材库约有400张，一次性构造QPixmap会让
        # 图鉴窗口打开时长时间阻塞；实际图片由对话框按滚动可见区域加载。
        self.setIcon(QIcon())
        self.toggled.connect(self._refresh_text)
        self._refresh_text(False)

    @property
    def preview_loaded(self) -> bool:
        """返回当前卡片是否已经持有解码后的缩略图。"""
        return self._preview_loaded

    def load_preview(self) -> None:
        """读取并显示原始像素素材；重复调用不会再次解码。"""
        if self._preview_loaded:
            return
        pixmap = QPixmap(str(self.entry.path))
        if not pixmap.isNull():
            # 游戏素材是像素图，使用快速缩放保持硬边缘，不做平滑重绘。
            preview = pixmap.scaled(
                MONSTER_ICON_WIDTH,
                MONSTER_ICON_HEIGHT,
                Qt.KeepAspectRatio,
                Qt.FastTransformation,
            )
            self.setIcon(QIcon(preview))
        self._preview_loaded = True

    def unload_preview(self) -> None:
        """释放远离可见区域的缩略图，控制长时间滚动后的内存占用。"""
        if not self._preview_loaded:
            return
        self.setIcon(QIcon())
        self._preview_loaded = False

    def _refresh_text(self, checked: bool) -> None:
        display_name = self.entry.name
        if len(display_name) > 13:
            display_name = display_name[:12] + "…"
        self.setText(("✓ " if checked else "") + display_name)


class MonsterGroupSection(QWidget):
    """一个 monsters 子目录及其五列缩略图网格。"""

    selection_changed = pyqtSignal()
    expanded_changed = pyqtSignal(bool)

    def __init__(self, group, expanded: bool = False, parent=None):
        super().__init__(parent)
        self.group = group
        self.cards: list[MonsterCard] = []
        self._expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.header = QFrame()
        self.header.setObjectName("monsterGroupHeader")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 5, 8, 5)
        header_layout.setSpacing(7)
        self.expand_button = QToolButton()
        self.expand_button.setObjectName("monsterGroupExpand")
        self.expand_button.setFixedSize(26, 26)
        self.expand_button.clicked.connect(lambda: self.set_expanded(not self._expanded))
        self.group_checkbox = QCheckBox(group.name)
        self.group_checkbox.setTristate(True)
        self.group_checkbox.setToolTip("勾选或取消当前目录中的全部怪物")
        self.group_checkbox.stateChanged.connect(self._on_group_state_changed)
        count_label = QLabel("{} 只".format(len(group.monsters)))
        count_label.setObjectName("monsterGroupCount")
        header_layout.addWidget(self.expand_button)
        header_layout.addWidget(self.group_checkbox)
        header_layout.addStretch()
        header_layout.addWidget(count_label)
        layout.addWidget(self.header)

        self.content = QFrame()
        self.content.setObjectName("monsterGroupContent")
        self.grid = QGridLayout(self.content)
        self.grid.setContentsMargins(5, 5, 5, 5)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(5)
        self.grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        for entry in group.monsters:
            card = MonsterCard(entry)
            card.toggled.connect(self._on_card_toggled)
            self.cards.append(card)
        self._relayout_cards()
        layout.addWidget(self.content)
        self.set_expanded(expanded)
        self._sync_group_checkbox()

    def set_expanded(self, expanded: bool) -> None:
        changed = self._expanded != bool(expanded)
        self._expanded = bool(expanded)
        self.expand_button.setText("▼" if self._expanded else "▶")
        self.content.setVisible(self._expanded)
        if not self._expanded:
            for card in self.cards:
                card.unload_preview()
        if changed:
            self.expanded_changed.emit(self._expanded)

    @property
    def is_expanded(self) -> bool:
        """返回当前分组是否已经展开。"""
        return self._expanded

    def _on_group_state_changed(self, state: int) -> None:
        if state == Qt.PartiallyChecked:
            return
        checked = state == Qt.Checked
        for card in self.cards:
            card.blockSignals(True)
            card.setChecked(checked)
            card._refresh_text(checked)
            card.blockSignals(False)
        self._sync_group_checkbox()
        self.selection_changed.emit()

    def _on_card_toggled(self, _checked: bool) -> None:
        self._sync_group_checkbox()
        self.selection_changed.emit()

    def _sync_group_checkbox(self) -> None:
        selected = sum(card.isChecked() for card in self.cards)
        self.group_checkbox.blockSignals(True)
        if not selected:
            state = Qt.Unchecked
        elif selected == len(self.cards):
            state = Qt.Checked
        else:
            state = Qt.PartiallyChecked
        self.group_checkbox.setCheckState(state)
        self.group_checkbox.blockSignals(False)

    def _relayout_cards(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        visible_cards = [card for card in self.cards if not card.isHidden()]
        for index, card in enumerate(visible_cards):
            self.grid.addWidget(
                card,
                index // MONSTER_CARD_COLUMNS,
                index % MONSTER_CARD_COLUMNS,
            )

    def apply_filter(self, query: str) -> bool:
        group_matches = not query or query in self.group.name.casefold()
        visible_count = 0
        for card in self.cards:
            matches = group_matches or query in card.entry.name.casefold()
            card.setHidden(not matches)
            visible_count += int(matches)
        self._relayout_cards()
        self.setVisible(visible_count > 0)
        if query and visible_count:
            self.set_expanded(True)
        return visible_count > 0

    def select_visible(self) -> None:
        for card in self.cards:
            if not card.isHidden():
                card.blockSignals(True)
                card.setChecked(True)
                card._refresh_text(True)
                card.blockSignals(False)
        self._sync_group_checkbox()

    def clear_selection(self) -> None:
        for card in self.cards:
            card.blockSignals(True)
            card.setChecked(False)
            card._refresh_text(False)
            card.blockSignals(False)
        self._sync_group_checkbox()


class MonsterAtlasDialog(QDialog):
    """展示 ``img/monsters`` 的目录层级和可多选懒加载卡片。"""

    templates_generated = pyqtSignal(object)

    def __init__(
        self,
        project_root: Path | str,
        is_automation_running: Callable[[], bool] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.project_root = Path(project_root).resolve()
        self.is_automation_running = is_automation_running or (lambda: False)
        self._group_sections: list[MonsterGroupSection] = []
        self._last_report: MonsterTemplateReport | None = None
        self._lazy_preview_refresh_pending = False

        self.setWindowTitle("怪物图鉴")
        self.setMinimumSize(790, 650)
        self.resize(860, 740)
        self.setStyleSheet(
            """
            QFrame#monsterGroupHeader {
                background: #0b1725;
                border: 1px solid #2b4058;
                border-radius: 7px;
            }
            QFrame#monsterGroupContent {
                background: #09111e;
                border: 1px solid #1f3045;
                border-radius: 7px;
            }
            QToolButton#monsterGroupExpand {
                padding: 0;
                background: #162238;
                border: 1px solid #344762;
                border-radius: 5px;
            }
            QLabel#monsterGroupCount {
                color: #9fb0c7;
                font-weight: 700;
            }
            QToolButton#monsterCard {
                background: #111b2b;
                color: #e7edf7;
                border: 1px solid #31425b;
                border-radius: 6px;
                padding: 3px;
                font-weight: 600;
            }
            QToolButton#monsterCard:hover {
                background: #182943;
                border-color: #6a91ca;
            }
            QToolButton#monsterCard:checked {
                background: #173d34;
                color: #a7f3d0;
                border: 3px solid #42d39b;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("怪物图鉴（五列缩略图多选）")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)

        description = QLabel(
            "每个等级目录每行显示5只怪物，图片按滚动可见区域懒加载，直接点击缩略图即可多选。"
            "优先拆单只眼睛、单独嘴巴、单撮头发、单个角等五官，每个特征同时生成原图和镜像。"
            "每只怪物按特征质量生成，最多 12 张，不会为了凑数量加入普通纹理。"
            "生成时还会保存完整原图和水平镜像图，方便手动截图补模板。"
            "模板越多识图耗时越高，建议只选择当前地图怪物。"
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索怪物名称或目录，例如 Pig、level_21-30")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        rescan_button = QPushButton("重新扫描")
        rescan_button.clicked.connect(self._populate_gallery)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(rescan_button)
        layout.addLayout(search_row)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.gallery_content = QWidget()
        self.gallery_layout = QVBoxLayout(self.gallery_content)
        self.gallery_layout.setContentsMargins(2, 2, 5, 2)
        self.gallery_layout.setSpacing(6)
        self.gallery_layout.setAlignment(Qt.AlignTop)
        self.gallery_scroll.setWidget(self.gallery_content)
        self.gallery_scroll.verticalScrollBar().valueChanged.connect(
            self._schedule_lazy_preview_refresh
        )
        self.gallery_scroll.verticalScrollBar().rangeChanged.connect(
            self._schedule_lazy_preview_refresh
        )
        layout.addWidget(self.gallery_scroll, 1)

        selection_row = QHBoxLayout()
        selection_row.setSpacing(6)
        select_visible_button = QPushButton("全选当前显示")
        select_visible_button.clicked.connect(self._select_visible)
        clear_button = QPushButton("清空选择")
        clear_button.clicked.connect(self._clear_selection)
        expand_button = QPushButton("展开全部")
        expand_button.clicked.connect(lambda: self._set_all_expanded(True))
        collapse_button = QPushButton("折叠全部")
        collapse_button.clicked.connect(lambda: self._set_all_expanded(False))
        selection_row.addWidget(select_visible_button)
        selection_row.addWidget(clear_button)
        selection_row.addWidget(expand_button)
        selection_row.addWidget(collapse_button)
        selection_row.addStretch()
        layout.addLayout(selection_row)

        self.selection_status = QLabel()
        self.selection_status.setStyleSheet("color: #9fb0c7;")
        layout.addWidget(self.selection_status)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.result_status = QLabel("尚未生成")
        self.result_status.setWordWrap(True)
        self.generate_button = QPushButton("生成并应用识图模板")
        self.generate_button.setMinimumHeight(34)
        self.generate_button.clicked.connect(self._generate_templates)
        close_button = QPushButton("关闭")
        close_button.setMinimumHeight(34)
        close_button.clicked.connect(self.accept)
        action_row.addWidget(self.result_status, 1)
        action_row.addWidget(self.generate_button)
        action_row.addWidget(close_button)
        layout.addLayout(action_row)

        self._populate_gallery()

    def _clear_gallery_widgets(self) -> None:
        while self.gallery_layout.count():
            item = self.gallery_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._group_sections.clear()

    def _populate_gallery(self) -> None:
        selected_before = {str(path) for path in self.selected_paths()}
        self._clear_gallery_widgets()
        groups = scan_monster_library(self.project_root)
        for index, group in enumerate(groups):
            section = MonsterGroupSection(group, expanded=index == 0)
            for card in section.cards:
                if str(card.entry.path) in selected_before:
                    card.setChecked(True)
            section._sync_group_checkbox()
            section.selection_changed.connect(self._update_selection_status)
            section.expanded_changed.connect(self._schedule_lazy_preview_refresh)
            self._group_sections.append(section)
            self.gallery_layout.addWidget(section)
        self.gallery_layout.addStretch()
        self._apply_filter(self.search_input.text())
        self._update_selection_status()
        self._schedule_lazy_preview_refresh()
        if not groups:
            self.result_status.setText(
                "未找到素材目录：{}".format(monster_library_directory(self.project_root))
            )

    def selected_paths(self) -> tuple[Path, ...]:
        return tuple(
            card.entry.path
            for section in self._group_sections
            for card in section.cards
            if card.isChecked()
        )

    def _apply_filter(self, raw_text: str) -> None:
        query = raw_text.strip().casefold()
        for section in self._group_sections:
            section.apply_filter(query)
        self._schedule_lazy_preview_refresh()

    def _select_visible(self) -> None:
        for section in self._group_sections:
            if section.isVisible():
                section.select_visible()
        self._update_selection_status()

    def _clear_selection(self) -> None:
        for section in self._group_sections:
            section.clear_selection()
        self._update_selection_status()

    def _set_all_expanded(self, expanded: bool) -> None:
        for section in self._group_sections:
            if section.isVisible():
                section.set_expanded(expanded)
        self._schedule_lazy_preview_refresh()

    def _schedule_lazy_preview_refresh(self, *_args) -> None:
        """合并滚动、展开和布局变化，只在下一轮事件循环刷新一次。"""
        if self._lazy_preview_refresh_pending:
            return
        self._lazy_preview_refresh_pending = True
        QTimer.singleShot(0, self._refresh_lazy_previews)

    def _refresh_lazy_previews(self) -> None:
        """仅保留当前视口上下各一屏范围内的怪物缩略图。"""
        self._lazy_preview_refresh_pending = False
        viewport = self.gallery_scroll.viewport()
        viewport_height = max(1, viewport.height())
        margin = viewport_height * MONSTER_PREVIEW_VIEWPORT_MARGIN
        preload_rect = viewport.rect().adjusted(0, -margin, 0, margin)
        for section in self._group_sections:
            section_available = section.isVisible() and section.is_expanded
            for card in section.cards:
                if not section_available or card.isHidden():
                    card.unload_preview()
                    continue
                card_top_left = card.mapTo(viewport, QPoint(0, 0))
                card_rect = QRect(card_top_left, card.size())
                if card_rect.intersects(preload_rect):
                    card.load_preview()
                else:
                    card.unload_preview()

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化后重新计算哪些五列卡片位于可见区域。"""
        super().resizeEvent(event)
        if hasattr(self, "gallery_scroll"):
            self._schedule_lazy_preview_refresh()

    def _update_selection_status(self, *_args) -> None:
        selected = self.selected_paths()
        selected_count = len(selected)
        estimated_count = (
            estimated_template_count(self.project_root, selected) if selected else 0
        )
        self.selection_status.setText(
            "已选择 {} 只怪物；每只最多 {} 张；预计最多生成 guai1.png ～ guai{}.png（共 {} 张）".format(
                selected_count,
                MAX_TEMPLATES_PER_MONSTER,
                estimated_count,
                estimated_count,
            )
            + ("；数量较多，可能明显降低识图速度" if selected_count > 10 else "")
            if selected_count
            else "尚未选择怪物"
        )
        self.generate_button.setEnabled(selected_count > 0)

    def _generate_templates(self) -> None:
        selected = self.selected_paths()
        if not selected:
            QMessageBox.information(self, "请选择怪物", "请至少选择一只怪物。")
            return
        if self.is_automation_running():
            QMessageBox.warning(
                self,
                "请先停止运行",
                "当前刷图任务正在运行。请先停止任务，再替换怪物识图模板。",
            )
            return

        self.generate_button.setEnabled(False)
        self.result_status.setText("正在提取单独特征并生成模板……")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            report = generate_monster_templates(self.project_root, selected)
        except Exception as exc:
            self.result_status.setText("生成失败，原有 guai* 已保留")
            QMessageBox.critical(self, "怪物模板生成失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.generate_button.setEnabled(True)

        self._last_report = report
        self.result_status.setText(
            "已应用：{} 只怪物，共 {} 张模板".format(
                len(report.selected_monsters), report.template_count
            )
        )
        self.templates_generated.emit(report)
        QMessageBox.information(
            self,
            "怪物图鉴模板已应用",
            "已选择 {} 只怪物，每只最多 {} 张，共生成 {} 张。\n"
            "旧的根目录 guai* 已清空并替换；地图子目录未改动。\n\n"
            "模板目录：{}\n"
            "手动截图原图/镜像：{}".format(
                len(report.selected_monsters),
                report.templates_per_monster,
                report.template_count,
                report.output_directory,
                report.source_asset_directory,
            ),
        )


__all__ = ("MonsterAtlasDialog", "MonsterCard", "MonsterGroupSection")

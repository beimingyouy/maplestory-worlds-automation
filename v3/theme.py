APP_STYLE = r"""
QWidget {
    color: #e7edf7;
    background: transparent;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei";
    font-size: 13px;
}
QMainWindow, QWidget#root {
    background: #0b1020;
}
QFrame#header, QFrame#panel, QGroupBox {
    background: #121a2d;
    border: 1px solid #24304a;
    border-radius: 12px;
}
QFrame#header {
    background: #111a31;
}
QLabel#appTitle {
    font-size: 24px;
    font-weight: 700;
    color: #f7f9ff;
}
QLabel#subtitle, QLabel#muted, QLabel#hotkeyText {
    color: #8f9db5;
}
QLabel#versionBadge {
    color: #8be9fd;
    background: #172b3b;
    border: 1px solid #24506a;
    border-radius: 9px;
    padding: 5px 10px;
    font-weight: 700;
}
QLabel#statusBadge {
    border-radius: 10px;
    padding: 7px 12px;
    font-weight: 700;
}
QLabel#statusBadge[state="idle"] {
    color: #b9c4d7;
    background: #202a3e;
    border: 1px solid #34405a;
}
QLabel#statusBadge[state="starting"], QLabel#statusBadge[state="stopping"] {
    color: #ffd580;
    background: #302817;
    border: 1px solid #665125;
}
QLabel#statusBadge[state="running"] {
    color: #78f0b3;
    background: #153226;
    border: 1px solid #246449;
}
QLabel#statusBadge[state="paused"] {
    color: #ffd580;
    background: #302817;
    border: 1px solid #80652c;
}
QLabel#statusBadge[state="error"] {
    color: #ff9b9b;
    background: #351b24;
    border: 1px solid #713243;
}
QGroupBox {
    margin-top: 12px;
    padding: 18px 14px 14px 14px;
    font-weight: 700;
    color: #cbd5e8;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    min-height: 35px;
    padding: 0 10px;
    background: #0d1425;
    border: 1px solid #2a3855;
    border-radius: 8px;
    selection-background-color: #367cf7;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #4b8cff;
}
QComboBox::drop-down {
    width: 28px;
    border: none;
}
QComboBox QAbstractItemView {
    background: #111a2d;
    border: 1px solid #33415f;
    selection-background-color: #2659aa;
    padding: 4px;
}
QCheckBox {
    spacing: 8px;
    min-height: 26px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1px solid #455474;
    border-radius: 5px;
    background: #0d1425;
}
QCheckBox::indicator:checked {
    background: #397ff5;
    border: 1px solid #65a1ff;
}
QPushButton {
    min-height: 38px;
    padding: 0 16px;
    border: 1px solid #344260;
    border-radius: 9px;
    background: #1a2439;
    color: #e9eef8;
    font-weight: 700;
}
QPushButton:hover {
    background: #24314a;
    border-color: #4c5f83;
}
QPushButton:pressed {
    background: #131b2d;
}
QPushButton:disabled {
    color: #647087;
    background: #151c2a;
    border-color: #252e42;
}
QPushButton#primaryButton {
    background: #397ff5;
    border-color: #5b99ff;
    color: white;
}
QPushButton#primaryButton:hover {
    background: #4b8cff;
}
QPushButton#dangerButton {
    background: #a63d55;
    border-color: #d15b74;
    color: white;
}
QPushButton#dangerButton:hover {
    background: #ba4963;
}
QPushButton#pauseButton {
    background: #4b3b16;
    border: 1px solid #8a6b25;
    color: #ffe29a;
    font-weight: 700;
}
QPushButton#pauseButton:hover {
    background: #624d1d;
}
QTextEdit#logView {
    background: #090f1d;
    border: 1px solid #26334c;
    border-radius: 10px;
    color: #cbd5e8;
    padding: 10px;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI";
    font-size: 12px;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollArea QWidget#qt_scrollarea_viewport {
    background: transparent;
}
QWidget#settingsRuntimeContent, QWidget#settingsRecordingPage {
    background: #0f1728;
}
QTabWidget::pane {
    background: #0f1728;
    border: 1px solid #293854;
    border-radius: 9px;
    top: -1px;
}
QTabBar::tab {
    min-height: 30px;
    padding: 0 13px;
    margin-right: 2px;
    color: #9dacC4;
    background: #172136;
    border: 1px solid #2b3956;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabBar::tab:hover {
    color: #d9e4f5;
    background: #1d2a43;
}
QTabBar::tab:selected {
    color: #eef4ff;
    background: #0f1728;
    border-color: #3c5784;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 3px;
}
QScrollBar::handle:vertical {
    background: #35425f;
    min-height: 30px;
    border-radius: 4px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QToolTip {
    color: #f1f5ff;
    background: #172136;
    border: 1px solid #3a4a6b;
    padding: 5px;
}
"""

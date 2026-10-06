"""仿 OBS 设置页的排版原语：分组标题 + 细横线 + 右对齐标签列。

**为什么要单独一层**：OBS 的设置页有个很具体的观感 ——
一条细横线、一个粗体分组标题、然后是「右对齐的标签 + 左对齐的控件」两列表格，
九个页面全部沿用同一套栅格。把它抽成 `SettingsPage` 之后，
每个页面只写「这一页有哪些项」，不再重复算列宽和对齐。

**置灰是这里的硬约束**：本项目的规矩是「做不到的项一律置灰，且必须写明原因」，
所以 `add_row` / `add_full` 都接受 `reason` 参数 —— 传了就自动
禁用控件 + 给标签和控件都挂上悬停说明，不允许出现"点了没反应"的控件。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# 标签列宽与控件最小宽：对齐 OBS 原版的观感（标签列约 190px，控件撑满剩余宽度）
LABEL_COLUMN_WIDTH = 190
CONTROL_MIN_WIDTH = 260

# ---------------------------------------------------------------- 置灰原因
# 措辞统一在这里定义，测试直接断言这些常量，避免各页面各写一句话。
#
# **分类必须经得起核对**：obsws-python 1.8.0 的 reqs.py 里 149 条请求是唯一事实来源，
# 本机 OBS 的 global.ini / basic.ini 用来说明"这项到底存在哪儿"。
# 核对结果是界面上这些项只落在两类，因此**只保留两类原因** ——
# 宁可少一个标签，也不要为了分类好看而把"其实改得了"的项写成"协议没有"：
#
#   * 落在 profile basic.ini 里的 → SetProfileParameter 等请求写得进去 → 未接入；
#   * 落在 global.ini 里、或压根不在配置文件里的（语言、渲染器、色彩格式、
#     源对齐吸附、无障碍配色、进程优先级、电平表衰减……）→ 协议没有任何
#     请求够得着 → 只有 OBS 本机界面能改。
#
# 另外单列"只读对照"，用于客户端能读、只拿来做参照的项（画布分辨率等）。
REASON_REMOTE = "远程控制客户端做不到：这项只有 OBS 本机的设置界面能改"
REASON_NOT_WIRED = "本阶段未接入：协议有办法改（SetProfileParameter 一类），接入前不去动 OBS 配置"
REASON_READONLY = "只读对照：用 OBS 端的实际取值做参照，本客户端不写它"

# 控件类型 → 在置灰时该说什么。复选框/下拉没有"值"，说法要和输入框分开。
_VALUE_WIDGETS = (QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox)


def disabled_tooltip(reason: str) -> str:
    return f"暂不可用：{reason}"


def set_disabled(widget: QWidget, reason: str) -> QWidget:
    """把控件置灰并写明原因（原因同时挂到 tooltip）。

    **容器要连子控件一起挂说明**：置灰会沿父子关系生效，所以父容器
    `setEnabled(False)` 之后里面的按钮也点不动了；但如果只给父容器写 tooltip，
    用户把鼠标停在**里面那个按钮**上时什么都看不到，等于"灰了但没说为什么"。
    这里递归挂一遍，保证每个真正被禁用的控件都能自解释。
    """
    text = disabled_tooltip(reason)
    widget.setEnabled(False)
    widget.setToolTip(text)
    for child in widget.findChildren(QWidget):
        child.setEnabled(False)
        child.setToolTip(text)
    return widget


def dim_placeholder(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("settingsHint")
    label.setWordWrap(True)
    return label


class SettingsPage(QWidget):
    """一个设置页：若干「细横线 + 粗体分组标题 + 表单项」。

    不画页面名 —— OBS 原版右侧内容区并不重复显示页面名（左侧列表已经选中了），
    加上去反而比原版多一行。
    """

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.page_title = title

        self._grid = QGridLayout()
        self._grid.setContentsMargins(14, 10, 14, 10)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(6)
        # 第 0 列（标签）固定宽度，第 1 列（控件）吃掉剩余宽度
        self._grid.setColumnMinimumWidth(0, LABEL_COLUMN_WIDTH)
        self._grid.setColumnStretch(0, 0)
        self._grid.setColumnStretch(1, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(self._grid)
        outer.addStretch(1)

        self._row = 0
        self._sections = 0

    # ---------------------------------------------------------------- 结构
    def add_section(self, title: str | None = None, note: str | None = None) -> None:
        """开一个新分组：先一条细横线，再粗体标题（标题可为空）。

        第一个分组顶上不画横线 —— OBS 里最上面那个分组直接从顶部开始。
        """
        if self._sections > 0:
            self._add_rule()
        self._sections += 1
        if title:
            label = QLabel(title)
            label.setObjectName("settingsSectionTitle")
            self._grid.addWidget(label, self._row, 0, 1, 2)
            self._row += 1
        if note:
            text = dim_placeholder(note)
            self._grid.addWidget(text, self._row, 0, 1, 2)
            self._row += 1

    def _add_rule(self) -> None:
        rule = QFrame()
        rule.setObjectName("settingsRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Plain)
        rule.setFixedHeight(1)
        rule.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 横线上下各留一段空白，贴近 OBS 里"分组之间空一段"的观感
        self._grid.setRowMinimumHeight(self._row, 12)
        self._row += 1
        self._grid.addWidget(rule, self._row, 0, 1, 2)
        self._row += 1
        self._grid.setRowMinimumHeight(self._row, 8)
        self._row += 1

    # ---------------------------------------------------------------- 表单项
    def add_row(self, label: str, control: QWidget, reason: str | None = None) -> QWidget:
        """一行「标签 + 控件」。传 `reason` 则标签与控件一起置灰并挂说明。"""
        text = QLabel(label)
        text.setObjectName("settingsFieldLabel")
        text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if isinstance(control, _VALUE_WIDGETS):
            control.setMinimumWidth(CONTROL_MIN_WIDTH)
        if reason:
            set_disabled(control, reason)
            text.setToolTip(disabled_tooltip(reason))
        self._grid.addWidget(text, self._row, 0)
        self._grid.addWidget(control, self._row, 1)
        self._row += 1
        return control

    def add_full(self, widget: QWidget, reason: str | None = None) -> QWidget:
        """占满整行的控件（复选框、按钮、说明文字），不占标签列。"""
        if reason:
            set_disabled(widget, reason)
        self._grid.addWidget(widget, self._row, 1)
        self._row += 1
        return widget

    def add_pair(self, first: QWidget, second: QWidget,
                 reason: str | None = None) -> tuple[QWidget, QWidget]:
        """标签列放 `first`（通常是复选框），控件列放 `second`。"""
        if reason:
            for widget in (first, second):
                set_disabled(widget, reason)
        self._grid.addWidget(first, self._row, 0,
                             alignment=Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
        self._grid.addWidget(second, self._row, 1)
        self._row += 1
        return first, second

    def add_hint(self, text: str) -> QLabel:
        """整行的灰色说明。"""
        label = dim_placeholder(text)
        self._grid.addWidget(label, self._row, 1)
        self._row += 1
        return label

    # ---------------------------------------------------------------- 收集
    def apply_to(self, config):
        """把本页的改动写回 AppConfig。默认无改动，子类按需覆盖。"""
        return config


# ---------------------------------------------------------------- 常用控件
def checkbox(text: str, checked: bool = False) -> QCheckBox:
    box = QCheckBox(text)
    box.setChecked(bool(checked))
    return box


def combo(options: tuple[tuple[str, object], ...], current: object = None) -> QComboBox:
    """`options` 是 (显示文字, 数据) 序列；按 `current` 选中。"""
    box = QComboBox()
    for label, value in options:
        box.addItem(label, value)
    if current is not None:
        index = box.findData(current)
        if index >= 0:
            box.setCurrentIndex(index)
    return box


def int_spin(minimum: int, maximum: int, value: int, suffix: str = "",
             step: int = 1, special: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    if suffix:
        spin.setSuffix(suffix)
    if special:
        spin.setSpecialValueText(special)
    spin.setValue(int(value))
    return spin


def float_spin(minimum: float, maximum: float, value: float, suffix: str = "",
               decimals: int = 1, step: float = 0.1, special: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    if suffix:
        spin.setSuffix(suffix)
    if special:
        spin.setSpecialValueText(special)
    spin.setValue(float(value))
    return spin


def button(text: str) -> QPushButton:
    return QPushButton(text)


def readonly_line(text: str) -> QLineEdit:
    """只读的一行文本：用来显示 OBS 端的实际取值。"""
    edit = QLineEdit(text)
    edit.setReadOnly(True)
    return edit

"""OBS 风格的面板容器：一条标题栏 + 内容区。

OBS 的每个停靠面板都是「深色标题栏 + 略浅的内容区」，这里统一封装，
避免五个面板各写一遍边框和标题样式。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import theme

TOOL_GLYPHS = ("+", "−", "▲", "▼", "≡")


class DockPanel(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("dockPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("dockTitle")

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 4, 4, 4)
        self.body_layout.setSpacing(3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body, 1)

    # ---------------------------------------------------------------- 内容
    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.body_layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout: QLayout, stretch: int = 0) -> QLayout:
        self.body_layout.addLayout(layout, stretch)
        return layout

    def add_stretch(self) -> None:
        self.body_layout.addStretch(1)


def tool_row(*buttons: QWidget, align_right: bool = False) -> QHBoxLayout:
    """面板底部那一排小方块按钮。"""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    if align_right:
        row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    if not align_right:
        row.addStretch(1)
    return row


def tool_button(
    glyph: str,
    tooltip: str = "",
    enabled: bool = True,
    callback=None,
    checkable: bool = False,
    width: int = 22,
) -> QPushButton:
    button = QPushButton(glyph)
    button.setObjectName("panelToolButton")
    button.setFixedWidth(width)
    button.setCheckable(checkable)
    button.setEnabled(enabled)
    button.setToolTip(tooltip)
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    if callback is not None:
        if checkable:
            button.toggled.connect(callback)
        else:
            button.clicked.connect(callback)
    return button


def placeholder_tool_button(glyph: str, what: str, width: int = 22) -> QPushButton:
    """OBS 里存在、但本阶段没接入的按钮：置灰并说明原因，不留"点了没反应"的坑。"""
    return tool_button(
        glyph,
        f"{what}：MVP 未接入，按钮暂不可用",
        enabled=False,
        width=width,
    )


class DockGrip(QWidget):
    """H8：QDockWidget 的极简标题条。

    DockPanel 自己已经画了标题条，dock 再画一个就重复了。这里只留一条细抓手：
    高度小、不显示文字（浮动时窗口标题会显示名字），但保住"能拖"这个关键能力
    —— QDockWidget 正是靠 titleBarWidget 作为拖拽热区的。

    右键给一个「浮动 / 停靠」菜单，否则用户不知道怎么把它弄出来。
    """

    def __init__(self, label: str, dock, window, parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._dock = dock
        self._window = window
        self.setFixedHeight(4)
        self.setToolTip(f"{label}：拖动可重排；右键可浮动 / 停靠")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def paintEvent(self, event) -> None:  # noqa: N802
        # 一条极淡的横线当"抓手"，比空白更容易发现
        from PySide6.QtGui import QColor, QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.color("HEADER")))
        painter.end()

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        floating = self._dock.isFloating()
        menu.addAction("停靠回主窗口" if floating else "浮动为独立窗口",
                       self._toggle_float)
        menu.addSeparator()
        menu.addAction("隐藏此面板", lambda: self._window.set_panel_visible(self._label, False))
        menu.exec(self.mapToGlobal(pos))

    def _toggle_float(self) -> None:
        self._dock.setFloating(not self._dock.isFloating())
        self._dock.show()


def dim_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    return label


def right_aligned(label: QLabel) -> QLabel:
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label

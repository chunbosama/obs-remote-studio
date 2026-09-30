"""「来源」面板（C1/C2），布局对齐 OBS。

每行右侧是眼睛（可见性，可用）和锁（MVP 未接入，置灰），
与 OBS 一致；难度在于 OBS 用的是图片资源，这里用 QPainter 手绘（见 utils/icons.py）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.state_store import CONNECTED, StateStore
from ...utils import icons
from .dock_panel import DockPanel, placeholder_tool_button, tool_row
from .media_control import MediaControl


class _SourceRow(QWidget):
    """一行来源：名字 + 眼睛 + 锁。"""

    def __init__(self, name: str, enabled: bool, on_toggle, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        label = QLabel(name)
        label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.eye = QToolButton()
        self.eye.setAutoRaise(True)
        self.eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.eye.setIcon(icons.eye_icon(enabled))
        self.eye.setToolTip("显示 / 隐藏该来源")
        self.eye.clicked.connect(on_toggle)

        lock = QToolButton()
        lock.setAutoRaise(True)
        lock.setIcon(icons.lock_icon(False, color="#6a6a6a"))
        lock.setEnabled(False)
        lock.setToolTip("锁定来源：MVP 未接入，按钮暂不可用")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(2)
        layout.addWidget(label, 1)
        layout.addWidget(self.eye)
        layout.addWidget(lock)


class SourcePanel(QWidget):
    def __init__(
        self,
        store: StateStore,
        on_toggle,
        media_callbacks=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.on_toggle = on_toggle
        self.media_callbacks = media_callbacks

        panel = DockPanel("来源")

        # 顶部：OBS 这里是"所选来源 + 设置 + 滤镜"，前两项属编辑类能力
        header = QHBoxLayout()
        header.setSpacing(3)
        current = QLabel("未选择源")
        current.setObjectName("hint")
        header.addWidget(current, 1)
        for text, what in (("设置", "来源属性"), ("滤镜", "滤镜列表")):
            button = placeholder_tool_button(text, what, width=44)
            header.addWidget(button)
        panel.add_layout(header)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        panel.add(self.list_widget, 1)

        # L1/L2：媒体控制条。没有媒体源时自己隐藏（见 MediaControl.refresh）
        self.media_control = None
        if media_callbacks is not None:
            self.media_control = MediaControl(store, media_callbacks)
            panel.add(self.media_control)

        panel.add_layout(
            tool_row(
                placeholder_tool_button("+", "新建来源"),
                placeholder_tool_button("−", "删除来源"),
                placeholder_tool_button("▲", "上移来源"),
                placeholder_tool_button("▼", "下移来源"),
                align_right=True,
            )
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)

        store.scene_items_changed.connect(self.refresh)
        store.current_scene_changed.connect(self._on_scene_changed)
        store.preview_scene_changed.connect(self._on_scene_changed)
        store.studio_changed.connect(self._on_scene_changed)
        store.connection_state_changed.connect(self._on_connection_state)
        self.setEnabled(False)

    def _on_connection_state(self, state: str) -> None:
        self.setEnabled(state == CONNECTED)

    def _on_scene_changed(self, *_args) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.list_widget.clear()
        scene = self.store.preview_scene if (
            self.store.studio_mode and self.store.preview_scene
        ) else self.store.current_scene
        for source in self.store.scene_items:
            item = QListWidgetItem()
            self.list_widget.addItem(item)
            row = _SourceRow(
                source.source_name,
                source.enabled,
                lambda _checked=False, s=source: self.on_toggle(
                    scene, s.item_id, not s.enabled
                ),
            )
            item.setSizeHint(row.sizeHint())
            self.list_widget.setItemWidget(item, row)

"""「转场动画」面板（G1/G2），布局对齐 OBS：转场下拉 + 时长 + 一排小按钮。

数据源是 StateStore 的转场字段，与预览区中间那一列共用，
两边都从 store 信号更新，因此不会出现两处显示不一致。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QWidget,
)

from ...core.state_store import CONNECTED, StateStore
from .dock_panel import DockPanel, placeholder_tool_button, tool_row


class TransitionsPanel(DockPanel):
    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__("转场动画", parent)
        self.store = store
        self.callbacks = callbacks  # transition / duration

        self.combo = QComboBox()
        self.combo.activated.connect(self._on_picked)
        self.add(self.combo)

        duration_row = QHBoxLayout()
        duration_row.setSpacing(4)
        duration_row.addWidget(QLabel("时长"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(50, 20000)
        self.duration_spin.setSingleStep(50)
        self.duration_spin.setSuffix(" ms")
        self.duration_spin.editingFinished.connect(self._on_duration_edited)
        duration_row.addWidget(self.duration_spin, 1)
        self.add_layout(duration_row)

        self.add_layout(
            tool_row(
                placeholder_tool_button("+", "添加转场"),
                placeholder_tool_button("−", "删除转场"),
                placeholder_tool_button("≡", "转场列表设置"),
                align_right=True,
            )
        )
        self.add_stretch()

        self._duration_timer = QTimer(self)
        self._duration_timer.setSingleShot(True)
        self._duration_timer.setInterval(400)
        self._duration_timer.timeout.connect(self._commit_duration)

        store.transitions_changed.connect(self._update_transitions)
        store.transition_changed.connect(self._update_from_store)
        store.capabilities_changed.connect(self._update_from_store)
        store.connection_state_changed.connect(self._on_connection_state)

        self.setEnabled(False)

    # ---------------------------------------------------------------- 更新
    def _on_connection_state(self, state: str) -> None:
        self.setEnabled(state == CONNECTED)

    def _update_transitions(self) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(self.store.transitions)
        self.combo.blockSignals(False)
        self._update_from_store()

    def _update_from_store(self) -> None:
        index = self.combo.findText(self.store.current_transition)
        if index >= 0:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(index)
            self.combo.blockSignals(False)

        supported = self.store.supports("SetCurrentSceneTransitionDuration")
        configurable = self.store.transition_configurable
        self.duration_spin.blockSignals(True)
        self.duration_spin.setValue(int(self.store.transition_duration_ms))
        self.duration_spin.blockSignals(False)
        self.duration_spin.setEnabled(supported and configurable)
        self.duration_spin.setToolTip(
            "" if supported and configurable
            else ("该转场不支持修改时长" if not configurable else "服务端不支持设置转场时长")
        )

    # ---------------------------------------------------------------- 交互
    def _on_picked(self, index: int) -> None:
        name = self.combo.itemText(index)
        if name:
            self.callbacks["transition"](name)

    def _on_duration_edited(self) -> None:
        self._duration_timer.start()

    def _commit_duration(self) -> None:
        self.callbacks["duration"](self.duration_spin.value())

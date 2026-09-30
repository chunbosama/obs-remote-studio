"""系统托盘（H6）。

- 托盘菜单里能直接开始/停止录制与直播、切换工作室模式；
- 点图标唤出/隐藏主窗口；
- 窗口隐藏时，录制/直播状态变化用气泡提示（窗口开着就不打扰）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.state_store import CONNECTED, StateStore
from ..utils import icons


class TrayIcon(QSystemTrayIcon):
    def __init__(self, store: StateStore, callbacks, parent=None):
        super().__init__(icons.app_icon(), parent)
        self.store = store
        self.callbacks = callbacks  # toggle_window / toggle_record / toggle_stream / studio_mode / quit

        menu = QMenu()
        self.show_action = menu.addAction("显示主窗口", self.callbacks["toggle_window"])
        menu.addSeparator()
        self.record_action = menu.addAction("开始录制", self.callbacks["toggle_record"])
        self.stream_action = menu.addAction("开始直播", self.callbacks["toggle_stream"])
        self.studio_action = menu.addAction("工作室模式", self._on_studio_clicked)
        self.studio_action.setCheckable(True)
        menu.addSeparator()
        menu.addAction("退出", self.callbacks["quit"])
        self.setContextMenu(menu)
        self._menu = menu

        self.activated.connect(self._on_activated)
        store.record_changed.connect(self._update_actions)
        store.stream_changed.connect(self._update_actions)
        store.studio_changed.connect(self._update_actions)
        store.connection_state_changed.connect(self._update_actions)
        self._update_actions()

    # ---------------------------------------------------------------- 交互
    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.callbacks["toggle_window"]()

    def _on_studio_clicked(self, checked: bool) -> None:
        self.callbacks["studio_mode"](checked)

    def set_window_visible(self, visible: bool) -> None:
        self.show_action.setText("隐藏主窗口" if visible else "显示主窗口")

    # ---------------------------------------------------------------- 更新
    def _update_actions(self) -> None:
        record = self.store.record
        self.record_action.setText("停止录制" if record.active else "开始录制")
        stream = self.store.stream
        self.stream_action.setText("停止直播" if stream.active else "开始直播")

        self.studio_action.blockSignals(True)
        self.studio_action.setChecked(self.store.studio_mode)
        self.studio_action.blockSignals(False)

        connected = self.store.connection_state == CONNECTED
        self.record_action.setEnabled(connected)
        self.stream_action.setEnabled(connected)
        self.studio_action.setEnabled(connected)
        self.setToolTip(
            f"OBS Remote Studio — {self.store.status_message or '未连接'}"
        )

    def notify(self, title: str, body: str) -> None:
        if QSystemTrayIcon.supportsMessages():
            self.showMessage(title, body, icons.app_icon(), 3000)

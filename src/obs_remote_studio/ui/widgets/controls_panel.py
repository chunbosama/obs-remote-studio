"""「控制按钮」面板（D1-D4 / D6-D8 / G3），布局对齐 OBS：一列竖直大按钮。

OBS 里依次是：开始直播 / 开始录制 / 启动虚拟摄像机 / 工作室模式 / 设置。
本项目在此基础上补了 OBS 也有、但 MVP 先搁置的两项：
- D6「暂停录制」：紧跟在录制按钮下，只在录制中且服务端支持时可用；
- D7「回放缓冲」：开/关 + 保存，服务端不支持时整组隐藏。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from ...core import protocol as P
from ...core.state_store import CONNECTED, StateStore
from ...utils import icons
from .dock_panel import DockPanel


class ControlsPanel(DockPanel):
    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__("控制按钮", parent)
        self.store = store
        self.callbacks = callbacks
        # toggle_stream / toggle_record / pause_record / virtualcam /
        # replay_toggle / replay_save / pause_supported / studio_mode / settings

        self.stream_btn = self._button("开始直播", icons.dot_icon(), self.callbacks["toggle_stream"])
        self.record_btn = self._button("开始录制", icons.dot_icon(), self.callbacks["toggle_record"])

        # D6：暂停录制。不做成大按钮，免得抢了「停止录制」的位置
        self.pause_btn = self._button("暂停录制", None, self.callbacks["pause_record"])
        self.pause_btn.setObjectName("miniButton")

        # D8：虚拟摄像机
        self.virtual_cam_btn = self._button(
            "启动虚拟摄像机", icons.dot_icon(), self.callbacks["virtualcam"]
        )

        # D7：回放缓冲（一行两个：开关 + 保存）
        self.replay_btn = self._button("开启回放缓冲", None, self.callbacks["replay_toggle"])
        self.replay_btn.setObjectName("miniButton")
        self.save_replay_btn = self._button("保存回放", None, self.callbacks["replay_save"])
        self.save_replay_btn.setObjectName("miniButton")
        replay_row = QHBoxLayout()
        replay_row.setContentsMargins(0, 0, 0, 0)
        replay_row.setSpacing(3)
        replay_row.addWidget(self.replay_btn, 1)
        replay_row.addWidget(self.save_replay_btn)
        self.add_layout(replay_row)

        self.studio_btn = self._button("工作室模式", None, None, checkable=True)
        self.studio_btn.setObjectName("studioModeButton")
        self.studio_btn.toggled.connect(self._on_studio_toggled)

        self.settings_btn = self._button("设置", None, None)
        self.settings_btn.clicked.connect(self.callbacks["settings"])

        self.add_stretch()

        store.record_changed.connect(self._update_buttons)
        store.stream_changed.connect(self._update_buttons)
        store.studio_changed.connect(self._update_buttons)
        store.virtualcam_changed.connect(self._update_buttons)
        store.replay_buffer_changed.connect(self._update_buttons)
        store.server_info_changed.connect(self._update_buttons)
        store.capabilities_changed.connect(self._update_buttons)
        store.connection_state_changed.connect(self._on_connection_state)
        # 注意：这里不能整体 setEnabled(False)，否则「设置」在未连接时也会被父级连带禁用
        self._on_connection_state(store.connection_state)

    def _button(self, text: str, icon, callback, checkable: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("controlButton")
        button.setCheckable(checkable)
        if icon is not None:
            button.setIcon(icon)
        if callback is not None:
            button.clicked.connect(callback)
        self.add(button)
        return button

    def _on_connection_state(self, state: str) -> None:
        self._connected = state == CONNECTED
        self._update_buttons()

    def refresh_icons(self) -> None:
        """换主题时要重画（图标是手绘像素图，颜色烧在里头）。"""
        self.stream_btn.setIcon(icons.dot_icon())
        self.record_btn.setIcon(icons.dot_icon())
        self.virtual_cam_btn.setIcon(icons.dot_icon())
        self._update_buttons()

    def _on_studio_toggled(self, checked: bool) -> None:
        self.callbacks["studio_mode"](checked)

    # ---------------------------------------------------------------- 状态
    def _update_buttons(self) -> None:
        connected = getattr(self, "_connected", False)
        record = self.store.record
        stream = self.store.stream

        self.record_btn.setText("停止录制" if record.active else "开始录制")
        self.stream_btn.setText("停止直播" if stream.active else "开始直播")

        # D6：暂停/继续只在录制中有意义；服务端不支持时（老 OBS）置灰并说明原因
        pause_supported = self.callbacks["pause_supported"]()
        self.pause_btn.setVisible(connected and (record.active or pause_supported))
        self.pause_btn.setEnabled(connected and record.active and pause_supported)
        self.pause_btn.setText("继续录制" if record.paused else "暂停录制")
        self.pause_btn.setToolTip(
            "暂停 / 继续录制（需要 OBS 30+ / obs-websocket 5.1+）"
            if pause_supported
            else "录制暂停：当前 OBS 版本不支持，按钮暂不可用"
        )

        # D8：虚拟摄像机。OBS 未装虚拟摄像机驱动时，能力列表里根本没有这几条请求；
        # 也可能是驱动在但资源当前不可用（604），同样收起来。
        virtualcam_supported = (
            connected
            and self.store.supports(P.REQ_GET_VIRTUALCAM_STATUS)
            and not self.store.is_unavailable(P.REQ_GET_VIRTUALCAM_STATUS)
        )
        self.virtual_cam_btn.setVisible(virtualcam_supported)
        self.virtual_cam_btn.setEnabled(virtualcam_supported)
        self.virtual_cam_btn.setText(
            "停止虚拟摄像机" if self.store.virtualcam.active else "启动虚拟摄像机"
        )

        # D7：回放缓冲。请求名合法但服务端回 604（这台机器没配回放缓冲）时一并收起来，
        # 免得按钮看着能用、点了却毫无反应。
        replay_supported = (
            connected
            and self.store.supports(P.REQ_GET_REPLAY_BUFFER_STATUS)
            and not self.store.is_unavailable(P.REQ_GET_REPLAY_BUFFER_STATUS)
        )
        self.replay_btn.setVisible(replay_supported)
        self.save_replay_btn.setVisible(replay_supported)
        replay_active = self.store.replay_buffer.active
        self.replay_btn.setEnabled(replay_supported)
        self.replay_btn.setText("关闭回放缓冲" if replay_active else "开启回放缓冲")
        self.save_replay_btn.setEnabled(replay_supported and replay_active)
        self.save_replay_btn.setToolTip(
            "把缓冲区里的最后一段立刻写盘"
            if replay_active
            else "回放缓冲未开启，没有可保存的内容"
        )

        self.stream_btn.setEnabled(connected)
        self.record_btn.setEnabled(connected)
        self.studio_btn.setEnabled(connected)
        self.settings_btn.setEnabled(True)  # 设置不依赖连接

        self.studio_btn.blockSignals(True)
        self.studio_btn.setChecked(self.store.studio_mode)
        self.studio_btn.blockSignals(False)

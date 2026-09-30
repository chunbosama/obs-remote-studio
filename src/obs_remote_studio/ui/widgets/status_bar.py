"""状态栏（H3 / A4 / A11 / D17 / H9），布局对齐 OBS：

    ● 已连接 127.0.0.1:4455 │ ▂▄▆█ │ 延迟 3 ms │ ⚠ 录制目录仅剩 1.2 GB
    │ ○ 录制 --:--:-- │ ○ 直播 --:--:-- │ CPU: 2.9% 30.00 / 30.00 FPS │ OBS 31.0

OBS 靠图标区分直播与录制计时，这里用「时钟图标 + 文字」保留可读性；
连接状态是我们比 OBS 多出来的信息，放在最左侧。

H9：段越来越多（RTT、录制预警…），右键可勾选显示哪些段，选择持久化。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QMenu,
    QStatusBar,
    QWidget,
)

from ...core.state_store import CONNECTED, CONNECTING, RECONNECTING, StateStore
from ...utils import icons
from ...utils.dpi import icon_pixmap
from .. import theme
from ...utils.formatting import dropped_ratio, format_duration, format_fps

def _state_text() -> dict[str, tuple[str, str]]:
    """状态文字与颜色（跟随主题）。"""
    return {
        CONNECTING: ("连接中", theme.color("YELLOW")),
        RECONNECTING: ("重连中", theme.color("YELLOW")),
        CONNECTED: ("已连接", theme.color("GREEN")),
    }
DOT = "●"

# H9：段的定义（key, 菜单名, 默认是否显示）。
# 顺序即状态栏从左到右/从右到左的排布顺序。
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("conn", "连接状态"),
    ("target", "地址与消息"),
    ("signal", "推流信号"),
    ("rtt", "网络延迟"),
    ("warning", "录制预警"),
    ("record", "录制时长"),
    ("stream", "直播时长"),
    ("perf", "性能指标"),
    ("version", "OBS 版本"),
)
SEGMENT_LABELS = dict(SEGMENTS)
# 这几个是"左侧"段，其余走 permanente 区（靠右）
LEFT_SEGMENTS = ("conn", "target", "signal")


class StatusBar(QStatusBar):
    def __init__(self, store: StateStore, callbacks=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks or {}
        self._visible: set[str] = set()

        self.conn_label = QLabel(f"{DOT} 未连接")
        self.target_label = QLabel("")
        self.target_label.setObjectName("hint")
        self.signal_label = QLabel()
        self.signal_label.setToolTip("推流信号强度")
        self.rtt_label = QLabel("延迟 --")
        self.rtt_label.setToolTip("与 OBS 的往返延迟（心跳探测）")
        self.warning_label = QLabel("")
        self.warning_label.setVisible(False)
        self.record_label = QLabel("录制 --:--:--")
        self.stream_label = QLabel("直播 --:--:--")
        self.perf_label = QLabel("CPU: --    -- / -- FPS")
        self.perf_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.version_label = QLabel("")
        self.version_label.setObjectName("hint")

        for label in (self.record_label, self.stream_label):
            label.setContentsMargins(0, 0, 0, 0)

        self._widgets: dict[str, QWidget] = {
            "conn": self.conn_label,
            "target": self.target_label,
            "signal": self.signal_label,
            "rtt": self.rtt_label,
            "warning": self.warning_label,
            "record": self.record_label,
            "stream": self.stream_label,
            "perf": self.perf_label,
            "version": self.version_label,
        }
        self._separators: list[QFrame] = []

        # H9：右键勾选显示哪些段
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_segment_menu)

        store.connection_state_changed.connect(self._update_connection)
        store.status_message_changed.connect(self._update_message)
        store.server_info_changed.connect(self._update_version)
        store.record_changed.connect(self._update_record)
        store.stream_changed.connect(self._update_stream)
        store.stats_changed.connect(self._update_stats)
        store.video_changed.connect(self._update_stats)
        store.health_changed.connect(self._update_health)
        store.record_warning_changed.connect(self._update_warning)

        self.apply_segments()
        self._update_signal()
        self._update_warning()
        self._update_health()

    # ---------------------------------------------------------------- H9：段管理
    def apply_segments(self) -> None:
        """按配置重建排布。空集合 = 全显示（首次运行不写死默认值）。"""
        configured = self.callbacks.get("segments")
        names = configured() if callable(configured) else None
        self._visible = set(names) if names else {key for key, _ in SEGMENTS}
        self._rebuild()

    def visible_segments(self) -> list[str]:
        return [key for key, _ in SEGMENTS if key in self._visible]

    def set_segment_visible(self, key: str, visible: bool) -> None:
        if visible:
            self._visible.add(key)
        else:
            self._visible.discard(key)
        self._rebuild()
        notify = self.callbacks.get("segments_changed")
        if callable(notify):
            notify(self.visible_segments())

    def _rebuild(self) -> None:
        for widget in self._widgets.values():
            self.removeWidget(widget)
        for line in self._separators:
            self.removeWidget(line)
        self._separators.clear()

        shown = [key for key, _ in SEGMENTS if key in self._visible]
        left = [key for key in shown if key in LEFT_SEGMENTS]
        right = [key for key in shown if key not in LEFT_SEGMENTS]

        for index, key in enumerate(left):
            if index:
                line = self._separator()
                self._separators.append(line)
                self.addWidget(line)
            widget = self._widgets[key]
            self.addWidget(widget, 1 if key == "target" else 0)
        for key in right:
            line = self._separator()
            self._separators.append(line)
            self.addPermanentWidget(line)
            self.addPermanentWidget(self._widgets[key])
        # 预警段自己控制可见性，别让 _rebuild 把它又露出来
        self._update_warning()

    def _show_segment_menu(self, pos) -> None:
        menu = QMenu(self)
        for key, label in SEGMENTS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key in self._visible)
            action.toggled.connect(
                lambda checked, k=key: self.set_segment_visible(k, checked)
            )
        menu.addSeparator()
        menu.addAction("全部显示", lambda: self.set_segment_visible_all(True))
        menu.exec(self.mapToGlobal(pos))

    def set_segment_visible_all(self, visible: bool) -> None:
        self._visible = {key for key, _ in SEGMENTS} if visible else set()
        self._rebuild()
        notify = self.callbacks.get("segments_changed")
        if callable(notify):
            notify(self.visible_segments())

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setStyleSheet(f"color: {theme.color('BORDER')};")
        return line

    # ---------------------------------------------------------------- 更新
    def _update_connection(self, state: str) -> None:
        text, color = _state_text().get(state, ("未连接", theme.color("TEXT_DIM")))
        self.conn_label.setText(f"{DOT} {text}")
        self.conn_label.setStyleSheet(f"color: {color};")
        self.target_label.setText(self.store.status_message)

    def _update_message(self, message: str) -> None:
        self.target_label.setText(message)

    def _update_version(self, info) -> None:
        self.version_label.setText(info.label())

    def refresh_icons(self) -> None:
        """换主题后重画信号格与分隔线。"""
        for line in self._separators:
            line.setStyleSheet(f"color: {theme.color('BORDER')};")
        self._update_signal()

    def _update_signal(self) -> None:
        stream = self.store.stream
        if not stream.active:
            level, color = 0, theme.color("TEXT_DISABLED")
        elif stream.reconnecting:
            level, color = 1, theme.color("YELLOW")
        elif stream.congestion > 0.2:
            level, color = 2, theme.color("YELLOW")
        else:
            level, color = 4, theme.color("GREEN")
        self.signal_label.setPixmap(
            icon_pixmap(icons.signal_icon(level, color=color), 16, self)
        )
        self.signal_label.setToolTip(
            "未推流" if not stream.active
            else ("推流重连中" if stream.reconnecting else "推流中")
        )

    # ---- A11：延迟 ----
    def _update_health(self) -> None:
        health = self.store.health
        self.rtt_label.setText(health.label)
        warn = self.callbacks.get("rtt_warn_ms")
        threshold = warn() if callable(warn) else 500
        if health.samples == 0:
            color = theme.color("TEXT_DIM")
        elif health.stalled:
            color = theme.color("RED")
        elif health.rtt_ms >= threshold:
            color = theme.color("YELLOW")
        else:
            color = theme.color("GREEN")
        self.rtt_label.setStyleSheet(f"color: {color};")
        if health.stalled:
            self.rtt_label.setToolTip("与 OBS 通信明显变慢（连续多次超阈值），界面操作可能迟滞")
        else:
            self.rtt_label.setToolTip("与 OBS 的往返延迟（心跳探测）")

    # ---- D17：录制预警 ----
    def _update_warning(self) -> None:
        warning = self.store.record_warning
        if not warning.message:
            self.warning_label.setVisible(False)
            self.warning_label.setText("")
            return
        icon = "⚠"
        color = theme.color("RED") if warning.level == "danger" else theme.color("YELLOW")
        self.warning_label.setText(f"{icon} {warning.message}")
        self.warning_label.setStyleSheet(f"color: {color};")
        self.warning_label.setToolTip(
            warning.message
            + (f"\n剩余空间 {warning.free_gb:.1f} GB" if warning.free_gb is not None else "")
        )
        self.warning_label.setVisible("warning" in self._visible)

    def _update_record(self) -> None:
        record = self.store.record
        mark = DOT if record.active else "○"
        suffix = "（暂停）" if record.paused else ""
        self.record_label.setText(f"{mark} 录制 {format_duration(record.duration_ms)}{suffix}")
        self.record_label.setStyleSheet(
            f"color: {theme.color('RED')};" if record.active else f"color: {theme.color('TEXT_DIM')};"
        )

    def _update_stream(self) -> None:
        stream = self.store.stream
        mark = DOT if stream.active else "○"
        self.stream_label.setText(f"{mark} 直播 {format_duration(stream.duration_ms)}")
        self.stream_label.setStyleSheet(
            f"color: {theme.color('RED')};" if stream.active else f"color: {theme.color('TEXT_DIM')};"
        )
        self._update_signal()

    def _update_stats(self) -> None:
        stats = self.store.stats
        expected = self.store.video.fps
        self.perf_label.setText(
            f"CPU: {stats.cpu_percent:.1f}%    "
            f"{format_fps(stats.fps)} / {format_fps(expected)} FPS"
        )
        self.perf_label.setToolTip(
            f"丢帧 {dropped_ratio(stats.output_skipped_frames, stats.output_total_frames)}"
            f"（{stats.output_skipped_frames} / {stats.output_total_frames}）"
            f"   渲染丢帧 {stats.render_skipped_frames} / {stats.render_total_frames}"
            f"   内存 {stats.memory_mb:.0f} MB"
        )

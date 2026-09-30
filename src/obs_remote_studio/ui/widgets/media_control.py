"""L1/L2：媒体源播放控制条。

放在「来源」面板底部 —— 媒体源本身就是场景来源，控件贴着来源列表最自然。
**没有任何媒体源时整条隐藏**，不给纯摄像头/游戏采集的用户添乱。

    [媒体源 ▼]  ⏮ ▶ ⏸ ⏹ ↻   00:12 / 03:00  ──────●──────

要点：
- 状态来自 `GetMediaInputStatus` 低频轮询 + 播放事件校正；
- 进度条拖动时才 seek，松手提交（节流复用推子那套）；
- VLC 源刚开播时 `mediaDuration` 可能是 0，要容忍 —— 此时进度条置灰不可拖。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QWidget,
)

from ...core import protocol as P
from ...utils.formatting import format_duration


class MediaControl(QWidget):
    def __init__(self, store, callbacks, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks
        self._current = ""
        self._seeking = False
        self._applying = False

        self.source_combo = QComboBox()
        self.source_combo.setToolTip("选择要控制的媒体源")
        self.source_combo.activated.connect(self._on_source_picked)

        self.prev_button = self._transport("⏮", "上一个（播放列表）", P.MEDIA_ACTION_PREVIOUS)
        self.play_button = self._transport("▶", "播放 / 继续", P.MEDIA_ACTION_PLAY)
        self.pause_button = self._transport("⏸", "暂停", P.MEDIA_ACTION_PAUSE)
        self.stop_button = self._transport("⏹", "停止", P.MEDIA_ACTION_STOP)
        self.restart_button = self._transport("↻", "从头重播", P.MEDIA_ACTION_RESTART)
        self.next_button = self._transport("⏭", "下一个（播放列表）", P.MEDIA_ACTION_NEXT)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(2)
        top.addWidget(self.source_combo, 1)
        for button in (
            self.prev_button,
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.restart_button,
            self.next_button,
        ):
            top.addWidget(button)

        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setToolTip("拖动跳转（松手生效）")
        self.progress.sliderPressed.connect(self._on_seek_start)
        self.progress.sliderReleased.connect(self._on_seek_end)
        self.progress.valueChanged.connect(self._on_progress_moved)
        self.progress.setSingleStep(10)

        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("hint")
        self.time_label.setMinimumWidth(96)
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(4)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.time_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)
        # 竖着排：上行是选源 + 传输键，下行是进度
        from PySide6.QtWidgets import QVBoxLayout

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(top)
        column.addLayout(bottom)
        layout.addLayout(column)

        store.media_changed.connect(self.refresh)
        store.capabilities_changed.connect(self.refresh)
        store.connection_state_changed.connect(lambda _state: self.refresh())
        self._connect_transport()
        self.refresh()

    @staticmethod
    def _transport(glyph: str, tooltip: str, action: str) -> QToolButton:
        button = QToolButton()
        button.setText(glyph)
        button.setAutoRaise(True)
        button.setToolTip(tooltip)
        button.setProperty("mediaAction", action)
        return button

    def _connect_transport(self) -> None:
        """只接一次。

        回调里读的是 `self._current`（点击那一刻的值），所以切源不需要重连信号。
        反复 disconnect/connect 会刷出 PySide6 的 RuntimeWarning。
        """
        for button in (
            self.prev_button,
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.restart_button,
            self.next_button,
        ):
            action = button.property("mediaAction")
            button.clicked.connect(
                lambda _checked=False, a=action: self.callbacks["media_action"](
                    self._current, a
                )
            )

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        items = self.store.media
        self.setVisible(bool(items))
        if not items:
            return

        names = [item.name for item in items]
        if self._current not in names:
            self._current = names[0]
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItems(names)
        self.source_combo.setCurrentText(self._current)
        self.source_combo.blockSignals(False)

        status = self.store.find_media(self._current)
        if status is None:
            return
        supported = self.store.supports(P.REQ_TRIGGER_MEDIA_INPUT_ACTION)

        for button in (
            self.prev_button,
            self.play_button,
            self.pause_button,
            self.stop_button,
            self.restart_button,
            self.next_button,
        ):
            button.setEnabled(supported)

        self.play_button.setEnabled(supported and not status.playing)
        self.pause_button.setEnabled(supported and status.playing)
        self.stop_button.setEnabled(supported and status.state not in (
            P.MEDIA_STATE_STOPPED, P.MEDIA_STATE_NONE, P.MEDIA_STATE_ENDED,
        ))

        has_duration = status.duration_ms > 0
        self.progress.setEnabled(has_duration)
        self.progress.setToolTip(
            "拖动跳转（松手生效）" if has_duration
            else "拿不到时长（VLC 源刚开播时常见），暂时不能跳转"
        )
        if not self._seeking:
            self._apply_position(status)

    def _apply_position(self, status) -> None:
        self._applying = True
        if status.duration_ms > 0:
            ratio = max(0.0, min(status.cursor_ms / status.duration_ms, 1.0))
            self.progress.setValue(int(round(ratio * 1000)))
        else:
            self.progress.setValue(0)
        self._applying = False
        self.time_label.setText(
            f"{format_duration(status.cursor_ms)} / "
            f"{format_duration(status.duration_ms) if status.duration_ms else '--:--'}"
            f"  ·  {P.MEDIA_STATE_LABELS.get(status.state, status.state)}"
        )

    # ---------------------------------------------------------------- 交互
    def _on_source_picked(self, index: int) -> None:
        name = self.source_combo.itemText(index)
        if name:
            self._current = name
            self.refresh()

    def _on_seek_start(self) -> None:
        self._seeking = True

    def _on_progress_moved(self, value: int) -> None:
        if self._applying or not self._seeking:
            return
        # 拖动中只更新文字，不 seek（松手才提交，避免滑一下打几十个请求）
        status = self.store.find_media(self._current)
        if status is None or status.duration_ms <= 0:
            return
        cursor = int(status.duration_ms * value / 1000)
        self.time_label.setText(
            f"{format_duration(cursor)} / {format_duration(status.duration_ms)}"
            f"  ·  {P.MEDIA_STATE_LABELS.get(status.state, status.state)}"
        )

    def _on_seek_end(self) -> None:
        self._seeking = False
        status = self.store.find_media(self._current)
        if status is None or status.duration_ms <= 0:
            return
        cursor = int(status.duration_ms * self.progress.value() / 1000)
        self.callbacks["media_seek"](self._current, cursor)

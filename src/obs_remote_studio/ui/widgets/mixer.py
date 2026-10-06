"""「混音器」面板（E 模块），外观与交互对齐 OBS。

每条音频源一行：源名 + 音量值、音量推子（非线性，0 dB 在 3/4 处）、
电平表（含峰值保持）、静音按钮、⋮ 菜单（高级音频属性 / 隐藏该源）。

要点：
- 拖动时只改本地显示（commit=False），松手或停手 150ms 后才发请求，避免刷请求；
- 电平表是高频事件（~20Hz 以上），控件按自己的 50ms 节奏读 store.meters，不发信号；
- 峰值保持 1.5 秒后回落，与 OBS 手感一致。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import protocol as P
from ...core.audio import (
    SLIDER_MAX,
    db_to_fader,
    fader_to_db,
    format_db,
    format_percent,
    mul_to_db,
)
from ...core.models import AudioInput
from ...core.state_store import CONNECTED, StateStore
from ...utils import icons
from ...utils.dpi import device_ratio, snap
from .. import theme
from .dock_panel import DockPanel, dim_label

METER_INTERVAL_MS = 50        # 电平表刷新节奏
PEAK_HOLD_MS = 1500           # 峰值保持时长
COMMIT_DELAY_MS = 150         # 停手多久后提交音量

# E8：键盘微调步长。默认 0.1 dB（够细），按住 Shift 变 1 dB（调得快）
FINE_STEP_DB = 0.1
COARSE_STEP_DB = 1.0


class _FaderSlider(QSlider):
    """E8：方向键按 **dB** 微调，而不是滑块自己的"1 格"。

    滑块刻度是非线性的（0 dB 在 3/4 处），所以直接走 QSlider 的默认步进
    会得到忽大忽小的 dB 变化。这里把方向键拦下来，改成按 dB 步进，
    再换算回滑块值 —— 手感才和 OBS 一致。
    """

    step_requested = Signal(float)   # dB 增量
    reset_requested = Signal()
    mute_requested = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        step = COARSE_STEP_DB if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else FINE_STEP_DB
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self.step_requested.emit(-step)
            event.accept()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self.step_requested.emit(step)
            event.accept()
            return
        if key == Qt.Key.Key_Delete:
            self.mute_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # 与 OBS/DAW 的习惯一致：双击回到 0 dB
        self.reset_requested.emit()
        event.accept()


class _LevelMeter(QWidget):
    """竖直电平条：绿→黄→红 + 峰值保持。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(7)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._level = 0.0
        self._peak = 0.0
        self._peak_at = 0.0

    def set_level(self, value: float, now_ms: float) -> None:
        value = max(0.0, min(value, 1.5))
        self._level = value
        if value >= self._peak:
            self._peak = value
            self._peak_at = now_ms
        elif now_ms - self._peak_at > PEAK_HOLD_MS:
            self._peak = max(value, self._peak - 0.03)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.color("METER_BG")))

        gradient = QLinearGradient(0, rect.height(), 0, 0)
        gradient.setColorAt(0.0, QColor(theme.color("METER_LOW")))
        gradient.setColorAt(0.75, QColor(theme.color("METER_MID")))
        gradient.setColorAt(1.0, QColor(theme.color("METER_HIGH")))

        # 电平高度按推子的非线性刻度走，视觉上和推子刻度对齐
        position = db_to_fader(mul_to_db(self._level))
        # O2：按物理像素取整，分数缩放下电平条的顶边才不会忽明忽暗
        ratio = device_ratio(self)
        height = int(snap(rect.height() * position, ratio))
        if height > 0:
            painter.fillRect(0, rect.height() - height, rect.width(), height, gradient)

        if self._peak > 0.001:
            peak_position = db_to_fader(mul_to_db(self._peak))
            y = rect.height() - int(snap(rect.height() * peak_position, ratio))
            painter.fillRect(0, max(0, y - 1), rect.width(), 2, QColor(theme.color("METER_PEAK")))


class _FaderRow(QWidget):
    """一条音频源。"""

    volume_preview = Signal(str, float)   # 拖动中（不发请求）
    volume_committed = Signal(str, float)  # 松手 / 停手
    mute_toggled = Signal(str)
    hide_requested = Signal(str)
    advanced_requested = Signal(str)

    def __init__(self, item: AudioInput, show_percent: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.name = item.name
        self._show_percent = show_percent

        self.name_label = QLabel(item.name)
        self.value_label = QLabel("0.0 dB")
        self.value_label.setObjectName("faderValue")
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        top.addWidget(self.name_label, 1)
        top.addWidget(self.value_label)

        self.fader = _FaderSlider(Qt.Orientation.Horizontal)
        self.fader.setRange(0, SLIDER_MAX)
        self.fader.setToolTip(
            "拖动调节音量，松手后生效\n"
            "方向键 ±0.1 dB（按住 Shift 为 ±1 dB）\n"
            "双击回到 0 dB　Delete 切换静音"
        )
        self.fader.valueChanged.connect(self._on_fader_moved)
        self.fader.sliderReleased.connect(self._commit_now)
        # E8：键盘微调 / 双击复位 / Delete 静音
        self.fader.step_requested.connect(self._nudge_db)
        self.fader.reset_requested.connect(self.reset_to_zero_db)
        self.fader.mute_requested.connect(lambda: self.mute_toggled.emit(self.name))

        self.meter = _LevelMeter()

        self.mute_button = QToolButton()
        self.mute_button.setCheckable(True)
        self.mute_button.setAutoRaise(True)
        self.mute_button.setToolTip("静音 / 取消静音")
        self.mute_button.clicked.connect(lambda: self.mute_toggled.emit(self.name))

        self.menu_button = QToolButton()
        self.menu_button.setText("⋮")
        self.menu_button.setAutoRaise(True)
        self.menu_button.setToolTip("更多")
        self.menu_button.clicked.connect(self._show_menu)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(4)
        bottom.addWidget(self.fader, 1)
        bottom.addWidget(self.meter)
        bottom.addWidget(self.mute_button)
        bottom.addWidget(self.menu_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 4)
        layout.setSpacing(1)
        layout.addLayout(top)
        layout.addLayout(bottom)

        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(COMMIT_DELAY_MS)
        self._commit_timer.timeout.connect(self._commit_now)
        self._applying = False

        self.update_from(item, show_percent)

    # ---------------------------------------------------------------- 更新
    def update_from(self, item: AudioInput, show_percent: bool | None = None) -> None:
        if show_percent is not None:
            self._show_percent = show_percent
        self._applying = True
        self.fader.setValue(int(round(db_to_fader(item.volume_db) * SLIDER_MAX)))
        self._applying = False
        self._update_value_label(item.volume_db, item.volume_mul)

        self.mute_button.blockSignals(True)
        self.mute_button.setChecked(item.muted)
        self.mute_button.blockSignals(False)
        self.mute_button.setIcon(icons.speaker_icon(item.muted))
        muted_style = f"color: {theme.color('RED')};" if item.muted else ""
        self.name_label.setStyleSheet(muted_style)

    def _update_value_label(self, db: float, mul: float | None = None) -> None:
        if self._show_percent:
            value = mul if mul is not None else (0.0 if db <= -100 else 10 ** (db / 20))
            self.value_label.setText(format_percent(value))
        else:
            self.value_label.setText(format_db(db))

    def set_level(self, value: float, now_ms: float) -> None:
        self.meter.set_level(value, now_ms)

    # ---------------------------------------------------------------- 交互
    def _on_fader_moved(self, value: int) -> None:
        if self._applying:
            return
        db = fader_to_db(value / SLIDER_MAX)
        self._update_value_label(db)
        self.volume_preview.emit(self.name, db)
        self._commit_timer.start()

    def _commit_now(self) -> None:
        self._commit_timer.stop()
        self.volume_committed.emit(self.name, fader_to_db(self.fader.value() / SLIDER_MAX))

    # ---- E8：键盘微调 / 复位 ----
    def _nudge_db(self, delta_db: float) -> None:
        current = fader_to_db(self.fader.value() / SLIDER_MAX)
        self._apply_db(current + delta_db)

    def reset_to_zero_db(self) -> None:
        self._apply_db(0.0)

    def _apply_db(self, target_db: float) -> None:
        """把目标 dB 落到滑块上，并立即提交（键盘操作没有"松手"这一刻）。"""
        clamped = max(-100.0, min(target_db, 26.0))
        value = int(round(db_to_fader(clamped) * SLIDER_MAX))
        if value == self.fader.value():
            return
        # 走 valueChanged 会触发 _on_fader_moved（预览 + 起节流），保持一致手感
        self.fader.setValue(value)
        self._commit_now()

    def _show_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("高级音频属性…", lambda: self.advanced_requested.emit(self.name))
        menu.addAction("从混音器中隐藏", lambda: self.hide_requested.emit(self.name))
        menu.exec(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height())))


class MixerPanel(DockPanel):
    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__("混音器", parent)
        self.store = store
        self.callbacks = callbacks  # set_volume / toggle_mute / hide / advanced / show_percent

        header = QHBoxLayout()
        header.setSpacing(3)
        self.unit_button = QPushButton("dB")
        self.unit_button.setObjectName("miniButton")
        self.unit_button.setFixedWidth(34)
        self.unit_button.setToolTip("在 dB 与百分比之间切换")
        self.unit_button.clicked.connect(self._toggle_unit)
        header.addWidget(self.unit_button, 0, Qt.AlignmentFlag.AlignLeft)

        # E9：一键全静音 / 全部取消静音。文案随当前状态变，所以只有一个按钮。
        self.mute_all_button = QPushButton("全静音")
        self.mute_all_button.setObjectName("miniButton")
        self.mute_all_button.setFixedWidth(56)
        self.mute_all_button.setToolTip("把所有音频源静音（再点一次全部恢复）")
        self.mute_all_button.clicked.connect(self._toggle_mute_all)
        header.addWidget(self.mute_all_button)

        header.addStretch(1)

        self.hidden_button = QToolButton()
        self.hidden_button.setText("已隐藏 ▾")
        self.hidden_button.setToolTip("恢复被隐藏的音频源")
        self.hidden_button.clicked.connect(self._show_hidden_menu)
        header.addWidget(self.hidden_button)

        self.advanced_button = QPushButton("高级")
        self.advanced_button.setObjectName("miniButton")
        self.advanced_button.setFixedWidth(38)
        self.advanced_button.setToolTip("打开高级音频属性")
        self.advanced_button.clicked.connect(lambda: self.callbacks["advanced"](None))
        header.addWidget(self.advanced_button)

        self.add_layout(header)

        self.empty_hint = dim_label("暂无音频源")
        self.add(self.empty_hint)

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._rows_container)
        self.add(scroll, 1)

        self._rows: dict[str, _FaderRow] = {}

        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(METER_INTERVAL_MS)
        self._meter_timer.timeout.connect(self._refresh_meters)

        store.audio_changed.connect(self.rebuild)
        store.audio_input_changed.connect(self._update_row)
        store.connection_state_changed.connect(self._on_connection_state)
        store.capabilities_changed.connect(self._update_buttons)
        # v4 兼容模式下没有电平表事件，电平条要收起来（见 _update_meters）
        store.compat_mode_changed.connect(self._update_meters)

        self._update_buttons()
        self.rebuild()

    # ---------------------------------------------------------------- 连接态
    def _on_connection_state(self, state: str) -> None:
        connected = state == CONNECTED
        self.setEnabled(connected)
        if connected:
            self._meter_timer.start()
        else:
            self._meter_timer.stop()
            self._clear_rows()

    def _update_buttons(self) -> None:
        self.advanced_button.setEnabled(self.store.supports(P.REQ_GET_INPUT_AUDIO_TRACKS))
        self.unit_button.setText("%" if self.callbacks["show_percent"]() else "dB")
        self._update_mute_all()
        self._update_meters()

    def _update_meters(self) -> None:
        """v4 没有电平表事件 —— 藏掉电平条，别画一排永远为 0 的死条。

        这不是"省点事"：`InputVolumeMeters` 在 obs-websocket v4 里**完全不存在**
        （没有任何电平表请求或事件，唯一的音频活跃信号是 `GetAudioActive` 的布尔值）。
        留着一个永远不动的电平条，用户只会以为软件坏了。
        """
        compat = self.store.compat_mode
        for row in self._rows.values():
            row.meter.setVisible(not compat)
            row.meter.setToolTip(
                "obs-websocket v4（兼容模式）没有电平表事件，无法显示电平"
                if compat else ""
            )

    def _update_mute_all(self) -> None:
        """还有没静音的就显示「全静音」，全静音了就显示「取消全静音」。"""
        items = self.store.audio_inputs
        self.mute_all_button.setVisible(bool(items))
        any_unmuted = any(not item.muted for item in items)
        self.mute_all_button.setText("全静音" if any_unmuted else "取消全静音")
        self.mute_all_button.setEnabled(bool(items))

    def _toggle_mute_all(self) -> None:
        any_unmuted = any(not item.muted for item in self.store.audio_inputs)
        self.callbacks["mute_all"](any_unmuted)

    # ---------------------------------------------------------------- 列表
    def _clear_rows(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self.empty_hint.setVisible(True)

    def rebuild(self) -> None:
        wanted = [item.name for item in self.store.audio_inputs]
        if wanted != list(self._rows):
            self._clear_rows()
            for item in self.store.audio_inputs:
                row = _FaderRow(item, self.callbacks["show_percent"]())
                row.volume_preview.connect(self.callbacks["volume_preview"])
                row.volume_committed.connect(self.callbacks["volume_commit"])
                row.mute_toggled.connect(self.callbacks["toggle_mute"])
                row.hide_requested.connect(
                    lambda name: self.callbacks["hide"](name, True)
                )
                row.advanced_requested.connect(self.callbacks["advanced"])
                self._rows_layout.addWidget(row)
                self._rows[item.name] = row
        else:
            for item in self.store.audio_inputs:
                self._rows[item.name].update_from(item, self.callbacks["show_percent"]())
        self.empty_hint.setVisible(not self._rows)
        self.hidden_button.setVisible(bool(self.store.hidden_inputs))
        self._update_buttons()

    def _update_row(self, name: str) -> None:
        item = self.store.find_audio_input(name)
        row = self._rows.get(name)
        if item is not None and row is not None:
            row.update_from(item, self.callbacks["show_percent"]())
        # 静音状态变了，全静音按钮的文案也要跟着变
        self._update_mute_all()

    def _refresh_meters(self) -> None:
        now_ms = _now_ms()
        meters = self.store.meters
        for name, row in self._rows.items():
            row.set_level(meters.get(name, 0.0), now_ms)

    def _toggle_unit(self) -> None:
        self.callbacks["set_show_percent"](not self.callbacks["show_percent"]())

    def _show_hidden_menu(self) -> None:
        menu = QMenu(self)
        hidden = sorted(self.store.hidden_inputs)
        if not hidden:
            menu.addAction("（没有隐藏的源）").setEnabled(False)
        for name in hidden:
            menu.addAction(f"恢复 {name}", lambda checked=False, n=name: self.callbacks["hide"](n, False))
        menu.exec(self.hidden_button.mapToGlobal(QPoint(0, self.hidden_button.height())))


def _now_ms() -> float:
    return time.monotonic() * 1000

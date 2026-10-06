"""预览区（F2 + G1/G2/G3），布局对齐 OBS：

    ┌── 预览：场景 ──┐ ┌─ 转场列 ─┐ ┌── 输出：场景 2 ──┐
    │    （画面）     │ │ 转场动画  │ │     （画面）      │
    │                │ │ 快捷转场  │ │                  │
    └────────────────┘ │ 转场/时长 │ └──────────────────┘
                       └──────────┘
    - 57% + [缩放至窗口 ▼]

演播室模式关闭时只显示「输出」一块（OBS 同样如此）。
画面仍是 F2 的 1 fps 缩略图，缩放只影响客户端显示，不额外请求 OBS。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core import protocol as P
from ...core.state_store import CONNECTED, StateStore
from ...utils.dpi import device_ratio, snap_rect
from .. import theme
from .dock_panel import placeholder_tool_button

NO_FRAME = "无画面\n（缩略图由 OBS 端逐帧编码，默认 1 秒 1 帧）"
ZOOM_STEPS = (10, 25, 33, 50, 66, 75, 100, 125, 150, 200)
FIT_LABEL = "缩放至窗口"
TRANSITION_COLUMN_WIDTH = 190
TBAR_THROTTLE_MS = 150   # G4：推杆提交节流（与混音器推子同档）
TBAR_HEIGHT = 22
TBAR_TOOLTIP = (
    "T 型推杆：向右推到底并松手即完成转场（预览 → 输出）。\n"
    "中途松手会退回，不会切换画面。与 OBS 一致，需要工作室模式。"
)


class _VideoView(QWidget):
    """画面绘制区。

    这里刻意不用 `QLabel.setPixmap()`：QLabel 会把图片尺寸算进 sizeHint，
    而缩略图又是"按控件大小缩放"来的，于是形成正反馈
    —— 控件变大 → 图变大 → sizeHint 变大 → 布局要求更大 → 控件再变大，
    顶层窗口的 minimumSize 被一路顶大，表现为边框每秒往右/往下挪一点。

    改成自己在 paintEvent 里画，尺寸完全由布局决定、不参与布局计算。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._fit = True
        self._percent = 100
        self.setMinimumSize(120, 68)  # 固定下限，不是"跟着图长"
        # Ignored：尺寸建议完全不参与布局
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 180)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, 68)

    def set_frame(self, raw: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):  # 解码失败保留上一帧，不闪黑
            return
        self._pixmap = pixmap
        self.update()

    def clear(self) -> None:
        self._pixmap = QPixmap()
        self.update()

    def set_zoom(self, fit: bool, percent: int) -> None:
        if (fit, percent) == (self._fit, self._percent):
            return
        self._fit = fit
        self._percent = percent
        self.update()

    def displayed_percent(self) -> int:
        if self._pixmap.isNull():
            return 0
        if not self._fit:
            return self._percent
        return round(
            self._target_size().width() / max(self._pixmap.width(), 1) * 100
        )

    def _target_size(self) -> QSize:
        if self._fit:
            return self.size()
        return QSize(
            max(1, self._pixmap.width() * self._percent // 100),
            max(1, self._pixmap.height() * self._percent // 100),
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.color("PREVIEW_BG")))
        if self._pixmap.isNull():
            painter.setPen(QColor(theme.color("TEXT_DISABLED")))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, NO_FRAME
            )
            return
        scaled = self._pixmap.scaled(
            self._target_size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # O2：把落点对齐到物理像素网格 —— 125%/150% 缩放下不对齐会让画面边缘发虚
        rect = snap_rect(
            QRect(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2,
                scaled.width(),
                scaled.height(),
            ),
            device_ratio(self),
        )
        painter.drawPixmap(rect, scaled)


class _PreviewPane(QWidget):
    """一块画面：标题（预览：场景）+ 黑色画面区。"""

    def __init__(self, prefix: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.prefix = prefix

        self.title_label = QLabel(prefix)
        self.title_label.setObjectName("paneTitle")

        self.video_area = QFrame()
        self.video_area.setObjectName("videoArea")
        self.video_area.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.view = _VideoView()
        video_layout = QVBoxLayout(self.video_area)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(self.view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.title_label)
        layout.addWidget(self.video_area, 1)

        self._scene_name = ""

    # ---------------------------------------------------------------- 画面
    def set_frame(self, raw: bytes) -> None:
        self.view.set_frame(raw)

    def clear(self) -> None:
        self.view.clear()

    def set_prefix(self, prefix: str) -> None:
        """演播室模式关闭时，这块画面在 OBS 里叫「预览」而不是「输出」。"""
        self.prefix = prefix
        self._refresh_title()

    def set_scene(self, name: str) -> None:
        self._scene_name = name
        self._refresh_title()

    def _refresh_title(self) -> None:
        name = getattr(self, "_scene_name", "")
        self.title_label.setText(f"{self.prefix}：{name}" if name else self.prefix)

    def paint_scale(self, fit: bool, percent: int) -> int:
        """套用缩放并重绘，返回实际显示百分比（fit 时由控件尺寸反推）。"""
        self.view.set_zoom(fit, percent)
        return self.view.displayed_percent()


class _TransitionColumn(QWidget):
    """两块画面中间那一列：转场触发、快捷转场、转场方式与时长、T 型推杆。"""

    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks
        self.setFixedWidth(TRANSITION_COLUMN_WIDTH)
        # 先置上：tbar 的 valueChanged 在 __init__ 里就接上了，
        # 之后任何 setValue 都会回调 _on_tbar_moved，必须能读到这个标志
        self._tbar_applying = False
        self._tbar_pending: float | None = None
        self._quick_buttons: list[QPushButton] = []
        # 快捷转场行末尾弹簧项的下标（只建一次，见 refresh_quick_slots）
        self._quick_stretch_index: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(2)
        self.transition_btn = QPushButton("转场动画")
        self.transition_btn.setObjectName("transitionButton")
        self.transition_btn.setToolTip("按当前转场与时长把预览推到节目")
        self.transition_btn.clicked.connect(self.callbacks["trigger_transition"])
        top.addWidget(self.transition_btn, 1)
        top.addWidget(placeholder_tool_button("⋮", "转场属性"))
        layout.addLayout(top)

        quick = QHBoxLayout()
        quick.setSpacing(2)
        quick.addWidget(QLabel("快捷转场"))
        quick.addStretch(1)
        self.quick_manage_btn = QToolButton()
        self.quick_manage_btn.setText("⋯")
        self.quick_manage_btn.setAutoRaise(True)
        self.quick_manage_btn.setToolTip("管理快捷转场槽位")
        self.quick_manage_btn.clicked.connect(self._manage_quick_slots)
        quick.addWidget(self.quick_manage_btn)
        layout.addLayout(quick)

        # G5：槽位按钮。点一下＝「设当前转场(含时长) → Trigger」两步打包
        self.quick_row = QHBoxLayout()
        self.quick_row.setSpacing(2)
        layout.addLayout(self.quick_row)

        self.combo = QComboBox()
        self.combo.activated.connect(self._on_picked)
        layout.addWidget(self.combo)

        duration = QHBoxLayout()
        duration.setSpacing(4)
        duration.addWidget(QLabel("时长"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(50, 20000)
        self.duration_spin.setSingleStep(50)
        self.duration_spin.setSuffix(" ms")
        self.duration_spin.editingFinished.connect(self._on_duration_edited)
        duration.addWidget(self.duration_spin, 1)
        layout.addLayout(duration)

        layout.addStretch(1)

        # G4：T 型推杆。
        # **方向必须和 OBS 一致**：OBS 的 tBar 是 `QSlider(Qt::Horizontal)`，
        # 值越大越接近完成（0 = 预览态，最大 = 已切到输出），往右推到底才是完成。
        # 之前做成了竖直滑块 —— Qt 竖直滑块默认最小值在**下**、最大值在**上**，
        # 于是"往下推"发出去的是 0.0，OBS 收到就回退，表现为"推到底也没转场"。
        self.tbar = QSlider(Qt.Orientation.Horizontal)
        self.tbar.setRange(0, 100)
        self.tbar.setValue(0)
        self.tbar.setFixedHeight(TBAR_HEIGHT)
        self.tbar.setToolTip(TBAR_TOOLTIP)
        # 与 OBS 相同：只响应"用户拖动"，程序 setValue（回读校准）不驱动转场
        self.tbar.valueChanged.connect(self._on_tbar_moved)
        self.tbar.sliderReleased.connect(self._on_tbar_released)

        # 整行套一个容器：OBS 版本不支持时要能把它**整行藏掉**
        # （布局本身没有 setVisible，必须包一层 QWidget）
        self.tbar_row = QWidget()
        tbar_row = QHBoxLayout(self.tbar_row)
        tbar_row.setContentsMargins(0, 0, 0, 0)
        tbar_row.setSpacing(4)
        tbar_row.addWidget(QLabel("预览"))
        tbar_row.addWidget(self.tbar, 1)
        tbar_row.addWidget(QLabel("输出"))
        layout.addWidget(self.tbar_row)

        # 推杆不可用时把原因直接写在界面上（工具栏只有 190px 宽，允许折行）
        self.tbar_hint = QLabel("")
        self.tbar_hint.setWordWrap(True)
        self.tbar_hint.setVisible(False)
        layout.addWidget(self.tbar_hint)

        # G4：拖动节流。推杆要连续推，所以是"按时发中间值"，
        # 与混音器推子那种"停手才提交"不同。
        self._tbar_timer = QTimer(self)
        self._tbar_timer.setSingleShot(True)
        self._tbar_timer.setInterval(TBAR_THROTTLE_MS)
        self._tbar_timer.timeout.connect(self._flush_tbar)

        self._duration_timer = QTimer(self)
        self._duration_timer.setSingleShot(True)
        self._duration_timer.setInterval(400)
        self._duration_timer.timeout.connect(self._commit_duration)

        store.transitions_changed.connect(self._update_transitions)
        store.transition_changed.connect(self._update_from_store)
        store.capabilities_changed.connect(self._update_from_store)
        # 转场方式换了也要刷新按钮/推杆（"剪切"不能用推杆，提示要跟着变）
        store.transition_changed.connect(self._update_button)
        # 被 OBS 拒过 / 工作室模式变了 → 推杆的可用性与原因文字都要跟着更新
        store.capabilities_changed.connect(self._update_button)
        store.studio_changed.connect(self._update_button)
        store.tbar_ignored_changed.connect(self._update_button)
        # 连上以后才知道 OBS 版本，而推杆显不显示要看版本
        store.server_info_changed.connect(lambda _info: self._update_button())
        store.transitioning_changed.connect(self._update_button)
        store.connection_state_changed.connect(self._on_connection_state)
        store.tbar_changed.connect(self._update_tbar)

        self._update_button()
        self.refresh_quick_slots()
        self.setEnabled(False)

    def _update_tbar(self) -> None:
        """外部（OBS 端推的 / 事件回读的）位置同步到滑块，但别触发再次下发。"""
        if self.tbar.isSliderDown():
            return  # 用户正按着，别抢
        self._apply_tbar_position(self.store.tbar_position)

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

    def _update_button(self) -> None:
        busy = self.store.transitioning
        self.transition_btn.setText("转场中…" if busy else "转场动画")
        self.transition_btn.setEnabled(self.store.studio_mode and not busy)
        # T 型推杆**不能**跟着 busy 置灰。
        # 坑（线上问题）：手推 T 条会让 OBS 立刻发 SceneTransitionStarted，
        # 于是 busy=True；如果这里把滑块禁用，用户正按着的控件会在拖到一半时被禁掉，
        # 拖拽中断 → sliderReleased 不再触发 → release=true 永远发不出去
        # → OBS 侧转场一直开着、SceneTransitionEnded 不来 → 按钮卡在「转场中」。
        # 所以推杆只看"能不能用"，不看"是不是正在转场"（OBS 里推杆也是随时可拖的）。
        tbar_ok = self._tbar_usable()
        # 版本门槛放在最前面：OBS ≥29.1 的 API 根本完不成手动推杆
        # （obs-studio issue #11372，修复 PR #13143 未合入），这时候**整行藏掉**，
        # 不摆一个必然无效的控件在那儿。
        version_ok = P.tbar_version_ok(self.store.server_info.obs_version)
        self.tbar_row.setVisible(version_ok)
        if not version_ok:
            self.tbar.setEnabled(False)
            self.tbar_hint.setText(
                f"OBS {self.store.server_info.obs_version} 的推杆 API 有缺陷"
                "（obs-studio #11372），已隐藏该控件"
            )
            self.tbar_hint.setVisible(True)
            for button in self._quick_buttons:
                button.setEnabled(self.store.studio_mode)
            return
        self.tbar.setEnabled(tbar_ok)
        self.tbar.setToolTip(TBAR_TOOLTIP if tbar_ok else f"T 型推杆不可用：{self._tbar_block_reason()}")
        # 把"为什么用不了 / 有什么要注意"**写在界面上**，别只藏在 tooltip 里 ——
        # 否则用户只看到一根拖不动的滑块，完全不知道要去 OBS 里开工作室模式
        note = self._tbar_block_reason() if not tbar_ok else self._tbar_note()
        self.tbar_hint.setText(note)
        self.tbar_hint.setVisible(bool(note))
        for button in self._quick_buttons:
            button.setEnabled(self.store.studio_mode)

    def _tbar_usable(self) -> bool:
        """推杆此刻能不能用：工作室模式 + 协议支持 + 没被 OBS 拒过。"""
        return (
            self.store.studio_mode
            and self.store.supports("SetTBarPosition")
            and not self.store.is_unavailable("SetTBarPosition")
        )

    def _tbar_block_reason(self) -> str:
        """**不能用**的原因，按"最该先解决的那一条"排。"""
        if not self.store.supports("SetTBarPosition"):
            # v4 里 SetTBarPosition 只有 4.9.0+ 才有；本项目的合成能力表
            # 只在 v4 上把它算作支持，所以这里要写清是"协议没有"还是"版本不够"
            if self.store.compat_mode:
                return (
                    "obs-websocket v4（兼容模式）没有 T 型推杆请求"
                    "（需要 v4.9.0 及以上，或升级到 OBS ≥ 28 的 v5）"
                )
            return "当前 OBS 不支持 T 型推杆"
        if not self.store.studio_mode:
            return "需要先在 OBS 里开启工作室模式（Studio Mode）"
        if self.store.is_unavailable("SetTBarPosition"):
            return self.store.unavailable_reason("SetTBarPosition") or "OBS 当前不接受推杆请求"
        return ""

    def _tbar_note(self) -> str:
        """能用、但有前提或已知问题时的提示。

        OBS 的 `ValidTBarTransition()` 明确排除 cut / stinger 两种转场，
        不过它推杆时会**自动临时改用淡入淡出**，所以推杆本身仍然有效 ——
        只是转场方式会被换掉，得说清楚，别让用户以为"我选的剪切怎么变淡入了"。
        """
        if self.store.tbar_ignored:
            # OBS 收下请求却毫无反应（未修复的 OBS 侧缺陷，见 controller.TBAR_OBS_BUG_HINT）
            return "OBS 收下了请求但没有反应（已知 OBS 侧缺陷），详见弹窗说明"
        if self._is_cut_transition():
            return "当前是「剪切」：推杆时 OBS 会自动改用淡入淡出"
        return ""

    def _is_cut_transition(self) -> bool:
        """OBS 不给「剪切」这类瞬时转场做手动推杆（见 ValidTBarTransition）。"""
        return (self.store.current_transition or "").strip().lower() in ("cut", "剪切")

    def _on_picked(self, index: int) -> None:
        name = self.combo.itemText(index)
        if name:
            self.callbacks["transition"](name)

    def _on_duration_edited(self) -> None:
        self._duration_timer.start()

    def _commit_duration(self) -> None:
        self.callbacks["duration"](self.duration_spin.value())

    # ---------------------------------------------------------------- G4：T 型推杆
    def _on_tbar_moved(self, value: int) -> None:
        if self._tbar_applying:
            return
        self._tbar_pending = value / 100.0
        if not self._tbar_timer.isActive():
            self._tbar_timer.start()

    def _flush_tbar(self) -> None:
        """把最新位置推给 OBS（release=False，表示"还在推"）。"""
        if self._tbar_pending is None:
            return
        position, self._tbar_pending = self._tbar_pending, None
        self.callbacks["tbar"](position, False)

    def _on_tbar_released(self) -> None:
        """松手：明确表达"完成"还是"取消"，别把状态吊在半空。

        OBS 的 `TBarReleased()` 只有两个分支 —— 距最大端 <= 10% 量程 → 完成，
        距 0 端 <= 10% → 回退，**中间那段什么都不做**（转场就那么挂着）。
        真按这个发，用户在中间松手就会看到"转场中"一直不结束，只能等看门狗兜底。
        所以这里按同一个 10% 容差替用户把意图定下来：
          - 推到了末端 → 原样发 release，让 OBS 自己完成（切场景）；
          - 没推到末端 → **显式发 (0.0, release)**，落到 OBS 的回退分支，
            保证一定有 SceneTransitionEnded 回来把状态收干净。

        本地**不**自己改滑块位置：完成/回退的真实结果由事件确认后再归位，
        否则会出现"OBS 还在动、界面已经弹回 0"的错位。
        """
        self._tbar_timer.stop()
        self._tbar_pending = None
        position = self.tbar.value() / 100.0
        if position >= 1.0 - P.TBAR_CLAMP:
            self.callbacks["tbar"](position, True)
        else:
            self.callbacks["tbar"](0.0, True)

    def _apply_tbar_position(self, position: float) -> None:
        self._tbar_applying = True
        self.tbar.setValue(int(round(max(0.0, min(position, 1.0)) * 100)))
        self._tbar_applying = False

    # ---------------------------------------------------------------- G5：快捷转场槽位
    def refresh_quick_slots(self) -> None:
        slots = self.callbacks["quick_slots"]()
        while len(self._quick_buttons) < len(slots):
            index = len(self._quick_buttons)
            button = QPushButton(str(index + 1))
            button.setObjectName("miniButton")
            button.setFixedWidth(22)
            # 槽位序号在创建时就固定了，接一次就够（不需要每次重建都重连信号）
            button.clicked.connect(
                lambda _checked=False, i=index: self.callbacks["quick_run"](i)
            )
            self._quick_buttons.append(button)
            self.quick_row.addWidget(button)
        for index, button in enumerate(self._quick_buttons):
            if index < len(slots):
                slot = slots[index]
                button.setVisible(True)
                button.setEnabled(self.store.studio_mode)
                button.setToolTip(
                    f"快捷转场 {index + 1}：{slot.get('transition', '')}"
                    f"（{slot.get('duration_ms', 0)} ms）\nCtrl+Alt+Shift+{index + 1}"
                )
            else:
                button.setVisible(False)
        # 末尾的弹簧项**只建一次**。以前放在这里每次都 addStretch，
        # 而本方法在 transitions_changed / 保存槽位 / 换主题时都会被调到 ——
        # 弹簧项只增不减，实测刷新 20 次后布局项从 2 涨到 22（每次 +1），
        # 既无上限增长，又会把槽位按钮往左挤。
        if self._quick_stretch_index is None:
            self.quick_row.addStretch(1)
            self._quick_stretch_index = self.quick_row.count() - 1
        self.quick_manage_btn.setEnabled(True)

    def _manage_quick_slots(self) -> None:
        self.callbacks["quick_manage"]()


class PreviewPanel(QWidget):
    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks  # transition / duration / trigger_transition

        self.preview_pane = _PreviewPane("预览")
        self.output_pane = _PreviewPane("输出")
        self.transition_column = _TransitionColumn(store, callbacks)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.preview_pane)
        self.splitter.addWidget(self.transition_column)
        self.splitter.addWidget(self.output_pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setSizes([420, TRANSITION_COLUMN_WIDTH, 420])
        self.splitter.setCollapsible(1, False)

        self.zoom_bar = self._build_zoom_bar()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 0)
        layout.setSpacing(2)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.zoom_bar)

        self._fit = True
        self._zoom_percent = 100
        self._actual_percent = 0

        store.current_scene_changed.connect(self._update_scenes)
        store.preview_scene_changed.connect(self._update_scenes)
        store.studio_changed.connect(self._on_studio_changed)
        store.connection_state_changed.connect(self._on_connection_state)
        store.frame_ready.connect(self._on_frame)

        self._apply_studio_mode()
        self._update_scenes()
        self.setEnabled(False)

    # ---------------------------------------------------------------- 缩放条
    def _build_zoom_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("zoomBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(4)

        zoom_out = QToolButton()
        zoom_out.setText("−")
        zoom_out.setToolTip("缩小")
        zoom_out.clicked.connect(lambda: self._step_zoom(-1))

        self.zoom_label = QLabel("—")
        self.zoom_label.setObjectName("zoomLabel")
        self.zoom_label.setMinimumWidth(40)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        zoom_in = QToolButton()
        zoom_in.setText("+")
        zoom_in.setToolTip("放大")
        zoom_in.clicked.connect(lambda: self._step_zoom(1))

        self.zoom_combo = QComboBox()
        self.zoom_combo.addItem(FIT_LABEL)
        self.zoom_combo.addItems(f"{value}%" for value in ZOOM_STEPS)
        self.zoom_combo.activated.connect(self._on_zoom_picked)
        self.zoom_combo.setFixedWidth(130)

        row.addWidget(zoom_out)
        row.addWidget(self.zoom_label)
        row.addWidget(zoom_in)
        row.addWidget(self.zoom_combo)
        row.addStretch(1)
        return bar

    def _step_zoom(self, direction: int) -> None:
        steps = list(ZOOM_STEPS)
        target = max(self._actual_percent, 1) if self._fit else self._zoom_percent
        index = steps.index(min(steps, key=lambda value: abs(value - target)))
        index = max(0, min(len(steps) - 1, index + direction))
        self._fit = False
        self._zoom_percent = steps[index]
        self.zoom_combo.blockSignals(True)
        self.zoom_combo.setCurrentIndex(index + 1)
        self.zoom_combo.blockSignals(False)
        self._rescale()

    def _on_zoom_picked(self, index: int) -> None:
        if index == 0:
            self._fit = True
        else:
            self._fit = False
            self._zoom_percent = ZOOM_STEPS[index - 1]
        self._rescale()

    def _rescale(self) -> None:
        actual = 0
        for pane in self._active_panes():
            actual = max(actual, pane.paint_scale(self._fit, self._zoom_percent))
        self._actual_percent = actual
        self.zoom_label.setText(f"{actual}%" if actual else "—")

    # ---------------------------------------------------------------- 状态
    def _active_panes(self) -> list[_PreviewPane]:
        if self.store.studio_mode:
            return [self.preview_pane, self.output_pane]
        return [self.output_pane]

    def _on_connection_state(self, state: str) -> None:
        connected = state == CONNECTED
        self.setEnabled(connected)
        if not connected:
            self.preview_pane.clear()
            self.output_pane.clear()
            self.zoom_label.setText("—")

    def _on_studio_changed(self) -> None:
        self._apply_studio_mode()
        self._update_scenes()
        self._rescale()

    def _apply_studio_mode(self) -> None:
        """与 OBS 一致：转场列只在演播室模式下出现，非演播室模式单画面铺满。"""
        studio = self.store.studio_mode
        self.preview_pane.setVisible(studio)
        self.transition_column.setVisible(studio)
        self.output_pane.set_prefix("输出" if studio else "预览")
        if not studio:
            self.preview_pane.clear()

    def _update_scenes(self, *_args) -> None:
        self.output_pane.set_scene(self.store.current_scene)
        if self.store.studio_mode:
            self.preview_pane.set_scene(self.store.preview_scene)

    def _on_frame(self, role: str, raw: bytes) -> None:
        if role == "preview":
            if not self.store.studio_mode:
                return
            pane = self.preview_pane
        else:
            pane = self.output_pane
        if not raw:
            pane.clear()
            return
        pane.set_frame(raw)
        self._rescale()

    # ---------------------------------------------------------------- G5
    def refresh_quick_slots(self) -> None:
        """槽位在设置里改过之后，让转场列重建按钮。"""
        self.transition_column.refresh_quick_slots()

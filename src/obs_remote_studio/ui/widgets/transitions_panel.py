"""「转场动画」面板（G1/G2 + P3），布局对齐 OBS：转场下拉 + 时长 + 一排小按钮。

数据源是 StateStore 的转场字段，与预览区中间那一列共用，
两边都从 store 信号更新，因此不会出现两处显示不一致。

P3 的「覆盖转场动画」也在这里：OBS 本机是场景列表右键菜单里的一项
（`TransitionOverride`），本项目把它放在转场面板 —— 因为它改的正是
"这个场景切换时用哪个转场"，和上面那组全局转场是同一件事的两个层级。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ...core import protocol as P
from ...core.state_store import CONNECTED, StateStore
from .dock_panel import DockPanel, placeholder_tool_button, tool_row

# 「无」= 清除覆盖、回到全局转场。OBS 本机菜单里这一项就叫「无」（None），
# 文案照抄以便和老用户的心智对上。
OVERRIDE_NONE_LABEL = "无（跟随全局）"


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

        # ---- P3：场景级转场覆盖（"这个场景切换时用 XX 转场 / YY 时长"）----
        # **必须做得紧凑**：这个面板默认只分到约 194px 高，加控件很容易把内容
        # 挤出可视区（实测加一组"标签+下拉+标签+数字框+说明"会溢出）。
        # 所以压成一行「覆盖 [下拉] [时长]」+ 一行说明，不再各占一行。
        override_row = QHBoxLayout()
        override_row.setSpacing(4)
        self.override_label = QLabel("覆盖")
        self.override_label.setToolTip("只对这个场景生效的转场（场景级覆盖）")
        override_row.addWidget(self.override_label)

        self.override_combo = QComboBox()
        # **不能让下拉内容影响最小尺寸**：这个下拉会随场景/能力变化重填，
        # 若 sizeHint 跟着候选文字变，每次重填都会让整行 dock 重新布局
        # （实测表现为"演播室模式刚开"之后窗口最小高度又被抬一次，
        #  布局稳定性测试因此判定为尺寸漂移）。固定按 N 个字符算宽，
        # 内容再长也只影响显示、不影响布局。
        self.override_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.override_combo.setMinimumContentsLength(6)
        self.override_combo.setMinimumWidth(72)
        self.override_combo.setToolTip(
            "只对这个场景生效的转场。选「无」即清除覆盖、回到全局转场。"
        )
        self.override_combo.activated.connect(self._on_override_picked)
        override_row.addWidget(self.override_combo, 1)

        self.override_duration_spin = QSpinBox()
        self.override_duration_spin.setRange(
            P.TRANSITION_OVERRIDE_MIN_MS, P.TRANSITION_OVERRIDE_MAX_MS
        )
        self.override_duration_spin.setSingleStep(50)
        self.override_duration_spin.setSuffix(" ms")
        self.override_duration_spin.setMaximumWidth(96)
        # 提交节流：与上方全局时长同款做法，避免连点数字框时一发一条请求
        self.override_duration_spin.editingFinished.connect(self._on_override_duration_edited)
        override_row.addWidget(self.override_duration_spin)
        self.add_layout(override_row)

        # 状态说明（一行灰色小字）：既讲"当前场景有没有覆盖"，也负责在
        # 时长不可改时说明原因 —— 单独再放一行提示会超预算。
        self.override_hint = QLabel("")
        self.override_hint.setObjectName("hint")
        self.add(self.override_hint)

        self.add_stretch()

        self._duration_timer = QTimer(self)
        self._duration_timer.setSingleShot(True)
        self._duration_timer.setInterval(400)
        self._duration_timer.timeout.connect(self._commit_duration)

        self._override_duration_timer = QTimer(self)
        self._override_duration_timer.setSingleShot(True)
        self._override_duration_timer.setInterval(400)
        self._override_duration_timer.timeout.connect(self._commit_override_duration)

        store.transitions_changed.connect(self._update_transitions)
        store.transition_changed.connect(self._update_from_store)
        store.transition_override_changed.connect(self._update_override)
        store.scenes_changed.connect(self._update_override)
        store.studio_changed.connect(self._update_override)
        # 能力列表变化也要刷覆盖那一组：否则连上 v4（没有这三条请求）后，
        # 覆盖下拉会停在"可用"状态 —— 因为它上一次是按乐观空集刷的。
        store.capabilities_changed.connect(self._update_from_store)
        store.capabilities_changed.connect(self._update_override)
        store.connection_state_changed.connect(self._on_connection_state)

        self.setEnabled(False)
        # 构造后立刻填一次：否则在收到第一条信号之前，
        # 覆盖下拉是空的（未连接时也要看到「无（跟随全局）」这一项）
        self._update_transitions()
        self._update_override()

    # ---------------------------------------------------------------- 更新
    def _on_connection_state(self, state: str) -> None:
        self.setEnabled(state == CONNECTED)

    def _update_transitions(self) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(self.store.transitions)
        self.combo.blockSignals(False)
        self._update_from_store()
        # 覆盖下拉的候选就是"「无」+ 全局转场列表"，所以转场列表一变也要重填 ——
        # 否则连上后转场列表到达时，覆盖下拉只有一个「无」，
        # 用户根本没法选一个覆盖转场（实测真会这样）。
        self._update_override()

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

    # ---------------------------------------------------------------- P3：覆盖转场动画
    def _override_supported(self) -> bool:
        return self.store.supports(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)

    def _update_override(self) -> None:
        """按当前目标场景的覆盖状态刷新这一组控件。

        **三种状态要分清**：
        - `override_for()` 回 None → 还没查到（未连接 / 刚切场景），显示"读取中"，
          而不是显示成"无覆盖" —— 后者会让用户以为覆盖被清掉了；
        - `(None, None)` → 确认没有覆盖 → 选中「无」；
        - 有名字 → 选中该转场。
        """
        target = self.store.override_scene
        known = self.store.override_for(target)

        supported = self._override_supported()
        # 下拉候选 = 「无」+ 全局转场列表
        self.override_combo.blockSignals(True)
        self.override_combo.clear()
        self.override_combo.addItem(OVERRIDE_NONE_LABEL, None)
        for name in self.store.transitions:
            self.override_combo.addItem(name, name)
        current = self.store.override_transition
        index = self.override_combo.findData(current) if known is not None else 0
        if index < 0:
            index = 0
        self.override_combo.setCurrentIndex(index)
        self.override_combo.blockSignals(False)

        self.override_combo.setEnabled(supported)
        if not supported:
            self.override_combo.setToolTip(
                self.store.support_reason(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)
                or "服务端不支持场景级转场覆盖"
            )
        else:
            self.override_combo.setToolTip(
                "只对这个场景生效的转场。选「无」即清除覆盖、回到上面的全局转场。"
            )

        # 时长：OBS 只在"转场名的覆盖存在"时才读它，所以没选名字时必须置灰说明
        has_name = bool(current)
        duration_enabled = supported and has_name
        duration = self.store.override_duration_ms
        self.override_duration_spin.blockSignals(True)
        self.override_duration_spin.setValue(
            int(duration) if duration is not None else int(self.store.transition_duration_ms)
        )
        self.override_duration_spin.blockSignals(False)
        self.override_duration_spin.setEnabled(duration_enabled)
        self.override_duration_spin.setToolTip(
            "" if duration_enabled
            else "先在上面选一个覆盖转场，时长才会生效（OBS 只在覆盖转场存在时读这个值）"
        )

        # **说明文字要短**：这个面板只有约 220px 宽，不换行的 QLabel 会把整段文字
        # 算进 minimumSizeHint，长文案会把面板顶宽、挤坏整行 dock 的比例。
        # 所以这里只给一句短语，完整原因一律放 tooltip。
        if not supported:
            detail = (
                self.store.support_reason(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)
                or "服务端不支持场景级转场覆盖"
            )
            self.override_hint.setText("不支持覆盖")
            self.override_hint.setToolTip(detail)
        elif known is None:
            self.override_hint.setText("读取中…")
            self.override_hint.setToolTip("正在读取该场景的转场覆盖")
        elif not has_name:
            self.override_hint.setText("无覆盖（用全局）")
            self.override_hint.setToolTip(
                f"{target or '当前场景'}：无覆盖，切换时使用全局转场。"
            )
        else:
            suffix = f"{duration} ms" if duration is not None else "用全局时长"
            self.override_hint.setText("覆盖中")
            self.override_hint.setToolTip(
                f"{target or '当前场景'}：切换时使用「{current}」（{suffix}）"
            )

    def _on_override_picked(self, index: int) -> None:
        if not self._override_supported():
            return
        value = self.override_combo.itemData(index)
        self.callbacks["override"](value, None)

    def _on_override_duration_edited(self) -> None:
        self._override_duration_timer.start()

    def _commit_override_duration(self) -> None:
        if not self._override_supported():
            return
        # 时长必须跟着转场名一起下发：OBS 只在覆盖转场存在时才用它，
        # 单独一个时长是死值（服务端也会因为缺 transitionName 而要求至少给一个字段）。
        name = self.store.override_transition
        if not name:
            return
        self.callbacks["override"](name, self.override_duration_spin.value())

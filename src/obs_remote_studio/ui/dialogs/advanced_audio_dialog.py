"""高级音频属性（E6）：监听类型 / 声道平衡 / 同步偏移 / 混音轨。

对应 OBS 的「高级音频属性」窗口，非模态，打开期间随 OBS 端变化实时更新。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import protocol as P
from ...core.state_store import StateStore

TRACK_COUNT = 6
COL_NAME = 0
COL_MONITOR = 1
COL_BALANCE = 2
COL_SYNC = 3
COL_TRACKS = 4


class AdvancedAudioDialog(QDialog):
    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks  # monitor / balance / sync / tracks / fetch

        self.setWindowTitle("高级音频属性")
        self.resize(680, 360)

        self.table = QTableWidget(0, COL_TRACKS + 1)
        self.table.setHorizontalHeaderLabels(
            ["源", "监听", "声道平衡", "同步偏移", "混音轨"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for column in (COL_MONITOR, COL_BALANCE, COL_SYNC, COL_TRACKS):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        note = QLabel(
            "监听 / 平衡 / 偏移 / 轨道在打开本窗口时才向 OBS 拉取，避免连接时逐源打四个请求。"
        )
        note.setObjectName("hint")
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addWidget(note)

        store.audio_changed.connect(self.rebuild)
        store.audio_settings_changed.connect(self._refresh_values)
        self.rebuild()

    # ---------------------------------------------------------------- 构建
    def rebuild(self) -> None:
        inputs = self.store.audio_inputs
        self.table.setRowCount(len(inputs))
        for row, item in enumerate(inputs):
            name_item = QTableWidgetItem(item.name)
            name_item.setData(Qt.ItemDataRole.UserRole, item.name)
            self.table.setItem(row, COL_NAME, name_item)

            monitor = QComboBox()
            for value in P.MONITOR_CHOICES:
                monitor.addItem(P.MONITOR_LABELS[value], value)
            monitor.currentIndexChanged.connect(
                lambda _index, r=row: self._on_monitor(r)
            )
            self.table.setCellWidget(row, COL_MONITOR, monitor)

            balance = QDoubleSpinBox()
            balance.setRange(0.0, 1.0)
            balance.setSingleStep(0.05)
            balance.setDecimals(2)
            balance.setValue(0.5)
            balance.valueChanged.connect(lambda value, r=row: self._on_balance(r, value))
            # v4 没有声道平衡（GetAudioBalance/SetAudioBalance 在 v4 里根本不存在），
            # 置灰并说明原因，别摆一个点了没反应的控件
            if not self.store.supports(P.REQ_SET_INPUT_AUDIO_BALANCE):
                balance.setEnabled(False)
                balance.setToolTip(self.store.support_reason(P.REQ_SET_INPUT_AUDIO_BALANCE))
            self.table.setCellWidget(row, COL_BALANCE, balance)

            sync = QSpinBox()
            sync.setRange(-950, 20000)
            sync.setSuffix(" ms")
            sync.setToolTip("音频同步偏移")
            sync.valueChanged.connect(lambda value, r=row: self._on_sync(r, value))
            if not self.store.supports(P.REQ_SET_INPUT_AUDIO_SYNC_OFFSET):
                sync.setEnabled(False)
                sync.setToolTip(self.store.support_reason(P.REQ_SET_INPUT_AUDIO_SYNC_OFFSET))
            self.table.setCellWidget(row, COL_SYNC, sync)

            tracks = QWidget()
            tracks_layout = QHBoxLayout(tracks)
            tracks_layout.setContentsMargins(0, 0, 0, 0)
            tracks_layout.setSpacing(2)
            for index in range(TRACK_COUNT):
                box = QCheckBox(str(index + 1))
                box.setToolTip(f"输出到混音轨 {index + 1}")
                box.toggled.connect(
                    lambda checked, r=row, i=index: self._on_track(r, i, checked)
                )
                tracks_layout.addWidget(box)
            self.table.setCellWidget(row, COL_TRACKS, tracks)

            # 逐源拉一次，回填时再刷新
            self.callbacks["fetch"](item.name)
        self._refresh_values()

    def select_source(self, name: str) -> None:
        """从混音器的 ⋮ 菜单打开时，定位到对应那一行。"""
        for row in range(self.table.rowCount()):
            if self._name_at(row) == name:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, COL_NAME))
                return

    # ---------------------------------------------------------------- 刷新
    def _name_at(self, row: int) -> str:
        item = self.table.item(row, COL_NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _refresh_values(self) -> None:
        for row in range(self.table.rowCount()):
            name = self._name_at(row)
            item = self.store.find_audio_input(name)
            if item is None:
                continue

            monitor = self.table.cellWidget(row, COL_MONITOR)
            if isinstance(monitor, QComboBox) and item.monitor_type:
                index = monitor.findData(item.monitor_type)
                if index >= 0 and index != monitor.currentIndex():
                    monitor.blockSignals(True)
                    monitor.setCurrentIndex(index)
                    monitor.blockSignals(False)

            balance = self.table.cellWidget(row, COL_BALANCE)
            if isinstance(balance, QDoubleSpinBox) and item.balance is not None:
                if abs(balance.value() - item.balance) > 1e-6:
                    balance.blockSignals(True)
                    balance.setValue(item.balance)
                    balance.blockSignals(False)

            sync = self.table.cellWidget(row, COL_SYNC)
            if isinstance(sync, QSpinBox) and item.sync_offset_ms is not None:
                if sync.value() != item.sync_offset_ms:
                    sync.blockSignals(True)
                    sync.setValue(item.sync_offset_ms)
                    sync.blockSignals(False)

            tracks = self.table.cellWidget(row, COL_TRACKS)
            if tracks is not None and item.tracks is not None:
                boxes = tracks.findChildren(QCheckBox)
                for index, box in enumerate(boxes):
                    wanted = bool(item.tracks & (1 << index))
                    if box.isChecked() != wanted:
                        box.blockSignals(True)
                        box.setChecked(wanted)
                        box.blockSignals(False)

    # ---------------------------------------------------------------- 交互
    def _on_monitor(self, row: int) -> None:
        combo = self.table.cellWidget(row, COL_MONITOR)
        if isinstance(combo, QComboBox):
            self.callbacks["monitor"](self._name_at(row), combo.currentData())

    def _on_balance(self, row: int, value: float) -> None:
        self.callbacks["balance"](self._name_at(row), value)

    def _on_sync(self, row: int, value: int) -> None:
        self.callbacks["sync"](self._name_at(row), value)

    def _on_track(self, row: int, index: int, _checked: bool) -> None:
        name = self._name_at(row)
        widget = self.table.cellWidget(row, COL_TRACKS)
        if widget is None:
            return
        mask = 0
        for position, box in enumerate(widget.findChildren(QCheckBox)):
            if box.isChecked():
                mask |= 1 << position
        self.callbacks["tracks"](name, mask)

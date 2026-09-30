"""G5：快捷转场槽位管理。

OBS 的快捷转场存在 profile 里、obs-websocket 不直接暴露，所以槽位由客户端自己维护：
每个槽位 = 「转场名 + 时长」，触发时打包成「先设当前转场 → 再 Trigger」两条请求。

上限 9 个，因为要绑 `Ctrl+Alt+Shift+1~9`。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

MAX_SLOTS = 9


class QuickTransitionsDialog(QDialog):
    def __init__(self, slots: list[dict], transitions: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("快捷转场槽位")
        self.resize(420, 360)
        self._transitions = list(transitions)
        self._rows: list[tuple[QComboBox, QSpinBox, QWidget]] = []

        hint = QLabel(
            "每个槽位保存「转场方式 + 时长」，点槽位按钮或按 Ctrl+Alt+Shift+数字 一键触发。\n"
            "触发时会先把 OBS 的当前转场改成槽位里的设置，再执行转场。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._rows_host)

        self.add_button = QPushButton("添加槽位")
        self.add_button.clicked.connect(lambda: self._add_row("", 300))
        self.add_button.setEnabled(bool(self._transitions))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.addWidget(hint)
        layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

        for slot in slots[:MAX_SLOTS]:
            self._add_row(
                str(slot.get("transition", "") or ""), int(slot.get("duration_ms", 300) or 300)
            )

    # ---------------------------------------------------------------- 行
    def _add_row(self, transition: str, duration_ms: int) -> None:
        if len(self._rows) >= MAX_SLOTS:
            return
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        index = len(self._rows) + 1
        row_layout.addWidget(QLabel(f"{index}."))

        combo = QComboBox()
        combo.addItems(self._transitions)
        if transition and transition in self._transitions:
            combo.setCurrentText(transition)
        combo.setMinimumWidth(120)
        row_layout.addWidget(combo, 1)

        spin = QSpinBox()
        spin.setRange(50, 20000)
        spin.setSingleStep(50)
        spin.setSuffix(" ms")
        spin.setValue(max(50, min(duration_ms, 20000)))
        row_layout.addWidget(spin)

        remove = QPushButton("删除")
        remove.setObjectName("miniButton")
        remove.clicked.connect(lambda _checked=False, r=row: self._remove_row(r))
        row_layout.addWidget(remove)

        self._rows_layout.insertWidget(len(self._rows), row)
        self._rows.append((combo, spin, row))
        self._refresh()
        # 没有可选转场时（还没连上 OBS），把行禁掉而不是让它显示空下拉
        row.setEnabled(bool(self._transitions))

    def _remove_row(self, row: QWidget) -> None:
        for index, (_, _, widget) in enumerate(self._rows):
            if widget is row:
                del self._rows[index]
                widget.setParent(None)
                widget.deleteLater()
                break
        self._refresh()

    def _refresh(self) -> None:
        self.add_button.setEnabled(
            bool(self._transitions) and len(self._rows) < MAX_SLOTS
        )
        for position, (_, _, widget) in enumerate(self._rows):
            label = widget.layout().itemAt(0).widget()
            if isinstance(label, QLabel):
                label.setText(f"{position + 1}.")

    # ---------------------------------------------------------------- 结果
    def result_slots(self) -> list[dict]:
        slots: list[dict] = []
        for combo, spin, _ in self._rows:
            name = combo.currentText().strip()
            if name:
                slots.append({"transition": name, "duration_ms": int(spin.value())})
        return slots

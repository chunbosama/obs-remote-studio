"""J3：诊断窗口 —— 实时滚动显示与 OBS 的原始 JSON 收发。

排障时的第一手材料：把 request / response / event / error 四种帧原样打出来。
刻意保持"哑"：不做任何解析、归并，只负责显示，免得诊断工具自己引入歧义。

关键设计：**关闭窗口时断开信号连接**，让 `raw_trace` 变成没人听的空 emit，
这样长期开着诊断窗口不会积压内存，也避免格式化开销。
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...utils.icons import app_icon

# 上限行数：诊断窗口常开，不设上限会一直吃内存
MAX_BLOCKS = 2000

_DIRECTION_LABELS = {"->": "发送", "<-": "接收"}
_KIND_LABELS = {
    "request": "请求",
    "response": "响应",
    "event": "事件",
    "error": "错误",
}


class DiagnosticsWindow(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("诊断窗口 — 原始 JSON 收发")
        self.setWindowIcon(app_icon())
        self.resize(760, 520)
        # 独立窗口，不随主窗口关闭一起消失
        self.setWindowFlag(Qt.WindowType.Window, True)

        self._connected = False
        self._counts = {"request": 0, "response": 0, "event": 0, "error": 0}

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)

        self.pause_check = QCheckBox("暂停滚动")
        self.errors_only_check = QCheckBox("只看错误")
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self.clear)
        self.status_label = QLabel("未连接信号")
        self.status_label.setObjectName("hint")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.pause_check)
        controls.addWidget(self.errors_only_check)
        controls.addWidget(self.clear_btn)
        controls.addStretch(1)
        controls.addWidget(self.status_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)

    # ---------------------------------------------------------------- 连接
    def attach(self, worker) -> None:
        """绑定 worker 的 raw_trace 信号（重复调用是安全的）。"""
        if self._connected:
            return
        worker.raw_trace.connect(self.append)
        self._connected = True
        self.status_label.setText("正在记录")

    def detach(self, worker) -> None:
        if not self._connected:
            return
        try:
            worker.raw_trace.disconnect(self.append)
        except (RuntimeError, TypeError):
            pass  # 已经断了
        self._connected = False
        self.status_label.setText("已停止记录")

    # ---------------------------------------------------------------- 写入
    def clear(self) -> None:
        self.view.clear()
        self._counts = {key: 0 for key in self._counts}
        self._refresh_status()

    def append(self, direction: str, kind: str, payload) -> None:
        if kind in self._counts:
            self._counts[kind] += 1
        if self.errors_only_check.isChecked() and kind != "error":
            self._refresh_status()
            return

        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        header = f"{_DIRECTION_LABELS.get(direction, direction)} {_KIND_LABELS.get(kind, kind)}"
        self.view.appendPlainText(f"── {header} " + "─" * 40)
        self.view.appendPlainText(text)
        if not self.pause_check.isChecked():
            self.view.verticalScrollBar().setValue(
                self.view.verticalScrollBar().maximum()
            )
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self._connected:
            return
        total = sum(self._counts.values())
        self.status_label.setText(
            f"共 {total} 条 · 请求 {self._counts['request']} · "
            f"响应 {self._counts['response']} · 事件 {self._counts['event']} · "
            f"错误 {self._counts['error']}"
        )

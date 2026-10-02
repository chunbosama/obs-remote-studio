"""J3：诊断窗口 —— 实时滚动显示与 OBS 的原始 JSON 收发。

排障时的第一手材料：把 request / response / event / error 四种帧原样打出来。
刻意保持"哑"：不做任何解析、归并，只负责显示，免得诊断工具自己引入歧义。

三条与排障直接相关的设计：
1. **失败也记成协议原样的响应帧**（`requestStatus.result=false` + code），
   因为排障的人第一眼就是去找 `requestStatus.code`。见 `obs_worker._emit_request_failure`。
2. **「暂停滚动」必须真的停住**。坑：`appendPlainText()` 自己就会把视图滚到底，
   只在后面判断一次 `setValue(maximum())` 是拦不住的 —— 要把滚动位置存下来再还原。
3. **待响应计数**：把"请求发了但一直没有回音"这类情况直接显示出来。
   没有它的话，"有请求帧、没响应帧"和"回了但你没看见"根本分不清。

关键设计：**关闭窗口时断开信号连接**，让 `raw_trace` 变成没人听的空 emit，
这样长期开着诊断窗口不会积压内存，也避免格式化开销。
"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt, QTimer
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
# 待响应状态的刷新间隔 / 超过多久算"慢"
PENDING_TICK_MS = 500
SLOW_REQUEST_S = 3.0

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
        # 已发出、还没等到回音的请求：类型 -> 各次发出时刻
        self._pending: dict[str, list[float]] = {}
        self._failed = 0

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)

        self.pause_check = QCheckBox("暂停滚动")
        self.pause_check.setToolTip(
            "勾选后视图不再跟着新内容往下跑，可以安心翻看已有内容。\n"
            "新内容仍会继续记录，点「回到底部」恢复跟随。"
        )
        self.errors_only_check = QCheckBox("只看错误")
        self.errors_only_check.setToolTip("只显示失败的响应与错误帧（含 requestStatus.result=false）")
        self.to_bottom_btn = QPushButton("回到底部")
        self.to_bottom_btn.clicked.connect(self.scroll_to_bottom)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self.clear)
        self.status_label = QLabel("未连接信号")
        self.status_label.setObjectName("hint")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.pause_check)
        controls.addWidget(self.errors_only_check)
        controls.addWidget(self.to_bottom_btn)
        controls.addWidget(self.clear_btn)
        controls.addStretch(1)
        controls.addWidget(self.status_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self.view, 1)

        # 待响应条目没有新帧也会"变老"，所以要靠自己定时刷新
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(PENDING_TICK_MS)
        self._tick_timer.timeout.connect(self._refresh_status)
        self._tick_timer.start()

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
        self._clear_pending()
        self._refresh_status()

    # ---------------------------------------------------------------- 写入
    def clear(self) -> None:
        self.view.clear()
        self._counts = {key: 0 for key in self._counts}
        self._failed = 0
        self._clear_pending()
        self._refresh_status()

    def scroll_to_bottom(self) -> None:
        self.pause_check.setChecked(False)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append(self, direction: str, kind: str, payload) -> None:
        if kind in self._counts:
            self._counts[kind] += 1
        self._track_pending(kind, payload)
        errorish = self._is_error_frame(kind, payload)
        if errorish:
            self._failed += 1

        if not self.errors_only_check.isChecked() or errorish:
            text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            header = f"{_DIRECTION_LABELS.get(direction, direction)} {_KIND_LABELS.get(kind, kind)}"
            # 暂停时要把位置存下来还原：appendPlainText **自己就会滚到底**，
            # 只在后面判断一次 setValue(maximum()) 是拦不住的
            bar = self.view.verticalScrollBar()
            keep = bar.value()
            self.view.appendPlainText(f"── {header} " + "─" * 40)
            if errorish and kind != "error":
                self.view.appendPlainText(f"!! 该请求失败（code={self._code_of(payload)}）")
            self.view.appendPlainText(text)
            if self.pause_check.isChecked():
                bar.setValue(keep)
            else:
                bar.setValue(bar.maximum())
        self._refresh_status()

    # ---------------------------------------------------------------- 待响应
    def _track_pending(self, kind: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        name = str(payload.get("requestType", "") or "")
        if not name:
            return
        if kind == "request":
            self._pending.setdefault(name, []).append(time.monotonic())
        elif kind in ("response", "error"):
            times = self._pending.get(name)
            if times:
                times.pop(0)
                if not times:
                    self._pending.pop(name, None)

    def _clear_pending(self) -> None:
        self._pending.clear()

    def _oldest_pending(self) -> tuple[str, float] | None:
        """最久没回音的那条请求：(请求名, 等待秒数)。"""
        best: tuple[str, float] | None = None
        now = time.monotonic()
        for name, times in self._pending.items():
            if not times:
                continue
            age = now - times[0]
            if best is None or age > best[1]:
                best = (name, age)
        return best

    # ---------------------------------------------------------------- 状态
    @staticmethod
    def _code_of(payload) -> object:
        if isinstance(payload, dict):
            status = payload.get("requestStatus")
            if isinstance(status, dict):
                return status.get("code", "?")
            return payload.get("code", "?")
        return "?"

    @staticmethod
    def _is_error_frame(kind: str, payload) -> bool:
        """这一帧是不是"出错"的帧。

        失败现在以**协议原样的响应帧**记入（`requestStatus.result=false`），
        所以「只看错误」不能只认 `kind == "error"`，否则最该看的那一类反而被过滤掉了。
        """
        if kind == "error":
            return True
        if kind == "response" and isinstance(payload, dict):
            status = payload.get("requestStatus")
            if isinstance(status, dict):
                return status.get("result") is False
        return False

    def _refresh_status(self) -> None:
        total = sum(self._counts.values())
        parts = [
            f"共 {total} 条",
            f"请求 {self._counts['request']}",
            f"响应 {self._counts['response']}",
            f"事件 {self._counts['event']}",
            f"错误 {self._counts['error']}",
        ]
        if self._failed:
            parts.append(f"失败 {self._failed}")
        pending = self._oldest_pending()
        if pending:
            name, age = pending
            text = f"待响应 {len(self._pending)} 项：{name}"
            if age >= SLOW_REQUEST_S:
                text += f"（已等 {age:.1f} 秒）"
            else:
                text += f"（{age:.1f} 秒）"
            parts.append(text)
        if self.pause_check.isChecked():
            parts.insert(0, "⏸ 已暂停滚动")
        # 没接信号时也要把计数显示出来（手工喂帧、或已 detach 时都能看清状态），
        # 只是要标明当前没在记录
        if not self._connected:
            parts.append("未连接信号")
        self.status_label.setText(" · ".join(parts))

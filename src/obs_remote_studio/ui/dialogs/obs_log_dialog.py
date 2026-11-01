"""Q10：OBS 端日志窗口。

为什么单独一个窗口，而不是塞进诊断窗口：诊断窗口是**原始 JSON 收发**（协议帧），
这一份是 **OBS 自己的日志**（插件从对面机器读回来的）。两者的用途和读者都不同 ——
排障时"客户端发了什么"和"OBS 那台机器上出了什么事"是两条线，混在一起反而更难读。

数据来源：插件 `getRecentLogs`（一次性拉）与 `logLine`（尾流，服务端限速 20 条/秒）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import plugin as PV
from ...utils.icons import app_icon

#: 自动刷新间隔。插件侧是环形缓冲（512 条），3 秒拉一次足够，也不会把链路压满。
AUTO_REFRESH_MS = 3000
#: 一次拉多少行。契约上限 500；默认 300 能覆盖"刚才那次失败"。
FETCH_LIMIT = 300


class ObsLogWindow(QWidget):
    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.store = controller.store
        self.setWindowTitle("OBS 日志 — 由插件从 OBS 那台机器读取")
        self.setWindowIcon(app_icon())
        self.resize(760, 480)
        # 独立窗口：看日志时不该被主窗口挡住
        self.setWindowFlag(Qt.WindowType.Window, True)

        self.level_combo = QComboBox()
        for label, value in (
            ("只看错误", "error"),
            ("警告及以上", "warning"),
            ("信息及以上", "info"),
            ("调试（最全）", "debug"),
        ):
            self.level_combo.addItem(label, value)
        self.level_combo.setCurrentIndex(1)
        self.level_combo.setToolTip(
            "level 是**严重度阈值**：选「警告及以上」只会回 warning 与 error 两类。"
        )

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        self.auto_check = QCheckBox("自动刷新")
        self.auto_check.setToolTip(f"每 {AUTO_REFRESH_MS // 1000} 秒拉一次最近日志")
        self.auto_check.toggled.connect(self._sync_timer)

        self.status_label = QLabel("")
        self.status_label.setObjectName("hint")
        self.status_label.setWordWrap(True)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)
        self.view.setPlaceholderText(
            "点「刷新」从 OBS 那台机器读取最近日志。\n"
            "插件没装时这里不会有内容 —— 远程读日志是插件独有的能力，ws 没有这个接口。"
        )

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel("级别"))
        top.addWidget(self.level_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.auto_check)
        top.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(AUTO_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

        self.store.plugin_changed.connect(self._render)
        self._render()
        self._sync_timer(False)

    # ---------------------------------------------------------------- 数据
    def refresh(self) -> None:
        """向插件要一次最近日志。拿不到能力时把原因写在状态行上。"""
        if not self.controller.plugin_supports(PV.FEATURE_LOGS):
            self.status_label.setText(
                "当前连接没有插件日志能力：需要 OBS 端安装插件，且处于进阶模式。"
            )
            return
        level = self.level_combo.currentData() or "warning"
        if not self.controller.fetch_logs(level, FETCH_LIMIT):
            self.status_label.setText("插件拒绝了日志请求（当前不可用）")
            return
        self.status_label.setText("正在读取 OBS 端日志…")

    def _sync_timer(self, enabled: bool) -> None:
        if enabled:
            self._timer.start()
            self.refresh()
        else:
            self._timer.stop()

    # ---------------------------------------------------------------- 渲染
    def _render(self) -> None:
        lines = list(self.store.plugin_logs)
        self.view.setPlainText(PV.format_logs(lines))
        if not lines:
            if self.controller.plugin_supports(PV.FEATURE_LOGS):
                self.status_label.setText("还没拉到日志，点「刷新」试试。")
            else:
                self.status_label.setText(
                    "当前没有可用的插件日志能力（标准模式 / 兼容模式）。"
                )
            return
        hint = PV.failure_hint(lines)
        text = f"共 {len(lines)} 行（按时间正序）"
        if hint:
            # 只做"提示"，不替代原文：猜错了用户还能自己看下面几百行
            text += f" · 可能的失败原因：{hint}"
        self.status_label.setText(text)

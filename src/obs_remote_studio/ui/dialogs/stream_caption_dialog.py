"""P7：推流字幕（CEA-608）。

用途：往**直播流**里注入一行字幕（报幕、提示、公告）。它走
`SendStreamCaption` 一条请求，**只对推流输出有效**。

三个必须说清楚的边界（都来自服务端实现 `RequestHandler_Stream.cpp`）：

1. **只在推流时可用**。服务端第一件事就是查 `obs_frontend_streaming_active()`，
   没推流直接回 **501 OutputNotRunning**。所以这里在没推流时就把输入禁用
   并写明原因，而不是让用户点了才吃一个错误。
2. **空串＝清除当前字幕**，这是一个有价值的操作，所以「清除」单独给一个按钮，
   不做成"输入框清空后自动发"（那会误发）。
3. **OBS 没有"持续显示"的概念**：一条一条发，新的覆盖旧的。因此窗口里
   **不保留任何本地字幕状态** —— 造一个 OBS 端并不存在的状态只会误导。
   同理不做"自动重发/定时轮播"。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import protocol as P
from ...core.state_store import StateStore


class StreamCaptionDialog(QDialog):
    """非模态的字幕发送窗。"""

    def __init__(self, store: StateStore, callbacks, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self.callbacks = callbacks  # send / blocker / clear
        self.setWindowTitle("推流字幕")
        self.setMinimumWidth(420)

        hint = QLabel(
            "往直播流里注入一行 CEA-608 字幕。\n"
            f"单行约 {P.CAPTION_LINE_CHARS} 字符以内最稳妥（更长时由 OBS 自行处理）。\n"
            "字幕只在**推流中**生效；发送后由观众端的播放器决定是否显示。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("输入字幕内容，回车发送")
        self.text_edit.setMaxLength(256)
        # 回车即发送，符合"报幕"这种要快的场景
        self.text_edit.returnPressed.connect(self._send)
        self.text_edit.textChanged.connect(self._update_counter)

        self.counter = QLabel("")
        self.counter.setObjectName("hint")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self._send)

        self.clear_btn = QPushButton("清除字幕")
        self.clear_btn.setObjectName("miniButton")
        self.clear_btn.setToolTip("发送一条空字幕，让 OBS 清掉当前显示的字幕")
        self.clear_btn.clicked.connect(self._clear)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self.text_edit, 1)
        row.addWidget(self.send_btn)

        buttons = QHBoxLayout()
        buttons.addWidget(self.clear_btn)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.addWidget(hint)
        layout.addLayout(row)
        layout.addWidget(self.counter)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

        # 状态一变就刷新可用性：推流开始/结束、连接状态、能力探测结果都影响它
        store.stream_changed.connect(self.refresh)
        store.connection_state_changed.connect(lambda _state: self.refresh())
        store.capabilities_changed.connect(self.refresh)
        self.refresh()

    # ---------------------------------------------------------------- 状态
    def refresh(self) -> None:
        """按当前能否发送，刷新输入与按钮的可用性，并把原因写在界面上。

        原因**必须显示出来**（不只藏在 tooltip 里）—— 这是本项目反复
        踩过的一条：置灰了不说为什么，用户只会以为功能坏了。
        """
        blocker = self.callbacks["blocker"]()
        can_send = not blocker

        self.text_edit.setEnabled(can_send)
        self.send_btn.setEnabled(can_send)
        # 「清除字幕」同样只在推流时才有意义（服务端一样会回 501）
        self.clear_btn.setEnabled(can_send)

        if blocker:
            self.status.setText(f"当前不可发送：{blocker}")
        else:
            self.status.setText("可以发送（直播进行中）")
        self._update_counter()

    def _update_counter(self) -> None:
        text = self.text_edit.text()
        count = len(text)
        limit = P.CAPTION_LINE_CHARS
        # 超出只是提示，不阻断：切分由 OBS 负责，客户端不替它决定
        suffix = "（超出单行建议长度，可能被 OBS 折行或截断）" if count > limit else ""
        self.counter.setText(f"{count} / {limit} 字符{suffix}")

    # ---------------------------------------------------------------- 动作
    def _send(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            # 空输入不当作"清除" —— 清除有专门的按钮，避免误操作
            self.status.setText("字幕内容为空；要清除当前字幕请点「清除字幕」")
            return
        if self.callbacks["send"](text):
            self.status.setText(f"已发送：{text}")
            self.text_edit.clear()
        else:
            # 多半是刚好掉线 / 推流停了：刷新一下让原因显示出来
            self.status.setText("发送失败，请检查连接与推流状态")
            self.refresh()

    def _clear(self) -> None:
        # 空串是协议允许的，含义就是"清屏"
        if self.callbacks["clear"]():
            self.status.setText("已发送清除指令")
        else:
            self.status.setText("清除失败，请检查连接与推流状态")
            self.refresh()

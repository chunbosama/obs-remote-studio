"""连接配置对话框（A1）。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ...core.models import ConnectionConfig
from ...core.settings import AppSettings


class ConnectionProbe(QThread):
    """在后台线程里试连一次，避免对话框卡住。

    两代协议都要试：先按用户选定的协议（或自动探测）连，拿到版本信息回报。
    """

    succeeded = Signal(str, str)   # obs_version, protocol
    failed = Signal(str)

    def __init__(self, conn: ConnectionConfig, timeout: float, protocol: str = "auto",
                 parent=None):
        super().__init__(parent)
        self.conn = conn
        self.timeout = timeout
        self.protocol = protocol or "auto"

    def run(self) -> None:  # noqa: D102
        try:
            from ...core import protocol_v4 as P4
            from ...core.client_v4 import V4ReqClient, detect_protocol

            protocol = self.protocol
            if protocol == "auto":
                protocol = detect_protocol(self.conn.host, self.conn.port, self.timeout)
            if protocol == P4.MODE_COMPAT:
                client = V4ReqClient(
                    host=self.conn.host,
                    port=self.conn.port,
                    password=self.conn.password,
                    timeout=self.timeout,
                )
            else:
                from obsws_python import ReqClient

                client = ReqClient(
                    host=self.conn.host,
                    port=self.conn.port,
                    password=self.conn.password,
                    timeout=self.timeout,
                    subs=0,
                )
            version = client.get_version()
            client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(
            str(getattr(version, "obs_version", "") or ""), protocol
        )


class ConnectDialog(QDialog):
    def __init__(self, settings: AppSettings, current: ConnectionConfig, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("连接 OBS")
        self.setMinimumWidth(400)

        self.host_edit = QLineEdit(current.host)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(current.port)
        self.password_edit = QLineEdit(current.password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password = QCheckBox("显示密码")
        self.show_password.toggled.connect(self._toggle_password)
        self.remember_check = QCheckBox("记住密码")
        self.remember_check.setChecked(bool(current.password))

        # 协议：默认自动探测（v5 一多半、v4 少数，探测几乎零成本）。
        # 极少数环境下（代理缓冲首帧）探测可能判错，所以留手动指定的口子。
        self.protocol_combo = QComboBox()
        for label, value in (
            ("自动识别（推荐）", "auto"),
            ("仅 obs-websocket v5（标准模式）", "v5"),
            ("仅 obs-websocket v4（兼容模式，OBS ≤ 27）", "v4"),
        ):
            self.protocol_combo.addItem(label, value)
        wanted = getattr(settings.load_config(), "protocol", "auto")
        index = self.protocol_combo.findData(wanted)
        self.protocol_combo.setCurrentIndex(index if index >= 0 else 0)
        self.protocol_combo.setToolTip(
            "自动识别：连上后等约 2 秒，v5 会主动发握手帧、v4 一直静默。\n"
            "v4 为兼容模式：v4 没有的组件会自动隐藏或置灰。"
        )

        form = QFormLayout()
        form.addRow("地址", self.host_edit)
        form.addRow("端口", self.port_spin)
        form.addRow("密码", self.password_edit)
        form.addRow("", self.show_password)
        form.addRow("", self.remember_check)
        form.addRow("协议", self.protocol_combo)

        self.recent_combo = QComboBox()
        self.recent_combo.setPlaceholderText("最近连接")
        self.recent = settings.recent_connections()
        for conn in self.recent:
            self.recent_combo.addItem(conn.label(), conn.as_dict())
        self.recent_combo.activated.connect(self._apply_recent)
        form.addRow("历史", self.recent_combo)

        self.probe_btn = QPushButton("测试连接")
        self.probe_btn.clicked.connect(self._probe)
        self.probe_label = QLabel("")
        self.probe_label.setWordWrap(True)
        probe_row = QHBoxLayout()
        probe_row.addWidget(self.probe_btn)
        probe_row.addWidget(self.probe_label, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(probe_row)
        layout.addWidget(self._hint())
        layout.addWidget(buttons)

        self._probe_thread: ConnectionProbe | None = None

    @staticmethod
    def _hint() -> QLabel:
        label = QLabel(
            "前提：OBS 已启用 WebSocket 服务器并设置密码；连接局域网机器需放行对应端口。\n"
            "OBS ≥ 28 用 v5（标准模式）；OBS ≤ 27 用 v4（兼容模式，会自动识别）。"
        )
        label.setWordWrap(True)
        label.setStyleSheet("color: #7b8794;")
        return label

    def _toggle_password(self, checked: bool) -> None:
        self.password_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _apply_recent(self, index: int) -> None:
        data = self.recent_combo.itemData(index)
        if not isinstance(data, dict):
            return
        self.host_edit.setText(str(data.get("host", "")))
        self.port_spin.setValue(int(data.get("port", 4455)))
        self.password_edit.setText(str(data.get("password", "")))

    def _probe(self) -> None:
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        self.probe_label.setText("正在测试…")
        self.probe_btn.setEnabled(False)
        conn = self.result_config()
        self._probe_thread = ConnectionProbe(
            conn, 3.0, self.protocol_combo.currentData() or "auto", self
        )
        self._probe_thread.succeeded.connect(self._probe_ok)
        self._probe_thread.failed.connect(self._probe_fail)
        self._probe_thread.finished.connect(lambda: self.probe_btn.setEnabled(True))
        self._probe_thread.start()

    def _probe_ok(self, obs_version: str, protocol: str) -> None:
        from ...core import protocol_v4 as P4

        mode = P4.mode_label(protocol)
        self.probe_label.setText(
            f"连接成功（OBS {obs_version or '未知版本'}，{mode}）"
        )
        self.probe_label.setStyleSheet("color: #1f9d55;")

    def _probe_fail(self, message: str) -> None:
        self.probe_label.setText(f"连接失败：{message}")
        self.probe_label.setStyleSheet("color: #d7263d;")

    def result_config(self) -> ConnectionConfig:
        password = self.password_edit.text() if self.remember_check.isChecked() else ""
        return ConnectionConfig(
            host=self.host_edit.text().strip() or "127.0.0.1",
            port=self.port_spin.value(),
            password=password,
        )

    def protocol_choice(self) -> str:
        """用户选的协议：auto / v5 / v4。"""
        return str(self.protocol_combo.currentData() or "auto")

    def accept(self) -> None:  # noqa: D102
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "连接 OBS", "请填写地址")
            return
        super().accept()

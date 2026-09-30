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
    """在后台线程里试连一次，避免对话框卡住。"""

    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, conn: ConnectionConfig, timeout: float, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.timeout = timeout

    def run(self) -> None:  # noqa: D102
        try:
            from obsws_python import ReqClient

            client = ReqClient(
                host=self.conn.host,
                port=self.conn.port,
                password=self.conn.password,
                timeout=self.timeout,
            )
            version = client.get_version()
            client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(str(getattr(version, "obs_version", "") or ""))


class ConnectDialog(QDialog):
    def __init__(self, settings: AppSettings, current: ConnectionConfig, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("连接 OBS")
        self.setMinimumWidth(380)

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

        form = QFormLayout()
        form.addRow("地址", self.host_edit)
        form.addRow("端口", self.port_spin)
        form.addRow("密码", self.password_edit)
        form.addRow("", self.show_password)
        form.addRow("", self.remember_check)

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
            "前提：OBS ≥ 28，已在「工具 → WebSocket 服务器设置」启用服务器并设置密码；"
            "连接局域网机器需放行 TCP 4455。"
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
        self._probe_thread = ConnectionProbe(conn, 3.0, self)
        self._probe_thread.succeeded.connect(self._probe_ok)
        self._probe_thread.failed.connect(self._probe_fail)
        self._probe_thread.finished.connect(lambda: self.probe_btn.setEnabled(True))
        self._probe_thread.start()

    def _probe_ok(self, obs_version: str) -> None:
        self.probe_label.setText(f"连接成功（OBS {obs_version or '未知版本'}）")
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

    def accept(self) -> None:  # noqa: D102
        if not self.host_edit.text().strip():
            QMessageBox.warning(self, "连接 OBS", "请填写地址")
            return
        super().accept()

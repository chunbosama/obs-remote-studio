"""设置对话框：重连策略、轮询间隔、请求超时（I2）。"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ...core.models import AppConfig
from ...utils import global_hotkeys, logging_setup


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(360)
        self.config = config

        self.reconnect_check = QCheckBox("断开后自动重连")
        self.reconnect_check.setChecked(config.reconnect.enabled)
        self.initial_spin = QSpinBox()
        self.initial_spin.setRange(200, 60_000)
        self.initial_spin.setSuffix(" ms")
        self.initial_spin.setValue(config.reconnect.initial_delay_ms)
        self.max_delay_spin = QSpinBox()
        self.max_delay_spin.setRange(1000, 600_000)
        self.max_delay_spin.setSuffix(" ms")
        self.max_delay_spin.setValue(config.reconnect.max_delay_ms)
        self.attempts_spin = QSpinBox()
        self.attempts_spin.setRange(0, 999)
        self.attempts_spin.setSpecialValueText("无限")
        self.attempts_spin.setValue(config.reconnect.max_attempts)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(200, 10_000)
        self.poll_spin.setSuffix(" ms")
        self.poll_spin.setValue(config.poll_interval_ms)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.5, 30.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(config.request_timeout_s)
        self.auto_check = QCheckBox("启动时自动连接上次配置")
        self.auto_check.setChecked(config.auto_connect_on_startup)
        self.preview_spin = QSpinBox()
        self.preview_spin.setRange(200, 10_000)
        self.preview_spin.setSuffix(" ms")
        self.preview_spin.setValue(config.preview_interval_ms)
        self.preview_quality_spin = QSpinBox()
        self.preview_quality_spin.setRange(10, 100)
        self.preview_quality_spin.setValue(config.preview_quality)

        # E：混音器
        self.meters_check = QCheckBox("订阅电平表（高频事件，重连后生效）")
        self.meters_check.setChecked(config.audio_meters)
        self.percent_check = QCheckBox("推子显示百分比（默认 dB）")
        self.percent_check.setChecked(config.mixer_show_percent)

        # H6：托盘与全局热键
        self.tray_check = QCheckBox("启用系统托盘图标")
        self.tray_check.setChecked(config.tray_enabled)
        self.close_to_tray_check = QCheckBox("关闭窗口时最小化到托盘")
        self.close_to_tray_check.setChecked(config.close_to_tray)
        self.hotkey_check = QCheckBox("启用全局热键（Ctrl+Alt+R 录制 / +L 直播 / +1~9 场景）")
        self.hotkey_check.setChecked(config.global_hotkeys)
        if not global_hotkeys.available():
            self.hotkey_check.setEnabled(False)
            self.hotkey_check.setText(self.hotkey_check.text() + "：当前系统不支持")

        # D10：防误触
        self.confirm_stop_check = QCheckBox("停止直播前二次确认")
        self.confirm_stop_check.setChecked(config.confirm_stop_stream)

        # I3：日志
        self.log_level_combo = QComboBox()
        for name in logging_setup.LEVELS:
            self.log_level_combo.addItem(
                logging_setup.LEVEL_LABELS.get(name, name), name
            )
        index = self.log_level_combo.findData(config.log_level)
        self.log_level_combo.setCurrentIndex(index if index >= 0 else 1)
        self.log_to_file_check = QCheckBox("同时写入日志文件（排障用）")
        self.log_to_file_check.setChecked(config.log_to_file)
        self.log_path_label = QLabel(str(logging_setup.log_file_path()))
        self.log_path_label.setObjectName("hint")
        self.log_path_label.setWordWrap(True)
        self.open_log_btn = QPushButton("打开日志目录")
        self.open_log_btn.clicked.connect(self._open_log_dir)

        # A11：连接健康。心跳间隔 0 = 关（不额外发请求）
        self.heartbeat_spin = QSpinBox()
        self.heartbeat_spin.setRange(0, 60_000)
        self.heartbeat_spin.setSingleStep(1000)
        self.heartbeat_spin.setSuffix(" ms")
        self.heartbeat_spin.setSpecialValueText("关闭")
        self.heartbeat_spin.setValue(max(0, int(config.heartbeat_interval_ms)))
        self.rtt_warn_spin = QSpinBox()
        self.rtt_warn_spin.setRange(50, 10_000)
        self.rtt_warn_spin.setSingleStep(50)
        self.rtt_warn_spin.setSuffix(" ms")
        self.rtt_warn_spin.setValue(int(config.rtt_warn_ms))

        # D17：磁盘与时长预警。0 = 关掉该项
        self.disk_warn_spin = QDoubleSpinBox()
        self.disk_warn_spin.setRange(0.0, 1000.0)
        self.disk_warn_spin.setSingleStep(0.5)
        self.disk_warn_spin.setDecimals(1)
        self.disk_warn_spin.setSuffix(" GB")
        self.disk_warn_spin.setSpecialValueText("关闭")
        self.disk_warn_spin.setValue(float(config.disk_warn_gb))
        self.record_minutes_spin = QSpinBox()
        self.record_minutes_spin.setRange(0, 24 * 60)
        self.record_minutes_spin.setSuffix(" 分钟")
        self.record_minutes_spin.setSpecialValueText("关闭")
        self.record_minutes_spin.setValue(int(config.record_warn_minutes))

        form = QFormLayout()
        form.addRow("", self.reconnect_check)
        form.addRow("初始重连间隔", self.initial_spin)
        form.addRow("最大重连间隔", self.max_delay_spin)
        form.addRow("最大重试次数", self.attempts_spin)
        form.addRow("状态轮询间隔", self.poll_spin)
        form.addRow("请求超时", self.timeout_spin)
        form.addRow("", self.auto_check)
        form.addRow("预览缩略图间隔", self.preview_spin)
        form.addRow("缩略图质量", self.preview_quality_spin)
        form.addRow("心跳间隔", self.heartbeat_spin)
        form.addRow("判卡阈值", self.rtt_warn_spin)
        form.addRow("录制目录剩余空间预警", self.disk_warn_spin)
        form.addRow("连续录制时长预警", self.record_minutes_spin)
        form.addRow("", self.meters_check)
        form.addRow("", self.percent_check)
        form.addRow("", self.tray_check)
        form.addRow("", self.close_to_tray_check)
        form.addRow("", self.hotkey_check)
        form.addRow("", self.confirm_stop_check)
        form.addRow("日志级别", self.log_level_combo)
        form.addRow("", self.log_to_file_check)
        form.addRow("日志位置", self.log_path_label)
        form.addRow("", self.open_log_btn)

        note = QLabel(
            "重连间隔按指数退避增长，直到达到上限。\n"
            "缩略图每帧都要 OBS 端完整编码一次，间隔越短越吃 CPU（1000 ms ≈ 1 fps）。\n"
            "电平表是 ~20Hz 的高频事件，带宽紧张时可以关掉。\n"
            "心跳用来量「通但慢」：空闲时发一次最轻的请求算往返延迟，连续超阈值就提示卡顿。"
            "填 0 可关闭。\n"
            "剩余空间预警只在 OBS 跑在本机时有效 —— 控局域网另一台时读不到对方的磁盘。\n"
            "全局热键可能与其它软件冲突；关掉窗口最小化到托盘后，用托盘菜单退出。\n"
            "日志文件按 1 MB 滚动，最多保留 3 份。"
        )
        note.setStyleSheet("color: #7b8794;")
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _open_log_dir(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        directory = logging_setup.log_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def result_config(self) -> AppConfig:
        reconnect = replace(
            self.config.reconnect,
            enabled=self.reconnect_check.isChecked(),
            initial_delay_ms=self.initial_spin.value(),
            max_delay_ms=self.max_delay_spin.value(),
            max_attempts=self.attempts_spin.value(),
        )
        return replace(
            self.config,
            reconnect=reconnect,
            poll_interval_ms=self.poll_spin.value(),
            request_timeout_s=self.timeout_spin.value(),
            auto_connect_on_startup=self.auto_check.isChecked(),
            preview_interval_ms=self.preview_spin.value(),
            preview_quality=self.preview_quality_spin.value(),
            audio_meters=self.meters_check.isChecked(),
            mixer_show_percent=self.percent_check.isChecked(),
            tray_enabled=self.tray_check.isChecked(),
            close_to_tray=self.close_to_tray_check.isChecked(),
            global_hotkeys=self.hotkey_check.isChecked(),
            confirm_stop_stream=self.confirm_stop_check.isChecked(),
            log_level=str(self.log_level_combo.currentData() or "INFO"),
            log_to_file=self.log_to_file_check.isChecked(),
            # A11 / D17
            heartbeat_interval_ms=self.heartbeat_spin.value(),
            rtt_warn_ms=self.rtt_warn_spin.value(),
            disk_warn_gb=self.disk_warn_spin.value(),
            record_warn_minutes=self.record_minutes_spin.value(),
        )

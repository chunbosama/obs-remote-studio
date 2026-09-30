"""配置持久化（QSettings）。

密码目前以明文存放在 QSettings；若后续要接 keyring，
只需替换 _read_password / _write_password 两个函数，上层无感知。
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings

from .models import AppConfig, ConnectionConfig, ReconnectPolicy

ORG_NAME = "obs-remote-studio"
APP_NAME = "OBS Remote Studio"

MAX_RECENT = 10

# 测试隔离：Windows 上 QSettings(org, app) 走的是**注册表**，
# 光调 QSettings.setPath(IniFormat, ...) 没用（格式不对）。
# 所以用环境变量换一个 org 名并用 INI 格式，测试再也碰不到真实配置。
ENV_ORG = "OBSRS_SETTINGS_ORG"
ENV_USE_INI = "OBSRS_SETTINGS_INI"


class AppSettings:
    def __init__(self, org: str | None = None, app: str | None = None) -> None:
        org = org or os.environ.get(ENV_ORG, ORG_NAME)
        app = app or APP_NAME
        if os.environ.get(ENV_USE_INI):
            self._qs = QSettings(
                QSettings.Format.IniFormat, QSettings.Scope.UserScope, org, app
            )
        else:
            self._qs = QSettings(org, app)

    @property
    def file_name(self) -> str:
        return self._qs.fileName()

    # ------------------------------------------------------------ 连接配置
    def load_config(self) -> AppConfig:
        cfg = AppConfig()
        cfg.connection = ConnectionConfig(
            host=self._get("connection/host", "127.0.0.1"),
            port=int(self._get("connection/port", 4455)),
            password=self._read_password(),
        )
        cfg.reconnect = ReconnectPolicy(
            enabled=self._get("reconnect/enabled", True, bool),
            initial_delay_ms=int(self._get("reconnect/initial_delay_ms", 1000)),
            max_delay_ms=int(self._get("reconnect/max_delay_ms", 30_000)),
            max_attempts=int(self._get("reconnect/max_attempts", 0)),
        )
        cfg.poll_interval_ms = int(self._get("poll/interval_ms", 1000))
        cfg.stats_every_n_polls = int(self._get("poll/stats_every_n_polls", 3))
        cfg.request_timeout_s = float(self._get("poll/request_timeout_s", 3.0))
        cfg.auto_connect_on_startup = self._get("general/auto_connect", False, bool)
        cfg.preview_interval_ms = int(self._get("preview/interval_ms", 1000))
        cfg.preview_width = int(self._get("preview/width", 480))
        cfg.preview_quality = int(self._get("preview/quality", 60))
        cfg.tray_enabled = self._get("window/tray", True, bool)
        cfg.close_to_tray = self._get("window/close_to_tray", False, bool)
        cfg.global_hotkeys = self._get("window/global_hotkeys", False, bool)
        cfg.confirm_stop_stream = self._get("general/confirm_stop_stream", True, bool)
        cfg.log_level = str(self._get("logging/level", "INFO") or "INFO").upper()
        cfg.log_to_file = self._get("logging/to_file", False, bool)
        cfg.audio_meters = self._get("audio/meters", True, bool)
        cfg.mixer_show_percent = self._get("audio/show_percent", False, bool)
        cfg.mixer_hidden_inputs = self._load_hidden_inputs()
        # A11：连接健康
        cfg.heartbeat_interval_ms = int(self._get("health/heartbeat_ms", 5000))
        cfg.rtt_warn_ms = int(self._get("health/rtt_warn_ms", 500))
        cfg.rtt_slow_streak = int(self._get("health/rtt_slow_streak", 3))
        # D17：磁盘与时长预警
        cfg.disk_warn_gb = float(self._get("record/disk_warn_gb", 2.0))
        cfg.record_warn_minutes = int(self._get("record/warn_minutes", 0))
        cfg.record_warn_gb = float(self._get("record/warn_gb", 0.0))
        # G5 / H9 / H10
        cfg.quick_transitions = self._load_quick_transitions()
        cfg.status_segments = self._load_str_list("ui/status_segments")
        cfg.compact_mode = self._get("ui/compact_mode", False, bool)
        return cfg

    def _load_hidden_inputs(self) -> list[str]:
        raw = self._get("audio/hidden_inputs", "[]")
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(item) for item in items] if isinstance(items, list) else []

    def _load_str_list(self, key: str) -> list[str]:
        raw = self._get(key, "[]")
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [str(item) for item in items] if isinstance(items, list) else []

    def _load_quick_transitions(self) -> list[dict]:
        """G5：快捷转场槽位。脏数据一律丢掉，别让配置把界面带崩。"""
        raw = self._get("transitions/quick", "[]")
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(items, list):
            return []
        slots: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("transition"):
                continue
            slots.append(
                {
                    "transition": str(item["transition"]),
                    "duration_ms": int(item.get("duration_ms", 300) or 300),
                }
            )
        return slots

    def save_connection(self, conn: ConnectionConfig) -> None:
        self._qs.setValue("connection/host", conn.host)
        self._qs.setValue("connection/port", conn.port)
        self._write_password(conn.password)
        self._add_recent(conn)
        self._qs.sync()

    def save_config(self, cfg: AppConfig) -> None:
        self.save_connection(cfg.connection)
        self._qs.setValue("reconnect/enabled", cfg.reconnect.enabled)
        self._qs.setValue("reconnect/initial_delay_ms", cfg.reconnect.initial_delay_ms)
        self._qs.setValue("reconnect/max_delay_ms", cfg.reconnect.max_delay_ms)
        self._qs.setValue("reconnect/max_attempts", cfg.reconnect.max_attempts)
        self._qs.setValue("poll/interval_ms", cfg.poll_interval_ms)
        self._qs.setValue("poll/stats_every_n_polls", cfg.stats_every_n_polls)
        self._qs.setValue("poll/request_timeout_s", cfg.request_timeout_s)
        self._qs.setValue("general/auto_connect", cfg.auto_connect_on_startup)
        self._qs.setValue("preview/interval_ms", cfg.preview_interval_ms)
        self._qs.setValue("preview/width", cfg.preview_width)
        self._qs.setValue("preview/quality", cfg.preview_quality)
        self._qs.setValue("window/tray", cfg.tray_enabled)
        self._qs.setValue("window/close_to_tray", cfg.close_to_tray)
        self._qs.setValue("window/global_hotkeys", cfg.global_hotkeys)
        self._qs.setValue("general/confirm_stop_stream", cfg.confirm_stop_stream)
        self._qs.setValue("logging/level", cfg.log_level)
        self._qs.setValue("logging/to_file", cfg.log_to_file)
        self._qs.setValue("audio/meters", cfg.audio_meters)
        self._qs.setValue("audio/show_percent", cfg.mixer_show_percent)
        self._qs.setValue(
            "audio/hidden_inputs",
            json.dumps(sorted(set(cfg.mixer_hidden_inputs)), ensure_ascii=False),
        )
        self._qs.setValue("health/heartbeat_ms", cfg.heartbeat_interval_ms)
        self._qs.setValue("health/rtt_warn_ms", cfg.rtt_warn_ms)
        self._qs.setValue("health/rtt_slow_streak", cfg.rtt_slow_streak)
        self._qs.setValue("record/disk_warn_gb", cfg.disk_warn_gb)
        self._qs.setValue("record/warn_minutes", cfg.record_warn_minutes)
        self._qs.setValue("record/warn_gb", cfg.record_warn_gb)
        self._qs.setValue(
            "transitions/quick", json.dumps(cfg.quick_transitions, ensure_ascii=False)
        )
        self._qs.setValue(
            "ui/status_segments", json.dumps(list(cfg.status_segments), ensure_ascii=False)
        )
        self._qs.setValue("ui/compact_mode", cfg.compact_mode)
        self._qs.sync()

    # ------------------------------------------------------------ 最近连接
    def recent_connections(self) -> list[ConnectionConfig]:
        raw = self._get("connection/recent", "[]")
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
        out: list[ConnectionConfig] = []
        for item in items:
            if isinstance(item, dict) and item.get("host"):
                out.append(
                    ConnectionConfig(
                        host=str(item["host"]),
                        port=int(item.get("port", 4455)),
                        password=str(item.get("password", "")),
                    )
                )
        return out

    def _add_recent(self, conn: ConnectionConfig) -> None:
        items = [c for c in self.recent_connections() if c.label() != conn.label()]
        items.insert(0, conn)
        payload = [
            {"host": c.host, "port": c.port, "password": c.password}
            for c in items[:MAX_RECENT]
        ]
        self._qs.setValue("connection/recent", json.dumps(payload, ensure_ascii=False))

    # ------------------------------------------------------------ 窗口布局
    def save_layout(self, geometry: bytes, state: bytes) -> None:
        self._qs.setValue("ui/geometry", geometry)
        self._qs.setValue("ui/state", state)

    def save_json(self, key: str, value) -> None:
        self._qs.setValue(f"ui/{key}", json.dumps(value, ensure_ascii=False))

    def load_json(self, key: str, default=None):
        raw = self._qs.value(f"ui/{key}")
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    def save_theme(self, name: str) -> None:
        self._qs.setValue("ui/theme", name)
    def load_theme(self, default: str = "dark") -> str:
        return str(self._qs.value("ui/theme", default) or default)

    def load_geometry(self) -> bytes | None:
        value = self._qs.value("ui/geometry")
        return value if isinstance(value, (bytes, bytearray)) else None

    # H10：迷你模式单独存一套几何，免得两种模式互相把尺寸覆盖掉
    def save_compact_geometry(self, geometry: bytes) -> None:
        self._qs.setValue("ui/compact_geometry", geometry)

    def load_compact_geometry(self) -> bytes | None:
        value = self._qs.value("ui/compact_geometry")
        return value if isinstance(value, (bytes, bytearray)) else None

    def load_state(self) -> bytes | None:
        value = self._qs.value("ui/state")
        return value if isinstance(value, (bytes, bytearray)) else None

    # ------------------------------------------------------------ 内部
    def _get(self, key: str, default, cast=None):
        value = self._qs.value(key)
        if value is None:
            return default
        if cast is bool:
            return str(value).lower() in ("true", "1", "yes")
        return cast(value) if cast else value

    def _read_password(self) -> str:
        return str(self._qs.value("connection/password", "") or "")

    def _write_password(self, password: str) -> None:
        self._qs.setValue("connection/password", password)

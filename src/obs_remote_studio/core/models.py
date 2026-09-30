"""MVP 用到的数据模型。纯 dataclass，不依赖 Qt / 网络。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 4455
    password: str = ""

    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def as_dict(self) -> dict[str, object]:
        return {"host": self.host, "port": self.port, "password": self.password}


@dataclass(frozen=True)
class Scene:
    name: str
    index: int


@dataclass
class SceneItem:
    item_id: int
    source_name: str
    source_type: str = ""
    source_kind: str = ""
    enabled: bool = True


@dataclass
class RecordStatus:
    active: bool = False
    paused: bool = False
    timecode: str = ""
    duration_ms: int = 0
    bytes_written: int = 0
    output_path: str = ""


@dataclass
class StreamStatus:
    active: bool = False
    reconnecting: bool = False
    timecode: str = ""
    duration_ms: int = 0
    congestion: float = 0.0
    bytes_written: int = 0
    skipped_frames: int = 0
    total_frames: int = 0


@dataclass
class ReplayBufferStatus:
    """D7：回放缓冲区状态。"""

    active: bool = False
    saved_path: str = ""  # 最近一次「保存回放」落盘的文件


@dataclass
class VirtualcamStatus:
    """D8：虚拟摄像机状态。"""

    active: bool = False


@dataclass
class MediaStatus:
    """L：一个媒体源的播放状态。"""

    name: str
    state: str = "OBS_MEDIA_STATE_NONE"
    duration_ms: int = 0   # VLC 源刚开播时可能读不到，容忍 0
    cursor_ms: int = 0

    @property
    def playing(self) -> bool:
        return self.state in ("OBS_MEDIA_STATE_PLAYING", "OBS_MEDIA_STATE_OPENING",
                             "OBS_MEDIA_STATE_BUFFERING")

    @property
    def paused(self) -> bool:
        return self.state == "OBS_MEDIA_STATE_PAUSED"


@dataclass
class ConnectionHealth:
    """A11：连接质量。慢 ≠ 断，这里专门记「通但卡」。"""

    rtt_ms: float = 0.0
    samples: int = 0
    slow_streak: int = 0      # 连续几次超阈值
    stalled: bool = False

    @property
    def label(self) -> str:
        if self.samples == 0:
            return "延迟 --"
        return f"延迟 {self.rtt_ms:.0f} ms"


@dataclass
class RecordWarning:
    """D17：磁盘 / 时长预警。level: "" | "warn" | "danger"。"""

    level: str = ""
    message: str = ""
    free_gb: float | None = None   # None = 拿不到（例如 OBS 在另一台机器上）


@dataclass
class AudioInput:
    """混音器里的一条音频源。"""

    name: str
    kind: str = ""
    volume_db: float = 0.0
    volume_mul: float = 1.0
    muted: bool = False
    # 下面几项按需拉取（打开高级音频属性时才查），None 表示还没取过
    monitor_type: str | None = None
    balance: float | None = None
    sync_offset_ms: int | None = None
    tracks: int | None = None


@dataclass
class ObsStats:
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    disk_space_mb: float = 0.0
    fps: float = 0.0
    render_skipped_frames: int = 0
    render_total_frames: int = 0
    output_skipped_frames: int = 0
    output_total_frames: int = 0


@dataclass
class VideoSettings:
    base_width: int = 0
    base_height: int = 0
    output_width: int = 0
    output_height: int = 0
    fps: float = 0.0


@dataclass
class ServerInfo:
    obs_version: str = ""
    websocket_version: str = ""
    rpc_version: int = 0

    def label(self) -> str:
        parts = [p for p in (self.obs_version, f"ws {self.websocket_version}") if p]
        return " / ".join(parts) if parts else "未知版本"


@dataclass
class ReconnectPolicy:
    enabled: bool = True
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30_000
    max_attempts: int = 0  # 0 = 无限重试


@dataclass
class AppConfig:
    """运行时可配置项（由 settings.py 持久化）。"""

    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    poll_interval_ms: int = 1000
    stats_every_n_polls: int = 3
    request_timeout_s: float = 3.0
    auto_connect_on_startup: bool = False
    # F2：缩略图间隔。每次调用都要 OBS 端完整编码一帧，别调太快。
    preview_interval_ms: int = 1000
    preview_width: int = 480
    preview_quality: int = 60
    # H6：托盘与全局热键
    tray_enabled: bool = True
    close_to_tray: bool = False
    global_hotkeys: bool = False       # 全局热键可能与其它软件冲突，默认关闭
    # D10：防误触。停止直播是不可逆的中断，默认要二次确认
    confirm_stop_stream: bool = True
    # I3：日志
    log_level: str = "INFO"            # DEBUG / INFO / WARNING / ERROR
    log_to_file: bool = False
    # E：混音器
    audio_meters: bool = True          # 订阅电平表（高频事件，关闭可省流量）
    mixer_show_percent: bool = False   # 推子显示百分比而不是 dB
    mixer_hidden_inputs: list[str] = field(default_factory=list)  # 手动隐藏的噪声源

    # A11：连接健康。空闲时发一次 GetVersion 当心跳，用它算 RTT。
    # 0 = 关闭心跳（不额外发请求）
    heartbeat_interval_ms: int = 5000
    rtt_warn_ms: int = 500             # 单次超过它算「慢」
    rtt_slow_streak: int = 3           # 连续几次慢判定为「卡」

    # D17：磁盘与时长预警
    disk_warn_gb: float = 2.0          # 录制目录剩余空间低于它报警（0 = 关）
    record_warn_minutes: int = 0       # 录制超过 N 分钟报警（0 = 关）
    record_warn_gb: float = 0.0        # 录制文件超过 N GB 报警（0 = 关）

    # G5：客户端侧快捷转场槽位。每项 {"transition": str, "duration_ms": int}
    quick_transitions: list[dict] = field(default_factory=list)

    # H9：状态栏显示哪些段（空 = 全显示，首次运行不写死默认值）
    status_segments: list[str] = field(default_factory=list)
    # H10：紧凑/迷你模式
    compact_mode: bool = False

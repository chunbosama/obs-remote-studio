"""OBS 端插件（vendor `obsrs`）的接口契约与纯解析逻辑。

契约出处：插件侧的 `docs/插件接口-MVP.md`（v0.2.1 / apiVersion 1）。
**改字段先改那份文档，再改这里** —— 客户端常量、假 vendor、C++ 实现三方都对齐它。

`tbar` 的 `release` 语义在插件 0.2.1 改过（详见契约 §3）：它会**结束手动转场会话**，
按位置判"完成 / 回退"，不再转发 `obs_frontend_release_tbar()`（那条路在 OBS ≥29.1
上永远落进取消分支）。客户端因此必须记住"会话还开着"，并在任何转场动作前收尾 ——
见 `controller.unwind_tbar_session()`。

三条从契约里带过来的硬规矩：

1. 每个请求**必有** `ok` 字段，成功与否只看它，不用"字段缺失"推断；
2. 能力探测只看 `features[]` / `featureMap`，**绝不比较插件版本号**
   （功能规划 §10.6 红线 7）；
3. `error` 只给人看，**不做字符串匹配来分支** —— 分支一律看 `ok` / `features`。

本模块不依赖 Qt / 网络，便于单测；网络与线程都在 worker 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 身份
VENDOR = "obsrs"
#: 0.1.x 客户端的旧 vendor 名。请求可以用它，但**事件只从主名发一次**。
VENDOR_ALIAS = "obs-remote-studio-helper"
API_VERSION = 1

# ---------------------------------------------------------------- 请求名
REQ_GET_INFO = "getInfo"
REQ_OPEN_STREAM = "openStream"
REQ_CLOSE_STREAM = "closeStream"
REQ_GET_STREAM = "getStream"
REQ_TBAR = "tbar"
REQ_GET_TBAR = "getTbar"
REQ_MACHINE_STATS = "getMachineStats"
REQ_RECENT_LOGS = "getRecentLogs"
REQ_SET_LOG_STREAM = "setLogStream"
REQ_CAPTURE_STATE = "getCaptureState"

# ---------------------------------------------------------------- 事件名
EVENT_STREAM_STATS = "streamStats"
EVENT_TBAR_CHANGED = "tbarChanged"
EVENT_LOG_LINE = "logLine"

# ---------------------------------------------------------------- 能力 ID
FEATURE_INFO = "info"
FEATURE_STREAM = "stream.mjpeg"
FEATURE_STREAM_PREVIEW = "stream.role.preview"
FEATURE_TBAR = "tbar"
FEATURE_MACHINE = "machine.stats"
FEATURE_LOGS = "logs"
FEATURE_ENCODER = "encoder.stats"
FEATURE_CAPTURE = "capture.indicator"

#: 六项「插件 MVP 必备」+ 两项未实现。顺序即界面上的展示顺序。
FEATURE_ORDER: tuple[tuple[str, str], ...] = (
    (FEATURE_INFO, "插件自述"),
    (FEATURE_STREAM, "实时画面"),
    (FEATURE_STREAM_PREVIEW, "预览画面（未实现）"),
    (FEATURE_TBAR, "推杆直驱"),
    (FEATURE_MACHINE, "机器状态"),
    (FEATURE_LOGS, "日志"),
    (FEATURE_ENCODER, "编码器明细（未实现）"),
    (FEATURE_CAPTURE, "采集指示"),
)

# 模式名（标题 / 状态栏 / 关于共用一处，避免三处各写一份）
MODE_ADVANCED = "进阶模式"
MODE_STANDARD = "标准模式"
MODE_COMPAT = "兼容模式"


def mode_label(protocol: str, plugin_active: bool) -> str:
    """连接形态 → 界面上的模式名。

    **插件优先**：装了插件就是"进阶模式"，不再区分 v5/v4 ——
    插件只存在于 v5 会话（vendor 是 obs-websocket 5 的扩展点），
    所以"进阶 + 标准"同时出现没有意义，只会让人以为有两个维度。
    """
    if plugin_active:
        return MODE_ADVANCED
    return MODE_COMPAT if protocol == "v4" else MODE_STANDARD


# ---------------------------------------------------------------- 数据结构
@dataclass
class PluginInfo:
    """`getInfo` 的结果。**取不到就是没装插件**，不存在"部分可用"。"""

    plugin_version: str = ""
    api_version: int = 0
    vendor: str = ""
    obs_version: str = ""
    websocket_api_version: str = ""
    features: dict[str, bool] = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    capture_active: bool = False
    capture_indicator: bool = False

    def supports(self, feature: str) -> bool:
        """某项能力是否可用。

        未实现的能力在契约里是**显式 false**，所以这里按"必须为 True"判断 ——
        缺席（老插件没报）与 false 同样视为不可用，界面上会置灰并给原因。
        """
        return bool(self.features.get(feature))

    def label(self) -> str:
        if not self.plugin_version:
            return "插件"
        return f"插件 {self.plugin_version}"


@dataclass
class MachineStats:
    """`getMachineStats`：OBS 那台机器上的磁盘 / 录制目录实况。

    比本机 `shutil.disk_usage` 强的地方不在数字，而在**错误码**：
    局域网 UNC 断线时能拿到 53/64/67，而不是一个沉默的 None。
    """

    hostname: str = ""
    obs_uptime_ms: int = 0
    record_dir: str = ""
    record_dir_known: bool = False
    record_dir_is_unc: bool = False
    record_dir_exists: bool = False
    record_dir_writable: bool = False
    record_dir_error: int = 0
    disk_known: bool = False
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0
    disk_avail_bytes: int = 0
    disk_error: int = 0
    recording_active: bool = False
    recording_paused: bool = False
    current_file: str = ""
    current_file_bytes: int = 0
    last_file: str = ""

    def free_gb(self) -> float | None:
        if not self.disk_known or self.disk_avail_bytes <= 0:
            return None
        return self.disk_avail_bytes / (1024 ** 3)

    def disk_problem(self) -> str:
        """为什么读不到剩余空间（人话）。读得到时回空串。"""
        if self.disk_known:
            return ""
        if not self.record_dir_known:
            return "插件不知道 OBS 的录制目录"
        code = self.disk_error or self.record_dir_error
        hint = WINERROR_HINTS.get(int(code), "")
        base = f"读不到 {self.record_dir or '录制目录'} 所在磁盘"
        if self.record_dir_is_unc:
            base += "（UNC 网络路径）"
        if code:
            base += f"：系统错误 {code}" + (f"（{hint}）" if hint else "")
        return base


#: Windows 常见错误码 → 人话。只覆盖"读盘/网络路径"这一路上真会遇到的。
WINERROR_HINTS: dict[int, str] = {
    2: "找不到文件",
    3: "找不到路径",
    5: "拒绝访问",
    21: "设备未就绪",
    53: "找不到网络路径",
    64: "指定的网络名不再可用",
    67: "找不到网络名",
    123: "文件名或路径语法错误",
    1326: "登录失败：用户名或密码不正确",
}


@dataclass
class LogLine:
    timestamp_ms: int = 0
    level: int = 0
    level_name: str = "info"
    message: str = ""


@dataclass
class TbarState:
    position: float = 0.0
    studio_mode: bool = False
    transitioning: bool = False
    transition: str = ""
    duration_ms: int = 0
    frontend_position: float | None = None
    applied: bool = False


@dataclass
class CaptureState:
    active: bool = False
    stream_running: bool = False
    viewers: int = 0
    indicator_running: bool = False
    indicator_visible: bool = False


@dataclass
class StreamInfo:
    """`openStream` 的响应。`url` 是**数据面**地址，与控制面无关。"""

    running: bool = False
    reused: bool = False
    role: str = "program"
    url: str = ""
    snapshot_url: str = ""
    token: str = ""
    width: int = 0
    height: int = 0
    target_fps: int = 0
    quality: int = 0
    expires_at_ms: int = 0


# ---------------------------------------------------------------- 解析
def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value) -> bool:
    return bool(value)


def _ok(payload) -> bool:
    return bool(_dict(payload).get("ok"))


def parse_info(payload) -> PluginInfo | None:
    """解析 `getInfo`。`ok=false` / 结构不对 → None（＝当没装插件）。"""
    data = _dict(payload)
    if not data.get("ok"):
        return None
    features: dict[str, bool] = {}
    # 契约里两种形状都给：featureMap 便于判空，features 数组是 libobs 唯一能序列化的形式。
    for name, enabled in _dict(data.get("featureMap")).items():
        features[str(name)] = bool(enabled)
    for item in data.get("features") or []:
        if isinstance(item, dict) and item.get("name"):
            features[str(item["name"])] = bool(item.get("enabled", True))
    return PluginInfo(
        plugin_version=str(data.get("pluginVersion", "") or ""),
        api_version=_int(data.get("apiVersion")),
        vendor=str(data.get("vendor", "") or ""),
        obs_version=str(data.get("obsVersion", "") or ""),
        websocket_api_version=str(data.get("websocketApiVersion", "") or ""),
        features=features,
        limits=_dict(data.get("limits")),
        capture_active=_bool(data.get("captureActive")),
        capture_indicator=_bool(data.get("captureIndicator")),
    )


def parse_machine_stats(payload) -> MachineStats | None:
    data = _dict(payload)
    if not data.get("ok"):
        return None
    rec = _dict(data.get("recordDir"))
    disk = _dict(data.get("disk"))
    recording = _dict(data.get("recording"))
    return MachineStats(
        hostname=str(data.get("hostname", "") or ""),
        obs_uptime_ms=_int(data.get("obsUptimeMs")),
        record_dir=str(rec.get("path", "") or ""),
        record_dir_known=_bool(rec.get("known")),
        record_dir_is_unc=_bool(rec.get("isUnc")),
        record_dir_exists=_bool(rec.get("exists")),
        record_dir_writable=_bool(rec.get("writable")),
        record_dir_error=_int(rec.get("writeError")),
        disk_known=_bool(disk.get("known")),
        disk_total_bytes=_int(disk.get("totalBytes")),
        disk_free_bytes=_int(disk.get("freeBytes")),
        disk_avail_bytes=_int(disk.get("availBytes")),
        disk_error=_int(disk.get("error")),
        recording_active=_bool(recording.get("active")),
        recording_paused=_bool(recording.get("paused")),
        current_file=str(recording.get("currentFile", "") or ""),
        current_file_bytes=_int(recording.get("currentFileBytes")),
        last_file=str(recording.get("lastFile", "") or ""),
    )


def parse_logs(payload) -> list[LogLine]:
    data = _dict(payload)
    if not data.get("ok"):
        return []
    lines: list[LogLine] = []
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            LogLine(
                timestamp_ms=_int(item.get("timestamp")),
                level=_int(item.get("level")),
                level_name=str(item.get("levelName", "") or ""),
                message=str(item.get("message", "") or ""),
            )
        )
    return lines


def parse_log_line(payload) -> LogLine | None:
    """`logLine` 事件（单行，没有 `ok` 外壳）。"""
    item = _dict(payload)
    if not item:
        return None
    return LogLine(
        timestamp_ms=_int(item.get("timestamp")),
        level=_int(item.get("level")),
        level_name=str(item.get("levelName", "") or log_level_name(_int(item.get("level")))),
        message=str(item.get("message", "") or ""),
    )


def parse_tbar(payload, *, strict: bool = True) -> TbarState | None:
    """解析 `tbar` / `getTbar` 的响应；`tbarChanged` 事件没有 `ok` 字段，用 strict=False。"""
    data = _dict(payload)
    if strict and not data.get("ok"):
        return None
    frontend = data.get("frontendPosition")
    return TbarState(
        position=clamp01(_float(data.get("position"))),
        studio_mode=_bool(data.get("studioMode")),
        transitioning=_bool(data.get("transitioning")),
        transition=str(data.get("transition", "") or ""),
        duration_ms=_int(data.get("durationMs")),
        frontend_position=None if frontend is None else clamp01(_float(frontend)),
        applied=_bool(data.get("applied", True)),
    )


def parse_capture(payload) -> CaptureState | None:
    data = _dict(payload)
    if not data.get("ok"):
        return None
    return CaptureState(
        active=_bool(data.get("captureActive")),
        stream_running=_bool(data.get("streamRunning")),
        viewers=_int(data.get("viewers")),
        indicator_running=_bool(data.get("indicatorRunning")),
        indicator_visible=_bool(data.get("indicatorVisible")),
    )


def parse_stream(payload) -> StreamInfo | None:
    data = _dict(payload)
    if not data.get("ok"):
        return None
    return StreamInfo(
        running=_bool(data.get("running")),
        reused=_bool(data.get("reused")),
        role=str(data.get("role", "program") or "program"),
        url=str(data.get("url", "") or ""),
        snapshot_url=str(data.get("snapshotUrl", "") or ""),
        token=str(data.get("token", "") or ""),
        width=_int(data.get("width")),
        height=_int(data.get("height")),
        target_fps=_int(data.get("targetFps")),
        quality=_int(data.get("quality")),
        expires_at_ms=_int(data.get("expiresAtMs")),
    )


# ---------------------------------------------------------------- 格式化
def clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def format_bytes(size: int) -> str:
    """字节 → 人话。给日志、录制文件大小用，不追求精确到小数位。"""
    if size <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_uptime(ms: int) -> str:
    seconds = max(int(ms) // 1000, 0)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def feature_summary(info: PluginInfo) -> str:
    """一行说明能力矩阵，给"关于"与诊断用。

    **未实现的显式写「未实现」而不是省略** —— 省略会让人以为插件太老，
    而契约里它们是刻意给 `false` 的（见接口文档 §1）。
    """
    parts = []
    for feature, title in FEATURE_ORDER:
        # 去掉"（未实现）"这类括注，但**不要按字数截断**：
        # "编码器明细（未实现）"[:4] 会变成"编码器明"，读起来不知所云。
        name = title.split("（", 1)[0]
        parts.append(f"{name}={'✓' if info.supports(feature) else '✗'}")
    return "  ".join(parts)


#: 日志里出现这些片段时，给一条"下一步该看什么"的提示。
#: 只做**提示**，不参与任何分支判断（分不清就别瞎猜，原样把日志给用户看）。
FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    ("authentication failed", "推流服务端拒绝了鉴权：串流密钥可能过期或被重置"),
    ("invalid stream key", "串流密钥无效，请到平台后台重新复制"),
    ("failed to connect", "连不上推流服务器：检查地址/端口、防火墙与 DNS"),
    ("connection timed out", "连接推流服务器超时：网络不通或对端无响应"),
    ("could not resolve", "域名解析失败：检查 DNS 与网络"),
    ("no space left", "磁盘写满，录制/日志写不下去"),
    ("disk full", "磁盘写满，录制/日志写不下去"),
    ("permission denied", "没有写权限：检查录制目录的权限"),
    ("encoder", "编码器初始化/运行出错：换编码器或降低分辨率/帧率"),
    ("dropped frames", "开始丢帧：上行不足或编码跟不上"),
)


def failure_hint(lines: list[LogLine]) -> str:
    """从最近日志里挑一条最可能的失败原因（挑不到回空串）。"""
    for line in reversed(lines):
        text = line.message.lower()
        for needle, hint in FAILURE_HINTS:
            if needle in text:
                return hint
    return ""


def log_level_name(level: int) -> str:
    """libobs 的日志级别（100/200/300/400）→ 短名。"""
    return {100: "error", 200: "warning", 300: "info", 400: "debug"}.get(int(level), "info")


def format_logs(lines: list[LogLine], *, with_date: bool = True) -> str:
    """日志逐行排版（诊断/日志窗口用）。时间戳按本地时间显示。"""
    from datetime import datetime

    rows = []
    for line in lines:
        stamp = datetime.fromtimestamp(line.timestamp_ms / 1000) if line.timestamp_ms else None
        prefix = stamp.strftime("%H:%M:%S") if stamp else "--:--:--"
        if with_date and stamp:
            prefix = stamp.strftime("%m-%d ") + prefix
        rows.append(f"{prefix} [{line.level_name or log_level_name(line.level):7}] {line.message}")
    return "\n".join(rows)


__all__ = [
    "API_VERSION",
    "CaptureState",
    "EVENT_LOG_LINE",
    "EVENT_STREAM_STATS",
    "EVENT_TBAR_CHANGED",
    "FEATURE_CAPTURE",
    "FEATURE_ENCODER",
    "FEATURE_INFO",
    "FEATURE_LOGS",
    "FEATURE_MACHINE",
    "FEATURE_ORDER",
    "FEATURE_STREAM",
    "FEATURE_STREAM_PREVIEW",
    "FEATURE_TBAR",
    "LogLine",
    "MODE_ADVANCED",
    "MODE_COMPAT",
    "MODE_STANDARD",
    "MachineStats",
    "PluginInfo",
    "REQ_CAPTURE_STATE",
    "REQ_CLOSE_STREAM",
    "REQ_GET_INFO",
    "REQ_GET_STREAM",
    "REQ_GET_TBAR",
    "REQ_MACHINE_STATS",
    "REQ_OPEN_STREAM",
    "REQ_RECENT_LOGS",
    "REQ_SET_LOG_STREAM",
    "REQ_TBAR",
    "StreamInfo",
    "TbarState",
    "VENDOR",
    "VENDOR_ALIAS",
    "clamp01",
    "failure_hint",
    "feature_summary",
    "format_bytes",
    "format_logs",
    "format_uptime",
    "log_level_name",
    "mode_label",
    "parse_capture",
    "parse_info",
    "parse_log_line",
    "parse_logs",
    "parse_machine_stats",
    "parse_stream",
    "parse_tbar",
]

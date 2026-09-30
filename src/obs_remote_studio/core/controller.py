"""控制器：把网络事件归约成状态，把 UI 动作翻译成请求。

它活在 UI 线程；阻塞 IO 全部在 ObsWorker 所在线程。
"""

from __future__ import annotations

import base64
import logging
import shutil
import time
from collections import deque
from dataclasses import replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from . import audio
from . import protocol as P
from .models import (
    AppConfig,
    AudioInput,
    ConnectionConfig,
    ConnectionHealth,
    MediaStatus,
    ObsStats,
    RecordStatus,
    RecordWarning,
    ReplayBufferStatus,
    Scene,
    SceneItem,
    ServerInfo,
    StreamStatus,
    VideoSettings,
    VirtualcamStatus,
)
from .obs_worker import FAIL_AUTH, FAIL_REFUSED, FAIL_TIMEOUT, ObsWorker
from .settings import AppSettings
from .state_store import (
    CONNECTED,
    CONNECTING,
    DISCONNECTED,
    RECONNECTING,
    StateStore,
)

logger = logging.getLogger(__name__)

TRANSPORT_FAILURE_THRESHOLD = 2  # 连续几次“连接级”失败才判定掉线

# L：媒体状态是逐源查询、响应不带 inputName，只能按顺序关联。
# 上一批没回来时不许重开一批（会错配），但也不能无限等 —— 超过这个秒数重开。
MEDIA_PENDING_TIMEOUT_S = 3.0
# L：动作发出后这段时间内，状态回执可能是"动作之前"发出去的，
# 期间以本地意图为准（同 D6 录制暂停的处理思路）
# D6：暂停/继续、G3：演播室模式，以及 L：媒体动作，用的是同一套"意图窗口"：
# 用户操作后的一小段时间内，任何"操作之前产生"的回执或事件都不得与之相悖。
# 之所以必须这样：请求响应走 ReqClient、事件走 EventClient，是两条独立的 websocket，
# 相互之间没有顺序保证；轮询又会在用户操作前就把请求发出去。
# 于是"陈旧数据后到"是常态而非意外，只能靠时间窗口仲裁。
INTENT_WINDOW_S = 2.0

# 这些请求按"逐源探测"使用，失败时只记日志、不弹框
_AUDIO_GET_REQUESTS = frozenset(
    {
        P.REQ_GET_INPUT_VOLUME,
        P.REQ_GET_INPUT_MUTE,
        P.REQ_GET_INPUT_AUDIO_MONITOR_TYPE,
        P.REQ_GET_INPUT_AUDIO_BALANCE,
        P.REQ_GET_INPUT_AUDIO_SYNC_OFFSET,
        P.REQ_GET_INPUT_AUDIO_TRACKS,
    }
)


class Controller(QObject):
    """UI 唯一需要打交道的对象。"""

    reconnect_scheduled = Signal(int, int)  # 第 n 次, 延迟毫秒

    def __init__(self, settings: AppSettings | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings or AppSettings()
        self.config: AppConfig = self.settings.load_config()
        self.store = StateStore(self)

        self._thread = QThread(self)
        self._worker = ObsWorker()
        self._worker.moveToThread(self._thread)
        self._bind_worker()
        self._thread.start()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.config.poll_interval_ms)
        self._poll_timer.timeout.connect(self._on_poll)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._on_reconnect_tick)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_scenes)

        self._items_timer = QTimer(self)
        self._items_timer.setSingleShot(True)
        self._items_timer.timeout.connect(self._refresh_scene_items)

        self._audio_timer = QTimer(self)
        self._audio_timer.setSingleShot(True)
        self._audio_timer.timeout.connect(self.refresh_audio)

        # 转场卡住兜底：「转场中」把转场按钮锁死，事件丢了就再也点不动
        self._transition_watchdog = QTimer(self)
        self._transition_watchdog.setSingleShot(True)
        self._transition_watchdog.timeout.connect(self._on_transition_watchdog)

        # L：媒体状态查询（响应不带 inputName，按请求顺序关联）
        self._media_timer = QTimer(self)
        self._media_timer.setSingleShot(True)
        self._media_timer.timeout.connect(self.refresh_media_status)
        self._pending_media: deque[str] = deque()
        self._media_asked_at: float = 0.0
        self._media_optimistic: dict[str, tuple[str, float]] = {}
        self._studio_intent: tuple[bool, float] | None = None

        # F2：缩略图轮询（默认 1 fps）
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(self.config.preview_interval_ms)
        self._frame_timer.timeout.connect(self._on_frame_tick)
        self._frame_queue: deque[str] = deque()
        self._frame_pending: set[str] = set()

        # A11：连接健康心跳。空闲时发一次 GetVersion，用它量 RTT。
        # 刻意**不用** GetStats：那一拍本来就够重，再叠心跳会互相干扰计时。
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(max(self.config.heartbeat_interval_ms, 1000))
        self._heartbeat_timer.timeout.connect(self._on_heartbeat)
        self._heartbeat_sent_at: float = 0.0
        self._heartbeat_pending: bool = False

        # D17：录制目录只拉一次，之后靠本地磁盘查剩余空间
        self._record_directory: str = ""
        self._record_directory_asked: bool = False
        # M：切换场景集合/配置文件后要整体作废本地状态
        self._collection_switching: bool = False

        # E9：批量静音的状态汇总（回执没有 handler，靠这个定时器收尾）
        self._mute_all_timer = QTimer(self)
        self._mute_all_timer.setSingleShot(True)
        self._mute_all_timer.setInterval(1500)
        self._mute_all_timer.timeout.connect(self._flush_mute_all)
        self._mute_all_pending: int = 0
        self._mute_all_failures: int = 0

        self._attempt = 0
        self._transport_failures = 0
        self._poll_ticks = 0
        self._record_since_ms: float | None = None
        self._stream_since_ms: float | None = None
        self._items_generation = 0
        self._items_scene: str = ""
        self._pending_audio: deque[tuple[str, str]] = deque()
        self._pending_enabled: deque[tuple[int, str, int]] = deque()
        self._items_buffer: dict[int, SceneItem] = {}
        self._manual_disconnect = False
        self._awaiting_cleanup = False
        self._transition_fallback_used = False
        # 同类错误在窗口期内只提示一次（_raise_error_once 用）
        self._last_error: str = ""
        self._last_error_at: float = 0.0
        # D6：暂停是乐观更新，记下"哪个请求最后被发出"，
        # 免得迟到的 GetRecordStatus 回执把刚翻的标记又按回去
        # D6：暂停是乐观更新。记下"用户想要什么"+一个有效期，期间到达的
        # 状态回执**和事件**都不得与之相悖（见 _pause_intent_blocks）。
        # 为什么连事件也要挡：RecordStateChanged 走的是**另一条 websocket**
        # （事件连接），与请求响应之间没有顺序保证 —— 一条"用户点继续之前"
        # 就发出的 PAUSED 事件完全可能后到，把刚翻过来的状态又按回去。
        self._pause_intent: tuple[bool, float] | None = None

    # ---------------------------------------------------------------- 生命周期
    @property
    def worker(self) -> ObsWorker:
        """J3：诊断窗口需要直接连 worker 的原始 JSON 旁路信号。

        只读暴露，不给外部替换 worker 的机会（线程亲和性很脆弱）。
        """
        return self._worker

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._reconnect_timer.stop()
        self._worker.request_disconnect.emit()
        self._thread.quit()
        self._thread.wait(3000)

    # ---------------------------------------------------------------- 连接
    def connect(self, conn: ConnectionConfig | None = None) -> None:
        if conn is not None:
            self.config.connection = conn
            self.settings.save_connection(conn)
        self._manual_disconnect = False
        self._attempt = 0
        self._transport_failures = 0
        self.store.set_connection_state(CONNECTING, "正在连接…")
        self._worker.request_connect.emit(self._connect_payload())

    def disconnect(self) -> None:
        self._manual_disconnect = True
        self._stop_polling()
        self._reconnect_timer.stop()
        self._worker.request_disconnect.emit()

    def apply_config(self, cfg: AppConfig) -> None:
        previous_heartbeat = self.config.heartbeat_interval_ms
        self.config = cfg
        self.settings.save_config(cfg)
        self._poll_timer.setInterval(cfg.poll_interval_ms)
        self._frame_timer.setInterval(cfg.preview_interval_ms)
        # A11：心跳间隔改了要立刻生效，不然得等到下次重连
        if cfg.heartbeat_interval_ms != previous_heartbeat:
            self._start_heartbeat()
        # D17：阈值改了立刻按新阈值重算一次，否则要等下一次录制状态刷新
        self._refresh_record_warning()

    def _connect_payload(self) -> dict:
        conn = self.config.connection
        return {
            "host": conn.host,
            "port": conn.port,
            "password": conn.password,
            "timeout": self.config.request_timeout_s,
            # 电平表是高频事件，是否订阅在连接时定；改了要重连才生效
            "subscription_mask": P.subscription_mask(self.config.audio_meters),
        }

    @Slot(dict)
    def _on_connected(self, info: dict) -> None:
        self._attempt = 0
        self._transport_failures = 0
        self.store.set_server_info(
            ServerInfo(
                obs_version=str(info.get("obs_version", "")),
                websocket_version=str(info.get("websocket_version", "")),
                rpc_version=int(info.get("rpc_version", 0) or 0),
            )
        )
        # 能力列表来自握手时那次 GetVersion，必须在 refresh_all 之前就位，
        # 否则 _refresh_transitions 只能靠"发了被拒"来试错。
        self.store.set_capabilities(info.get("available_requests") or [])
        self._transition_fallback_used = False
        # 新连接：录制目录要重新问（可能换了台机器），RTT 样本也从头算
        self._record_directory = ""
        self._record_directory_asked = False
        self._heartbeat_pending = False
        self.store.set_health(ConnectionHealth())

        version = P.parse_version(str(info.get("websocket_version", "")))
        if version and version < (5, 0):
            self.store.error_raised.emit(
                "协议版本过低",
                f"检测到 obs-websocket {info.get('websocket_version')}，MVP 需要 v5（OBS ≥ 28）。",
            )
        # 状态栏自己会显示"已连接"，这里只给地址，免得读成"已连接 已连接 …"
        conn = self.config.connection
        self.store.set_connection_state(CONNECTED, conn.label())
        # 已经连上了，任何挂起的重连都要撤掉。
        # 不加这一步会出问题：上一轮掉线排的退避计时器如果在本次连接**之后**才到点，
        # 会再发一次 connect，于是 _on_connected 跑两遍、refresh_all 也跑两遍
        # （表现为同一批请求被重复下发）。
        self._reconnect_timer.stop()
        self._awaiting_cleanup = False
        self._poll_timer.start()
        self._frame_timer.start()
        self._start_heartbeat()
        self.refresh_all()

    @Slot(str, str)
    def _on_connect_failed(self, kind: str, message: str) -> None:
        title = {
            FAIL_AUTH: "认证失败",
            FAIL_TIMEOUT: "连接超时",
            FAIL_REFUSED: "连接被拒绝",
        }.get(kind, "连接失败")
        detail = {
            FAIL_AUTH: "密码错误，或 OBS 未启用 WebSocket 认证。请检查 OBS 的「工具 → WebSocket 服务器设置」。",
            FAIL_TIMEOUT: f"无法在 {self.config.request_timeout_s:g} 秒内连通 {self.config.connection.label()}，"
            "请确认 OBS 已启动且端口未被防火墙拦截。",
            FAIL_REFUSED: f"{self.config.connection.label()} 拒绝连接，请确认 OBS 已启动且已启用 WebSocket 服务器。",
        }.get(kind, message or "未知错误")
        logger.warning("连接失败[%s]: %s", kind, message)

        if self._manual_disconnect:
            self.store.set_connection_state(DISCONNECTED, "已断开")
            return
        if not self._schedule_reconnect():
            self.store.set_connection_state(DISCONNECTED, title)
            self.store.error_raised.emit(title, detail)

    @Slot(str)
    def _on_disconnected(self, reason: str) -> None:
        self._stop_polling()
        self.store.reset_runtime_state()
        if self._awaiting_cleanup:
            # 掉线流程里主动发起的清理，重连已安排好，这里只做状态收尾
            self._awaiting_cleanup = False
            return
        if self._manual_disconnect:
            self.store.set_connection_state(DISCONNECTED, "已断开")
            return
        self._schedule_reconnect()

    @Slot(str)
    def _on_event_link_failed(self, message: str) -> None:
        logger.warning("事件通道建立失败：%s", message)
        self.store.error_raised.emit(
            "事件订阅失败",
            f"控制指令仍可用，但 OBS 端的变化不会自动同步到本客户端（{message}）。",
        )

    def _schedule_reconnect(self) -> bool:
        """返回 True 表示已安排重连。"""
        policy = self.config.reconnect
        if not policy.enabled:
            return False
        if policy.max_attempts and self._attempt >= policy.max_attempts:
            return False

        delay = min(
            policy.initial_delay_ms * (2 ** self._attempt), policy.max_delay_ms
        )
        self._attempt += 1
        self.store.set_connection_state(
            RECONNECTING, f"连接中断，{max(delay // 1000, 1)} 秒后重连（第 {self._attempt} 次）"
        )
        self.reconnect_scheduled.emit(self._attempt, delay)
        self._reconnect_timer.setInterval(delay)
        self._reconnect_timer.start()
        return True

    @Slot()
    def _on_reconnect_tick(self) -> None:
        self.store.set_connection_state(CONNECTING, "正在重连…")
        self._worker.request_connect.emit(self._connect_payload())

    # ---------------------------------------------------------------- 轮询
    def _stop_polling(self) -> None:
        self._poll_timer.stop()
        self._frame_timer.stop()
        self._heartbeat_timer.stop()
        self._heartbeat_pending = False
        self._transition_watchdog.stop()
        self._frame_queue.clear()
        self._frame_pending.clear()
        self._pending_audio.clear()
        self._poll_ticks = 0

    @Slot()
    def _on_poll(self) -> None:
        if self.store.connection_state != CONNECTED:
            return
        self.send(P.REQ_GET_RECORD_STATUS)
        self.send(P.REQ_GET_STREAM_STATUS)
        self._poll_ticks += 1
        if self._poll_ticks % max(self.config.stats_every_n_polls, 1) == 0:
            self.send(P.REQ_GET_STATS)
            # D8：虚拟摄像机没有"状态被外部改掉"以外的场景，跟着低频那拍刷就够
            self.send_if_supported(P.REQ_GET_VIRTUALCAM_STATUS)
            # L1：媒体播放状态同样跟低频 —— 播放中才会变，没必要每秒查
            if self.store.media:
                self.refresh_media_status()

    def refresh_all(self) -> None:
        if self.store.connection_state != CONNECTED:
            return
        self.send(P.REQ_GET_SCENE_LIST)
        self.send(P.REQ_GET_VIDEO_SETTINGS)
        self.send(P.REQ_GET_RECORD_STATUS)
        self.send(P.REQ_GET_STREAM_STATUS)
        self.send(P.REQ_GET_STATS)
        # D7/D8
        self.send_if_supported(P.REQ_GET_REPLAY_BUFFER_STATUS)
        self.send_if_supported(P.REQ_GET_VIRTUALCAM_STATUS)
        # G
        self.send_if_supported(P.REQ_GET_STUDIO_MODE_ENABLED)
        self._refresh_transitions()
        # E
        self.refresh_audio()
        # D17：录制目录只在第一次刷新时问一次
        if not self._record_directory_asked:
            self._record_directory_asked = True
            self.send_if_supported(P.REQ_GET_RECORD_DIRECTORY)
        # M：场景集合 / 配置文件列表
        self.refresh_config_lists()

    # ---------------------------------------------------------------- A11：连接健康
    def _start_heartbeat(self) -> None:
        interval = self.config.heartbeat_interval_ms
        if interval <= 0:
            self._heartbeat_timer.stop()
            self.store.set_health(ConnectionHealth())
            return
        self._heartbeat_timer.setInterval(max(interval, 1000))
        self._heartbeat_timer.start()

    @Slot()
    def _on_heartbeat(self) -> None:
        """空闲时量一次 RTT。

        与 A5 的区别：A5 只管「连上 / 断了」两态，弱网下请求排着队但界面看不出来。
        这里专门测「通但慢」：发一条最轻的 GetVersion，量往返耗时。
        """
        if self.store.connection_state != CONNECTED:
            return
        if self._heartbeat_pending:
            # 上一拍还没回来 —— 说明确实卡住了，直接记一次超时样本，
            # 不用等真正的超时把界面吊着
            self._record_rtt(self.config.request_timeout_s * 1000)
            return
        self._heartbeat_pending = True
        self._heartbeat_sent_at = time.monotonic()
        self.send(P.REQ_GET_VERSION)

    def _handle_get_version(self, data) -> None:
        """心跳回执。注意：握手那次 GetVersion 走的不是这条路（走 connected 载荷）。"""
        if not self._heartbeat_pending:
            return
        self._heartbeat_pending = False
        elapsed = (time.monotonic() - self._heartbeat_sent_at) * 1000
        self._record_rtt(elapsed)

    def _record_rtt(self, rtt_ms: float) -> None:
        health = self.store.health
        warn = max(self.config.rtt_warn_ms, 1)
        slow = rtt_ms >= warn
        streak = health.slow_streak + 1 if slow else 0
        # 平滑一下，避免单次抖动就让状态栏数字乱跳
        smoothed = rtt_ms if health.samples == 0 else health.rtt_ms * 0.6 + rtt_ms * 0.4
        self.store.set_health(
            ConnectionHealth(
                rtt_ms=smoothed,
                samples=health.samples + 1,
                slow_streak=streak,
                stalled=streak >= max(self.config.rtt_slow_streak, 1),
            )
        )

    # ---------------------------------------------------------------- D17：磁盘与时长预警
    def _refresh_record_warning(self) -> None:
        """录制中才算磁盘/时长；一停就清掉，免得挂个黄条在那。"""
        record = self.store.record
        if not record.active:
            self.store.set_record_warning(RecordWarning())
            return

        warning = RecordWarning()
        used_gb = record.bytes_written / (1024 ** 3)

        # 体积阈值（不需要查磁盘，先判）
        if self.config.record_warn_gb > 0 and used_gb >= self.config.record_warn_gb:
            warning = RecordWarning(
                level="warn",
                message=f"录制文件已达 {used_gb:.1f} GB",
            )

        minutes = record.duration_ms / 60000
        if self.config.record_warn_minutes > 0 and minutes >= self.config.record_warn_minutes:
            warning = RecordWarning(
                level="warn",
                message=f"已连续录制 {minutes:.0f} 分钟",
            )

        # 剩余空间：录制目录是 **OBS 那台机器** 上的路径。
        # 局域网控另一台时本地查不到 —— 这时 free_gb 留 None，界面不显示这一项，
        # 而不是报一个假的数字。
        free_gb = self._free_space_gb()
        if free_gb is not None and self.config.disk_warn_gb > 0:
            if free_gb <= self.config.disk_warn_gb:
                warning = RecordWarning(
                    level="danger",
                    message=f"录制目录仅剩 {free_gb:.1f} GB",
                    free_gb=free_gb,
                )
        if warning.free_gb is None:
            warning.free_gb = free_gb
        self.store.set_record_warning(warning)

    def _free_space_gb(self) -> float | None:
        if not self._record_directory:
            return None
        try:
            return shutil.disk_usage(self._record_directory).free / (1024 ** 3)
        except OSError:
            # 路径不存在（OBS 在别的机器上 / 盘符未挂载）：宁可不显示，也不报假的
            return None

    def _handle_get_record_directory(self, data) -> None:
        self._record_directory = str(getattr(data, "record_directory", "") or "")
        self._refresh_record_warning()

    # ---------------------------------------------------------------- E9：一键全静音
    def set_all_muted(self, muted: bool) -> None:
        """逐源下发 SetInputMute。

        刻意**不并发**：worker 是按队列一条条处理的，逐个下发既能保持顺序，
        也避免在弱网下一口气压满 socket。失败只汇总提示一次（见 _flush_mute_all）。
        """
        names = [item.name for item in self.store.audio_inputs]
        if not names:
            return
        for name in names:
            self.store.update_audio_input(name, muted=muted)
        self._mute_all_pending = len(names)
        self._mute_all_failures = 0
        for name in names:
            self.send(P.REQ_SET_INPUT_MUTE, {"inputName": name, "inputMuted": muted})
        # 回执没有专门的 handler，用一个小定时器收尾总结
        self._mute_all_timer.start()

    def _flush_mute_all(self) -> None:
        failures, self._mute_all_failures = self._mute_all_failures, 0
        self._mute_all_pending = 0
        if failures:
            self._raise_error_once(f"批量静音：有 {failures} 个音频源没能切换")


    # ---------------------------------------------------------------- 指令
    def send(self, request_type: str, data: dict | None = None) -> None:
        self._worker.request_execute.emit(request_type, data or {})

    # ---------------------------------------------------------------- E：音频
    def refresh_audio(self) -> None:
        self._pending_audio.clear()
        self.send_if_supported(P.REQ_GET_INPUT_LIST)

    # 音频请求的响应**不带 inputName**（只回数值），只能按"请求顺序"关联是谁的。
    # 队列元素是 (请求名, 源名)，取用时按请求名匹配，容忍失败造成的错位。
    def _queue_audio(self, request_type: str, name: str) -> None:
        self._pending_audio.append((request_type, name))
        self.send(request_type, {"inputName": name})

    def _pop_pending_audio(self, request_type: str) -> str | None:
        for index, (kind, name) in enumerate(self._pending_audio):
            if kind == request_type:
                del self._pending_audio[index]
                return name
        return None

    def set_input_volume_preview(self, name: str, volume_db: float) -> None:
        """推子拖动中：只更新本地显示，不发请求（松手后才提交）。"""
        self.set_input_volume(name, volume_db, commit=False)

    def set_input_volume(self, name: str, volume_db: float, commit: bool = True) -> None:
        """推子变化时高频调用；commit=False 表示还在拖动，只改本地显示。"""
        mul = audio.db_to_mul(volume_db)
        self.store.update_audio_input(name, volume_db=volume_db, volume_mul=mul)
        if not commit:
            return
        self.send(P.REQ_SET_INPUT_VOLUME, {"inputName": name, "inputVolumeDb": volume_db})

    def set_input_mute(self, name: str, muted: bool) -> None:
        self.store.update_audio_input(name, muted=muted)
        self.send(P.REQ_SET_INPUT_MUTE, {"inputName": name, "inputMuted": muted})

    def toggle_input_mute(self, name: str) -> None:
        item = self.store.find_audio_input(name)
        if item is not None:
            self.set_input_mute(name, not item.muted)
            return
        self.send(P.REQ_TOGGLE_INPUT_MUTE, {"inputName": name})

    def set_input_hidden(self, name: str, hidden: bool) -> None:
        hidden_names = set(self.config.mixer_hidden_inputs)
        if hidden:
            hidden_names.add(name)
        else:
            hidden_names.discard(name)
        self.config.mixer_hidden_inputs = sorted(hidden_names)
        self.settings.save_config(self.config)
        self.store.set_hidden_inputs(hidden_names)
        self.refresh_audio()  # 隐藏项在列表里过滤掉，需要重新拉一次

    def set_monitor_type(self, name: str, monitor_type: str) -> None:
        self.store.update_audio_input(name, monitor_type=monitor_type)
        self.send(
            P.REQ_SET_INPUT_AUDIO_MONITOR_TYPE,
            {"inputName": name, "monitorType": monitor_type},
        )

    def set_balance(self, name: str, balance: float) -> None:
        self.store.update_audio_input(name, balance=balance)
        self.send(
            P.REQ_SET_INPUT_AUDIO_BALANCE,
            {"inputName": name, "inputAudioBalance": balance},
        )

    def set_sync_offset(self, name: str, offset_ms: int) -> None:
        self.store.update_audio_input(name, sync_offset_ms=offset_ms)
        self.send(
            P.REQ_SET_INPUT_AUDIO_SYNC_OFFSET,
            {"inputName": name, "inputAudioSyncOffset": offset_ms},
        )

    def set_tracks(self, name: str, tracks: int) -> None:
        self.store.update_audio_input(name, tracks=tracks)
        self.send(
            P.REQ_SET_INPUT_AUDIO_TRACKS,
            {"inputName": name, "inputAudioTracks": _tracks_to_payload(tracks)},
        )

    def fetch_advanced_audio(self, name: str) -> None:
        """打开高级音频属性时按需拉取，避免连接时对每个源打四个请求。"""
        for request_type in (
            P.REQ_GET_INPUT_AUDIO_MONITOR_TYPE,
            P.REQ_GET_INPUT_AUDIO_BALANCE,
            P.REQ_GET_INPUT_AUDIO_SYNC_OFFSET,
            P.REQ_GET_INPUT_AUDIO_TRACKS,
        ):
            if self.store.supports(request_type):
                self._queue_audio(request_type, name)

    def fetch_advanced_audio_all(self) -> None:
        for item in self.store.audio_inputs:
            self.fetch_advanced_audio(item.name)

    @Slot(str, object)
    def _handle_get_input_list(self, data) -> None:
        raw = getattr(data, "inputs", []) or []
        candidates = [
            AudioInput(name=str(item.get("inputName", "")), kind=str(item.get("inputKind", "") or ""))
            for item in raw
            if isinstance(item, dict) and item.get("inputName")
        ]
        hidden = set(self.config.mixer_hidden_inputs)
        known = {item.name for item in self.store.audio_inputs}
        keep: list[AudioInput] = []
        for item in candidates:
            if item.name in hidden:
                continue
            # 白名单命中，或者电平表证明它真的在出声
            if P.is_audio_kind(item.kind) or item.name in self.store.meters:
                if item.name in known:  # 保留已有的音量/静音值，避免闪一下
                    old = self.store.find_audio_input(item.name)
                    if old is not None:
                        item.volume_db = old.volume_db
                        item.volume_mul = old.volume_mul
                        item.muted = old.muted
                        item.monitor_type = old.monitor_type
                        item.balance = old.balance
                        item.sync_offset_ms = old.sync_offset_ms
                        item.tracks = old.tracks
                keep.append(item)

        self.store.set_audio_inputs(keep)
        for item in keep:
            if item.name not in known:
                self._queue_audio(P.REQ_GET_INPUT_VOLUME, item.name)
                self._queue_audio(P.REQ_GET_INPUT_MUTE, item.name)
        self.store.set_hidden_inputs(hidden)
        # L：顺手挑出媒体源。用的就是同一份 GetInputList 结果，不额外发请求。
        self._update_media_sources(candidates)

    def _update_media_sources(self, inputs: list[AudioInput]) -> None:
        names = [item.name for item in inputs if P.is_media_kind(item.kind)]
        if not self.store.supports(P.REQ_GET_MEDIA_INPUT_STATUS):
            names = []
        existing = {item.name for item in self.store.media}
        # 列表没变就别重建，免得把正在播放的状态又刷回初始值
        if names == [item.name for item in self.store.media]:
            return
        media: list[MediaStatus] = []
        for name in names:
            old = self.store.find_media(name)
            media.append(old if old is not None and name in existing else MediaStatus(name=name))
        self.store.set_media(media)
        self.refresh_media_status()

    def refresh_media_status(self) -> None:
        """L1：媒体状态逐源查询。

        响应不带 inputName，只能按**请求顺序**关联（与混音器同一套路）。
        因此上一批还在飞时绝不能重开一批 —— 否则会把甲的 state 写到乙身上。
        但万一有响应丢了（超时/被丢弃），队列会卡死，所以给一个超时兜底。
        """
        if self._pending_media:
            if time.monotonic() - self._media_asked_at < MEDIA_PENDING_TIMEOUT_S:
                return
            logger.debug("媒体状态查询超时未回，重开一批")
            self._pending_media.clear()
        self._media_asked_at = time.monotonic()
        for item in self.store.media:
            self._pending_media.append(item.name)
            self.send(P.REQ_GET_MEDIA_INPUT_STATUS, {"inputName": item.name})

    def _handle_get_media_input_status(self, data) -> None:
        if not self._pending_media:
            return
        name = self._pending_media.popleft()
        duration = int(getattr(data, "media_duration", 0) or 0)
        cursor = int(getattr(data, "media_cursor", 0) or 0)
        state = str(getattr(data, "media_state", "") or "")
        # 刚发过动作的话，这条回执很可能是**动作之前**发出去那批的状态，
        # 直接采纳会把刚翻的按钮按回去（和 D6 录制暂停同一个坑）。
        # 所以给一个短暂的"意图窗口"，窗口内状态以本地意图为准，
        # 时长/进度照常采纳（它们不受动作影响）。
        optimistic = self._media_optimistic.get(name)
        if optimistic is not None:
            want, until = optimistic
            if time.monotonic() < until:
                state = want
            else:
                self._media_optimistic.pop(name, None)
        self.store.update_media(
            name, state=state or None, duration_ms=duration, cursor_ms=cursor
        )

    def media_action(self, name: str, action: str) -> None:
        """L1：播放 / 暂停 / 停止 / 重播 / 上一个 / 下一个。"""
        if not name or not action:
            return
        if not self.send_if_supported(
            P.REQ_TRIGGER_MEDIA_INPUT_ACTION,
            {"inputName": name, "mediaAction": action},
        ):
            return
        # 乐观翻转，紧接着查一次校准（事件也会来，但事件不是每台 OBS 都齐）
        self._optimistic_media(name, action)
        self._debounced(self._media_timer)

    def _optimistic_media(self, name: str, action: str) -> None:
        mapping = {
            P.MEDIA_ACTION_PLAY: P.MEDIA_STATE_PLAYING,
            P.MEDIA_ACTION_RESTART: P.MEDIA_STATE_PLAYING,
            P.MEDIA_ACTION_PAUSE: P.MEDIA_STATE_PAUSED,
            P.MEDIA_ACTION_STOP: P.MEDIA_STATE_STOPPED,
        }
        state = mapping.get(action)
        if state is None:
            return
        # 记下意图 + 有效期：期间到达的状态回执可能是动作之前发出的
        self._media_optimistic[name] = (
            state,
            time.monotonic() + INTENT_WINDOW_S,
        )
        fields = {"state": state}
        if action == P.MEDIA_ACTION_STOP:
            fields["cursor_ms"] = 0
        if action == P.MEDIA_ACTION_RESTART:
            fields["cursor_ms"] = 0
        self.store.update_media(name, **fields)

    def seek_media(self, name: str, cursor_ms: int) -> None:
        """L2：跳到指定位置（松手才调用，调用方负责节流）。"""
        if not name:
            return
        if not self.send_if_supported(
            P.REQ_SET_MEDIA_INPUT_CURSOR,
            {"inputName": name, "mediaCursor": int(max(0, cursor_ms))},
        ):
            return
        self.store.update_media(name, cursor_ms=int(max(0, cursor_ms)))
        self._debounced(self._media_timer)

    # ---------------------------------------------------------------- M：场景集合 / 配置文件
    def config_switch_supported(self) -> bool:
        return self.store.supports(P.REQ_SET_CURRENT_SCENE_COLLECTION)

    def refresh_config_lists(self) -> None:
        self.send_if_supported(P.REQ_GET_SCENE_COLLECTION_LIST)
        self.send_if_supported(P.REQ_GET_PROFILE_LIST)

    def _handle_get_scene_collection_list(self, data) -> None:
        names = [str(item) for item in (getattr(data, "scene_collections", []) or [])]
        current = str(getattr(data, "current_scene_collection_name", "") or "")
        self.store.set_scene_collections(names, current)

    def _handle_get_profile_list(self, data) -> None:
        names = [str(item) for item in (getattr(data, "profiles", []) or [])]
        current = str(getattr(data, "current_profile_name", "") or "")
        self.store.set_profiles(names, current)

    def config_switch_blocker(self) -> str:
        """录制 / 推流进行中一律不许切。

        切换会让 OBS 重新加载全部场景，正在写的录制文件与正在推的流都会被打断，
        这个代价不是用户点一下确认框就想付的 —— 所以直接禁止，连确认框都不弹。
        """
        if self.store.record.active:
            return "正在录制，切换会打断当前录制文件"
        if self.store.stream.active:
            return "正在推流，切换会让观众断流"
        return ""

    def switch_scene_collection(self, name: str) -> None:
        if not name or name == self.store.current_scene_collection:
            return
        # 拦在控制器层：界面上的确认框只是第一道，热键 / 托盘 / 将来的自动化
        # 都可能绕过 UI 直接调到这里。录制与推流进行中一律不放行。
        blocker = self.config_switch_blocker()
        if blocker:
            logger.warning("拒绝切换场景集合：%s", blocker)
            return
        if not self.send_if_supported(
            P.REQ_SET_CURRENT_SCENE_COLLECTION, {"sceneCollectionName": name}
        ):
            return
        # 顺序要紧：先整体作废（reset 会把集合名也清掉），再写上目标名。
        # 反过来的话刚写的名字会被 reset 抹掉，界面上闪一下空。
        self.store.reset_runtime_state()
        self.store.set_scene_collections(self.store.scene_collections, name)

    def switch_profile(self, name: str) -> None:
        if not name or name == self.store.current_profile:
            return
        blocker = self.config_switch_blocker()
        if blocker:
            logger.warning("拒绝切换配置文件：%s", blocker)
            return
        if not self.send_if_supported(P.REQ_SET_CURRENT_PROFILE, {"profileName": name}):
            return
        self.store.reset_runtime_state()
        self.store.set_profiles(self.store.profiles, name)

    @Slot(str, object)
    def _handle_get_input_volume(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_VOLUME)
        if name is None:
            return
        mul = float(getattr(data, "input_volume_mul", 1.0) or 0.0)
        db = getattr(data, "input_volume_db", None)
        self.store.update_audio_input(
            name,
            volume_mul=mul,
            volume_db=float(db) if db is not None else audio.mul_to_db(mul),
        )

    @Slot(str, object)
    def _handle_get_input_mute(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_MUTE)
        if name is None:
            return
        self.store.update_audio_input(name, muted=bool(getattr(data, "input_muted", False)))

    @Slot(str, object)
    def _handle_get_input_audio_monitor_type(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_AUDIO_MONITOR_TYPE)
        if name is None:
            return
        self.store.update_audio_input(
            name, monitor_type=str(getattr(data, "monitor_type", "") or "")
        )
        self.store.audio_settings_changed.emit()

    @Slot(str, object)
    def _handle_get_input_audio_balance(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_AUDIO_BALANCE)
        if name is None:
            return
        self.store.update_audio_input(
            name, balance=float(getattr(data, "input_audio_balance", 0.5) or 0.0)
        )
        self.store.audio_settings_changed.emit()

    @Slot(str, object)
    def _handle_get_input_audio_sync_offset(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_AUDIO_SYNC_OFFSET)
        if name is None:
            return
        self.store.update_audio_input(
            name, sync_offset_ms=int(getattr(data, "input_audio_sync_offset", 0) or 0)
        )
        self.store.audio_settings_changed.emit()

    @Slot(str, object)
    def _handle_get_input_audio_tracks(self, data) -> None:
        name = self._pop_pending_audio(P.REQ_GET_INPUT_AUDIO_TRACKS)
        if name is None:
            return
        self.store.update_audio_input(
            name, tracks=_payload_to_tracks(getattr(data, "input_audio_tracks", {}))
        )
        self.store.audio_settings_changed.emit()

    # ---- 音频事件 ----
    def _event_input_created(self, data) -> None:
        self._debounced(self._audio_timer)

    def _event_input_removed(self, data) -> None:
        self._debounced(self._audio_timer)

    def _event_input_name_changed(self, data) -> None:
        self._debounced(self._audio_timer)

    def _event_input_volume_changed(self, data) -> None:
        name = str(getattr(data, "input_name", "") or "")
        mul = float(getattr(data, "input_volume_mul", 1.0) or 0.0)
        db = getattr(data, "input_volume_db", None)
        self.store.update_audio_input(
            name,
            volume_mul=mul,
            volume_db=float(db) if db is not None else audio.mul_to_db(mul),
        )

    def _event_input_mute_state_changed(self, data) -> None:
        name = str(getattr(data, "input_name", "") or "")
        self.store.update_audio_input(name, muted=bool(getattr(data, "input_muted", False)))

    def _event_input_volume_meters(self, data) -> None:
        # 高频事件（可达 60Hz）：只覆盖字典，不发信号，控件按自己的节奏读
        self.store.set_meters(audio.parse_meters_event(data))

    def send_if_supported(self, request_type: str, data: dict | None = None) -> bool:
        """按 availableRequests 决定发不发，返回是否已发出。

        协议里各版本的请求名并不一致（例如转场列表在 5.0 叫 GetTransitionList、
        5.1 起叫 GetSceneTransitionList），照书抄会吃 204。
        """
        if not self.store.supports(request_type):
            logger.debug("服务端不支持 %s，跳过", request_type)
            return False
        self.send(request_type, data)
        return True

    def supports_pause_record(self) -> bool:
        """D6：录制暂停要 ws 5.1+ / OBS 30+，两个条件都满足才放行按钮。

        `availableRequests` 才是权威依据；版本号只作为老服务端不上报能力时的兜底。
        """
        if self.store.rejected_requests and P.REQ_PAUSE_RECORD in self.store.rejected_requests:
            return False
        if self.store.supported_requests:
            return self.store.supports(P.REQ_PAUSE_RECORD)
        return P.version_at_least(self.store.server_info.websocket_version, (5, 1))

    def switch_scene(self, name: str) -> None:
        if not name or name == self.store.current_scene:
            return
        self.store.set_current_scene(name)
        self._debounced(self._items_timer)  # 与事件触发的刷新合并，避免重复拉取
        self.send(P.REQ_SET_CURRENT_PROGRAM_SCENE, {"sceneName": name})

    def set_item_enabled(self, scene: str, item_id: int, enabled: bool) -> None:
        self._apply_item_enabled(scene, item_id, enabled)
        self.send(
            P.REQ_SET_SCENE_ITEM_ENABLED,
            {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled},
        )

    def toggle_record(self) -> None:
        if self.store.record.active:
            self.send(P.REQ_STOP_RECORD)
        else:
            self.send(P.REQ_START_RECORD)
        self.send(P.REQ_GET_RECORD_STATUS)

    # ---- D6：录制暂停 / 继续 ----
    def pause_intent_wanted(self) -> bool | None:
        """当前还有效的暂停意图（None = 没有）。未过期才算数。"""
        if self._pause_intent is None:
            return None
        wanted, until = self._pause_intent
        if time.monotonic() >= until:
            self._pause_intent = None
            return None
        return wanted

    def _reconcile_pause(self, reported: bool) -> bool:
        """把「服务端报的暂停态」和「用户意图」调和成一个对外可见的值。

        - 服务端已经跟上意图 → 清掉意图，直接用服务端的（两边本来就一致）；
        - 服务端还没跟上（这条回执/事件是操作之前产生的）→ 暂时以意图为准；
        - 没有意图/意图过期 → 完全信服务端。
        """
        wanted = self.pause_intent_wanted()
        if wanted is None:
            return reported
        if reported == wanted:
            self._pause_intent = None
        return wanted
    def toggle_pause_record(self) -> None:
        record = self.store.record
        if not record.active:
            return
        request = P.REQ_RESUME_RECORD if record.paused else P.REQ_PAUSE_RECORD
        if not self.send_if_supported(request):
            return
        # 乐观翻转并记下意图：OBS 的 RecordStateChanged 不带 outputPaused 字段，
        # 只能靠这两条 Pause/Resume 请求自己记，回执到了再校准
        wanted = not record.paused
        self._pause_intent = (wanted, time.monotonic() + INTENT_WINDOW_S)
        self.store.set_record(replace(record, paused=wanted))
        self.send(P.REQ_GET_RECORD_STATUS)

    def toggle_stream(self) -> None:
        if self.store.stream.active:
            self.send(P.REQ_STOP_STREAM)
        else:
            self.send(P.REQ_START_STREAM)
        self.send(P.REQ_GET_STREAM_STATUS)

    # ---- D7：回放缓冲区 ----
    def toggle_replay_buffer(self) -> None:
        request = (
            P.REQ_STOP_REPLAY_BUFFER if self.store.replay_buffer.active
            else P.REQ_START_REPLAY_BUFFER
        )
        if not self.send_if_supported(request):
            return
        self.send_if_supported(P.REQ_GET_REPLAY_BUFFER_STATUS)

    def save_replay_buffer(self) -> None:
        """把缓冲区里的最后一段立刻写盘。"""
        if not self.store.replay_buffer.active:
            return
        self.send_if_supported(P.REQ_SAVE_REPLAY_BUFFER)

    # ---- D8：虚拟摄像机 ----
    def toggle_virtualcam(self) -> None:
        request = (
            P.REQ_STOP_VIRTUALCAM if self.store.virtualcam.active else P.REQ_START_VIRTUALCAM
        )
        if not self.send_if_supported(request):
            return
        self.send_if_supported(P.REQ_GET_VIRTUALCAM_STATUS)

    # ---- 场景点击：演播室模式下点场景是“设为预览”，否则直接切节目 ----
    def scene_clicked(self, name: str) -> None:
        if self.store.studio_mode:
            self.set_preview_scene(name)
        else:
            self.switch_scene(name)

    # ---- G1/G2：转场 ----
    def set_transition(self, name: str) -> None:
        if not name or name == self.store.current_transition:
            return
        self.store.set_current_transition(name)
        self.send(P.REQ_SET_CURRENT_SCENE_TRANSITION, {"transitionName": name})

    def set_transition_duration(self, duration_ms: int) -> None:
        self.store.set_transition_duration(duration_ms)
        if not self.store.supports(P.REQ_SET_TRANSITION_DURATION):
            logger.warning("服务端不支持 %s，时长仅保存在本地", P.REQ_SET_TRANSITION_DURATION)
            return
        self.send(P.REQ_SET_TRANSITION_DURATION, {"transitionDuration": int(duration_ms)})

    # ---- G3：演播室模式 ----
    def set_studio_mode(self, enabled: bool) -> None:
        # 乐观更新 + 记意图：连接时在途的 GetStudioModeEnabled 回执可能后到，
        # 不能让陈旧值把用户刚切的状态按回去（见 _reconcile_flag）
        self._studio_intent = (bool(enabled), time.monotonic() + INTENT_WINDOW_S)
        self.store.set_studio_mode(enabled)
        self.send(P.REQ_SET_STUDIO_MODE_ENABLED, {"studioModeEnabled": bool(enabled)})
        if enabled:
            self.send(P.REQ_GET_SCENE_LIST)  # 拿 currentPreviewSceneName
        self._debounced(self._items_timer)

    def set_preview_scene(self, name: str) -> None:
        if not name or name == self.store.preview_scene:
            return
        self.store.set_preview_scene(name)
        self._debounced(self._items_timer)
        self.send(P.REQ_SET_CURRENT_PREVIEW_SCENE, {"sceneName": name})

    def trigger_transition(self) -> None:
        """用当前转场把预览切到节目。"""
        self.send(P.REQ_TRIGGER_STUDIO_MODE_TRANSITION)

    # ---- G4：T 型推杆 ----
    def tbar_supported(self) -> bool:
        return self.store.supports(P.REQ_SET_TBAR_POSITION)

    def set_tbar_position(self, position: float, release: bool = False) -> None:
        """手动推转场。

        `release=False` 用于拖动过程中的每一帧（OBS 会按这个位置把转场"推"到一半）；
        `release=True` 表示松手 —— 推到底就完成转场，中途松手 OBS 会自己回退。
        """
        if not self.store.studio_mode:
            return
        if not self.send_if_supported(
            P.REQ_SET_TBAR_POSITION,
            {"position": float(position), "release": bool(release)},
        ):
            return
        self.store.set_tbar_position(position)
        # 推杆是在**有意识地延长**这次转场，看门狗要跟着往后推，
        # 否则慢慢推的时候会被兜底逻辑打断
        if self.store.transitioning:
            self._arm_transition_watchdog()

    # ---- G5：快捷转场槽位（客户端侧，不碰 profile）----
    def quick_transitions(self) -> list[dict]:
        return list(self.config.quick_transitions)

    def save_quick_transitions(self, slots: list[dict]) -> None:
        self.config.quick_transitions = list(slots)
        self.settings.save_config(self.config)

    def run_quick_transition(self, index: int) -> None:
        """「先设当前转场（含时长）→ 再 Trigger」两步打包。

        OBS 的快捷转场存在 profile 里、协议不直接暴露，所以槽位由客户端自己维护，
        效果等价于 OBS 的快捷转场，且完全不用碰 SetProfileParameter。
        """
        slots = self.config.quick_transitions
        if not 0 <= index < len(slots):
            return
        if not self.store.studio_mode:
            self._raise_error_once("快捷转场只在工作室模式下有效")
            return
        slot = slots[index]
        name = str(slot.get("transition", "") or "")
        if not name:
            return
        duration = int(slot.get("duration_ms", 0) or 0)
        # 顺序不能反：worker 按队列串行下发，OBS 会先改转场再执行
        self.store.set_current_transition(name, duration_ms=duration or None)
        self.send_if_supported(P.REQ_SET_CURRENT_SCENE_TRANSITION, {"transitionName": name})
        if duration > 0:
            self.send_if_supported(
                P.REQ_SET_TRANSITION_DURATION, {"transitionDuration": duration}
            )
        self.send(P.REQ_TRIGGER_STUDIO_MODE_TRANSITION)

    def cut_to_preview(self) -> None:
        """CUT：不经转场，直接把预览场景顶到节目。"""
        scene = self.store.preview_scene or self.store.current_scene
        if not scene:
            return
        self.send(P.REQ_SET_CURRENT_PROGRAM_SCENE, {"sceneName": scene})
        self.send(P.REQ_GET_SCENE_LIST)

    # ---- B5：场景增删改名 ----
    def scene_exists(self, name: str) -> bool:
        return any(scene.name == name for scene in self.store.scenes)

    def supports_scene_edit(self) -> bool:
        """B5 三个请求能力一致，用一个代表判断即可。"""
        return self.store.supports(P.REQ_CREATE_SCENE)

    def create_scene(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            self._raise_error_once("场景名不能为空")
            return
        if self.scene_exists(name):
            self._raise_error_once(f"已存在名为「{name}」的场景")
            return
        # 不由本地乐观插入：名字是否合法由服务端说了算（非法字符等），
        # 等 SceneCreated 事件/GetSceneList 回执再落状态更稳。
        self.send(P.REQ_CREATE_SCENE, {"sceneName": name})
        self.send(P.REQ_GET_SCENE_LIST)

    def rename_scene(self, old_name: str, new_name: str) -> None:
        old_name = (old_name or "").strip()
        new_name = (new_name or "").strip()
        if not old_name or not new_name:
            self._raise_error_once("场景名不能为空")
            return
        if old_name == new_name:
            return
        if self.scene_exists(new_name):
            self._raise_error_once(f"已存在名为「{new_name}」的场景")
            return
        self.send(P.REQ_SET_SCENE_NAME, {"sceneName": old_name, "newSceneName": new_name})
        self.send(P.REQ_GET_SCENE_LIST)

    def remove_scene(self, name: str) -> None:
        name = (name or "").strip()
        if not name or not self.scene_exists(name):
            return
        self.send(P.REQ_REMOVE_SCENE, {"sceneName": name})
        self.send(P.REQ_GET_SCENE_LIST)

    def can_remove_scene(self, name: str) -> bool:
        """OBS 不允许删掉最后一个场景。"""
        return bool(name) and len(self.store.scenes) > 1 and self.scene_exists(name)

    # ---- B6：场景排序 ----
    def supports_scene_reorder(self) -> bool:
        return self.store.supports(P.REQ_SET_SCENE_INDEX)

    def move_scene(self, name: str, new_index: int) -> None:
        """把 `name` 移到 OBS 内部的 `new_index` 位置。

        注意入参用的是**OBS 的 sceneIndex**（升序，0 在最底层），
        而不是界面上从上到下的行号 —— 界面是倒序的，调用方负责换算。
        """
        if not name or not self.scene_exists(name):
            return
        if not self.supports_scene_reorder():
            logger.warning("服务端不支持 %s，排序仅显示层生效", P.REQ_SET_SCENE_INDEX)
            return
        self.send(
            P.REQ_SET_SCENE_INDEX,
            {"sceneName": name, "newIndex": int(new_index)},
        )
        self.send(P.REQ_GET_SCENE_LIST)

    # ---------------------------------------------------------------- 结果
    @Slot(str, object)
    def _on_result(self, request_type: str, response) -> None:
        handler = getattr(self, f"_handle_{P.to_snake(request_type)}", None)
        if handler is None:
            return
        try:
            handler(response)
        except Exception:  # noqa: BLE001
            logger.exception("处理 %s 响应时出错", request_type)

    # 能力列表不在这里处理：握手时那一次 GetVersion 在 _on_connected 里就已落地，
    # 曾经这里有个 _handle_get_version，但没有任何路径会发出 GetVersion 请求，
    # 于是 supported_requests 永远是空集、supports() 永远乐观返回 True —— 能力探测形同虚设。

    def _handle_get_scene_list(self, data) -> None:
        raw_scenes = getattr(data, "scenes", []) or []
        scenes = [
            Scene(name=str(item.get("sceneName", "")), index=int(item.get("sceneIndex", 0)))
            for item in raw_scenes
            if isinstance(item, dict) and item.get("sceneName")
        ]
        scenes.sort(key=lambda s: s.index)
        # OBS 界面中越靠上的场景 sceneIndex 越大，这里翻转为“从上到下”的展示顺序
        scenes.reverse()
        current = getattr(data, "current_program_scene_name", "") or ""
        preview = getattr(data, "current_preview_scene_name", "") or ""
        self.store.set_scenes(scenes, current)
        if preview:
            self.store.set_preview_scene(preview)
        self._refresh_scene_items()

    def _handle_get_video_settings(self, data) -> None:
        numerator = getattr(data, "fps_numerator", 0) or 0
        denominator = getattr(data, "fps_denominator", 0) or 0
        fps = (numerator / denominator) if denominator else float(
            getattr(data, "fps_integer", 0) or 0
        )
        self.store.set_video(
            VideoSettings(
                base_width=int(getattr(data, "base_width", 0) or 0),
                base_height=int(getattr(data, "base_height", 0) or 0),
                output_width=int(getattr(data, "output_width", 0) or 0),
                output_height=int(getattr(data, "output_height", 0) or 0),
                fps=fps,
            )
        )

    def _handle_get_record_status(self, data) -> None:
        active = bool(getattr(data, "output_active", False))
        duration = int(getattr(data, "output_duration", 0) or 0)
        if active:
            if self._record_since_ms is None:
                self._record_since_ms = time.monotonic() * 1000
            if not duration:
                duration = int(time.monotonic() * 1000 - self._record_since_ms)
        else:
            self._record_since_ms = None
        paused = bool(getattr(data, "output_paused", False))
        # 暂停刚发出去、回执还没回来时，OBS 可能仍报旧值（这个回执是"操作之前"发出去的）。
        # 以「用户意图」为准，避免界面上的按钮来回跳；
        # 服务端跟上了、或意图过期，就完全信服务端。
        paused = self._reconcile_pause(paused)
        self.store.set_record(
            RecordStatus(
                active=active,
                paused=paused,
                timecode=str(getattr(data, "output_timecode", "") or ""),
                duration_ms=duration,
                bytes_written=int(getattr(data, "output_bytes", 0) or 0),
                output_path=str(getattr(data, "output_path", "") or ""),
            )
        )
        self._refresh_record_warning()

    def _handle_get_stream_status(self, data) -> None:
        active = bool(getattr(data, "output_active", False))
        duration = int(getattr(data, "output_duration", 0) or 0)
        if active:
            if self._stream_since_ms is None:
                self._stream_since_ms = time.monotonic() * 1000
            if not duration:
                duration = int(time.monotonic() * 1000 - self._stream_since_ms)
        else:
            self._stream_since_ms = None
        self.store.set_stream(
            StreamStatus(
                active=active,
                reconnecting=bool(getattr(data, "output_reconnecting", False)),
                timecode=str(getattr(data, "output_timecode", "") or ""),
                duration_ms=duration,
                congestion=float(getattr(data, "output_congestion", 0) or 0),
                bytes_written=int(getattr(data, "output_bytes", 0) or 0),
                skipped_frames=int(getattr(data, "output_skipped_frames", 0) or 0),
                total_frames=int(getattr(data, "output_total_frames", 0) or 0),
            )
        )

    def _handle_get_replay_buffer_status(self, data) -> None:
        # 拿到正常响应说明资源其实可用（例如用户刚在 OBS 里配好回放缓冲），撤销降级标记
        self.store.clear_unavailable(P.REQ_GET_REPLAY_BUFFER_STATUS)
        self.store.set_replay_buffer(
            ReplayBufferStatus(
                active=bool(getattr(data, "output_active", False)),
                saved_path=self.store.replay_buffer.saved_path,
            )
        )

    def _handle_get_virtualcam_status(self, data) -> None:
        self.store.clear_unavailable(P.REQ_GET_VIRTUALCAM_STATUS)
        self.store.set_virtualcam(
            VirtualcamStatus(active=bool(getattr(data, "output_active", False)))
        )

    def _handle_get_stats(self, data) -> None:
        self.store.set_stats(
            ObsStats(
                cpu_percent=float(getattr(data, "cpu_usage", 0) or 0),
                memory_mb=float(getattr(data, "memory_usage", 0) or 0),
                disk_space_mb=float(getattr(data, "available_disk_space", 0) or 0),
                fps=float(getattr(data, "active_fps", 0) or 0),
                render_skipped_frames=int(getattr(data, "render_skipped_frames", 0) or 0),
                render_total_frames=int(getattr(data, "render_total_frames", 0) or 0),
                output_skipped_frames=int(getattr(data, "output_skipped_frames", 0) or 0),
                output_total_frames=int(getattr(data, "output_total_frames", 0) or 0),
            )
        )

    def _handle_get_scene_item_list(self, data) -> None:
        raw_items = getattr(data, "scene_items", []) or []
        scene = self._items_scene
        self._items_buffer = {
            int(item.get("sceneItemId", -1)): SceneItem(
                item_id=int(item.get("sceneItemId", -1)),
                source_name=str(item.get("sourceName", "")),
                source_type=str(item.get("sourceType", "") or ""),
                source_kind=str(item.get("sourceKind", "") or ""),
                enabled=True,
            )
            for item in raw_items
            if isinstance(item, dict)
        }
        self._pending_enabled = deque(
            (self._items_generation, scene, item_id) for item_id in self._items_buffer
        )
        if not self._pending_enabled:
            self.store.set_scene_items([])
            return
        # 可见性不在 GetSceneItemList 返回里，逐项查询；请求按序返回，用队列做关联
        for item_id in self._items_buffer:
            self.send(
                P.REQ_GET_SCENE_ITEM_ENABLED,
                {"sceneName": scene, "sceneItemId": item_id},
            )

    def _handle_get_scene_item_enabled(self, data) -> None:
        if not self._pending_enabled:
            return
        generation, scene, item_id = self._pending_enabled.popleft()
        if generation != self._items_generation:
            return
        item = self._items_buffer.get(item_id)
        if item is not None:
            item.enabled = bool(getattr(data, "scene_item_enabled", False))
        if not self._pending_enabled:  # 最后一项返回，整体提交
            items = list(self._items_buffer.values())
            items.reverse()  # 与 OBS 一致：越靠上的来源越先显示
            self.store.set_scene_items(items)

    # ---------------------------------------------------------------- 事件
    @Slot(str, object)
    def _on_event(self, event_type: str, data) -> None:
        handler = getattr(self, f"_event_{P.to_snake(event_type)}", None)
        if handler is None:
            logger.debug("未处理的事件：%s", event_type)
            return
        try:
            handler(data)
        except Exception:  # noqa: BLE001
            logger.exception("处理事件 %s 时出错", event_type)

    def _event_current_program_scene_changed(self, data) -> None:
        name = str(getattr(data, "scene_name", "") or "")
        if not name:
            return
        self.store.set_current_scene(name)
        self._debounced(self._items_timer)

    def _event_scene_created(self, data) -> None:
        self._debounced(self._refresh_timer)

    def _event_scene_removed(self, data) -> None:
        self._debounced(self._refresh_timer)

    def _event_scene_name_changed(self, data) -> None:
        self._debounced(self._refresh_timer)

    def _event_scene_list_reindexed(self, data) -> None:
        # 场景顺序变了：直接重拉一次场景列表（场景数量少，代价低）
        self._debounced(self._refresh_timer)

    # ---- C4：来源列表增量同步 ----
    # 这些事件都带 sceneName，只在**当前展示的场景**上做增量，其余场景等切过去再拉。
    # 只要事件信息不足（缺 sceneName / 缺 item），就退回整体刷新兜底 —— 宁可多一次往返，
    # 也不能让列表和 OBS 实际对不上。
    def _event_scene_item_created(self, data) -> None:
        scene = str(getattr(data, "scene_name", "") or "")
        if not self._is_active_source_scene(scene):
            return
        raw = getattr(data, "scene_item", None) or {}
        item_id = int(raw.get("sceneItemId", -1)) if isinstance(raw, dict) else -1
        source_name = str(raw.get("sourceName", "") or "") if isinstance(raw, dict) else ""
        if item_id < 0 or not source_name:
            # 个别版本不带完整 sceneItem，退回整体刷新
            self._debounced(self._items_timer)
            return
        item = SceneItem(
            item_id=item_id,
            source_name=source_name,
            source_type=str(raw.get("sourceType", "") or ""),
            source_kind=str(raw.get("sourceKind", "") or ""),
            # 新建的项默认可见（OBS 的 CreateSceneItem 默认 enabled=True），
            # 若实际不是，紧跟的 SceneItemEnableStateChanged 会纠正
            enabled=bool(raw.get("sceneItemEnabled", True)),
        )
        self.store.add_scene_item(item)

    def _event_scene_item_removed(self, data) -> None:
        scene = str(getattr(data, "scene_name", "") or "")
        if not self._is_active_source_scene(scene):
            return
        item_id = int(getattr(data, "scene_item_id", -1))
        if item_id < 0:
            self._debounced(self._items_timer)
            return
        self.store.remove_scene_item(item_id)

    def _event_scene_item_list_reindexed(self, data) -> None:
        scene = str(getattr(data, "scene_name", "") or "")
        if not self._is_active_source_scene(scene):
            return
        raw_items = getattr(data, "scene_items", None)
        if not isinstance(raw_items, list):
            self._debounced(self._items_timer)
            return
        # 事件里按 OBS 的 z 序（sceneIndex 升序）给；展示是倒序，所以反过来
        ordered = [
            int(entry.get("sceneItemId", -1))
            for entry in raw_items
            if isinstance(entry, dict) and entry.get("sceneItemId") is not None
        ]
        ordered.reverse()
        self.store.reorder_scene_items(ordered)

    def _event_input_name_changed(self, data) -> None:
        old_name = str(getattr(data, "old_input_name", "") or "")
        new_name = str(getattr(data, "input_name", "") or "")
        if not new_name:
            return
        if not self.store.rename_scene_item_source(old_name, new_name):
            return
        # 混音器里的同名源也要跟着改，否则推子会挂到一个已改名的源上
        self.refresh_audio()

    # ---- L：媒体源 ----
    def _media_state_from_event(self, name: str, state: str, **extra) -> None:
        """事件驱动的媒体状态更新，同样要过"意图窗口"。

        坑：连点「播放 → 暂停」时，OBS 的 `MediaInputPlaybackStarted` 可能**迟到**，
        在 PAUSE 之后才到，于是把刚翻成"已暂停"的状态又按回"播放中"。
        判定标准与状态回执一致：窗口内一律以本地意图为准。
        """
        if not name:
            return
        optimistic = self._media_optimistic.get(name)
        if optimistic is not None:
            want, until = optimistic
            if time.monotonic() < until:
                logger.debug("忽略迟到的媒体事件（%s）：本地意图是 %s", state, want)
                return
            self._media_optimistic.pop(name, None)
        self.store.update_media(name, state=state, **extra)

    def _event_media_input_playback_started(self, data) -> None:
        name = str(getattr(data, "input_name", "") or "")
        self._media_state_from_event(name, P.MEDIA_STATE_PLAYING, cursor_ms=0)

    def _event_media_input_playback_ended(self, data) -> None:
        name = str(getattr(data, "input_name", "") or "")
        self._media_state_from_event(name, P.MEDIA_STATE_ENDED)
        if name:
            # 播完之后 cursor 会停在末尾，回读一次以免进度条显示错位
            self._debounced(self._media_timer)

    def _event_media_input_action_triggered(self, data) -> None:
        action = str(getattr(data, "media_action", "") or "")
        name = str(getattr(data, "input_name", "") or "")
        if name and action:
            self._optimistic_media(name, action)

    # ---- M：场景集合与配置文件 ----
    def _event_current_scene_collection_changed(self, data) -> None:
        name = str(getattr(data, "scene_collection_name", "") or "")
        if name:
            self.store.set_scene_collections(self.store.scene_collections, name)
            self._after_config_switch()

    def _event_current_profile_changed(self, data) -> None:
        name = str(getattr(data, "profile_name", "") or "")
        if name:
            self.store.set_profiles(self.store.profiles, name)
            self._after_config_switch()

    def _after_config_switch(self) -> None:
        """切换场景集合 / 配置文件后，OBS 侧的场景与输出可能整体换了一套。

        必须**整体作废重来**，不能做增量 —— 增量更新会在新旧数据之间产生
        一个"看起来对但其实是混合"的中间态。
        """
        self.store.reset_runtime_state()
        self._record_directory_asked = False
        if self.store.connection_state == CONNECTED:
            self.refresh_all()

    def _is_active_source_scene(self, scene: str) -> bool:
        """事件里的场景是不是"来源列表当前展示的那个"。"""
        if not scene:
            return False
        return scene == self.source_scene

    def _event_scene_item_enable_state_changed(self, data) -> None:
        scene = str(getattr(data, "scene_name", "") or "")
        item_id = int(getattr(data, "scene_item_id", -1))
        enabled = bool(getattr(data, "scene_item_enabled", False))
        if scene and scene != self.store.current_scene:
            return
        self._apply_item_enabled(scene, item_id, enabled)

    # ---- F2：缩略图 ----
    def _handle_get_source_screenshot(self, data) -> None:
        role = self._frame_queue.popleft() if self._frame_queue else "program"
        self._frame_pending.discard(role)
        image_data = getattr(data, "image_data", "") or ""
        payload = image_data.split(",", 1)[-1] if "," in image_data else image_data
        if not payload:
            return
        try:
            self.store.frame_ready.emit(role, base64.b64decode(payload))
        except Exception:  # noqa: BLE001
            logger.debug("缩略图解码失败", exc_info=True)

    @Slot()
    def _on_frame_tick(self) -> None:
        if self.store.connection_state != CONNECTED:
            return
        roles = ["preview", "program"] if self.store.studio_mode else ["program"]
        for role in roles:
            if role in self._frame_pending:
                continue
            scene = (
                self.store.preview_scene if role == "preview" else self.store.current_scene
            )
            if not scene:
                continue
            self._frame_pending.add(role)
            self._frame_queue.append(role)
            payload: dict[str, object] = {
                "sourceName": scene,
                "imageFormat": "jpeg",
                "imageCompressionQuality": int(self.config.preview_quality),
            }
            width = int(self.config.preview_width)
            if width > 0:
                payload["imageWidth"] = width
                # 宽高一起给，按画布比例算高，避免只给宽度时部分版本处理不一致
                video = self.store.video
                if video.base_width and video.base_height:
                    payload["imageHeight"] = max(
                        1, round(width * video.base_height / video.base_width)
                    )
            self.send(P.REQ_GET_SOURCE_SCREENSHOT, payload)

    # ---- G ----
    def _reconcile_flag(self, intent: tuple[bool, float] | None, reported: bool) -> bool:
        """把「服务端的布尔状态」和「用户刚表达的意图」调和成一个对外可见的值。

        这是个反复踩到的坑，统一成一条规则：
        **乐观更新之后，任何"操作之前就产生"的回执或事件都不得与之相悖。**

        成立的原因：请求响应走 ReqClient、事件走 EventClient，是**两条独立的 websocket**，
        互相之间没有顺序保证；再加上轮询会在用户操作前就把请求发出去，
        所以"陈旧数据后到"是常态而不是意外，必须靠一段时间窗口仲裁。

        返回 True/False 表示该用哪个值。
        """
        if intent is None:
            return reported
        wanted, until = intent
        if time.monotonic() >= until:
            return reported          # 窗口过期：完全信服务端
        return wanted                # 窗口内：以意图为准

    def _intent_expired(self, intent: tuple[bool, float] | None) -> bool:
        return intent is None or time.monotonic() >= intent[1]

    def _handle_get_studio_mode_enabled(self, data) -> None:
        reported = bool(getattr(data, "studio_mode_enabled", False))
        # 连接时那批刷新发出的 GetStudioModeEnabled，回执可能落在用户切开关之后。
        # 不挡的话会把刚打开的演播室模式按回关闭（界面上闪一下，而且这一瞬
        # T 型推杆会被判成"非演播室模式"从而把用户的拖动静默丢掉）。
        self.store.set_studio_mode(self._reconcile_flag(self._studio_intent, reported))
        if self._intent_expired(self._studio_intent) or reported == self.store.studio_mode:
            self._studio_intent = None
        self._debounced(self._items_timer)

    def _refresh_transitions(self) -> None:
        """按服务端能力选请求名：优先 GetSceneTransitionList（5.1+），退回 GetTransitionList（5.0）。"""
        if not self.send_if_supported(P.REQ_GET_SCENE_TRANSITION_LIST):
            self.send_if_supported(P.REQ_GET_TRANSITION_LIST)
        self.send_if_supported(P.REQ_GET_CURRENT_SCENE_TRANSITION)

    def _read_transition_names(self, data) -> None:
        raw = getattr(data, "transitions", []) or []
        names = [
            str(item.get("transitionName", ""))
            for item in raw
            if isinstance(item, dict) and item.get("transitionName")
        ]
        self.store.set_transitions(names)

    def _handle_get_scene_transition_list(self, data) -> None:
        """5.1+ 的一次性返回：列表 + 当前转场。"""
        self._read_transition_names(data)
        current = str(getattr(data, "current_scene_transition_name", "") or "")
        if current:
            self.store.set_current_transition(current)

    def _handle_get_transition_list(self, data) -> None:
        self._read_transition_names(data)

    def _handle_get_current_scene_transition(self, data) -> None:
        duration = getattr(data, "transition_duration", None)
        configurable = getattr(data, "transition_configurable", None)
        self.store.set_current_transition(
            str(getattr(data, "transition_name", "") or ""),
            int(duration) if isinstance(duration, (int, float)) else None,
            bool(configurable) if configurable is not None else None,
        )

    def _event_current_preview_scene_changed(self, data) -> None:
        name = str(getattr(data, "scene_name", "") or "")
        if not name:
            return
        self.store.set_preview_scene(name)
        self._debounced(self._items_timer)

    def _event_current_scene_transition_changed(self, data) -> None:
        self.store.set_current_transition(str(getattr(data, "transition_name", "") or ""))

    def _event_current_scene_transition_duration_changed(self, data) -> None:
        duration = getattr(data, "transition_duration", None)
        if isinstance(duration, (int, float)):
            self.store.set_transition_duration(int(duration))

    def _event_scene_transition_started(self, data) -> None:
        self.store.set_transitioning(True)
        self._arm_transition_watchdog()

    def _event_scene_transition_ended(self, data) -> None:
        self._transition_watchdog.stop()
        self.store.set_transitioning(False)
        # G4：转场结束时 T 型推杆要归位（无论这次是推到底完成、还是中途松手回退），
        # 顺手回读一次，让本地的推杆位置跟 OBS 真正对上。
        self.store.set_tbar_position(0.0)

    def _arm_transition_watchdog(self) -> None:
        """给"转场中"加一道兜底。

        `SceneTransitionEnded` 万一不来（事件丢失、OBS 异常退出转场态），
        `transitioning` 会一直是 True，转场按钮就**永久**卡在「转场中」点不动。
        所以按转场时长推算一个上限，到点还没结束就自己收回来。

        上限取得很宽松（≥5 秒、最长 2 分钟）：兜底晚一点没大碍，
        而**过早**清掉会让用户在转场还没结束时又点一次转场，那才是真问题。
        """
        interval = min(max(self.store.transition_duration_ms * 3, 5000), 120_000)
        self._transition_watchdog.start(interval)

    @Slot()
    def _on_transition_watchdog(self) -> None:
        if not self.store.transitioning:
            return
        logger.warning("转场结束事件迟迟未到，自动复位「转场中」状态")
        self.store.set_transitioning(False)
        self.store.set_tbar_position(0.0)

    def _event_studio_mode_state_changed(self, data) -> None:
        reported = bool(getattr(data, "studio_mode_enabled", False))
        enabled = self._reconcile_flag(self._studio_intent, reported)
        if self._intent_expired(self._studio_intent) or reported == enabled:
            self._studio_intent = None
        self.store.set_studio_mode(enabled)
        self._debounced(self._items_timer)
        if enabled:
            self.send(P.REQ_GET_SCENE_LIST)

    def _event_record_state_changed(self, data) -> None:
        state = str(getattr(data, "output_state", "") or "")
        active = bool(getattr(data, "output_active", False)) or state in P.ACTIVE_OUTPUT_STATES
        # OBS 的 RecordStateChanged 不直接带 outputPaused，但 PAUSED/RESUMED 两个状态
        # 只会在暂停切换时出现，用它们翻转本地标记，再靠 GetRecordStatus 校正
        paused = self.store.record.paused
        reported: bool | None = None
        if state == P.OUTPUT_PAUSED:
            reported = True
        elif state == P.OUTPUT_RESUMED:
            reported = False
        elif not active:
            reported = False  # 停止录制后暂停概念就没了
        if reported is not None:
            # 关键：事件走的是**另一条 websocket**，和请求响应之间没有顺序保证。
            # 一条"用户按下继续之前"就发出的 PAUSED 事件完全可能后到 ——
            # 直接采信它会把刚翻过来的状态又按回去，而且把意图也丢了，
            # 界面就卡在"已暂停"，要等下一拍轮询才可能纠正。
            # 所以这里同样要过意图窗口，不能无条件相信事件。
            paused = self._reconcile_pause(reported)
            if not active:
                # 停止录制时意图不再有意义，清掉免得残留
                self._pause_intent = None
        self.store.set_record(
            RecordStatus(
                active=active,
                paused=paused,
                timecode=self.store.record.timecode,
                duration_ms=self.store.record.duration_ms,
                bytes_written=self.store.record.bytes_written,
                output_path=str(getattr(data, "output_path", "") or self.store.record.output_path),
            )
        )
        self.send(P.REQ_GET_RECORD_STATUS)

    def _event_stream_state_changed(self, data) -> None:
        state = str(getattr(data, "output_state", "") or "")
        active = bool(getattr(data, "output_active", False)) or state in P.ACTIVE_OUTPUT_STATES
        reconnecting = state == P.OUTPUT_RECONNECTING
        self.store.set_stream(
            StreamStatus(
                active=active,
                reconnecting=reconnecting,
                timecode=self.store.stream.timecode,
                duration_ms=self.store.stream.duration_ms,
                congestion=self.store.stream.congestion,
                bytes_written=self.store.stream.bytes_written,
                skipped_frames=self.store.stream.skipped_frames,
                total_frames=self.store.stream.total_frames,
            )
        )
        self.send(P.REQ_GET_STREAM_STATUS)

    # ---- D7：回放缓冲区 ----
    def _event_replay_buffer_state_changed(self, data) -> None:
        state = str(getattr(data, "output_state", "") or "")
        active = bool(getattr(data, "output_active", False)) or state in P.ACTIVE_OUTPUT_STATES
        self.store.set_replay_buffer(
            ReplayBufferStatus(active=active, saved_path=self.store.replay_buffer.saved_path)
        )

    def _event_replay_buffer_saved(self, data) -> None:
        path = str(getattr(data, "saved_replay_path", "") or "")
        self.store.set_replay_buffer(
            ReplayBufferStatus(active=self.store.replay_buffer.active, saved_path=path)
        )
        if path:
            self.store.status_message_changed.emit(f"回放已保存：{path}")

    # ---- D8：虚拟摄像机 ----
    def _event_virtualcam_state_changed(self, data) -> None:
        state = str(getattr(data, "output_state", "") or "")
        active = bool(getattr(data, "output_active", False)) or state in P.ACTIVE_OUTPUT_STATES
        self.store.set_virtualcam(VirtualcamStatus(active=active))

    # ---------------------------------------------------------------- 内部
    def _debounced(self, timer: QTimer, delay_ms: int = 120) -> None:
        if timer.isActive():
            return
        timer.setInterval(delay_ms)
        timer.start()

    @Slot()
    def _refresh_scenes(self) -> None:
        if self.store.connection_state == CONNECTED:
            self.send(P.REQ_GET_SCENE_LIST)

    @Slot()
    def _refresh_scene_items(self) -> None:
        scene = self.source_scene
        if self.store.connection_state != CONNECTED or not scene:
            return
        self._items_generation += 1
        self._pending_enabled.clear()
        self._items_buffer = {}
        self._items_scene = scene
        self.send(P.REQ_GET_SCENE_ITEM_LIST, {"sceneName": scene})

    @property
    def source_scene(self) -> str:
        """来源列表当前操作的场景：演播室模式下是预览场景，否则是节目场景。"""
        if self.store.studio_mode and self.store.preview_scene:
            return self.store.preview_scene
        return self.store.current_scene

    def _apply_item_enabled(self, scene: str, item_id: int, enabled: bool) -> None:
        for item in self.store.scene_items:
            if item.item_id == item_id:
                item.enabled = enabled
                self.store.scene_items_changed.emit()
                return

    @Slot(str, str, bool, int)
    def _on_request_failed(
        self, request_type: str, message: str, transport_lost: bool, code: int = 0
    ) -> None:
        if request_type == P.REQ_GET_SOURCE_SCREENSHOT:
            # 缩略图 1fps 地失败会刷屏，静默丢弃并放回配额
            role = self._frame_queue.popleft() if self._frame_queue else None
            if role:
                self._frame_pending.discard(role)
            logger.debug("获取缩略图失败：%s", message)
            return
        if request_type in _AUDIO_GET_REQUESTS:
            # 音频查询是"逐源探测"，个别源不支持很正常，别弹框（否则一开就弹一堆）
            self._pop_pending_audio(request_type)
            logger.debug("音频查询 %s 失败：%s", request_type, message)
            return
        if request_type == P.REQ_SET_INPUT_MUTE and self._mute_all_pending:
            # E9：批量静音中的单个失败先攒着，等这一批走完再汇总提示一次
            self._mute_all_failures += 1
            self._mute_all_pending = max(0, self._mute_all_pending - 1)
            logger.debug("批量静音中 %s 失败：%s", message)
            return
        # 204 的文案就是 "... Your request type is not valid"，
        # 所以"是否名字不对"必须先判，否则下面那段回退永远不会被执行到。
        rejected = code == P.ERR_INVALID_REQUEST or (
            not code and ("not valid" in message.lower() or "code 204" in message.lower())
        )
        if rejected:
            self.store.mark_unsupported(request_type)
            if (
                request_type == P.REQ_GET_SCENE_TRANSITION_LIST
                and not self._transition_fallback_used
            ):
                # 服务端是 5.0：换老名字再试一次
                self._transition_fallback_used = True
                logger.warning("服务端不认 %s，退回 %s", request_type, P.REQ_GET_TRANSITION_LIST)
                self.send(P.REQ_GET_TRANSITION_LIST)
                return
            # 其余情况属于协议能力不匹配，用户无能为力：记日志即可，不弹框
            logger.warning("服务端不支持请求 %s：%s", request_type, message)
            return
        # 604/601 这类"请求合法但资源当前不可用"（没配回放缓冲、虚拟摄像机驱动缺失…）
        # 都是**预期的常态**，不是用户做错了什么。之前这里直接落到弹框分支，
        # 于是每轮 refresh_all 都会弹一次 "Replay buffer is not available"。
        # 处理方式与音频探测一致：静默记日志，不下发提示。
        if code in P.RESOURCE_ERROR_CODES:
            self.store.mark_unavailable(request_type)
            logger.debug("请求 %s 的资源当前不可用（code %d）：%s", request_type, code, message)
            return
        if not transport_lost:
            logger.warning("请求 %s 失败：%s", request_type, message)
            self._raise_error_once(f"{request_type}：{message}")
            return

        # 连接级失败才计入掉线判定，且只在"已连接"状态下计。
        # 连接还没建立/正在重建时收到的传输错误，属于上一条连接的残响，
        # 计进去会把刚落好的新连接又判成掉线。
        if self.store.connection_state != CONNECTED:
            logger.debug("忽略非连接态下的传输失败：%s（%s）", request_type, message)
            return
        self._transport_failures += 1
        logger.warning("请求 %s 遇到连接级错误（%d/%d）：%s",
                       request_type, self._transport_failures, TRANSPORT_FAILURE_THRESHOLD, message)
        if self._transport_failures >= TRANSPORT_FAILURE_THRESHOLD:
            self._handle_link_lost()

    def _handle_link_lost(self) -> None:
        # 只有"当前确实连着"才谈得上掉线。连接中/重连中/已断开时再来一次判定，
        # 只可能是上一条连接的残响，照着它去重连就会连出重复会话。
        if self.store.connection_state != CONNECTED:
            logger.debug("忽略非连接态下的掉线判定（当前 %s）", self.store.connection_state)
            self._transport_failures = 0
            return
        self._stop_polling()
        self.store.reset_runtime_state()
        if not self._manual_disconnect:
            self._awaiting_cleanup = True
            if not self._schedule_reconnect():
                self._awaiting_cleanup = False
                self.store.set_connection_state(DISCONNECTED, "连接中断")
        self._worker.request_disconnect.emit()  # 清理残留连接

    def _raise_error_once(self, detail: str, window_s: float = 10.0) -> None:
        """同一条错误在 window_s 内只提示一次，避免轮询类请求刷屏。"""
        now = time.monotonic()
        if self._last_error == detail and now - self._last_error_at < window_s:
            return
        self._last_error = detail
        self._last_error_at = now
        self.store.error_raised.emit("操作失败", detail)

    def _bind_worker(self) -> None:
        w = self._worker
        w.connected.connect(self._on_connected)
        w.connect_failed.connect(self._on_connect_failed)
        w.disconnected.connect(self._on_disconnected)
        w.event_link_failed.connect(self._on_event_link_failed)
        w.event_received.connect(self._on_event)
        w.result_ready.connect(self._on_result)
        w.request_failed.connect(self._on_request_failed)

# ---------------------------------------------------------------- 辅助
def _payload_to_tracks(payload) -> int:
    """GetInputAudioTracks 返回 {"1": true, "2": false, ...} -> 位掩码。"""
    if not isinstance(payload, dict):
        return 0
    mask = 0
    for key, value in payload.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if value and 1 <= index <= 6:
            mask |= 1 << (index - 1)
    return mask


def _tracks_to_payload(mask: int) -> dict[str, bool]:
    return {str(index): bool(mask & (1 << (index - 1))) for index in range(1, 7)}

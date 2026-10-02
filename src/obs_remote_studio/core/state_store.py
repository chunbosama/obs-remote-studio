"""全局状态中心：唯一数据源，UI 只订阅它的信号。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from .models import (
    AudioInput,
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

DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"


class StateStore(QObject):
    connection_state_changed = Signal(str)
    status_message_changed = Signal(str)
    server_info_changed = Signal(object)

    scenes_changed = Signal()
    current_scene_changed = Signal(str)
    scene_items_changed = Signal()

    record_changed = Signal()
    stream_changed = Signal()
    stats_changed = Signal()
    video_changed = Signal()

    # D7/D8：回放缓冲与虚拟摄像机
    replay_buffer_changed = Signal()
    virtualcam_changed = Signal()

    # G：转场与演播室模式
    studio_changed = Signal()
    preview_scene_changed = Signal(str)
    transitions_changed = Signal()
    transition_changed = Signal()
    transitioning_changed = Signal()

    # F2：缩略图（role: program / preview）
    frame_ready = Signal(str, bytes)

    # E：音频
    audio_changed = Signal()                 # 列表重建
    audio_input_changed = Signal(str)        # 单个源的音量/静音等变化
    audio_settings_changed = Signal()        # 高级音频属性变化（监听/平衡/偏移/轨道）

    # GetVersion 返回的 availableRequests，用于能力探测
    capabilities_changed = Signal()

    # A11：连接健康（RTT / 卡顿）
    health_changed = Signal()
    # D17：磁盘与时长预警
    record_warning_changed = Signal()
    # L：媒体源
    media_changed = Signal()
    # M：场景集合与配置文件
    collections_changed = Signal()
    profiles_changed = Signal()
    # G4：T 型推杆位置（0.0~1.0）
    tbar_changed = Signal()
    # G4：OBS 收下了推杆请求却没有反应（已知的 OBS 侧缺陷，见 controller 里的说明）
    tbar_ignored_changed = Signal()

    error_raised = Signal(str, str)  # 标题, 详情

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.connection_state: str = DISCONNECTED
        self.status_message: str = "未连接"
        self.server_info = ServerInfo()

        self.scenes: list[Scene] = []
        self.current_scene: str = ""
        self.scene_items: list[SceneItem] = []

        self.record = RecordStatus()
        self.stream = StreamStatus()
        self.stats = ObsStats()
        self.video = VideoSettings()
        self.replay_buffer = ReplayBufferStatus()
        self.virtualcam = VirtualcamStatus()

        self.studio_mode: bool = False
        self.preview_scene: str = ""
        self.transitions: list[str] = []
        self.current_transition: str = ""
        self.transition_duration_ms: int = 300
        self.transition_configurable: bool = True
        self.transitioning: bool = False
        self.supported_requests: set[str] = set()
        self.rejected_requests: set[str] = set()  # 服务端明确回过 204 的请求
        # 服务端回过 604：请求名合法，但这台机器上该资源当前不可用
        # （没配回放缓冲、虚拟摄像机驱动缺失…）。用于把对应按钮置灰。
        self.unavailable_requests: set[str] = set()
        # 每个不可用请求的**原因**，给 tooltip / 提示文字用
        self.unavailable_reasons: dict[str, str] = {}

        # E：混音器。meters 更新极其频繁（60Hz 级），刻意不发信号，
        # 由电平表控件按自己的刷新率来读，避免信号风暴。
        self.audio_inputs: list[AudioInput] = []
        self.meters: dict[str, float] = {}
        self.hidden_inputs: set[str] = set()

        # A11 / D17 / G4 / L / M
        self.health = ConnectionHealth()
        self.record_warning = RecordWarning()
        self.tbar_position: float = 0.0
        # OBS 收下了推杆请求却毫无反应（已知的 OBS 侧缺陷）——用来在界面上解释原因
        self.tbar_ignored: bool = False
        self.media: list[MediaStatus] = []
        self.scene_collections: list[str] = []
        self.current_scene_collection: str = ""
        self.profiles: list[str] = []
        self.current_profile: str = ""

    # ---------------------------------------------------------------- 写入
    def set_health(self, health: ConnectionHealth) -> None:
        self.health = health
        self.health_changed.emit()

    def set_record_warning(self, warning: RecordWarning) -> None:
        # 只在真正变化时发信号：轮询每拍都会算一次
        if (warning.level, warning.message) == (
            self.record_warning.level,
            self.record_warning.message,
        ):
            self.record_warning = warning
            return
        self.record_warning = warning
        self.record_warning_changed.emit()

    def set_tbar_position(self, value: float) -> None:
        self.tbar_position = max(0.0, min(float(value), 1.0))
        self.tbar_changed.emit()

    def set_tbar_ignored(self, value: bool) -> None:
        """OBS 收下推杆请求却没有产生任何转场动作（已知 OBS 侧缺陷）。"""
        value = bool(value)
        if self.tbar_ignored == value:
            return
        self.tbar_ignored = value
        self.tbar_ignored_changed.emit()

    def set_media(self, items: list[MediaStatus]) -> None:
        self.media = items
        self.media_changed.emit()

    def find_media(self, name: str) -> MediaStatus | None:
        for item in self.media:
            if item.name == name:
                return item
        return None

    def update_media(self, name: str, **fields) -> bool:
        item = self.find_media(name)
        if item is None:
            return False
        changed = False
        for key, value in fields.items():
            if value is not None and getattr(item, key, None) != value:
                setattr(item, key, value)
                changed = True
        if changed:
            self.media_changed.emit()
        return changed

    def set_scene_collections(self, names: list[str], current: str = "") -> None:
        self.scene_collections = names
        self.current_scene_collection = current or self.current_scene_collection
        self.collections_changed.emit()

    def set_profiles(self, names: list[str], current: str = "") -> None:
        self.profiles = names
        self.current_profile = current or self.current_profile
        self.profiles_changed.emit()
    def set_connection_state(self, state: str, message: str = "") -> None:
        if state != self.connection_state:
            self.connection_state = state
            self.connection_state_changed.emit(state)
        if message and message != self.status_message:
            self.status_message = message
            self.status_message_changed.emit(message)

    def set_server_info(self, info: ServerInfo) -> None:
        self.server_info = info
        self.server_info_changed.emit(info)

    def set_scenes(self, scenes: list[Scene], current: str = "") -> None:
        self.scenes = scenes
        if current:
            self.current_scene = current
        self.scenes_changed.emit()
        if current:
            self.current_scene_changed.emit(current)

    def set_current_scene(self, name: str) -> None:
        if name == self.current_scene:
            return
        self.current_scene = name
        self.current_scene_changed.emit(name)
        self.scenes_changed.emit()

    def reorder_scenes_by_display(self, names: list[str]) -> None:
        """按**界面展示顺序**（从上到下）重排场景，并回填 OBS 的 sceneIndex。

        界面是倒序的：展示第 0 行对应最大的 sceneIndex。
        回填是必须的 —— 后续再拿这个列表和 OBS 的 sceneIndex 比对时不会错位。
        """
        by_name = {scene.name: scene for scene in self.scenes}
        ordered = [by_name[n] for n in names if n in by_name]
        seen = {scene.name for scene in ordered}
        ordered.extend(scene for scene in self.scenes if scene.name not in seen)
        total = len(ordered)
        for row, scene in enumerate(ordered):
            object.__setattr__(scene, "index", total - 1 - row)
        self.scenes = ordered
        self.scenes_changed.emit()

    def set_scene_items(self, items: list[SceneItem]) -> None:
        self.scene_items = items
        self.scene_items_changed.emit()

    # ---- C4：来源列表增量更新（避免每次事件都整体重拉）----
    def add_scene_item(self, item: SceneItem) -> bool:
        """按 OBS 语义插入：新来源在最上层，也就是展示列表的最前面。

        返回是否真的插入了（重复 item_id 视为已存在，返回 False）。
        """
        if any(existing.item_id == item.item_id for existing in self.scene_items):
            return False
        self.scene_items.insert(0, item)
        self.scene_items_changed.emit()
        return True

    def remove_scene_item(self, item_id: int) -> bool:
        for index, item in enumerate(self.scene_items):
            if item.item_id == item_id:
                del self.scene_items[index]
                self.scene_items_changed.emit()
                return True
        return False

    def reorder_scene_items(self, order: list[int]) -> None:
        """`order` 是按展示顺序（从上到下）的 item_id 列表。

        只认列表里出现过的 id：事件里可能缺项（时序竞争），
        缺的项按原相对位置追加到末尾，绝不凭空丢来源。
        """
        by_id = {item.item_id: item for item in self.scene_items}
        reordered = [by_id[i] for i in order if i in by_id]
        seen = {item.item_id for item in reordered}
        reordered.extend(item for item in self.scene_items if item.item_id not in seen)
        if [i.item_id for i in reordered] == [i.item_id for i in self.scene_items]:
            return
        self.scene_items = reordered
        self.scene_items_changed.emit()

    def rename_scene_item_source(self, input_name: str, new_name: str) -> bool:
        """InputNameChanged：把当前场景里引用了该 input 的来源改名。

        注意改名是**来源级**的，同一个 input 可能被多个场景项引用，
        但这里只维护"当前展示的这一个场景"，所以只改本列表内的匹配项。
        """
        if not input_name or input_name == new_name:
            return False
        changed = False
        for item in self.scene_items:
            if item.source_name == input_name:
                item.source_name = new_name
                changed = True
        if changed:
            self.scene_items_changed.emit()
        return changed

    def set_record(self, status: RecordStatus) -> None:
        self.record = status
        self.record_changed.emit()

    def set_stream(self, status: StreamStatus) -> None:
        self.stream = status
        self.stream_changed.emit()

    def set_stats(self, stats: ObsStats) -> None:
        self.stats = stats
        self.stats_changed.emit()

    def set_video(self, video: VideoSettings) -> None:
        self.video = video
        self.video_changed.emit()

    def set_replay_buffer(self, status: ReplayBufferStatus) -> None:
        self.replay_buffer = status
        self.replay_buffer_changed.emit()

    def set_virtualcam(self, status: VirtualcamStatus) -> None:
        self.virtualcam = status
        self.virtualcam_changed.emit()

    # ---- G ----
    def set_studio_mode(self, enabled: bool) -> None:
        if enabled == self.studio_mode:
            return
        self.studio_mode = enabled
        self.studio_changed.emit()

    def set_preview_scene(self, name: str) -> None:
        if name == self.preview_scene:
            return
        self.preview_scene = name
        self.preview_scene_changed.emit(name)

    def set_transitions(self, names: list[str]) -> None:
        self.transitions = names
        self.transitions_changed.emit()

    def set_current_transition(self, name: str, duration_ms: int | None = None,
                               configurable: bool | None = None) -> None:
        self.current_transition = name
        if duration_ms is not None:
            self.transition_duration_ms = duration_ms
        if configurable is not None:
            self.transition_configurable = configurable
        self.transition_changed.emit()

    def set_transition_duration(self, duration_ms: int) -> None:
        if duration_ms == self.transition_duration_ms:
            return
        self.transition_duration_ms = duration_ms
        self.transition_changed.emit()

    def set_transitioning(self, value: bool) -> None:
        if value == self.transitioning:
            return
        self.transitioning = value
        self.transitioning_changed.emit()

    # ---- E：音频 ----
    def set_audio_inputs(self, inputs: list[AudioInput]) -> None:
        self.audio_inputs = inputs
        self.audio_changed.emit()

    def find_audio_input(self, name: str) -> AudioInput | None:
        return next((item for item in self.audio_inputs if item.name == name), None)

    def update_audio_input(self, name: str, **fields) -> None:
        item = self.find_audio_input(name)
        if item is None:
            return
        for key, value in fields.items():
            if value is not None and hasattr(item, key):
                setattr(item, key, value)
        self.audio_input_changed.emit(name)

    def set_meters(self, meters: dict[str, float]) -> None:
        self.meters = meters

    def set_hidden_inputs(self, names) -> None:
        self.hidden_inputs = set(names or ())
        self.audio_changed.emit()

    def set_capabilities(self, requests) -> None:
        self.supported_requests = set(requests or ())
        self.rejected_requests.clear()  # 新连接重新评估
        self.unavailable_requests.clear()
        self.unavailable_reasons.clear()
        self.capabilities_changed.emit()

    def mark_unsupported(self, request_type: str) -> None:
        """服务端回过 204，此后不再发这个请求。"""
        if request_type in self.rejected_requests:
            return
        self.rejected_requests.add(request_type)
        self.capabilities_changed.emit()

    def mark_unavailable(self, request_type: str, reason: str = "") -> None:
        """服务端回过 604 / 506 一类"请求名没问题，但当前状态下用不了"。

        与 mark_unsupported 分开：这类是运行时的常态（这台机器就没配回放缓冲、
        OBS 那边没开工作室模式），不代表协议不支持。
        这里只用于把 UI 收起来并说明原因，发不发请求仍由 supports() 决定。
        """
        if request_type in self.unavailable_requests:
            return
        self.unavailable_requests.add(request_type)
        if reason:
            self.unavailable_reasons[request_type] = reason
        self.capabilities_changed.emit()

    def is_unavailable(self, request_type: str) -> bool:
        return request_type in self.unavailable_requests

    def unavailable_reason(self, request_type: str) -> str:
        return self.unavailable_reasons.get(request_type, "")

    def clear_unavailable(self, request_type: str) -> None:
        """资源恢复可用（拿到正常响应）时撤销降级，让 UI 重新露出来。"""
        if request_type not in self.unavailable_requests:
            return
        self.unavailable_requests.discard(request_type)
        self.unavailable_reasons.pop(request_type, None)
        self.capabilities_changed.emit()

    def supports(self, request_type: str) -> bool:
        if request_type in self.rejected_requests:
            return False
        # 老服务端不上报 availableRequests 时按乐观处理，被拒后由 mark_unsupported 兜住
        return not self.supported_requests or request_type in self.supported_requests

    def reset_runtime_state(self) -> None:
        """断开时清空，避免残留旧状态误导操作。"""
        self.scenes = []
        self.current_scene = ""
        self.scene_items = []
        self.record = RecordStatus()
        self.stream = StreamStatus()
        self.stats = ObsStats()
        self.video = VideoSettings()
        self.replay_buffer = ReplayBufferStatus()
        self.virtualcam = VirtualcamStatus()
        self.studio_mode = False
        self.preview_scene = ""
        self.transitions = []
        self.transitioning = False
        self.rejected_requests.clear()
        self.unavailable_requests.clear()
        self.audio_inputs = []
        self.meters = {}
        self.health = ConnectionHealth()
        self.record_warning = RecordWarning()
        self.tbar_position = 0.0
        # OBS 收下了推杆请求却毫无反应（已知的 OBS 侧缺陷）——用来在界面上解释原因
        self.tbar_ignored = False
        self.media = []
        self.scene_collections = []
        self.current_scene_collection = ""
        self.profiles = []
        self.current_profile = ""
        self.audio_changed.emit()
        self.scenes_changed.emit()
        self.scene_items_changed.emit()
        self.record_changed.emit()
        self.stream_changed.emit()
        self.stats_changed.emit()
        self.video_changed.emit()
        self.studio_changed.emit()
        self.transitions_changed.emit()
        self.replay_buffer_changed.emit()
        self.virtualcam_changed.emit()
        self.health_changed.emit()
        self.record_warning_changed.emit()
        self.media_changed.emit()
        self.collections_changed.emit()
        self.profiles_changed.emit()
        self.tbar_changed.emit()

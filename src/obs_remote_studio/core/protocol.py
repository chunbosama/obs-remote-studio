"""obs-websocket v5 协议常量与工具。

只放“与 OBS 对话所需的静态知识”，不依赖 Qt，方便单测。
"""

from __future__ import annotations

import re

from obsws_python import Subs

# ---------------------------------------------------------------- 订阅位掩码
# 该掩码只用于 EventClient。ReqClient 必须用 subs=0：
# obsws-python 的 req() 是“发一个、收一个”，一旦请求连接也订阅事件，
# 事件帧会被当成响应解析（responseData/requestStatus 缺失）。
#
# 低容量事件按类别订阅（bit0~bit10）；高频事件必须**单独置位**才会推送，
# 不置位就不会收到 —— 电平表（InputVolumeMeters）走的正是这条规则。
_BASE_SUBSCRIPTIONS: int = int(
    Subs.SCENES
    | Subs.OUTPUTS
    | Subs.SCENEITEMS
    | Subs.TRANSITIONS  # 转场变更 / 转场开始结束
    | Subs.INPUTS  # 音量 / 静音 / 增删改名
    | Subs.UI  # 演播室模式开关
    | Subs.MEDIAINPUTS  # L：媒体源播放状态与动作事件
    | Subs.CONFIG  # M：场景集合 / 配置文件切换
)


def subscription_mask(audio_meters: bool = True) -> int:
    """是否订阅电平表（高频事件，60Hz 左右，关掉能省不少流量）。"""
    mask = _BASE_SUBSCRIPTIONS
    if audio_meters:
        mask |= int(Subs.INPUTVOLUMEMETERS)
    return mask


# 兼容旧调用：默认带电平表
SUBSCRIPTION_MASK: int = subscription_mask(True)

# ---------------------------------------------------------------- 事件
# obs-websocket 事件名 -> obsws-python 回调函数名（on_<snake_case>）
EVENTS: dict[str, str] = {
    "CurrentProgramSceneChanged": "on_current_program_scene_changed",
    "SceneCreated": "on_scene_created",
    "SceneRemoved": "on_scene_removed",
    "SceneNameChanged": "on_scene_name_changed",
    "SceneItemCreated": "on_scene_item_created",
    "SceneItemRemoved": "on_scene_item_removed",
    "SceneItemEnableStateChanged": "on_scene_item_enable_state_changed",
    "SceneItemListReindexed": "on_scene_item_list_reindexed",
    "SceneListReindexed": "on_scene_list_reindexed",
    "RecordStateChanged": "on_record_state_changed",
    "StreamStateChanged": "on_stream_state_changed",
    # D6/D7/D8：录制暂停、回放缓冲、虚拟摄像机
    "ReplayBufferStateChanged": "on_replay_buffer_state_changed",
    "ReplayBufferSaved": "on_replay_buffer_saved",
    "VirtualcamStateChanged": "on_virtualcam_state_changed",
    # G：转场与演播室模式
    "CurrentPreviewSceneChanged": "on_current_preview_scene_changed",
    "CurrentSceneTransitionChanged": "on_current_scene_transition_changed",
    "CurrentSceneTransitionDurationChanged": "on_current_scene_transition_duration_changed",
    "SceneTransitionStarted": "on_scene_transition_started",
    "SceneTransitionEnded": "on_scene_transition_ended",
    "StudioModeStateChanged": "on_studio_mode_state_changed",
    # E：音频
    "InputCreated": "on_input_created",
    "InputRemoved": "on_input_removed",
    "InputNameChanged": "on_input_name_changed",
    "InputVolumeChanged": "on_input_volume_changed",
    "InputMuteStateChanged": "on_input_mute_state_changed",
    "InputVolumeMeters": "on_input_volume_meters",
    # L：媒体源
    "MediaInputPlaybackStarted": "on_media_input_playback_started",
    "MediaInputPlaybackEnded": "on_media_input_playback_ended",
    "MediaInputActionTriggered": "on_media_input_action_triggered",
    # M：场景集合 / 配置文件
    "CurrentSceneCollectionChanging": "on_current_scene_collection_changing",
    "CurrentSceneCollectionChanged": "on_current_scene_collection_changed",
    "CurrentProfileChanging": "on_current_profile_changing",
    "CurrentProfileChanged": "on_current_profile_changed",
}

# ---------------------------------------------------------------- 请求
REQ_GET_VERSION = "GetVersion"
REQ_GET_SCENE_LIST = "GetSceneList"
REQ_SET_CURRENT_PROGRAM_SCENE = "SetCurrentProgramScene"
REQ_GET_SCENE_ITEM_LIST = "GetSceneItemList"
REQ_GET_SCENE_ITEM_ENABLED = "GetSceneItemEnabled"
REQ_SET_SCENE_ITEM_ENABLED = "SetSceneItemEnabled"
# B5：场景增删改名
REQ_CREATE_SCENE = "CreateScene"
REQ_REMOVE_SCENE = "RemoveScene"
REQ_SET_SCENE_NAME = "SetSceneName"
# B6：场景排序（部分版本无此请求，走能力探测降级）
REQ_SET_SCENE_INDEX = "SetSceneIndex"
REQ_START_RECORD = "StartRecord"
REQ_STOP_RECORD = "StopRecord"
# D6：录制暂停 / 继续（ws 5.1+ / OBS 30+，老服务端靠能力探测自动禁用按钮）
REQ_PAUSE_RECORD = "PauseRecord"
REQ_RESUME_RECORD = "ResumeRecord"
REQ_GET_RECORD_STATUS = "GetRecordStatus"
REQ_START_STREAM = "StartStream"
REQ_STOP_STREAM = "StopStream"
REQ_GET_STREAM_STATUS = "GetStreamStatus"
REQ_GET_STATS = "GetStats"
REQ_GET_VIDEO_SETTINGS = "GetVideoSettings"

# D7：回放缓冲区
REQ_START_REPLAY_BUFFER = "StartReplayBuffer"
REQ_STOP_REPLAY_BUFFER = "StopReplayBuffer"
REQ_GET_REPLAY_BUFFER_STATUS = "GetReplayBufferStatus"
REQ_SAVE_REPLAY_BUFFER = "SaveReplayBuffer"

# D8：虚拟摄像机
REQ_START_VIRTUALCAM = "StartVirtualcam"
REQ_STOP_VIRTUALCAM = "StopVirtualcam"
REQ_GET_VIRTUALCAM_STATUS = "GetVirtualcamStatus"

# F2：静态缩略图
REQ_GET_SOURCE_SCREENSHOT = "GetSourceScreenshot"

# G1/G2：转场
# 注意：obs-websocket 5.0 是 GetTransitionList，5.1 起改名为 GetSceneTransitionList，
# 老名字在 5.3+（OBS 30/31）已被移除，用了会返回 204 "Your request type is not valid"。
# 因此首选新名字，拿不到 availableRequests 时才退回老名字。
REQ_GET_SCENE_TRANSITION_LIST = "GetSceneTransitionList"
REQ_GET_TRANSITION_LIST = "GetTransitionList"
REQ_GET_CURRENT_SCENE_TRANSITION = "GetCurrentSceneTransition"
REQ_SET_CURRENT_SCENE_TRANSITION = "SetCurrentSceneTransition"
REQ_SET_TRANSITION_DURATION = "SetCurrentSceneTransitionDuration"

# E：音频 / 混音器
REQ_GET_INPUT_LIST = "GetInputList"
REQ_GET_INPUT_VOLUME = "GetInputVolume"
REQ_SET_INPUT_VOLUME = "SetInputVolume"
REQ_GET_INPUT_MUTE = "GetInputMute"
REQ_SET_INPUT_MUTE = "SetInputMute"
REQ_TOGGLE_INPUT_MUTE = "ToggleInputMute"
REQ_GET_INPUT_AUDIO_BALANCE = "GetInputAudioBalance"
REQ_SET_INPUT_AUDIO_BALANCE = "SetInputAudioBalance"
REQ_GET_INPUT_AUDIO_SYNC_OFFSET = "GetInputAudioSyncOffset"
REQ_SET_INPUT_AUDIO_SYNC_OFFSET = "SetInputAudioSyncOffset"
REQ_GET_INPUT_AUDIO_MONITOR_TYPE = "GetInputAudioMonitorType"
REQ_SET_INPUT_AUDIO_MONITOR_TYPE = "SetInputAudioMonitorType"
REQ_GET_INPUT_AUDIO_TRACKS = "GetInputAudioTracks"
REQ_SET_INPUT_AUDIO_TRACKS = "SetInputAudioTracks"

# 监听类型
MONITOR_NONE = "OBS_MONITORING_TYPE_NONE"
MONITOR_ONLY = "OBS_MONITORING_TYPE_MONITOR_ONLY"
MONITOR_AND_OUTPUT = "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"
MONITOR_CHOICES = (MONITOR_NONE, MONITOR_ONLY, MONITOR_AND_OUTPUT)
MONITOR_LABELS = {
    MONITOR_NONE: "关闭监听",
    MONITOR_ONLY: "仅监听",
    MONITOR_AND_OUTPUT: "监听并输出",
}

# 混音器里只显示"可能带音频"的源。
# obs-websocket 没有"该源是否含音频"的标志位，所以先按 kind 猜，
# 再用电平表事件反证（真正有音频的源一定会推电平），最后还有用户手动隐藏兜底。
AUDIO_KIND_PREFIXES = (
    "wasapi_",
    "coreaudio_",
    "pulse_",
    "alsa_",
    "jack_",
    "snd_",
    "audio_",
    "decklink_",
)
AUDIO_KIND_NAMES = frozenset(
    {
        "media_source",
        "ffmpeg_source",
        "vlc_source",
        "browser_source",
        "ndi_source",
        "game_capture",
        "monitor_capture",
        "dshow_input",
        "screen_capture",
    }
)


def is_audio_kind(kind: str) -> bool:
    if not kind:
        return False
    return kind.startswith(AUDIO_KIND_PREFIXES) or kind in AUDIO_KIND_NAMES


# G3：演播室模式
REQ_GET_STUDIO_MODE_ENABLED = "GetStudioModeEnabled"
REQ_SET_STUDIO_MODE_ENABLED = "SetStudioModeEnabled"
REQ_SET_CURRENT_PREVIEW_SCENE = "SetCurrentPreviewScene"
REQ_TRIGGER_STUDIO_MODE_TRANSITION = "TriggerStudioModeTransition"

# G4：T 型推杆（手动推转场）
# 注意：obs-websocket 5.x **只有** SetTBarPosition，没有 GetTBarPosition
# （v4 时代那个叫 GetTransitionPosition，v5 已移除）。所以位置只能由客户端自己维护，
# 用 SceneTransitionStarted/Ended 事件来校准，不要试图去"回读"。
REQ_SET_TBAR_POSITION = "SetTBarPosition"

# OBS 判定"推到底了"带 10% 量程的容差（window-basic-main-transitions.cpp 里
# T_BAR_CLAMP = T_BAR_PRECISION / 10）。客户端必须用同一个阈值，
# 否则会出现"OBS 觉得到了、我觉得没到"的状态错位。UI 与控制器共用这一个定义。
TBAR_CLAMP = 0.1

# T 型推杆的**版本门槛**。
# obs-studio issue #11372：`obs_frontend_set_tbar_position()` 在 **29.0.2 及更早可用**，
# 自 **29.1.0-beta1** 起不再更新推杆控件，导致 `TBarReleased()` 读到的永远是 0，
# API 无法完成手动转场；到 32.1.2 仍未修（修复 PR #13143 尚未合入）。
# 所以只在这个版本及更早才提供推杆。
TBAR_LAST_WORKING_VERSION = (29, 0, 2)


def tbar_version_ok(obs_version: str) -> bool:
    """按 OBS 版本判断 T 型推杆的 API 还能不能用。

    **读不出/拿不到版本时返回 True** —— 不能因为"不知道"就把功能藏掉；
    那种情况交给运行期的"请求成功但没效果"自检兜底（见 controller._on_tbar_effect_check）。
    """
    parsed = parse_version(obs_version)
    if not parsed:
        return True
    length = max(len(parsed), len(TBAR_LAST_WORKING_VERSION))
    left = parsed + (0,) * (length - len(parsed))
    right = TBAR_LAST_WORKING_VERSION + (0,) * (length - len(TBAR_LAST_WORKING_VERSION))
    return left <= right

# L：媒体源控制
REQ_GET_MEDIA_INPUT_STATUS = "GetMediaInputStatus"
REQ_TRIGGER_MEDIA_INPUT_ACTION = "TriggerMediaInputAction"
REQ_SET_MEDIA_INPUT_CURSOR = "SetMediaInputCursor"
REQ_OFFSET_MEDIA_INPUT_CURSOR = "OffsetMediaInputCursor"

# M：场景集合与配置文件（改用户 OBS 配置，切换前必须二次确认）
REQ_GET_SCENE_COLLECTION_LIST = "GetSceneCollectionList"
REQ_SET_CURRENT_SCENE_COLLECTION = "SetCurrentSceneCollection"
REQ_GET_PROFILE_LIST = "GetProfileList"
REQ_SET_CURRENT_PROFILE = "SetCurrentProfile"

# D12/D17：录制目录（D17 用它算本地剩余空间）
REQ_GET_RECORD_DIRECTORY = "GetRecordDirectory"

# P7：推流字幕（CEA-608）。
# 服务端实现（RequestHandler_Stream.cpp）三件事要记住：
#   ① `captionText` 是**必填**字段，但**允许空串** —— 空串就是"清除当前字幕"；
#   ② 它要求推流**正在进行**，否则回 501 OutputNotRunning；
#   ③ OBS 以 display_duration=0.0 调 obs_output_output_caption_text2()，
#      含义是"这条立即生效、下一条可紧接着发"，不需要客户端等待。
# 另外 OBS 内部按 CAPTION_LINE_BYTES 截断单行（CEA-608 惯例 32 字符），
# 所以界面上按 32 字符给提示，但**不做硬截断**（不替 OBS 决定怎么切）。
REQ_SEND_STREAM_CAPTION = "SendStreamCaption"
# CEA-608 单行惯例长度，仅用于界面提示与计数显示
CAPTION_LINE_CHARS = 32

# ---------------------------------------------------------------- 输出状态
OUTPUT_STARTING = "OBS_WEBSOCKET_OUTPUT_STARTING"
OUTPUT_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OUTPUT_STOPPING = "OBS_WEBSOCKET_OUTPUT_STOPPING"
OUTPUT_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
OUTPUT_RECONNECTING = "OBS_WEBSOCKET_OUTPUT_RECONNECTING"
# 录制暂停 / 继续（只用于 RecordStateChanged）
OUTPUT_PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
OUTPUT_RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"

ACTIVE_OUTPUT_STATES = frozenset({OUTPUT_STARTING, OUTPUT_STARTED, OUTPUT_RECONNECTING})

# ---------------------------------------------------------------- 媒体源（L）
# TriggerMediaInputAction 的动作枚举
MEDIA_ACTION_PLAY = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"
MEDIA_ACTION_PAUSE = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE"
MEDIA_ACTION_STOP = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP"
MEDIA_ACTION_RESTART = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"
MEDIA_ACTION_NEXT = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT"
MEDIA_ACTION_PREVIOUS = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS"

# GetMediaInputStatus.mediaState 的取值
MEDIA_STATE_NONE = "OBS_MEDIA_STATE_NONE"
MEDIA_STATE_PLAYING = "OBS_MEDIA_STATE_PLAYING"
MEDIA_STATE_OPENING = "OBS_MEDIA_STATE_OPENING"
MEDIA_STATE_BUFFERING = "OBS_MEDIA_STATE_BUFFERING"
MEDIA_STATE_PAUSED = "OBS_MEDIA_STATE_PAUSED"
MEDIA_STATE_STOPPED = "OBS_MEDIA_STATE_STOPPED"
MEDIA_STATE_ENDED = "OBS_MEDIA_STATE_ENDED"
MEDIA_STATE_ERROR = "OBS_MEDIA_STATE_ERROR"

# 正在播放（含准备阶段）——按钮文案与进度条是否走时都看这个集合
MEDIA_ACTIVE_STATES = frozenset(
    {MEDIA_STATE_PLAYING, MEDIA_STATE_OPENING, MEDIA_STATE_BUFFERING}
)
MEDIA_STATE_LABELS = {
    MEDIA_STATE_NONE: "空闲",
    MEDIA_STATE_PLAYING: "播放中",
    MEDIA_STATE_OPENING: "打开中",
    MEDIA_STATE_BUFFERING: "缓冲中",
    MEDIA_STATE_PAUSED: "已暂停",
    MEDIA_STATE_STOPPED: "已停止",
    MEDIA_STATE_ENDED: "已播完",
    MEDIA_STATE_ERROR: "出错",
}

# 这些 inputKind 才是媒体源，其余源不该出现播放控件
MEDIA_INPUT_KINDS = frozenset({"ffmpeg_source", "vlc_source"})


def is_media_kind(kind: str) -> bool:
    """`media_source` 是 OBS 内部通用名，也一并认。"""
    return (kind or "") in MEDIA_INPUT_KINDS or (kind or "").startswith("media_source")

# ---------------------------------------------------------------- 错误码
# obs-websocket v5 的 requestStatus.code。100 成功，其它为失败分类。
ERR_SUCCESS = 100
# 400~：请求本身有问题（名字/参数）——用户改不了，属"能力不匹配"，已由能力探测兜住
ERR_INVALID_REQUEST = 204  # Your request type is not valid（5.x 里 204 = RequestTypeInvalid）
ERR_MISSING_PARAMS = 300
ERR_RESOURCE_NOT_AVAILABLE = 604  # 请求合法，但目标资源当前不可用/未配置
# 601/602/603 一类"资源问题"：请求名字对，但对象不存在或当前不可用。
# 这些都是**运行时的正常业务状态**（例如这台机器没配回放缓冲、没有摄像头），
# 不该当成"操作失败"弹框打扰用户，只在 debug 日志里留痕即可。
RESOURCE_ERROR_CODES = frozenset({600, 601, 602, 603, 604, 605})
# 506 StudioModeNotActive：obs-websocket 里 SetTBarPosition / TriggerStudioModeTransition
# 第一件事就是检查工作室模式，没开就直接回这个码（见 RequestHandler_Transitions.cpp）。
# 它是**用户可纠正**的状态错误（去 OBS 里开工作室模式即可），所以既不能静默吞掉、
# 也不该按"操作失败"糊一个弹框了事 —— 得把原因讲清楚。
ERR_STUDIO_MODE_NOT_ACTIVE = 506
# 501 OutputNotRunning：请求合法，但那个输出当前没在跑。
# P7 推流字幕就靠它 —— 没推流时发字幕一定被拒（服务端第一件事就是查
# obs_frontend_streaming_active()）。这是**用户可理解、可纠正**的状态
# （去点「开始直播」即可），所以要给出明确中文提示，而不是弹一个
# "SendStreamCaption：Output is not running" 这种内部口气的框。
ERR_OUTPUT_NOT_RUNNING = 501

# ---------------------------------------------------------------- 名称转换
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    """CamelCase -> snake_case（GetSceneItemList -> get_scene_item_list）。"""
    return _CAMEL_RE.sub("_", name).lower()


# ---------------------------------------------------------------- 版本能力
def parse_version(text: str) -> tuple[int, ...]:
    """'5.5.0' -> (5, 5, 0)；解析失败返回空元组。"""
    parts: list[int] = []
    for chunk in (text or "").split("."):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def version_at_least(text: str, wanted: tuple[int, ...]) -> bool:
    """`text` 是否 >= `wanted`。解析不出来时返回 False（按不支持处理，按钮置灰）。"""
    parsed = parse_version(text)
    if not parsed:
        return False
    # 补齐长度再比，避免 (5,) 被当成 (5, 0, 0) 之外的东西
    length = max(len(parsed), len(wanted))
    return parsed + (0,) * (length - len(parsed)) >= wanted + (0,) * (length - len(wanted))

"""obs-websocket v4（OBS ≤ 27）兼容层：把 v5 请求/事件与 v4 之间做双向翻译。

**为什么要写成"翻译层"而不是在 UI 里到处 if v4**：
本项目全部功能开关都挂在 `StateStore.supports(v5请求名)` 上。只要让 v4 会话
**用 v5 的词汇上报能力**（见 `synthesize_capabilities`），那么"能力探测 → 按钮显隐"
这套既有逻辑一行都不用改；v4 里根本不存在的功能（电平表、声道平衡、删除场景…）
自然就不会出现在能力集合里，界面于是自动置灰/隐藏 —— 正是需求要的效果。

分层：
- 本模块**纯逻辑**，不依赖 Qt、不碰网络，方便单测（`tests/v4_compat_test.py`）。
- 真正的 socket 收发在 `client_v4.py`，它把 `call(v4请求名, 字段)` 交给下面的解析器。

v4 与 v5 的关键差异（都已对照 4.9.1 官方文档与 `src/` 源码核实）：
1. 连上后**服务端不发任何东西**（v5 会立刻发 op=0 Hello）→ 靠"是否收到 op 帧"判协议。
2. **没有 op 码/没有 d 包装**：请求 `{request-type, message-id, ...字段平铺}`，
   应答 `{message-id, status:"ok"|"error", error?, ...}`。`message-id` 必填。
3. **没有事件订阅位掩码**：所有事件推给每一条已认证连接。高频电平表事件
   **v4 完全没有**（v5 的 `InputVolumeMeters` 无对应物）。
4. **错误只有字符串，没有数字码**（v5 有 `requestStatus.code`）。所以这里要把
   v4 的错误文案**反推成 v5 的数字码**，否则 `controller._on_request_failed` 里
   那套"204 自愈 / 506 纠正工作室模式 / 604 置灰"的分支会全部失效。
5. 字段名大小写/连字符极不一致（`source` vs `sourceName`、`mute` vs `muted`、
   `transition-name` vs `transitionName`、`studio-mode`…），所以映射必须逐条写死。
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

# ---------------------------------------------------------------- 模式与端口
MODE_STANDARD = "v5"   # 标准模式
MODE_COMPAT = "v4"     # 兼容模式
MODE_LABELS = {MODE_STANDARD: "标准模式", MODE_COMPAT: "兼容模式"}

V4_DEFAULT_PORT = 4444
V5_DEFAULT_PORT = 4455

# v5 的数字错误码，v4 的错误文案会被反推成它们（见 error_code）
ERR_INVALID_REQUEST = 204   # Your request type is not valid
ERR_AUTH_FAILED = 4009      # v5 里认证失败用的 websocket 关闭码


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, MODE_LABELS[MODE_STANDARD])


# ---------------------------------------------------------------- 异常
class V4Error(Exception):
    """v4 服务端用 status:"error" 回绝了一次请求。"""

    def __init__(self, message: str, code: int = 0) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class V4Unsupported(V4Error):
    """这条 v4 请求在本服务端上不存在（`available-requests` 里没有）。

    与"请求失败"分开：可选能力要靠它做降级（例如老 v4 没有 GetSceneItemList）。
    """


class V4AuthError(V4Error):
    """认证失败（v5 是关闭码 4009，v4 只是回一条错误帧且不断开连接）。"""


# ---------------------------------------------------------------- 字段名转换
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    """CamelCase -> snake_case（与 obsws-python 的 util.to_snake_case 一致）。"""
    return _CAMEL_RE.sub("_", name).lower()


def as_object(payload: dict | None):
    """把 v5 形状的 dict 变成"能按属性读"的对象。

    `controller._handle_*` 全都是 `getattr(data, "scene_items", [])` 这种读法
    （因为 v5 走的是 obsws-python 的 `as_dataclass`）。这里复刻那个契约：
    **只有顶层**转成属性，嵌套的 list/dict 保持原样（控制器内部用 `.get("sceneName")`
    读嵌套项，也必须保持 dict）。另外挂一个 `response_data` 便于诊断窗口显示原始 JSON。
    """
    if payload is None:
        return None

    class _Resp:
        __slots__ = ()

    attrs = {to_snake(key): value for key, value in payload.items()}
    obj = type("V5Resp", (), {})()
    for key, value in attrs.items():
        setattr(obj, key, value)
    obj.response_data = payload
    return obj


# ---------------------------------------------------------------- 小工具
def parse_timecode_ms(text: str) -> int:
    """'HH:MM:SS.mmm' -> 毫秒。解析不出来回 0（v4 的时长字段都长这样）。"""
    if not text:
        return 0
    match = re.match(r"^(\d+):(\d+):(\d+)(?:\.(\d+))?$", str(text).strip())
    if not match:
        return 0
    hours, minutes, seconds, fraction = match.groups()
    millis = int((fraction or "0").ljust(3, "0")[:3])
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + millis


def db_to_mul(db: float) -> float:
    """dB -> 线性增益（v4 的 GetVolume 只给一个值，另一个要自己算）。"""
    if db is None:
        return 1.0
    if db <= -100.0:
        return 0.0
    return 10.0 ** (float(db) / 20.0)


def _strip_data_uri(value: str) -> str:
    """v4 的 TakeSourceScreenshot 回的是完整 data URI，v5 的 imageData 是裸 base64。"""
    text = str(value or "")
    if text.startswith("data:") and "," in text:
        return text.split(",", 1)[1]
    return text


# ---------------------------------------------------------------- 枚举映射
# 监听类型：v4 用短名，v5 用 OBS_MONITORING_TYPE_* 长名
_MONITOR_V4_TO_V5 = {
    "none": "OBS_MONITORING_TYPE_NONE",
    "monitorOnly": "OBS_MONITORING_TYPE_MONITOR_ONLY",
    "monitorAndOutput": "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT",
    "unknown": "",
}
_MONITOR_V5_TO_V4 = {v: k for k, v in _MONITOR_V4_TO_V5.items() if k != "unknown"}

# 媒体状态：v4 短名 -> v5 OBS_MEDIA_STATE_*
_MEDIA_V4_TO_V5 = {
    "none": "OBS_MEDIA_STATE_NONE",
    "playing": "OBS_MEDIA_STATE_PLAYING",
    "opening": "OBS_MEDIA_STATE_OPENING",
    "buffering": "OBS_MEDIA_STATE_BUFFERING",
    "paused": "OBS_MEDIA_STATE_PAUSED",
    "stopped": "OBS_MEDIA_STATE_STOPPED",
    "ended": "OBS_MEDIA_STATE_ENDED",
    "error": "OBS_MEDIA_STATE_ERROR",
    "unknown": "OBS_MEDIA_STATE_NONE",
}

# v5 的 TriggerMediaInputAction 动作枚举 -> v4 的"一动作一条请求"
_MEDIA_ACTION_TO_V4 = {
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY": ("PlayPauseMedia", "play"),
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE": ("PlayPauseMedia", "pause"),
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP": ("StopMedia", None),
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART": ("RestartMedia", None),
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT": ("NextMedia", None),
    "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS": ("PreviousMedia", None),
}

# v5 的输出状态枚举（`RecordStateChanged.outputState` 等）。
# ⚠️ v4 用一组独立事件表达状态，翻译时必须填**这些 v5 枚举值**，
# 因为 controller 里有 `state in P.ACTIVE_OUTPUT_STATES` /
# `state == P.OUTPUT_PAUSED` 这类判断，填短名（"STARTED"）会导致判定失效。
OUT_V5_STARTING = "OBS_WEBSOCKET_OUTPUT_STARTING"
OUT_V5_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OUT_V5_STOPPING = "OBS_WEBSOCKET_OUTPUT_STOPPING"
OUT_V5_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
OUT_V5_RECONNECTING = "OBS_WEBSOCKET_OUTPUT_RECONNECTING"
OUT_V5_PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
OUT_V5_RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"


# ---------------------------------------------------------------- 错误码反推
def error_code(message: str, v5_name: str | None = None) -> int:
    """把 v4 的错误文案反推成 v5 的 `requestStatus.code`。

    这一步是**必须的**：`controller._on_request_failed` 全靠数字码分流
    （204 名字不对→自愈、506 工作室模式→当场纠正、501 没推流→中文提示、
    600~605 资源不可用→只记日志不弹框）。v4 只回字符串，不反推的话
    这些分支永远进不去，界面会退化成"到处弹英文错误框"。

    返回 0 表示"不知道该归哪类"，上层按通用失败处理。
    """
    text = (message or "").strip()
    lower = text.lower()

    if lower == "invalid request type":
        return 204
    if lower == "not authenticated":
        # v5 里认证失败是关闭码 4009；这里用同一个数字，别让它落进"资源不可用"被静默吞掉
        return ERR_AUTH_FAILED
    if "studio mode" in lower:
        # "studio mode not enabled" / "studio mode not active" / "studio mode already active"
        return 506
    if lower == "streaming not active" and v5_name == "SendStreamCaption":
        # 只有"发字幕时没推流"才对应 v5 的 501 OutputNotRunning
        return 501
    if "already exists" in lower:
        return 601
    if "does not exist" in lower or "doesn't exist" in lower or "doesnt exist" in lower:
        return 600
    if "invalid sceneitem" in lower:
        return 600
    if "disabled in settings" in lower:
        return 604
    return 0


# ---------------------------------------------------------------- 能力合成
# v5 请求名 -> 它在 v4 上需要的 v4 请求名（全部可用才算支持）。
#
# **不在这张表里的 v5 请求，一律视为 v4 不支持**，于是不会进入
# `StateStore.supported_requests`，界面自动置灰/隐藏。已确认 v4 完全没有的：
#   · GetInputAudioBalance / SetInputAudioBalance —— messageMap 里根本没有（声道平衡）
#   · RemoveScene                              —— v4 无任何删除场景的请求
#   · SetSceneIndex                            —— v5 也没有（协议本身不支持排序）
#   · InputVolumeMeters 事件                    —— v4 无任何电平表能力
#   · ReplayBufferSaved 事件                    —— v4 的 SaveReplayBuffer 不发事件
REQUIRES: dict[str, tuple[str, ...]] = {
    "GetVersion": ("GetVersion",),
    "GetSceneList": ("GetSceneList",),
    "SetCurrentProgramScene": ("SetCurrentScene",),
    # 优先用 4.9.0 的 GetSceneItemList；老版本在解析器里退回 GetSceneList
    "GetSceneItemList": ("GetSceneList",),
    "GetSceneItemEnabled": ("GetSceneItemProperties",),
    "SetSceneItemEnabled": ("SetSceneItemProperties",),
    "CreateScene": ("CreateScene",),
    "SetSceneName": ("SetSourceName",),
    "StartRecord": ("StartRecording",),
    "StopRecord": ("StopRecording",),
    "PauseRecord": ("PauseRecording",),
    "ResumeRecord": ("ResumeRecording",),
    "GetRecordStatus": ("GetRecordingStatus",),
    "GetRecordDirectory": ("GetRecordingFolder",),
    "StartStream": ("StartStreaming",),
    "StopStream": ("StopStreaming",),
    "GetStreamStatus": ("GetStreamingStatus",),
    "SendStreamCaption": ("SendCaptions",),
    "GetStats": ("GetStats",),
    "GetVideoSettings": ("GetVideoInfo",),
    "GetSourceScreenshot": ("TakeSourceScreenshot",),
    "StartReplayBuffer": ("StartReplayBuffer",),
    "StopReplayBuffer": ("StopReplayBuffer",),
    "GetReplayBufferStatus": ("GetReplayBufferStatus",),
    "SaveReplayBuffer": ("SaveReplayBuffer",),
    "StartVirtualcam": ("StartVirtualCam",),
    "StopVirtualcam": ("StopVirtualCam",),
    "GetVirtualcamStatus": ("GetVirtualCamStatus",),
    "GetStudioModeEnabled": ("GetStudioModeStatus",),
    "SetStudioModeEnabled": ("EnableStudioMode", "DisableStudioMode"),
    "SetCurrentPreviewScene": ("SetPreviewScene",),
    "TriggerStudioModeTransition": ("TransitionToProgram",),
    "GetSceneTransitionList": ("GetTransitionList",),
    "GetCurrentSceneTransition": ("GetCurrentTransition",),
    "SetCurrentSceneTransition": ("SetCurrentTransition",),
    "SetCurrentSceneTransitionDuration": ("SetTransitionDuration",),
    "SetTBarPosition": ("SetTBarPosition",),
    "GetInputList": ("GetSourcesList",),
    "GetInputVolume": ("GetVolume",),
    "SetInputVolume": ("SetVolume",),
    "GetInputMute": ("GetMute",),
    "SetInputMute": ("SetMute",),
    "ToggleInputMute": ("ToggleMute",),
    "GetInputAudioMonitorType": ("GetAudioMonitorType",),
    "SetInputAudioMonitorType": ("SetAudioMonitorType",),
    "GetInputAudioSyncOffset": ("GetSyncOffset",),
    "SetInputAudioSyncOffset": ("SetSyncOffset",),
    "GetInputAudioTracks": ("GetTracks",),
    "SetInputAudioTracks": ("SetTracks",),
    "GetMediaInputStatus": ("GetMediaState", "GetMediaDuration", "GetMediaTime"),
    "TriggerMediaInputAction": ("PlayPauseMedia",),
    "SetMediaInputCursor": ("SetMediaTime",),
    "OffsetMediaInputCursor": ("ScrubMedia",),
    "GetSceneCollectionList": ("ListSceneCollections", "GetCurrentSceneCollection"),
    "SetCurrentSceneCollection": ("SetCurrentSceneCollection",),
    "GetProfileList": ("ListProfiles", "GetCurrentProfile"),
    "SetCurrentProfile": ("SetCurrentProfile",),
}

# v4 里没有、但界面需要知道"用不了"的 v5 名字。列出来是为了让界面能给出
# **具体原因**（而不是笼统的"服务端不支持"）。
V4_ABSENT_REQUESTS: tuple[str, ...] = (
    "GetInputAudioBalance",
    "SetInputAudioBalance",
    "RemoveScene",
    "SetSceneIndex",
)


def synthesize_capabilities(v4_requests) -> set[str]:
    """v4 的 `available-requests`（一坨请求名）-> v5 请求名集合。

    这是整个兼容层的枢纽：`StateStore.supports()` 认得是 v5 名字，
    于是所有 UI 置灰逻辑不用改一行。
    """
    available = set(v4_requests or ())
    return {
        v5_name
        for v5_name, needed in REQUIRES.items()
        if needed and all(item in available for item in needed)
    }


# ---------------------------------------------------------------- 请求解析器
# 每个解析器收到 (call, params)：
#   call(v4请求名, 字段) -> v4 应答 dict；请求名不存在时抛 V4Unsupported；
#   status 为 error 时抛 V4Error。返回 **v5 形状的 dict**（或 None 表示无返回数据）。
#
# 之所以用"解析器函数"而不是静态映射表：有些 v5 请求在 v4 上要拆成多条
# （GetMediaInputStatus 要 3 条、SetInputAudioTracks 最多 6 条），还有些要
# 看前一条的结果再决定下一条（GetSceneList 要先问工作室模式才谈得上预览场景）。
CallFn = Callable[[str, dict | None], dict]


def _resolve_get_version(call: CallFn, params: dict) -> dict:
    """GetVersion 在 v4 上同样存在，但**形状完全不同**（见 normalize_version）。

    这条路径必须支持：控制器的心跳（A11 连接健康）走的就是普通请求通道发
    `GetVersion`，不是只在握手时用一次。少了它，v4 会话里心跳会被判成 204
    "请求名不对"，于是 RTT 永远没样本、还被误标为"服务端不支持"。
    """
    data = call("GetVersion", None) or {}
    raw = str(data.get("available-requests", "") or "")
    v4_requests = {item.strip() for item in raw.split(",") if item.strip()}
    return {
        "obsVersion": str(data.get("obs-studio-version", "") or ""),
        "obsWebSocketVersion": str(data.get("obs-websocket-version", "") or ""),
        "rpcVersion": 1,
        # 这里回的是**合成的 v5 名字**，与握手路径口径一致
        "availableRequests": sorted(synthesize_capabilities(v4_requests)),
    }


def _resolve_get_scene_list(call: CallFn, params: dict) -> dict:
    data = call("GetSceneList", None)
    scenes = []
    raw = data.get("scenes") or []
    total = len(raw)
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        # v4 的 scenes 是"界面上从上到下"（源码里 insert(0) 构造，与自然枚举相反）。
        # 而 v5 的 sceneIndex 语义是"0 在最底层"，控制器拿到后
        # sort(升序) 再 reverse() 得到展示顺序。所以这里反向编号，
        # 让 排序+翻转 之后正好还原成 v4 给的展示顺序。
        scenes.append(
            {
                "sceneName": str(item.get("name", "")),
                "sceneIndex": max(total - 1 - position, 0),
            }
        )

    result = {
        "currentProgramSceneName": str(data.get("current-scene", "") or ""),
        "scenes": scenes,
    }
    # v5 的 GetSceneList 顺带返回预览场景；v4 要单独问，且只认工作室模式开启时
    try:
        studio = bool((call("GetStudioModeStatus", None) or {}).get("studio-mode"))
        if studio:
            preview = call("GetPreviewScene", None) or {}
            name = str(preview.get("name", "") or "")
            if name:
                result["currentPreviewSceneName"] = name
    except (V4Unsupported, V4Error):
        # 工作室模式相关请求缺失或被拒都不该影响场景列表本身
        pass
    return result


def _resolve_set_current_program_scene(call: CallFn, params: dict) -> None:
    call("SetCurrentScene", {"scene-name": params.get("sceneName", "")})


def _resolve_get_scene_item_list(call: CallFn, params: dict) -> dict:
    scene = params.get("sceneName") or ""
    try:
        # 4.9.0+ 专用请求：顺序与 v5 一致（自然 z 序，底 -> 顶），且带 sourceKind
        data = call("GetSceneItemList", {"sceneName": scene} if scene else None)
        items = [
            {
                "sceneItemId": int(item.get("itemId", -1)),
                "sourceName": str(item.get("sourceName", "")),
                "sourceType": _v4_source_type(item.get("sourceType")),
                "sourceKind": str(item.get("sourceKind", "") or ""),
            }
            for item in (data.get("sceneItems") or [])
            if isinstance(item, dict)
        ]
        return {"sceneItems": items}
    except V4Unsupported:
        pass

    # 老 v4 退回 GetSceneList：scene 的 sources 是**反序**的（顶 -> 底），
    # 必须翻回来才符合 v5 的 z 序语义，否则界面来源列表会上下颠倒。
    data = call("GetSceneList", None)
    scene_name = scene or str(data.get("current-scene", "") or "")
    for entry in data.get("scenes") or []:
        if isinstance(entry, dict) and str(entry.get("name", "")) == scene_name:
            items = [
                {
                    "sceneItemId": int(item.get("id", -1)),
                    "sourceName": str(item.get("name", "")),
                    # v4 在 GetSceneList 的 SceneItem 里把 `type` 塞成了 **kind**
                    "sourceType": "OBS_SOURCE_TYPE_UNKNOWN",
                    "sourceKind": str(item.get("type", "") or ""),
                }
                for item in (entry.get("sources") or [])
                if isinstance(item, dict)
                and int(item.get("id", -1)) >= 0
                and item.get("name")
            ]
            items.reverse()
            return {"sceneItems": items}
    return {"sceneItems": []}


def _v4_source_type(value) -> str:
    """v4 的 sourceType（input/scene/unknown）-> v5 的 OBS_SOURCE_TYPE_* 常量。"""
    text = str(value or "").lower()
    if text == "input":
        return "OBS_SOURCE_TYPE_INPUT"
    if text == "scene":
        return "OBS_SOURCE_TYPE_SCENE"
    return "OBS_SOURCE_TYPE_UNKNOWN"


def _resolve_get_scene_item_enabled(call: CallFn, params: dict) -> dict:
    data = call(
        "GetSceneItemProperties",
        {
            "scene-name": params.get("sceneName", ""),
            "item": {"id": int(params.get("sceneItemId", -1))},
        },
    )
    return {"sceneItemEnabled": bool(data.get("visible", False))}


def _resolve_set_scene_item_enabled(call: CallFn, params: dict) -> None:
    # 用 4.3.0 的 SetSceneItemProperties（官方推荐，粒度细、未废弃）而不是
    # 0.3 的 SetSceneItemRender。注意字段是 `visible`，而 SetSceneItemRender
    # 用的是 `render` —— 两者是同一个底层值，只是键名不同（传错键会吃 missing parameters）。
    call(
        "SetSceneItemProperties",
        {
            "scene-name": params.get("sceneName", ""),
            "item": {"id": int(params.get("sceneItemId", -1))},
            "visible": bool(params.get("sceneItemEnabled", False)),
        },
    )


def _resolve_create_scene(call: CallFn, params: dict) -> None:
    call("CreateScene", {"sceneName": params.get("sceneName", "")})


def _resolve_set_scene_name(call: CallFn, params: dict) -> None:
    # v4 没有 SetSceneName；场景在 libobs 里也是 source，所以走 SetSourceName
    call(
        "SetSourceName",
        {
            "sourceName": params.get("sceneName", ""),
            "newName": params.get("newSceneName", ""),
        },
    )


def _resolve_get_record_status(call: CallFn, params: dict) -> dict:
    data = call("GetRecordingStatus", None)
    return {
        "outputActive": bool(data.get("isRecording", False)),
        "outputPaused": bool(data.get("isRecordingPaused", False)),
        "outputTimecode": str(data.get("recordTimecode", "") or ""),
        # v4 没有 outputDuration/outputBytes，只能拿时间码反算
        "outputDuration": parse_timecode_ms(data.get("recordTimecode", "")),
        "outputBytes": 0,
        "outputPath": str(data.get("recordingFilename", "") or ""),
    }


def _resolve_get_stream_status(call: CallFn, params: dict) -> dict:
    data = call("GetStreamingStatus", None)
    timecode = str(data.get("stream-timecode", "") or "")
    return {
        "outputActive": bool(data.get("streaming", False)),
        # v4 根本没有"重连中"这个概念，恒定 False
        "outputReconnecting": False,
        "outputTimecode": timecode,
        "outputDuration": parse_timecode_ms(timecode),
        "outputCongestion": 0.0,
        "outputBytes": 0,
        "outputSkippedFrames": 0,
        "outputTotalFrames": 0,
    }


def _resolve_get_stats(call: CallFn, params: dict) -> dict:
    data = call("GetStats", None) or {}
    # v4 把统计塞在 `stats` 子对象里，v5 是平铺的
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else data
    return {
        "cpuUsage": float(stats.get("cpu-usage", 0) or 0),
        "memoryUsage": float(stats.get("memory-usage", 0) or 0),
        "availableDiskSpace": float(stats.get("free-disk-space", 0) or 0),
        "activeFps": float(stats.get("fps", 0) or 0),
        "averageFrameRenderTime": float(stats.get("average-frame-time", 0) or 0),
        "renderSkippedFrames": int(stats.get("render-missed-frames", 0) or 0),
        "renderTotalFrames": int(stats.get("render-total-frames", 0) or 0),
        "outputSkippedFrames": int(stats.get("output-skipped-frames", 0) or 0),
        "outputTotalFrames": int(stats.get("output-total-frames", 0) or 0),
    }


def _resolve_get_video_settings(call: CallFn, params: dict) -> dict:
    data = call("GetVideoInfo", None) or {}
    fps = float(data.get("fps", 0) or 0)
    # v5 返回分子/分母，控制器用它算 fps；v4 直接给 double，这里等价换算一下
    return {
        "baseWidth": int(data.get("baseWidth", 0) or 0),
        "baseHeight": int(data.get("baseHeight", 0) or 0),
        "outputWidth": int(data.get("outputWidth", 0) or 0),
        "outputHeight": int(data.get("outputHeight", 0) or 0),
        "fpsNumerator": int(round(fps * 1000)),
        "fpsDenominator": 1000,
        "fpsInteger": int(round(fps)),
    }


def _resolve_get_record_directory(call: CallFn, params: dict) -> dict:
    data = call("GetRecordingFolder", None) or {}
    return {"recordDirectory": str(data.get("rec-folder", "") or "")}


def _resolve_get_source_screenshot(call: CallFn, params: dict) -> dict:
    fields: dict[str, Any] = {}
    if params.get("sourceName"):
        fields["sourceName"] = params["sourceName"]
    fields["embedPictureFormat"] = str(params.get("imageFormat") or "jpeg")
    # 字段名不同：v5 是 imageWidth/imageHeight，v4 是 width/height
    if params.get("imageWidth"):
        fields["width"] = int(params["imageWidth"])
    if params.get("imageHeight"):
        fields["height"] = int(params["imageHeight"])
    quality = params.get("imageCompressionQuality")
    if quality is not None:
        fields["compressionQuality"] = int(quality)
    data = call("TakeSourceScreenshot", fields) or {}
    # v4 回的是完整 data URI，v5 只回裸 base64
    return {"imageData": _strip_data_uri(data.get("img", ""))}


def _resolve_send_stream_caption(call: CallFn, params: dict) -> None:
    # v4 的字段叫 text（v5 是 captionText）。
    # 语义差异：v4 在没推流时**静默成功**（output 为空就什么都不做），
    # 不像 v5 会回 501；所以"没推流不许发"只能由客户端本地先拦（见 controller.stream_caption_blocker）。
    call("SendCaptions", {"text": params.get("captionText", "")})


def _resolve_get_replay_buffer_status(call: CallFn, params: dict) -> dict:
    data = call("GetReplayBufferStatus", None) or {}
    return {"outputActive": bool(data.get("isReplayBufferActive", False))}


def _resolve_get_virtualcam_status(call: CallFn, params: dict) -> dict:
    data = call("GetVirtualCamStatus", None) or {}
    return {"outputActive": bool(data.get("isVirtualCam", False))}


def _resolve_get_studio_mode(call: CallFn, params: dict) -> dict:
    data = call("GetStudioModeStatus", None) or {}
    return {"studioModeEnabled": bool(data.get("studio-mode", False))}


def _resolve_set_studio_mode(call: CallFn, params: dict) -> None:
    # v4 没有 SetStudioModeEnabled：按目标布尔值分派到 Enable/Disable
    if params.get("studioModeEnabled"):
        call("EnableStudioMode", None)
    else:
        call("DisableStudioMode", None)


def _resolve_set_preview_scene(call: CallFn, params: dict) -> None:
    call("SetPreviewScene", {"scene-name": params.get("sceneName", "")})


def _resolve_trigger_transition(call: CallFn, params: dict) -> None:
    call("TransitionToProgram", None)


def _resolve_get_scene_transition_list(call: CallFn, params: dict) -> dict:
    data = call("GetTransitionList", None) or {}
    transitions = [
        {"transitionName": str(item.get("name", ""))}
        for item in (data.get("transitions") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "currentSceneTransitionName": str(data.get("current-transition", "") or ""),
        "transitions": transitions,
    }


def _resolve_get_current_transition(call: CallFn, params: dict) -> dict:
    data = call("GetCurrentTransition", None) or {}
    result: dict[str, Any] = {
        "transitionName": str(data.get("name", "") or ""),
    }
    # v4 只在"该转场可配时长"时才给 duration；由此反推 v5 的 configurable
    if isinstance(data.get("duration"), (int, float)):
        result["transitionDuration"] = int(data["duration"])
        result["transitionConfigurable"] = True
    else:
        result["transitionConfigurable"] = False
    return result


def _resolve_set_current_transition(call: CallFn, params: dict) -> None:
    call("SetCurrentTransition", {"transition-name": params.get("transitionName", "")})


def _resolve_set_transition_duration(call: CallFn, params: dict) -> None:
    call("SetTransitionDuration", {"duration": int(params.get("transitionDuration", 0))})


def _resolve_set_tbar_position(call: CallFn, params: dict) -> None:
    fields: dict[str, Any] = {"position": float(params.get("position", 0.0))}
    if "release" in params:
        fields["release"] = bool(params["release"])
    call("SetTBarPosition", fields)


# ---- 音频（字段名在 v4 里格外不统一：source / sourceName 混用，mute / muted 混用）
def _resolve_get_input_list(call: CallFn, params: dict) -> dict:
    data = call("GetSourcesList", None) or {}
    inputs = [
        {
            "inputName": str(item.get("name", "")),
            "inputKind": str(item.get("typeId", "") or ""),
            "unversionedInputKind": str(item.get("typeId", "") or ""),
        }
        for item in (data.get("sources") or [])
        if isinstance(item, dict)
        and item.get("name")
        # v4 的 GetSourcesList 把场景/滤镜/转场**一并**返回，只留 type=="input"
        and str(item.get("type", "")).lower() == "input"
    ]
    return {"inputs": inputs}


def _resolve_get_input_volume(call: CallFn, params: dict) -> dict:
    data = call(
        "GetVolume",
        {"source": params.get("inputName", ""), "useDecibel": True},
    ) or {}
    db = float(data.get("volume", 0.0) or 0.0)
    return {"inputVolumeMul": db_to_mul(db), "inputVolumeDb": db}


def _resolve_set_input_volume(call: CallFn, params: dict) -> None:
    # 控制器只发 dB（见 controller.set_input_volume），所以固定 useDecibel=True
    if "inputVolumeDb" in params:
        call(
            "SetVolume",
            {
                "source": params.get("inputName", ""),
                "volume": float(params["inputVolumeDb"]),
                "useDecibel": True,
            },
        )
        return
    call(
        "SetVolume",
        {"source": params.get("inputName", ""), "volume": float(params.get("inputVolumeMul", 1.0))},
    )


def _resolve_get_input_mute(call: CallFn, params: dict) -> dict:
    data = call("GetMute", {"source": params.get("inputName", "")}) or {}
    return {"inputMuted": bool(data.get("muted", False))}


def _resolve_set_input_mute(call: CallFn, params: dict) -> None:
    # 注意 v4 的键是 `mute`，而 getter 返回的是 `muted`
    call(
        "SetMute",
        {"source": params.get("inputName", ""), "mute": bool(params.get("inputMuted", False))},
    )


def _resolve_toggle_input_mute(call: CallFn, params: dict) -> None:
    call("ToggleMute", {"source": params.get("inputName", "")})


def _resolve_get_monitor_type(call: CallFn, params: dict) -> dict:
    data = call("GetAudioMonitorType", {"sourceName": params.get("inputName", "")}) or {}
    short = str(data.get("monitorType", "") or "")
    return {"monitorType": _MONITOR_V4_TO_V5.get(short, short)}


def _resolve_set_monitor_type(call: CallFn, params: dict) -> None:
    wanted = str(params.get("monitorType", "") or "")
    call(
        "SetAudioMonitorType",
        {
            "sourceName": params.get("inputName", ""),
            # 界面用 v5 的长枚举，v4 只认短枚举
            "monitorType": _MONITOR_V5_TO_V4.get(wanted, wanted),
        },
    )


def _resolve_get_sync_offset(call: CallFn, params: dict) -> dict:
    data = call("GetSyncOffset", {"source": params.get("inputName", "")}) or {}
    nanos = int(data.get("offset", 0) or 0)
    # ⚠️ 单位差 100 万倍：v4 是纳秒，v5 是毫秒。不换算就是静默的错误值。
    return {"inputAudioSyncOffset": int(round(nanos / 1_000_000))}


def _resolve_set_sync_offset(call: CallFn, params: dict) -> None:
    millis = int(params.get("inputAudioSyncOffset", 0) or 0)
    call(
        "SetSyncOffset",
        {"source": params.get("inputName", ""), "offset": millis * 1_000_000},
    )


def _resolve_get_audio_tracks(call: CallFn, params: dict) -> dict:
    data = call("GetTracks", {"sourceName": params.get("inputName", "")}) or {}
    # v4 回 track1..track6 六个平铺布尔，v5 回 {"1": bool, ...}
    return {
        "inputAudioTracks": {
            str(index): bool(data.get(f"track{index}", False)) for index in range(1, 7)
        }
    }


def _resolve_set_audio_tracks(call: CallFn, params: dict) -> None:
    tracks = params.get("inputAudioTracks") or {}
    name = params.get("inputName", "")
    # v4 的 SetTracks 一次只改一条轨道，所以 v5 的位掩码要拆成最多 6 条请求
    for index in range(1, 7):
        wanted = tracks.get(str(index), tracks.get(index))
        call(
            "SetTracks",
            {
                "sourceName": name,
                "track": index,
                "active": bool(wanted) if wanted is not None else False,
            },
        )


# ---- 媒体源（v4 是"一动作一条请求"，v5 是一条请求带动作枚举）
def _resolve_get_media_input_status(call: CallFn, params: dict) -> dict:
    name = params.get("inputName", "")
    state = (call("GetMediaState", {"sourceName": name}) or {}).get("mediaState", "")
    duration = (call("GetMediaDuration", {"sourceName": name}) or {}).get("mediaDuration", 0)
    cursor = (call("GetMediaTime", {"sourceName": name}) or {}).get("timestamp", 0)
    return {
        "mediaState": _MEDIA_V4_TO_V5.get(str(state or ""), str(state or "")),
        "mediaDuration": int(duration or 0),
        "mediaCursor": int(cursor or 0),
    }


def _resolve_trigger_media_action(call: CallFn, params: dict) -> None:
    action = str(params.get("mediaAction", "") or "")
    mapped = _MEDIA_ACTION_TO_V4.get(action)
    if mapped is None:
        raise V4Error(f"unknown media action: {action}")
    v4_request, behavior = mapped
    fields: dict[str, Any] = {"sourceName": params.get("inputName", "")}
    if behavior == "play":
        # v4 的 PlayPauseMedia 省略 playPause 时会**取反**（读-改-写），有竞态；
        # 必须显式给布尔值才是确定的
        fields["playPause"] = False
    elif behavior == "pause":
        fields["playPause"] = True
    call(v4_request, fields)


def _resolve_set_media_cursor(call: CallFn, params: dict) -> None:
    call(
        "SetMediaTime",
        {
            "sourceName": params.get("inputName", ""),
            "timestamp": int(max(0, int(params.get("mediaCursor", 0) or 0))),
        },
    )


def _resolve_offset_media_cursor(call: CallFn, params: dict) -> None:
    call(
        "ScrubMedia",
        {
            "sourceName": params.get("inputName", ""),
            "timeOffset": int(params.get("mediaCursorOffset", 0) or 0),
        },
    )


# ---- 场景集合 / 配置文件（v4 把"列表"和"当前项"拆成两条请求）
def _resolve_get_scene_collection_list(call: CallFn, params: dict) -> dict:
    listing = call("ListSceneCollections", None) or {}
    current = call("GetCurrentSceneCollection", None) or {}
    names = [
        str(item.get("sc-name", ""))
        for item in (listing.get("scene-collections") or [])
        if isinstance(item, dict) and item.get("sc-name")
    ]
    return {
        "currentSceneCollectionName": str(current.get("sc-name", "") or ""),
        "sceneCollections": names,
    }


def _resolve_set_scene_collection(call: CallFn, params: dict) -> None:
    call("SetCurrentSceneCollection", {"sc-name": params.get("sceneCollectionName", "")})


def _resolve_get_profile_list(call: CallFn, params: dict) -> dict:
    listing = call("ListProfiles", None) or {}
    current = call("GetCurrentProfile", None) or {}
    names = [
        str(item.get("profile-name", ""))
        for item in (listing.get("profiles") or [])
        if isinstance(item, dict) and item.get("profile-name")
    ]
    return {
        "currentProfileName": str(current.get("profile-name", "") or ""),
        "profiles": names,
    }


def _resolve_set_profile(call: CallFn, params: dict) -> None:
    call("SetCurrentProfile", {"profile-name": params.get("profileName", "")})


def _noop(call: CallFn, params: dict) -> None:
    """无返回数据的请求：字段翻译在 _SIMPLE_CALLS 里，这里只负责调用。"""


# ---------------------------------------------------------------- 请求表
# 无返回数据的"简单改名 + 字段改名"请求：v5 名 -> (v4 名, {v5字段: v4字段})
_SIMPLE_CALLS: dict[str, tuple[str, dict[str, str]]] = {
    "StartRecord": ("StartRecording", {}),
    "StopRecord": ("StopRecording", {}),
    "PauseRecord": ("PauseRecording", {}),
    "ResumeRecord": ("ResumeRecording", {}),
    "StartStream": ("StartStreaming", {}),
    "StopStream": ("StopStreaming", {}),
    "StartReplayBuffer": ("StartReplayBuffer", {}),
    "StopReplayBuffer": ("StopReplayBuffer", {}),
    "SaveReplayBuffer": ("SaveReplayBuffer", {}),
    "StartVirtualcam": ("StartVirtualCam", {}),
    "StopVirtualcam": ("StopVirtualCam", {}),
}

RESOLVERS: dict[str, CallFn] = {
    "GetVersion": _resolve_get_version,
    "GetSceneList": _resolve_get_scene_list,
    "SetCurrentProgramScene": _resolve_set_current_program_scene,
    "GetSceneItemList": _resolve_get_scene_item_list,
    "GetSceneItemEnabled": _resolve_get_scene_item_enabled,
    "SetSceneItemEnabled": _resolve_set_scene_item_enabled,
    "CreateScene": _resolve_create_scene,
    "SetSceneName": _resolve_set_scene_name,
    "GetRecordStatus": _resolve_get_record_status,
    "GetStreamStatus": _resolve_get_stream_status,
    "GetStats": _resolve_get_stats,
    "GetVideoSettings": _resolve_get_video_settings,
    "GetRecordDirectory": _resolve_get_record_directory,
    "GetSourceScreenshot": _resolve_get_source_screenshot,
    "SendStreamCaption": _resolve_send_stream_caption,
    "GetReplayBufferStatus": _resolve_get_replay_buffer_status,
    "GetVirtualcamStatus": _resolve_get_virtualcam_status,
    "GetStudioModeEnabled": _resolve_get_studio_mode,
    "SetStudioModeEnabled": _resolve_set_studio_mode,
    "SetCurrentPreviewScene": _resolve_set_preview_scene,
    "TriggerStudioModeTransition": _resolve_trigger_transition,
    "GetSceneTransitionList": _resolve_get_scene_transition_list,
    "GetCurrentSceneTransition": _resolve_get_current_transition,
    "SetCurrentSceneTransition": _resolve_set_current_transition,
    "SetCurrentSceneTransitionDuration": _resolve_set_transition_duration,
    "SetTBarPosition": _resolve_set_tbar_position,
    "GetInputList": _resolve_get_input_list,
    "GetInputVolume": _resolve_get_input_volume,
    "SetInputVolume": _resolve_set_input_volume,
    "GetInputMute": _resolve_get_input_mute,
    "SetInputMute": _resolve_set_input_mute,
    "ToggleInputMute": _resolve_toggle_input_mute,
    "GetInputAudioMonitorType": _resolve_get_monitor_type,
    "SetInputAudioMonitorType": _resolve_set_monitor_type,
    "GetInputAudioSyncOffset": _resolve_get_sync_offset,
    "SetInputAudioSyncOffset": _resolve_set_sync_offset,
    "GetInputAudioTracks": _resolve_get_audio_tracks,
    "SetInputAudioTracks": _resolve_set_audio_tracks,
    "GetMediaInputStatus": _resolve_get_media_input_status,
    "TriggerMediaInputAction": _resolve_trigger_media_action,
    "SetMediaInputCursor": _resolve_set_media_cursor,
    "OffsetMediaInputCursor": _resolve_offset_media_cursor,
    "GetSceneCollectionList": _resolve_get_scene_collection_list,
    "SetCurrentSceneCollection": _resolve_set_scene_collection,
    "GetProfileList": _resolve_get_profile_list,
    "SetCurrentProfile": _resolve_set_profile,
    **{name: _noop for name in _SIMPLE_CALLS},
}

# 有返回数据、但只是"改个名"的请求：v5 名 -> (v4 名, 要回哪些 v4 字段作顶层字段)
_SIMPLE_GETS: dict[str, tuple[str, dict[str, str]]] = {
    # 这几个 v4 的应答形状与 v5 几乎一致，直接取字段即可
    "GetRecordingStatus": ("GetRecordingStatus", {}),
}


def resolve(v5_name: str, params: dict | None, call: CallFn):
    """把一条 v5 请求落到 v4 上执行，返回 **v5 形状的 dict**（无返回数据则 None）。"""
    params = params or {}

    simple = _SIMPLE_CALLS.get(v5_name)
    if simple is not None:
        v4_name, mapping = simple
        fields = {v4_key: params.get(v5_key) for v5_key, v4_key in mapping.items()}
        call(v4_name, {k: v for k, v in fields.items() if v is not None} or None)
        return None

    resolver = RESOLVERS.get(v5_name)
    if resolver is None:
        # 不在表里 = v4 没有这个能力。正常情况下能力探测已经把它挡掉了
        # （不会进 supported_requests），走到这里说明是探测之外的漏网调用。
        raise V4Unsupported(f"{v5_name} 在 obs-websocket v4 上没有对应请求")
    return resolver(call, params)


# ---------------------------------------------------------------- 事件翻译
# v4 的 update-type -> (v5 事件名, 固定附加字段)
#
# 说明：v4 的输出状态是**一组独立事件**（RecordingStarting/Started/…），
# v5 是**单一事件 + outputState 枚举**，所以这里把前者折算成后者，
# 好让 controller 里现成的 `_event_record_state_changed` 直接复用。
_EVENT_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    # 场景 / 来源
    "SwitchScenes": ("CurrentProgramSceneChanged", {}),
    "ScenesChanged": ("SceneListReindexed", {}),
    "SceneItemAdded": ("SceneItemCreated", {}),
    "SceneItemRemoved": ("SceneItemRemoved", {}),
    "SceneItemVisibilityChanged": ("SceneItemEnableStateChanged", {}),
    "SourceOrderChanged": ("SceneItemListReindexed", {}),
    # 输出
    "RecordingStarting": ("RecordStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTING}),
    "RecordingStarted": ("RecordStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTED}),
    "RecordingStopping": ("RecordStateChanged", {"outputActive": True, "outputState": OUT_V5_STOPPING}),
    "RecordingStopped": ("RecordStateChanged", {"outputActive": False, "outputState": OUT_V5_STOPPED}),
    "RecordingPaused": ("RecordStateChanged", {"outputActive": True, "outputState": OUT_V5_PAUSED}),
    "RecordingResumed": ("RecordStateChanged", {"outputActive": True, "outputState": OUT_V5_RESUMED}),
    "StreamStarting": ("StreamStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTING}),
    "StreamStarted": ("StreamStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTED}),
    "StreamStopping": ("StreamStateChanged", {"outputActive": True, "outputState": OUT_V5_STOPPING}),
    "StreamStopped": ("StreamStateChanged", {"outputActive": False, "outputState": OUT_V5_STOPPED}),
    "ReplayStarting": ("ReplayBufferStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTING}),
    "ReplayStarted": ("ReplayBufferStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTED}),
    "ReplayStopping": ("ReplayBufferStateChanged", {"outputActive": True, "outputState": OUT_V5_STOPPING}),
    "ReplayStopped": ("ReplayBufferStateChanged", {"outputActive": False, "outputState": OUT_V5_STOPPED}),
    "VirtualCamStarted": ("VirtualcamStateChanged", {"outputActive": True, "outputState": OUT_V5_STARTED}),
    "VirtualCamStopped": ("VirtualcamStateChanged", {"outputActive": False, "outputState": OUT_V5_STOPPED}),
    # 转场 / 工作室模式
    "SwitchTransition": ("CurrentSceneTransitionChanged", {}),
    "TransitionDurationChanged": ("CurrentSceneTransitionDurationChanged", {}),
    "TransitionBegin": ("SceneTransitionStarted", {}),
    "TransitionEnd": ("SceneTransitionEnded", {}),
    "StudioModeSwitched": ("StudioModeStateChanged", {}),
    "PreviewSceneChanged": ("CurrentPreviewSceneChanged", {}),
    # 音频
    "SourceVolumeChanged": ("InputVolumeChanged", {}),
    "SourceMuteStateChanged": ("InputMuteStateChanged", {}),
    # 媒体
    "MediaStarted": ("MediaInputPlaybackStarted", {}),
    "MediaEnded": ("MediaInputPlaybackEnded", {}),
    # 配置
    "SceneCollectionChanged": ("CurrentSceneCollectionChanged", {}),
    "ProfileChanged": ("CurrentProfileChanged", {}),
}

# v4 的"一动作一事件" -> v5 的单一动作事件（靠 mediaAction 区分）
_MEDIA_ACTION_EVENTS = {
    "MediaPlaying": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY",
    "MediaPaused": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE",
    "MediaRestarted": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART",
    "MediaStopped": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
    "MediaNext": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT",
    "MediaPrevious": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS",
}

# v4 字段名 -> v5 字段名（按事件类型各自的差异处理）
_FIELD_MAP_COMMON = {
    "scene-name": "sceneName",
    "item-id": "sceneItemId",
    "item-visible": "sceneItemEnabled",
    "sourceName": "inputName",
    "transition-name": "transitionName",
    "new-duration": "transitionDuration",
    "new-state": "studioModeEnabled",
    "sceneCollection": "sceneCollectionName",
    "profile": "profileName",
    "name": "transitionName",
    "muted": "inputMuted",
    "volume": "inputVolumeMul",
    "volumeDb": "inputVolumeDb",
    "previousName": "oldInputName",
    "newName": "inputName",
}


def normalize_event(v4_type: str, fields: dict) -> tuple[str, dict] | None:
    """v4 事件 -> (v5 事件名, v5 形状的 payload)。映射不出来返回 None（忽略）。

    v4 把**所有**事件推给每条连接（没有订阅位掩码），所以会收到大量本项目
    不关心的事件；不认识的一律安静丢弃，别在 controller 里刷"未处理的事件"。
    """
    fields = fields or {}

    # 媒体动作事件：v5 把它们收成一条 MediaInputActionTriggered + mediaAction
    action = _MEDIA_ACTION_EVENTS.get(v4_type)
    if action:
        return (
            "MediaInputActionTriggered",
            {"inputName": str(fields.get("sourceName", "") or ""), "mediaAction": action},
        )

    # Source* 一族按 sourceType 分流成"场景相关"还是"输入相关"
    if v4_type in ("SourceCreated", "SourceDestroyed", "SourceRenamed"):
        source_type = str(fields.get("sourceType", "") or "").lower()
        name = str(fields.get("sourceName", "") or fields.get("newName", "") or "")
        if source_type == "scene":
            if v4_type == "SourceCreated":
                return "SceneCreated", {"sceneName": name}
            if v4_type == "SourceDestroyed":
                return "SceneRemoved", {"sceneName": name}
            return (
                "SceneNameChanged",
                {
                    "sceneName": str(fields.get("newName", "") or ""),
                    "oldSceneName": str(fields.get("previousName", "") or ""),
                },
            )
        if source_type == "input":
            if v4_type == "SourceCreated":
                return (
                    "InputCreated",
                    {
                        "inputName": name,
                        "inputKind": str(fields.get("sourceKind", "") or ""),
                    },
                )
            if v4_type == "SourceDestroyed":
                return "InputRemoved", {"inputName": name}
            return (
                "InputNameChanged",
                {
                    "inputName": str(fields.get("newName", "") or ""),
                    "oldInputName": str(fields.get("previousName", "") or ""),
                },
            )
        # filter/transition 之类本项目不订阅
        return None

    mapped = _EVENT_MAP.get(v4_type)
    if mapped is None:
        return None
    v5_type, extra = mapped

    payload: dict[str, Any] = dict(extra)
    for key, value in fields.items():
        if key in ("update-type", "stream-timecode", "rec-timecode"):
            continue
        payload[_FIELD_MAP_COMMON.get(key, key)] = value

    # ---- 各事件的字段名/形状差异 ----
    if v4_type == "SceneItemAdded":
        # v5 的 SceneItemCreated 带一个完整的 sceneItem 对象
        payload["sceneItem"] = {
            "sceneItemId": int(fields.get("item-id", -1)),
            "sourceName": str(fields.get("item-name", "") or ""),
        }
        payload.pop("itemName", None)
    elif v4_type == "SceneItemRemoved":
        payload["sceneItemId"] = int(fields.get("item-id", -1))
        payload.pop("itemName", None)
    elif v4_type == "SceneItemVisibilityChanged":
        payload["sceneItemEnabled"] = bool(fields.get("item-visible", False))
    elif v4_type == "SourceOrderChanged":
        # v4 的 scene-items 里每项是 {source-name, item-id}
        payload["sceneItems"] = [
            {"sceneItemId": int(item.get("item-id", -1))}
            for item in (fields.get("scene-items") or [])
            if isinstance(item, dict)
        ]
        payload.pop("sceneItems_raw", None)
    elif v4_type == "MediaStarted":
        payload["inputName"] = str(fields.get("sourceName", "") or "")
    elif v4_type == "MediaEnded":
        payload["inputName"] = str(fields.get("sourceName", "") or "")

    return v5_type, payload


# 只有这些 v5 事件本项目有处理器；其余映射了也没用（controller 会记 debug）
def event_is_relevant(v5_type: str) -> bool:
    return v5_type in _RELEVANT_V5_EVENTS


_RELEVANT_V5_EVENTS = frozenset(
    {
        "CurrentProgramSceneChanged",
        "SceneCreated",
        "SceneRemoved",
        "SceneNameChanged",
        "SceneItemCreated",
        "SceneItemRemoved",
        "SceneItemEnableStateChanged",
        "SceneItemListReindexed",
        "SceneListReindexed",
        "RecordStateChanged",
        "StreamStateChanged",
        "ReplayBufferStateChanged",
        "VirtualcamStateChanged",
        "CurrentPreviewSceneChanged",
        "CurrentSceneTransitionChanged",
        "CurrentSceneTransitionDurationChanged",
        "SceneTransitionStarted",
        "SceneTransitionEnded",
        "StudioModeStateChanged",
        "InputCreated",
        "InputRemoved",
        "InputNameChanged",
        "InputVolumeChanged",
        "InputMuteStateChanged",
        "MediaInputPlaybackStarted",
        "MediaInputPlaybackEnded",
        "MediaInputActionTriggered",
        "CurrentSceneCollectionChanged",
        "CurrentProfileChanged",
    }
)


def normalize_version(fields: dict, capabilities: set[str]) -> dict:
    """v4 的 GetVersion 应答 -> 本项目内部用的"已归一化版本信息"。

    `available_requests` 特意放**合成后的 v5 名字**：worker 会把它直接交给
    `controller._on_connected` → `store.set_capabilities()`，于是能力探测
    与全部界面显隐逻辑对 v4/v5 一视同仁。
    """
    fields = fields or {}
    raw = str(fields.get("available-requests", "") or "")
    v4_requests = {item.strip() for item in raw.split(",") if item.strip()}
    return {
        "protocol": MODE_COMPAT,
        "obs_version": str(fields.get("obs-studio-version", "") or ""),
        "websocket_version": str(fields.get("obs-websocket-version", "") or ""),
        "rpc_version": int(fields.get("version", 0) or 0),
        "available_requests": sorted(capabilities),
        "v4_requests": sorted(v4_requests),
    }

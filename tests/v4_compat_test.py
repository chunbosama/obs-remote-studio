"""obs-websocket v4 兼容回归测试。

分两段：
- **[A] 纯逻辑**：`protocol_v4` 的名字/字段/错误码/事件翻译（不依赖 Qt、不联网）。
- **[B] 端到端**：用 `fake_obs_v4_server.py` 起一个**忠实模拟 v4 的服务器**，
  让真正的 `Controller` 连上去跑一遍主链路，验证：
  协议探测、模式标识（标题/状态栏）、场景与来源顺序、
  音频字段与单位换算、以及 **v4 没有的能力在界面上被隐藏或置灰**。

运行：python tests/v4_compat_test.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试必须与真实配置隔离（理由见 smoke_test.py）
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fake_obs_v4_server import DEFAULT_PASSWORD, FakeObsV4Server  # noqa: E402

from obs_remote_studio.core import protocol as P  # noqa: E402
from obs_remote_studio.core import protocol_v4 as P4  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig, ServerInfo  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED  # noqa: E402

CHECKS: list[str] = []
FAILED: list[str] = []
SLOT_ERRORS: list[str] = []


def _slot_exception_hook(kind, value, traceback_) -> None:
    """Qt 会吞掉槽函数里的异常，必须显式接住（否则"坏了但全绿"）。"""
    import traceback as _traceback

    text = "".join(_traceback.format_exception(kind, value, traceback_)).strip()
    SLOT_ERRORS.append(text)
    print(text, file=sys.stderr)


sys.excepthook = _slot_exception_hook


def check(name: str, condition: bool, extra: str = "") -> None:
    if condition:
        CHECKS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {extra}".strip())
        print(f"  [FAIL] {name} {extra}")


def wait_until(predicate, timeout: float = 10.0, app: QApplication | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.02)
    return predicate()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ==================================================================== [A] 纯逻辑
def logic_tests() -> None:
    print("[A1] 模式标识")
    check("v5 -> 标准模式", P4.mode_label(P4.MODE_STANDARD) == "标准模式")
    check("v4 -> 兼容模式", P4.mode_label(P4.MODE_COMPAT) == "兼容模式")
    check("ServerInfo 默认标准模式", ServerInfo().mode_label() == "标准模式")
    check("ServerInfo(protocol=v4) -> 兼容模式",
          ServerInfo(protocol="v4").mode_label() == "兼容模式")

    print("\n[A2] v4 错误文案 -> v5 数字码")
    # 这套反推是必须的：controller._on_request_failed 全靠数字码分流
    check("invalid request type -> 204", P4.error_code("invalid request type") == 204)
    check("Not Authenticated -> 4009", P4.error_code("Not Authenticated") == 4009)
    check("studio mode not enabled -> 506", P4.error_code("studio mode not enabled") == 506)
    check("studio mode not active -> 506", P4.error_code("studio mode not active") == 506)
    check("已存在 -> 601", P4.error_code("scene with this name already exists") == 601)
    check("不存在 -> 600", P4.error_code("requested scene does not exist") == 600)
    check("doesn't exist 也认 -> 600",
          P4.error_code("requested scene doesn't exist") == 600)
    check("replay buffer disabled -> 604",
          P4.error_code("replay buffer disabled in settings") == 604)
    check("没推流发字幕 -> 501",
          P4.error_code("streaming not active", "SendStreamCaption") == 501)
    check("没推流但非字幕 -> 0（不误判）",
          P4.error_code("streaming not active", "StartStream") == 0)
    check("认不出来 -> 0", P4.error_code("something odd") == 0)

    print("\n[A3] 能力合成：v4 没有的东西不能进能力表")
    # v4.9.1 的完整能力（含 GetSceneItemList），用它当"最全的 v4"
    full = {
        "GetVersion", "GetAuthRequired", "GetSceneList", "GetCurrentScene",
        "GetPreviewScene", "SetCurrentScene", "CreateScene", "SetSourceName",
        "GetSceneItemList", "GetSceneItemProperties", "SetSceneItemProperties",
        "ReorganizeSceneItems", "GetRecordingStatus", "StartRecording",
        "StopRecording", "PauseRecording", "ResumeRecording", "GetRecordingFolder",
        "GetStreamingStatus", "StartStreaming", "StopStreaming", "SendCaptions",
        "GetStats", "GetVideoInfo", "TakeSourceScreenshot",
        "GetReplayBufferStatus", "StartReplayBuffer", "StopReplayBuffer",
        "SaveReplayBuffer", "GetVirtualCamStatus", "StartVirtualCam",
        "StopVirtualCam", "GetStudioModeStatus", "EnableStudioMode",
        "DisableStudioMode", "SetPreviewScene", "TransitionToProgram",
        "GetTransitionList", "GetCurrentTransition", "SetCurrentTransition",
        "SetTransitionDuration", "GetSourcesList", "GetVolume", "SetVolume",
        "GetMute", "SetMute", "ToggleMute", "GetAudioMonitorType",
        "SetAudioMonitorType", "GetSyncOffset", "SetSyncOffset", "GetTracks",
        "SetTracks", "GetMediaState", "GetMediaDuration", "GetMediaTime",
        "SetMediaTime", "ScrubMedia", "PlayPauseMedia", "StopMedia",
        "RestartMedia", "NextMedia", "PreviousMedia", "ListSceneCollections",
        "GetCurrentSceneCollection", "SetCurrentSceneCollection", "ListProfiles",
        "GetCurrentProfile", "SetCurrentProfile", "SetTBarPosition",
    }
    caps = P4.synthesize_capabilities(full)
    check("能合成出 v5 名字（GetSceneList）", "GetSceneList" in caps)
    check("能合成出 GetSceneItemList", "GetSceneItemList" in caps)
    check("能合成出 GetInputList（v4 的 GetSourcesList 改名）",
          "GetInputList" in caps)
    check("能合成出 GetRecordStatus", "GetRecordStatus" in caps)
    check("声道平衡：v4 没有 -> 不在能力表",
          "GetInputAudioBalance" not in caps and "SetInputAudioBalance" not in caps)
    check("删除场景：v4 没有 -> 不在能力表", "RemoveScene" not in caps)
    check("场景排序：协议本就没有 -> 不在能力表", "SetSceneIndex" not in caps)
    check("v4_ABSENT_REQUESTS 覆盖了这三类",
          {"GetInputAudioBalance", "SetInputAudioBalance", "RemoveScene", "SetSceneIndex"}
          <= set(P4.V4_ABSENT_REQUESTS))
    # 空能力表（老服务端不上报）不能合成出任何东西
    check("空能力表 -> 空集合", P4.synthesize_capabilities([]) == set())

    print("\n[A4] 事件翻译")
    ev = P4.normalize_event("SwitchScenes", {"scene-name": "开场", "sources": []})
    check("SwitchScenes -> CurrentProgramSceneChanged",
          ev is not None and ev[0] == "CurrentProgramSceneChanged", str(ev))
    check("  且 sceneName 字段正确", ev[1].get("sceneName") == "开场", str(ev[1]))

    ev = P4.normalize_event("RecordingStarted", {})
    check("RecordingStarted -> RecordStateChanged(STARTED)",
          ev[0] == "RecordStateChanged"
          and ev[1]["outputState"] == P.OUTPUT_STARTED
          and ev[1]["outputActive"] is True, str(ev))
    check("输出状态必须是 v5 枚举（否则 ACTIVE_OUTPUT_STATES 判定失效）",
          ev[1]["outputState"].startswith("OBS_WEBSOCKET_OUTPUT_"), str(ev[1]))
    check("STARTED 落在 ACTIVE_OUTPUT_STATES 里",
          ev[1]["outputState"] in P.ACTIVE_OUTPUT_STATES, str(ev[1]))
    ev = P4.normalize_event("RecordingStopped", {})
    check("RecordingStopped -> RecordStateChanged(STOPPED) 且 active=False",
          ev[1]["outputState"] == P.OUTPUT_STOPPED and ev[1]["outputActive"] is False,
          str(ev))
    ev = P4.normalize_event("RecordingPaused", {})
    check("RecordingPaused -> RecordStateChanged(PAUSED) 用 v5 的 PAUSED 枚举",
          ev[1]["outputState"] == P.OUTPUT_PAUSED, str(ev))

    ev = P4.normalize_event("StreamStarted", {})
    check("StreamStarted -> StreamStateChanged", ev[0] == "StreamStateChanged", str(ev))
    ev = P4.normalize_event("VirtualCamStarted", {})
    check("VirtualCamStarted -> VirtualcamStateChanged",
          ev[0] == "VirtualcamStateChanged", str(ev))
    ev = P4.normalize_event("ReplayStarted", {})
    check("ReplayStarted -> ReplayBufferStateChanged",
          ev[0] == "ReplayBufferStateChanged", str(ev))

    ev = P4.normalize_event("SourceCreated",
                            {"sourceName": "新场景", "sourceType": "scene"})
    check("SourceCreated(scene) -> SceneCreated",
          ev[0] == "SceneCreated" and ev[1]["sceneName"] == "新场景", str(ev))
    ev = P4.normalize_event("SourceCreated",
                            {"sourceName": "麦克风", "sourceType": "input",
                             "sourceKind": "wasapi_input_capture"})
    check("SourceCreated(input) -> InputCreated 带 inputKind",
          ev[0] == "InputCreated" and ev[1]["inputKind"] == "wasapi_input_capture",
          str(ev))
    ev = P4.normalize_event("SourceDestroyed",
                            {"sourceName": "结束", "sourceType": "scene"})
    check("SourceDestroyed(scene) -> SceneRemoved", ev[0] == "SceneRemoved", str(ev))
    ev = P4.normalize_event("SourceRenamed",
                            {"previousName": "旧", "newName": "新", "sourceType": "input"})
    check("SourceRenamed(input) -> InputNameChanged",
          ev[0] == "InputNameChanged" and ev[1]["oldInputName"] == "旧"
          and ev[1]["inputName"] == "新", str(ev))
    ev = P4.normalize_event("SourceRenamed",
                            {"previousName": "旧场景", "newName": "新场景",
                             "sourceType": "scene"})
    check("SourceRenamed(scene) -> SceneNameChanged",
          ev[0] == "SceneNameChanged" and ev[1]["oldSceneName"] == "旧场景", str(ev))
    check("SourceCreated(filter) 被忽略", P4.normalize_event(
        "SourceCreated", {"sourceName": "f", "sourceType": "filter"}) is None)

    ev = P4.normalize_event("SceneItemVisibilityChanged",
                            {"scene-name": "主画面", "item-id": 11,
                             "item-visible": False, "item-name": "桌面音频"})
    check("SceneItemVisibilityChanged -> ...EnableStateChanged",
          ev[0] == "SceneItemEnableStateChanged", str(ev))
    check("  item-visible -> sceneItemEnabled",
          ev[1]["sceneItemEnabled"] is False and ev[1]["sceneItemId"] == 11, str(ev[1]))

    ev = P4.normalize_event("SourceOrderChanged",
                            {"scene-name": "主画面",
                             "scene-items": [{"item-id": 10}, {"item-id": 11}]})
    check("SourceOrderChanged -> SceneItemListReindexed 带 sceneItems 列表",
          ev[0] == "SceneItemListReindexed"
          and [i["sceneItemId"] for i in ev[1]["sceneItems"]] == [10, 11], str(ev))

    ev = P4.normalize_event("MediaPaused", {"sourceName": "媒体源2"})
    check("MediaPaused -> MediaInputActionTriggered(PAUSE)",
          ev[0] == "MediaInputActionTriggered"
          and ev[1]["mediaAction"] == P.MEDIA_ACTION_PAUSE, str(ev))
    ev = P4.normalize_event("MediaStarted", {"sourceName": "媒体源2"})
    check("MediaStarted -> MediaInputPlaybackStarted", ev[0] == "MediaInputPlaybackStarted")

    ev = P4.normalize_event("SwitchTransition", {"transition-name": "Cut"})
    check("SwitchTransition -> CurrentSceneTransitionChanged",
          ev[0] == "CurrentSceneTransitionChanged"
          and ev[1]["transitionName"] == "Cut", str(ev))
    ev = P4.normalize_event("TransitionDurationChanged", {"new-duration": 500})
    check("TransitionDurationChanged -> ...DurationChanged",
          ev[1]["transitionDuration"] == 500, str(ev))
    ev = P4.normalize_event("TransitionBegin", {"name": "Fade"})
    check("TransitionBegin -> SceneTransitionStarted", ev[0] == "SceneTransitionStarted")
    ev = P4.normalize_event("StudioModeSwitched", {"new-state": True})
    check("StudioModeSwitched -> StudioModeStateChanged",
          ev[0] == "StudioModeStateChanged" and ev[1]["studioModeEnabled"] is True, str(ev))
    ev = P4.normalize_event("PreviewSceneChanged", {"scene-name": "开场"})
    check("PreviewSceneChanged -> CurrentPreviewSceneChanged",
          ev[0] == "CurrentPreviewSceneChanged", str(ev))
    ev = P4.normalize_event("SourceVolumeChanged",
                            {"sourceName": "麦克风", "volume": 0.5, "volumeDb": -6.0})
    check("SourceVolumeChanged -> InputVolumeChanged 带 db 与 mul",
          ev[0] == "InputVolumeChanged" and ev[1]["inputVolumeDb"] == -6.0
          and ev[1]["inputVolumeMul"] == 0.5, str(ev))
    ev = P4.normalize_event("SourceMuteStateChanged",
                            {"sourceName": "麦克风", "muted": True})
    check("SourceMuteStateChanged -> InputMuteStateChanged",
          ev[0] == "InputMuteStateChanged" and ev[1]["inputMuted"] is True, str(ev))
    ev = P4.normalize_event("SceneCollectionChanged", {"sceneCollection": "直播方案"})
    check("SceneCollectionChanged -> CurrentSceneCollectionChanged",
          ev[1]["sceneCollectionName"] == "直播方案", str(ev))
    ev = P4.normalize_event("ProfileChanged", {"profile": "高清推流"})
    check("ProfileChanged -> CurrentProfileChanged",
          ev[1]["profileName"] == "高清推流", str(ev))
    check("不认识的事件 -> None（安静丢弃）",
          P4.normalize_event("Heartbeat", {}) is None)
    check("电平表事件不该出现在 v4 映射里",
          P4.normalize_event("InputVolumeMeters", {}) is None)

    print("\n[A5] 单位与形状换算")
    check("时间码 -> 毫秒", P4.parse_timecode_ms("00:00:12.000") == 12_000)
    check("时间码（带小时）", P4.parse_timecode_ms("01:02:03.500") == 3_723_500)
    check("坏时间码 -> 0", P4.parse_timecode_ms("乱码") == 0)
    check("空时间码 -> 0", P4.parse_timecode_ms("") == 0)
    check("data URI 被剥掉",
          P4._strip_data_uri("data:image/png;base64,AAAA") == "AAAA")
    check("裸 base64 原样返回", P4._strip_data_uri("AAAA") == "AAAA")

    print("\n[A6] 应答对象：顶层可属性访问，嵌套仍是 dict")
    obj = P4.as_object({"sceneItems": [{"sceneItemId": 1}], "sceneName": "主画面"})
    check("顶层 -> 属性", obj.scene_name == "主画面")
    check("顶层列表仍可迭代且元素是 dict",
          obj.scene_items[0]["sceneItemId"] == 1)
    check("response_data 保留原始 JSON 供诊断窗口用",
          obj.response_data["sceneName"] == "主画面")


    print("\n[A7] GetVersion 也要能走普通请求通道（心跳用）")
    # 这条曾经漏掉：控制器的心跳（A11）是用普通请求发 GetVersion 的，
    # 不是只在握手时用一次。缺了它，v4 会话里心跳会被判成 204，
    # RTT 永远没样本、能力还会被误标成"不支持"。
    calls = []

    def fake_call(name, fields):
        calls.append(name)
        return {
            "obs-studio-version": "27.2.4",
            "obs-websocket-version": "4.9.1",
            "available-requests": "GetVersion,GetSceneList,SetCurrentScene,GetStudioModeStatus",
        }

    ver = P4.resolve("GetVersion", {}, fake_call)
    check("GetVersion 能解析", isinstance(ver, dict) and calls == ["GetVersion"], str(calls))
    check("  版本字段正确", ver["obsVersion"] == "27.2.4"
          and ver["obsWebSocketVersion"] == "4.9.1", str(ver))
    check("  能力是合成的 v5 名字", "GetSceneList" in ver["availableRequests"], str(ver))


# ==================================================================== [B] 端到端
def e2e_tests() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-v4-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    port = free_port()
    server = FakeObsV4Server(host="127.0.0.1", port=port)
    server.start()
    print(f"假 OBS v4 服务器：127.0.0.1:{port}")

    app = QApplication([])
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 2.0
    controller.config.reconnect.initial_delay_ms = 300
    controller.config.reconnect.max_delay_ms = 300
    conn = ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)

    print("\n[B1] 协议自动探测与模式标识")
    controller.connect(conn)
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("v4 连接成功（自动探测）", ok, controller.store.status_message)
    info = controller.store.server_info
    check("识别为 v4 协议", info.protocol == "v4", info.protocol)
    check("标题用的模式名是「兼容模式」", info.mode_label() == "兼容模式", info.mode_label())
    check("版本信息回显 OBS 27.2.4", info.obs_version == "27.2.4", info.obs_version)
    check("状态栏版本段也标出兼容模式",
          info.protocol == "v4" and "兼容模式" in info.mode_label())
    check("store.compat_mode 已置位", controller.store.compat_mode is True)

    print("\n[B2] v4 没有的能力被挡在能力表外")
    store = controller.store
    check("声道平衡不受支持", not store.supports(P.REQ_SET_INPUT_AUDIO_BALANCE))
    check("删除场景不受支持", not store.supports(P.REQ_REMOVE_SCENE))
    check("场景排序不受支持", not store.supports(P.REQ_SET_SCENE_INDEX))
    check("置灰原因提到 v4/兼容模式",
          "v4" in store.support_reason(P.REQ_REMOVE_SCENE)
          or "兼容" in store.support_reason(P.REQ_REMOVE_SCENE),
          store.support_reason(P.REQ_REMOVE_SCENE))
    check("回放缓冲（v4 未开启）不受支持",
          not store.supports(P.REQ_GET_REPLAY_BUFFER_STATUS))
    check("虚拟摄像机（v4 未开启）不受支持",
          not store.supports(P.REQ_GET_VIRTUALCAM_STATUS))
    check("支持的请求有正常原因（空串）",
          store.support_reason(P.REQ_GET_SCENE_LIST) == "")

    print("\n[B2a] v4 不支持的操作在控制器层也不下发（不只靠界面置灰）")
    # 界面按钮已置灰，但热键/托盘/将来自动化都可能绕过 UI 直接调控制器。
    # 这三条曾经是裸 send()，会白发一条注定 204 的请求。
    before = list(server.state.requests)
    controller.set_balance("麦克风", 0.25)          # v4 无声道平衡
    controller.remove_scene("结束")                 # v4 无删除场景
    controller.move_scene("结束", 0)                # 协议本就没有场景排序
    controller.create_scene("新场景")               # v4 的 CreateScene 需 4.9.0+（fixture 未开）
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    newly = [r for r in server.state.requests[len(before):]]
    check("声道平衡没有下发给 v4", "SetAudioBalance" not in newly, str(newly))
    check("删除场景没有下发给 v4", "RemoveScene" not in newly, str(newly))
    check("场景排序没有下发（协议无此请求）", "SetSceneIndex" not in newly, str(newly))
    check("新建场景没有下发给能力不足的 v4", "CreateScene" not in newly, str(newly))
    check("本地状态未被误改（删除/重排都没生效）",
          "结束" in [s.name for s in store.scenes], str([s.name for s in store.scenes]))

    print("\n[B3] 场景与来源（顺序、字段、可见性）")
    wait_until(lambda: len(store.scenes) == 3, 8, app)
    names = [s.name for s in store.scenes]
    check("场景列表加载且展示顺序与 v4 一致",
          names == ["开场", "主画面", "结束"], str(names))
    check("当前场景识别", store.current_scene == "主画面", store.current_scene)

    wait_until(lambda: len(store.scene_items) == 3, 8, app)
    items = [i.source_name for i in store.scene_items]
    check("来源列表加载（与 v5 同样的展示顺序）",
          items == ["游戏画面", "桌面音频", "摄像头"], str(items))
    check("来源可见性默认全开", all(i.enabled for i in store.scene_items))
    kinds = {i.source_name: i.source_kind for i in store.scene_items}
    check("来源带 sourceKind（v4 的 GetSceneItemList 直接给）",
          kinds.get("摄像头") == "dshow_input", str(kinds))

    print("\n[B4] 来源显隐（v4 走 SetSceneItemProperties.visible）")
    target = next(i for i in store.scene_items if i.source_name == "摄像头")
    controller.set_item_enabled("主画面", target.item_id, False)
    wait_until(lambda: server.state.visible["主画面"][target.item_id] is False, 6, app)
    check("服务端可见性真的被改掉",
          server.state.visible["主画面"][target.item_id] is False)
    check("本地也同步为隐藏",
          not next(i.enabled for i in store.scene_items if i.source_name == "摄像头"))

    print("\n[B5] 场景切换与新增")
    controller.switch_scene("开场")
    wait_until(lambda: server.state.current_scene == "开场", 6, app)
    check("场景切换下发（SetCurrentScene）", server.state.current_scene == "开场")
    wait_until(lambda: store.current_scene == "开场", 6, app)
    check("本地当前场景跟随", store.current_scene == "开场", store.current_scene)
    wait_until(lambda: [i.source_name for i in store.scene_items] == ["字幕", "背景"], 6, app)
    check("来源列表跟随刷新",
          [i.source_name for i in store.scene_items] == ["字幕", "背景"],
          str([i.source_name for i in store.scene_items]))

    print("\n[B5a] 心跳（A11）也要能在 v4 上量到 RTT")
    # 心跳是普通请求通道发的 GetVersion —— 这条曾经在 v4 上被解析表漏掉，
    # 结果是 RTT 永远没样本、而且 GetVersion 还会被误标为"不支持"。
    controller.config.heartbeat_interval_ms = 1000
    controller.apply_config(controller.config)
    rtt_samples_before = store.health.samples
    ok = wait_until(lambda: store.health.samples > rtt_samples_before, 10, app)
    check("心跳拿到 RTT 样本（GetVersion 在 v4 上可用）", ok,
          f"samples={store.health.samples}")
    check("GetVersion 没有被误标成不支持",
          P.REQ_GET_VERSION not in store.rejected_requests
          and store.supports(P.REQ_GET_VERSION))
    check("RTT 数值合理（>0）", store.health.rtt_ms > 0, str(store.health.rtt_ms))

    print("\n[B6] 录制与推流")
    controller.toggle_record()
    wait_until(lambda: store.record.active, 6, app)
    check("开始录制", store.record.active)
    # 时长来自轮询回执（v4 只给 recordTimecode，要自己反算），可能比
    # 状态翻转晚一拍到，所以等它落下来再断言（别拿 0 去判定）
    wait_until(lambda: store.record.duration_ms == 12_000, 6, app)
    check("时长由 v4 时间码反算（12 秒）",
          store.record.duration_ms == 12_000, str(store.record.duration_ms))
    controller.toggle_pause_record()
    wait_until(lambda: store.record.paused, 6, app)
    check("暂停录制（v4 的 PauseRecording）", store.record.paused)
    controller.toggle_pause_record()
    wait_until(lambda: not store.record.paused, 6, app)
    check("继续录制", not store.record.paused)
    controller.toggle_record()
    wait_until(lambda: not store.record.active, 6, app)
    check("停止录制", not store.record.active)

    controller.toggle_stream()
    wait_until(lambda: store.stream.active, 6, app)
    check("开始推流", store.stream.active)
    # 时长来自 GetStreamStatus 的回执（v4 只给 stream-timecode，要自己反算），
    # 它可能比 StreamStarted 事件晚一步到，所以等它落下来再断言（别拿 0 判定）
    wait_until(lambda: store.stream.duration_ms == 30_000, 6, app)
    check("推流时长由时间码反算（30 秒）",
          store.stream.duration_ms == 30_000, str(store.stream.duration_ms))
    check("v4 没有重连概念，恒为 False", store.stream.reconnecting is False)
    controller.toggle_stream()
    wait_until(lambda: not store.stream.active, 6, app)
    check("停止推流", not store.stream.active)

    print("\n[B7] 推流字幕（v4 用 SendCaptions，但没推流时是静默成功）")
    check("v4 支持字幕能力", controller.caption_supported())
    check("未推流时本地先拦住（不能假装发成功）",
          controller.stream_caption_blocker() != "", controller.stream_caption_blocker())
    before = len(server.state.captions)
    check("未推流时不下发", controller.send_stream_caption("不该发") is False)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    check("服务端没收到任何字幕", len(server.state.captions) == before)
    controller.toggle_stream()
    wait_until(lambda: store.stream.active, 6, app)
    check("推流中不再拦截", controller.stream_caption_blocker() == "")
    check("推流中下发成功", controller.send_stream_caption("v4 字幕"))
    wait_until(lambda: "v4 字幕" in server.state.captions, 6, app)
    check("服务端收到字幕", "v4 字幕" in server.state.captions,
          str(server.state.captions))
    # 收干净：后面 B11 要切场景集合/配置文件，而"正在推流"是**故意**禁止切换的
    # （见 controller.config_switch_blocker），不停掉推流会把这层保护误判成缺陷。
    controller.toggle_stream()
    wait_until(lambda: not store.stream.active, 6, app)
    check("字幕段收尾：停止推流", not store.stream.active)

    print("\n[B8] 混音器（v4 的字段名与单位都不同）")
    wait_until(lambda: len(store.audio_inputs) > 0, 8, app)
    audio_names = [i.name for i in store.audio_inputs]
    check("音频源已列出", "桌面音频" in audio_names, str(audio_names))
    check("纯视频源（字幕）不在混音器里", "字幕" not in audio_names, str(audio_names))
    mic = store.find_audio_input("麦克风")
    wait_until(lambda: store.find_audio_input("麦克风") is not None
               and store.find_audio_input("麦克风").muted is True, 8, app)
    mic = store.find_audio_input("麦克风")
    check("静音状态回读（v4 的 GetMute.muted）", mic.muted is True, str(mic.muted))
    check("音量回读（v4 的 GetVolume 用 dB 问）",
          abs(mic.volume_db + 6.0) < 1e-6, str(mic.volume_db))

    controller.set_input_volume("麦克风", -3.0)
    wait_until(lambda: abs(server.state.volumes_db["麦克风"] + 3.0) < 1e-6, 6, app)
    check("设置音量下发（SetVolume + useDecibel）",
          abs(server.state.volumes_db["麦克风"] + 3.0) < 1e-6,
          str(server.state.volumes_db["麦克风"]))
    controller.set_input_mute("麦克风", False)
    wait_until(lambda: server.state.muted["麦克风"] is False, 6, app)
    check("取消静音下发（v4 字段叫 mute）", server.state.muted["麦克风"] is False)

    print("\n[B9] 高级音频属性（v4 缺声道平衡，且偏移单位是纳秒）")
    controller.fetch_advanced_audio("麦克风")
    wait_until(lambda: (store.find_audio_input("麦克风") or mic).sync_offset_ms is not None,
               8, app)
    mic = store.find_audio_input("麦克风")
    check("同步偏移已回读（v4 纳秒 -> 毫秒）", mic.sync_offset_ms == 0,
          str(mic.sync_offset_ms))
    controller.set_sync_offset("麦克风", 250)
    wait_until(lambda: server.state.sync_offset_ns["麦克风"] == 250 * 1_000_000, 6, app)
    check("设置同步偏移按纳秒下发（×1000000）",
          server.state.sync_offset_ns["麦克风"] == 250 * 1_000_000,
          str(server.state.sync_offset_ns["麦克风"]))
    check("监听类型已回读（v4 短名 -> v5 长名）",
          mic.monitor_type in (None, "OBS_MONITORING_TYPE_NONE"), str(mic.monitor_type))
    controller.set_monitor_type("麦克风", "OBS_MONITORING_TYPE_MONITOR_ONLY")
    wait_until(lambda: server.state.monitor_types["麦克风"] == "monitorOnly", 6, app)
    check("设置监听类型按短名下发给 v4",
          server.state.monitor_types["麦克风"] == "monitorOnly",
          str(server.state.monitor_types["麦克风"]))
    controller.set_tracks("麦克风", 0b000011)
    wait_until(lambda: server.state.tracks["麦克风"] == 0b000011, 6, app)
    check("混音轨位掩码逐轨下发（v4 一次只改一条）",
          server.state.tracks["麦克风"] == 0b000011, str(server.state.tracks["麦克风"]))

    print("\n[B10] 转场与工作室模式")
    wait_until(lambda: "Fade" in store.transitions, 8, app)
    check("转场列表加载", "Fade" in store.transitions, str(store.transitions))
    check("当前转场识别", store.current_transition == "Fade", store.current_transition)
    check("Fade 可配时长（v4 给了 duration）", store.transition_configurable is True)
    controller.set_transition("Cut")
    wait_until(lambda: server.state.current_transition == "Cut", 6, app)
    check("切换转场下发（SetCurrentTransition.transition-name）",
          server.state.current_transition == "Cut")
    controller.set_transition_duration(500)
    wait_until(lambda: server.state.transition_duration == 500, 6, app)
    check("设置转场时长下发（v4 字段叫 duration）",
          server.state.transition_duration == 500)
    check("工作室模式能力存在", store.supports(P.REQ_GET_STUDIO_MODE_ENABLED))
    check("T 型推杆：fixture 未提供 -> 不受支持",
          not store.supports(P.REQ_SET_TBAR_POSITION))
    controller.set_studio_mode(True)
    wait_until(lambda: server.state.studio_mode is True, 6, app)
    check("开工作室模式（v4 走 EnableStudioMode）", server.state.studio_mode is True)
    controller.set_preview_scene("结束")
    wait_until(lambda: server.state.preview_scene == "结束", 6, app)
    check("设置预览场景（v4 的 SetPreviewScene）", server.state.preview_scene == "结束")
    controller.trigger_transition()
    wait_until(lambda: server.state.current_scene == "结束", 8, app)
    check("执行转场（v4 的 TransitionToProgram）",
          server.state.current_scene == "结束", server.state.current_scene)
    controller.set_studio_mode(False)
    wait_until(lambda: server.state.studio_mode is False, 6, app)
    check("关工作室模式（v4 走 DisableStudioMode）", server.state.studio_mode is False)

    print("\n[B11] 场景集合 / 配置文件")
    wait_until(lambda: len(store.scene_collections) == 3, 8, app)
    check("场景集合列表加载", store.scene_collections == ["未命名", "直播方案", "录屏方案"],
          str(store.scene_collections))
    check("当前场景集合识别", store.current_scene_collection == "未命名")
    check("配置文件列表加载", "高清推流" in store.profiles, str(store.profiles))
    controller.switch_scene_collection("直播方案")
    wait_until(lambda: server.state.current_scene_collection == "直播方案", 6, app)
    check("切换场景集合下发（v4 的 sc-name）",
          server.state.current_scene_collection == "直播方案",
          server.state.current_scene_collection)
    controller.switch_profile("高清推流")
    wait_until(lambda: server.state.current_profile == "高清推流", 6, app)
    check("切换配置文件下发（v4 的 profile-name）",
          server.state.current_profile == "高清推流")

    print("\n[B12] 界面：模式标签与置灰/隐藏")
    from obs_remote_studio.ui.main_window import MainWindow

    # MainWindow 构造时会按当前 store 状态刷一次标题（不再只依赖后续信号），
    # 所以"先连上、后开窗"也要立刻显示正确的模式标签。
    window = MainWindow(controller)
    check("窗口标题含「兼容模式」",
          "兼容模式" in window.windowTitle(), window.windowTitle())
    check("窗口标题含 OBS 版本",
          "27.2.4" in window.windowTitle(), window.windowTitle())

    mixer = window.mixer_panel
    wait_until(lambda: len(mixer._rows) > 0, 6, app)
    rows_hidden = [
        row.meter.isHidden() for row in mixer._rows.values()
    ]
    check("混音器电平条被隐藏（v4 无电平表事件）",
          bool(rows_hidden) and all(rows_hidden), str(rows_hidden))

    scene_panel = window.scene_panel
    check("来源面板可用（v4 支持显隐）",
          controller.store.supports(P.REQ_SET_SCENE_ITEM_ENABLED))
    scene_panel.refresh()
    check("删除场景按钮被禁用（v4 无 RemoveScene）",
          not scene_panel.remove_btn.isEnabled()
          or not scene_panel._can_remove_current(), "disabled check")
    check("场景排序按钮被禁用（协议不支持）",
          not scene_panel.up_btn.isEnabled() and not scene_panel.down_btn.isEnabled())

    controls = window.controls_panel
    controls._update_buttons()
    check("控制面板：回放缓冲按钮隐藏（v4 未配）",
          not controls.replay_btn.isVisible())
    check("控制面板：虚拟摄像机按钮隐藏（v4 未配）",
          not controls.virtual_cam_btn.isVisible())
    check("控制面板：静音/录制按钮可用", controls.record_btn.isEnabled())

    # v5 的标准模式标签不能被 v4 影响
    store.set_server_info(ServerInfo(obs_version="31.0.0",
                                     websocket_version="5.5.0", protocol="v5"))
    check("切回 v5 时标题显示「标准模式」",
          "标准模式" in window.windowTitle(), window.windowTitle())
    store.set_server_info(info)

    print("\n[B13] 界面：高级音频属性里声道平衡置灰")
    from obs_remote_studio.ui.dialogs.advanced_audio_dialog import (
        COL_BALANCE,
        AdvancedAudioDialog,
    )

    dialog = AdvancedAudioDialog(
        store,
        {"monitor": lambda *a: None, "balance": lambda *a: None,
         "sync": lambda *a: None, "tracks": lambda *a: None,
         "fetch": lambda *a: None},
        window,
    )
    balance_widgets = [
        dialog.table.cellWidget(row, COL_BALANCE)
        for row in range(dialog.table.rowCount())
    ]
    check("高级音频属性里声道平衡被置灰",
          bool(balance_widgets) and all(
              not (w is None or w.isEnabled()) for w in balance_widgets
          ),
          str([None if w is None else w.isEnabled() for w in balance_widgets]))
    dialog.close()

    print("\n[B14] 断开后模式信息仍保留（标题不闪回标准模式）")
    controller.disconnect()
    wait_until(lambda: store.connection_state != CONNECTED, 6, app)
    check("断开后仍记着上次是兼容模式",
          store.server_info.protocol == "v4", store.server_info.protocol)
    check("断开后标题仍显示兼容模式",
          "兼容模式" in window.windowTitle(), window.windowTitle())

    window.close()
    controller.shutdown()
    server.stop()


def legacy_v4_fallback_test() -> None:
    """老 v4（< 4.9）没有 GetSceneItemList 时必须退回 GetSceneList 并把顺序翻正。

    这条用例抓过一个真实缺陷：`raw_call` 原来直接抛 OBSSDKRequestError，
    而解析器是靠 `V4Unsupported` 触发降级的 —— 于是降级路径**永远不会**走到，
    来源列表在老 v4 上会整片空白。
    """
    port = free_port()
    server = FakeObsV4Server(host="127.0.0.1", port=port, scene_item_list=False)
    server.start()
    app = QApplication.instance() or QApplication([])
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 2.0
    controller.config.protocol = "v4"
    controller.connect(
        ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)
    )
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 12, app)
    check("老 v4（无 GetSceneItemList）也能连上", ok)
    check("老 v4 上 GetSceneItemList 仍算支持（走降级）",
          controller.store.supports(P.REQ_GET_SCENE_ITEM_LIST))

    wait_until(lambda: len(controller.store.scene_items) == 3, 8, app)
    names = [i.source_name for i in controller.store.scene_items]
    # v4 的 GetSceneList.sources 是**反序**（顶 -> 底），降级时必须翻回来，
    # 否则来源列表会上下颠倒
    check("降级后来源顺序正确（反序被翻正）",
          names == ["游戏画面", "桌面音频", "摄像头"], str(names))

    # 显隐也要能在老 v4 上工作（走 SetSceneItemProperties）
    item = next(i for i in controller.store.scene_items if i.source_name == "摄像头")
    controller.set_item_enabled("主画面", item.item_id, False)
    wait_until(lambda: server.state.visible["主画面"][item.item_id] is False, 6, app)
    check("老 v4 上来源显隐仍可用",
          server.state.visible["主画面"][item.item_id] is False)

    controller.disconnect()
    controller.shutdown()
    server.stop()


def manual_override_test() -> None:
    """强制指定协议也应生效（探测不是唯一途径）。"""
    port = free_port()
    server = FakeObsV4Server(host="127.0.0.1", port=port)
    server.start()
    app = QApplication.instance() or QApplication([])
    controller = Controller()
    controller.config.poll_interval_ms = 500
    controller.config.request_timeout_s = 2.0
    controller.config.protocol = "v4"      # 显式钉死，跳过探测
    controller.connect(
        ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)
    )
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 12, app)
    check("显式指定 v4 也能连上", ok)
    check("显式指定时 protocol=v4", controller.store.server_info.protocol == "v4")
    check("显式指定时 compat_mode=True", controller.store.compat_mode is True)
    controller.disconnect()
    controller.shutdown()
    server.stop()


def main() -> int:
    logic_tests()
    e2e_tests()
    legacy_v4_fallback_test()
    manual_override_test()

    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    if SLOT_ERRORS:
        print(f"\n警告：测试期间有 {len(SLOT_ERRORS)} 个槽函数抛异常（Qt 默认会吞掉）：")
        for item in SLOT_ERRORS[:3]:
            print("  " + item.splitlines()[-1])
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""极简 obs-websocket **v4**（legacy）服务器，仅供本项目的 v4 兼容测试使用。

与 `fake_obs_server.py`（v5）的关键差异 —— 这几点正是兼容层要对付的东西：

| 方面            | v5（fake_obs_server.py）                    | v4（本文件）                                        |
| --------------- | ------------------------------------------- | --------------------------------------------------- |
| 握手            | 连上立刻发 op=0 Hello                        | **连上什么都不发**                                   |
| 分帧            | op 码 + `d` 包装                             | **裸 JSON**，无 op                                   |
| 请求            | `{op:6, d:{requestType, requestId, requestData}}` | `{request-type, message-id, ...字段平铺}`        |
| 响应            | `{op:7, d:{requestStatus:{result, code}}}`   | `{message-id, status:"ok"/"error", error?}`          |
| 事件            | 按 `eventSubscriptions` 位掩码推送            | **无条件推给每条连接**（无订阅机制）                  |
| 错误            | 数字 `code`（100/204/506/604…）              | **只有字符串** `status` + `error`                    |
| 电平表          | `InputVolumeMeters` 高频事件                 | **完全不存在**（没有任何电平表能力）                  |
| 认证失败        | 关闭连接（4009）                             | 回一条错误帧，**连接保持**                            |

**一条连接同时承载响应与事件**（v4 没有独立的订阅通道），所以本文件会把事件
广播给所有连接 —— 客户端的请求连接因此也会收到事件帧，必须自己按 `message-id` 过滤。

实现范围：覆盖本项目会用到的请求 + 相关事件；不做 ExecuteBatch、不做真实编码。
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import Any

import websockets

# v4 与 v5 的认证算法完全相同，PNG 生成也照旧复用，避免重复实现
from fake_obs_server import expected_auth, make_png

DEFAULT_PASSWORD = "testpass"
DEFAULT_OBS_VERSION = "27.2.4"
DEFAULT_WS_VERSION = "4.9.1"

# 与 v5 假服务器一致的输出状态文案（v4 用独立事件表达状态，这里折算给 v5 语义用）
STARTING = "OBS_WEBSOCKET_OUTPUT_STARTING"
STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
STOPPING = "OBS_WEBSOCKET_OUTPUT_STOPPING"
STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"

# OBS 判定推杆"推到底"带 10% 量程容差（与 v5 假服务器、真实 OBS 一致）
T_BAR_CLAMP = 0.1


class V4ObsState:
    """v4 服务器的一份可变状态。"""

    def __init__(self) -> None:
        # 与 v5 假服务器同名的场景/来源，方便两边对照
        self.scenes = ["开场", "主画面", "结束"]
        self.current_scene = "主画面"
        self.preview_scene = "主画面"
        # **自然枚举顺序（底 → 顶）**：v5 与 v4 的 GetSceneItemList 都是这个顺序；
        # 而 v4 的 GetSceneList.sources 要反过来（顶 → 底）
        self.items: dict[str, list[dict[str, Any]]] = {
            "开场": [
                {"id": 1, "name": "背景", "kind": "image_source"},
                {"id": 2, "name": "字幕", "kind": "text_ft2_source"},
            ],
            "主画面": [
                {"id": 10, "name": "摄像头", "kind": "dshow_input"},
                {"id": 11, "name": "桌面音频", "kind": "wasapi_output_capture"},
                {"id": 12, "name": "游戏画面", "kind": "game_capture"},
            ],
            "结束": [
                {"id": 20, "name": "结束画面", "kind": "image_source"},
            ],
        }
        self.visible: dict[str, dict[int, bool]] = {
            scene: {item["id"]: True for item in items}
            for scene, items in self.items.items()
        }
        self.record_active = False
        self.record_paused = False
        self.stream_active = False
        self.replay_buffer_active = False
        self.virtualcam_active = False
        self.record_ms = 0
        self.stream_ms = 0
        # 转场
        self.transitions = ["Cut", "Fade", "Swipe"]
        self.current_transition = "Fade"
        self.transition_duration = 300
        self.studio_mode = False
        self.tbar_position = 0.0
        self.tbar_transitioning = False
        # 音频：key 是源名
        self.audio_kinds = {
            "桌面音频": "wasapi_output_capture",
            "麦克风": "wasapi_input_capture",
            "媒体源2": "ffmpeg_source",
            "摄像头": "dshow_input",
            "字幕": "text_ft2_source",
        }
        self.volumes_db = {"桌面音频": 0.0, "麦克风": -6.0, "媒体源2": -12.0, "摄像头": 0.0}
        self.muted = {"桌面音频": False, "麦克风": True, "媒体源2": False, "摄像头": False}
        self.monitor_types = {name: "none" for name in self.audio_kinds}
        # v4 的同步偏移单位是**纳秒**
        self.sync_offset_ns = {name: 0 for name in self.audio_kinds}
        self.tracks = {name: 0b000001 for name in self.audio_kinds}
        # 媒体
        self.media: dict[str, dict[str, Any]] = {
            "媒体源2": {"state": "stopped", "duration": 180_000, "cursor": 0}
        }
        # 场景集合 / 配置文件
        self.scene_collections = ["未命名", "直播方案", "录屏方案"]
        self.current_scene_collection = "未命名"
        self.profiles = ["未命名", "高清推流", "低码率"]
        self.current_profile = "未命名"
        self.record_directory = "D:/record"
        self.captions: list[str] = []
        self.screenshot_count = 0
        self.requests: list[str] = []


class FakeObsV4Server:
    """在独立线程里跑 asyncio 的 v4 服务器。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        password: str = DEFAULT_PASSWORD,
        auth_required: bool = True,
        obs_version: str = DEFAULT_OBS_VERSION,
        ws_version: str = DEFAULT_WS_VERSION,
        # 与 v5 假服务器同名的能力开关：关掉 = 该请求不存在（回 invalid request type）
        pause_record: bool = True,
        replay_buffer: bool = False,
        virtualcam: bool = False,
        media_inputs: bool = False,
        config_switch: bool = True,
        scene_edit: bool = False,
        tbar: bool = False,
        send_captions: bool = True,
        take_screenshot: bool = True,
        studio_mode: bool = True,
        transition_list: bool = True,
        scene_item_list: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.auth_required = auth_required
        self.obs_version = obs_version
        self.ws_version = ws_version
        self.pause_record = pause_record
        self.replay_buffer = replay_buffer
        self.virtualcam = virtualcam
        self.media_inputs = media_inputs
        self.config_switch = config_switch
        self.scene_edit = scene_edit
        self.tbar = tbar
        self.send_captions = send_captions
        self.take_screenshot = take_screenshot
        self.studio_mode = studio_mode
        self.transition_list = transition_list
        # GetSceneItemList 是 4.9.0 才有的；关掉可模拟更老的 v4，
        # 用来验证客户端的降级路径（退回 GetSceneList 并翻转顺序）
        self.scene_item_list = scene_item_list

        self.state = V4ObsState()
        self.requests: list[str] = []
        # 每条连接是否已认证；v4 只有"认证/未认证"两态
        self._authed: dict[Any, bool] = {}
        self._connections: set[Any] = set()
        self._session_challenge = "qrstuvwxyz123456"
        self._session_salt = "abcdefghijklmnop"

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stop_future: Any = None
        self._status_task: Any = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> int:
        """启动并返回实际端口；起不来就抛异常，绝不假装成功。"""
        self._thread.start()
        ready = self._ready.wait(10)
        if isinstance(self._startup_error, BaseException):
            raise RuntimeError(
                f"假 OBS v4 服务器启动失败（{self.host}:{self.port}）：{self._startup_error}"
            ) from self._startup_error
        if not ready:
            raise RuntimeError(
                f"假 OBS v4 服务器 10 秒内未就绪（{self.host}:{self.port}），端口可能被占用"
            )
        return self.port

    def stop(self) -> None:
        if not self._loop.is_running():
            return

        def _finish() -> None:
            if self._stop_future is not None and not self._stop_future.done():
                self._stop_future.set_result(None)

        self._loop.call_soon_threadsafe(_finish)
        self._thread.join(5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - 交给 start() 抛出
            self._startup_error = exc
            self._ready.set()

    async def _serve(self) -> None:
        async with websockets.serve(self._handler, self.host, self.port) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            self._stop_future = self._loop.create_future()
            self._status_task = asyncio.ensure_future(self._stream_status_loop())
            await self._stop_future
            self._status_task.cancel()

    async def _stream_status_loop(self) -> None:
        """v4 每 2 秒推一次 StreamStatus —— 但**只在推流时**。"""
        while True:
            await asyncio.sleep(2.0)
            if not self.state.stream_active:
                continue
            await self.broadcast("StreamStatus", self._stream_status_fields())

    def _stream_status_fields(self) -> dict:
        st = self.state
        return {
            "streaming": st.stream_active,
            "recording": st.record_active,
            "recording-paused": st.record_paused,
            "replay-buffer-active": st.replay_buffer_active,
            "bytes-per-sec": 1_200_000,
            "kbits-per-sec": 9375,
            "strain": 0.0,
            "total-stream-time": st.stream_ms // 1000,
            "num-total-frames": 1800,
            "num-dropped-frames": 3,
            "fps": 60.0,
            "render-total-frames": 3600,
            "render-missed-frames": 1,
            "output-total-frames": 1800,
            "output-skipped-frames": 3,
            "average-frame-time": 3.2,
            "cpu-usage": 7.5,
            "memory-usage": 512.0,
            "free-disk-space": 204_800.0,
            "preview-only": False,
        }

    # ---------------------------------------------------------------- 连接
    async def _handler(self, ws) -> None:
        self._connections.add(ws)
        # authRequired=false 时未认证也照样放行；true 时先当作未认证
        self._authed[ws] = not self.auth_required
        try:
            # ⚠️ 这里**什么都不发** —— v4 没有 Hello，客户端靠"静默"认出这是 v4
            async for raw in ws:
                await self._handle_message(ws, raw)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(ws)
            self._authed.pop(ws, None)

    async def _handle_message(self, ws, raw) -> None:
        # v4 对非文本帧直接静默丢弃；websockets 只给我们文本/二进制，二进制一律忽略
        if isinstance(raw, (bytes, bytearray)):
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            # 非法 JSON：回错误帧且**不带 message-id**，连接保持
            await self._send(ws, {"status": "error", "error": "invalid JSON payload"})
            return
        if not isinstance(data, dict):
            await self._send(ws, {"status": "error", "error": "missing request parameters"})
            return

        if "request-type" not in data or "message-id" not in data:
            # 缺字段时服务端用 nullptr 当 id，因此**回帧里没有 message-id**
            await self._send(ws, {"status": "error", "error": "missing request parameters"})
            return

        req_type = str(data.get("request-type"))
        message_id = str(data.get("message-id"))
        fields = {
            key: value
            for key, value in data.items()
            if key not in ("request-type", "message-id")
        }
        self.requests.append(req_type)
        self.state.requests.append(req_type)

        # 认证闸门：只有这三条在未认证时放行
        if self.auth_required and not self._authed.get(ws, False):
            if req_type not in ("GetVersion", "GetAuthRequired", "Authenticate"):
                await self._reply_error(ws, message_id, "Not Authenticated")
                return

        if req_type not in self.available_requests():
            await self._reply_error(ws, message_id, "invalid request type")
            return

        try:
            response, events, error = self._dispatch(req_type, fields, ws)
        except Exception as exc:  # noqa: BLE001 - 假服务器不该把测试带崩
            await self._reply_error(ws, message_id, f"server error: {exc}")
            return

        if error is not None:
            await self._reply_error(ws, message_id, error)
            return

        await self._send(
            ws,
            {"message-id": message_id, "status": "ok", **(response or {})},
        )
        for item in events or []:
            await self.broadcast(*item)

    async def _reply_error(self, ws, message_id: str, message: str) -> None:
        await self._send(
            ws, {"message-id": message_id, "status": "error", "error": message}
        )

    async def _send(self, ws, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload))
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 能力
    def available_requests(self) -> list[str]:
        """v4 的请求全集（GetVersion 会把它拼成逗号分隔字符串上报）。"""
        base = [
            "GetVersion",
            "GetAuthRequired",
            "Authenticate",
            "SetHeartbeat",
            "GetStats",
            "GetVideoInfo",
            "GetSceneList",
            "GetCurrentScene",
            "SetCurrentScene",
            "GetSceneItemProperties",
            "SetSceneItemProperties",
            "SetSceneItemRender",
            "ReorderSceneItems",
            "GetRecordingStatus",
            "StartRecording",
            "StopRecording",
            "StartStopRecording",
            "GetRecordingFolder",
            "GetStreamingStatus",
            "StartStreaming",
            "StopStreaming",
            "StartStopStreaming",
            "GetSourcesList",
            "GetSourceTypesList",
            "GetSpecialSources",
            "GetAudioActive",
            "GetVolume",
            "SetVolume",
            "GetMute",
            "SetMute",
            "ToggleMute",
            "GetAudioMonitorType",
            "SetAudioMonitorType",
            "GetSyncOffset",
            "SetSyncOffset",
        ]
        if self.scene_item_list:
            base.append("GetSceneItemList")
        if self.pause_record:
            base += ["PauseRecording", "ResumeRecording"]
        if self.replay_buffer:
            base += [
                "GetReplayBufferStatus",
                "StartReplayBuffer",
                "StopReplayBuffer",
                "StartStopReplayBuffer",
                "SaveReplayBuffer",
            ]
        if self.virtualcam:
            base += [
                "GetVirtualCamStatus",
                "StartVirtualCam",
                "StopVirtualCam",
                "StartStopVirtualCam",
            ]
        if self.media_inputs:
            base += [
                "GetMediaState",
                "GetMediaDuration",
                "GetMediaTime",
                "SetMediaTime",
                "ScrubMedia",
                "GetMediaSourcesList",
                "PlayPauseMedia",
                "RestartMedia",
                "StopMedia",
                "NextMedia",
                "PreviousMedia",
            ]
        if self.config_switch:
            base += [
                "ListSceneCollections",
                "GetCurrentSceneCollection",
                "SetCurrentSceneCollection",
                "ListProfiles",
                "GetCurrentProfile",
                "SetCurrentProfile",
            ]
        if self.scene_edit:
            base += ["CreateScene", "SetSourceName"]
        if self.transition_list:
            base += [
                "GetTransitionList",
                "GetCurrentTransition",
                "SetCurrentTransition",
                "SetTransitionDuration",
                "GetTransitionDuration",
            ]
        if self.tbar:
            base += ["GetTransitionPosition", "SetTBarPosition", "ReleaseTBar"]
        if self.studio_mode:
            base += [
                "GetStudioModeStatus",
                "GetPreviewScene",
                "SetPreviewScene",
                "TransitionToProgram",
                "EnableStudioMode",
                "DisableStudioMode",
                "ToggleStudioMode",
            ]
        if self.send_captions:
            base.append("SendCaptions")
        if self.take_screenshot:
            base.append("TakeSourceScreenshot")
        # GetTracks / SetTracks 是 4.9.1 才有
        base += ["GetTracks", "SetTracks"]
        return base

    # ---------------------------------------------------------------- 分发
    def _dispatch(self, req_type: str, fields: dict, ws):
        """返回 (response_data, events, error_message)。"""
        st = self.state

        # ---- General ----
        if req_type == "GetVersion":
            return {
                "version": 1.1,
                "obs-websocket-version": self.ws_version,
                "obs-studio-version": self.obs_version,
                # v4 是**逗号分隔字符串**，不是数组
                "available-requests": ",".join(self.available_requests()),
                "supported-image-export-formats": "png,jpg,jpeg,bmp",
            }, None, None

        if req_type == "GetAuthRequired":
            data: dict[str, Any] = {"authRequired": self.auth_required}
            if self.auth_required:
                data["challenge"] = self._session_challenge
                data["salt"] = self._session_salt
            return data, None, None

        if req_type == "Authenticate":
            if self._authed.get(ws, False):
                return None, None, "already authenticated"
            if "auth" not in fields:
                return None, None, "missing request parameters"
            auth = str(fields.get("auth", "") or "")
            if not auth:
                return None, None, "auth not specified!"
            if auth != expected_auth(
                self.password, self._session_salt, self._session_challenge
            ):
                # ⚠️ 认证失败**不关连接**，只回错误帧
                return None, None, "Authentication Failed."
            self._authed[ws] = True
            return None, None, None

        if req_type == "SetHeartbeat":
            return {"enable": bool(fields.get("enable", False))}, None, None

        if req_type == "GetStats":
            return {
                "stats": {
                    "fps": 60.0,
                    "render-total-frames": 3600,
                    "render-missed-frames": 1,
                    "output-total-frames": 1800,
                    "output-skipped-frames": 3,
                    "average-frame-time": 3.2,
                    "cpu-usage": 7.5,
                    "memory-usage": 512.0,
                    "free-disk-space": 204_800.0,
                }
            }, None, None

        if req_type == "GetVideoInfo":
            return {
                "baseWidth": 1920,
                "baseHeight": 1080,
                "outputWidth": 1920,
                "outputHeight": 1080,
                "fps": 60.0,
                "videoFormat": "VIDEO_FORMAT_NV12",
                "colorSpace": "VIDEO_CS_709",
                "colorRange": "VIDEO_RANGE_PARTIAL",
                "scaleType": "VIDEO_SCALE_BICUBIC",
            }, None, None

        # ---- Scenes ----
        if req_type == "GetSceneList":
            # 场景**没有 sceneIndex**；数组顺序 = 界面顺序（从上到下）
            scenes = [
                {"name": name, "sources": self._scene_sources(name)}
                for name in st.scenes
            ]
            return {"current-scene": st.current_scene, "scenes": scenes}, None, None

        if req_type == "SetCurrentScene":
            name = str(fields.get("scene-name", "") or "")
            if name not in st.scenes:
                return None, None, "requested scene does not exist"
            st.current_scene = name
            return None, [("SwitchScenes", {"scene-name": name, "sources": self._scene_sources(name)})], None

        if req_type == "GetCurrentScene":
            return {
                "name": st.current_scene,
                "sources": self._scene_sources(st.current_scene),
            }, None, None

        if req_type == "CreateScene":
            name = str(fields.get("sceneName", "") or "")
            if not name:
                return None, None, "missing request parameters"
            if name in st.scenes:
                return None, None, "scene with this name already exists"
            st.scenes.append(name)
            st.items[name] = []
            st.visible[name] = {}
            return None, [
                (
                    "SourceCreated",
                    {
                        "sourceName": name,
                        "sourceType": "scene",
                        "sourceKind": "scene",
                        "sourceSettings": {},
                    },
                )
            ], None

        if req_type == "SetSourceName":
            old = str(fields.get("sourceName", "") or "")
            new = str(fields.get("newName", "") or "")
            if not old or not new:
                return None, None, "missing request parameters"
            if new in st.scenes or new in st.audio_kinds:
                return None, None, "a source with that name already exists"
            source_type = "unknown"
            if old in st.scenes:
                st.scenes[st.scenes.index(old)] = new
                st.items[new] = st.items.pop(old, [])
                st.visible[new] = st.visible.pop(old, {})
                if st.current_scene == old:
                    st.current_scene = new
                if st.preview_scene == old:
                    st.preview_scene = new
                source_type = "scene"
            elif old in st.audio_kinds:
                st.audio_kinds[new] = st.audio_kinds.pop(old)
                for table in (st.volumes_db, st.muted, st.monitor_types,
                              st.sync_offset_ns, st.tracks):
                    if old in table:
                        table[new] = table.pop(old)
                source_type = "input"
            else:
                return None, None, "specified source doesn't exist"
            return None, [
                (
                    "SourceRenamed",
                    {"previousName": old, "newName": new, "sourceType": source_type},
                )
            ], None

        # ---- Scene items ----
        if req_type == "GetSceneItemList":
            scene = str(fields.get("sceneName", "") or st.current_scene)
            if scene not in st.items:
                return None, None, "requested scene is invalid or doesnt exist"
            # 只有这四个字段：**没有可见性、没有 index**；自然顺序（底 -> 顶）
            return {
                "sceneName": scene,
                "sceneItems": [
                    {
                        "itemId": int(item["id"]),
                        "sourceKind": str(item["kind"]),
                        "sourceName": str(item["name"]),
                        "sourceType": "input",
                    }
                    for item in st.items[scene]
                ],
            }, None, None

        if req_type == "GetSceneItemProperties":
            scene, item_id, error = self._resolve_item(fields)
            if error:
                return None, None, error
            item = self._find_item(scene, item_id)
            return {
                "name": item["name"],
                "itemId": item_id,
                "visible": bool(st.visible.get(scene, {}).get(item_id, True)),
            }, None, None

        if req_type in ("SetSceneItemProperties", "SetSceneItemRender"):
            if req_type == "SetSceneItemRender":
                if "render" not in fields:
                    return None, None, "missing request parameters"
                if not fields.get("source") and fields.get("item") is None:
                    return None, None, "missing request parameters"
                scene = str(fields.get("scene-name", "") or st.current_scene)
                if fields.get("source"):
                    name = str(fields["source"])
                    item = next(
                        (i for i in st.items.get(scene, []) if i["name"] == name), None
                    )
                    if item is None:
                        return None, None, "specified scene item name doesn't exist"
                    item_id = int(item["id"])
                else:
                    item_id = int(fields["item"])
                    if self._find_item(scene, item_id) is None:
                        return None, None, "specified scene item ID doesn't exist"
            else:
                scene, item_id, error = self._resolve_item(fields)
                if error:
                    return None, None, error

            if "visible" in fields:
                wanted = bool(fields["visible"])
            elif req_type == "SetSceneItemRender":
                wanted = bool(fields["render"])
            else:
                # 只改别的属性，不动可见性
                return None, None, None

            st.visible.setdefault(scene, {})[item_id] = wanted
            item = self._find_item(scene, item_id)
            return None, [
                (
                    "SceneItemVisibilityChanged",
                    {
                        "scene-name": scene,
                        "item-name": str(item["name"]) if item else "",
                        "item-id": item_id,
                        "item-visible": wanted,
                    },
                )
            ], None

        if req_type == "ReorderSceneItems":
            scene = str(fields.get("scene", "") or st.current_scene)
            if scene not in st.items:
                return None, None, "requested scene doesn't exist"
            wanted = fields.get("items")
            if not isinstance(wanted, list):
                return None, None, "sceneItem order not specified"
            by_id = {int(i["id"]): i for i in st.items[scene]}
            ordered: list[dict[str, Any]] = []
            for entry in wanted:
                if not isinstance(entry, dict):
                    continue
                if entry.get("id") in by_id:
                    ordered.append(by_id.pop(int(entry["id"])) )
            ordered.extend(by_id.values())
            st.items[scene] = ordered
            # v4 用 SourceOrderChanged 表达重排，且每项是 {source-name, item-id}
            return None, [
                (
                    "SourceOrderChanged",
                    {
                        "scene-name": scene,
                        "scene-items": [
                            {"source-name": i["name"], "item-id": int(i["id"])}
                            for i in ordered
                        ],
                    },
                )
            ], None

        # ---- Recording ----
        if req_type == "GetRecordingStatus":
            data = {
                "isRecording": st.record_active,
                "isRecordingPaused": st.record_paused,
            }
            # 只有真正在录时才带这两个字段
            if st.record_active:
                data["recordTimecode"] = "00:00:12.000"
                data["recordingFilename"] = "D:/record.mkv"
            return data, None, None

        if req_type == "StartRecording":
            if st.record_active:
                return None, None, "recording already active"
            st.record_active = True
            st.record_paused = False
            return None, [
                ("RecordingStarting", {}),
                ("RecordingStarted", {"recordingFilename": "D:/record.mkv"}),
            ], None

        if req_type == "StopRecording":
            if not st.record_active:
                return None, None, "recording not active"
            st.record_active = False
            st.record_paused = False
            return None, [
                ("RecordingStopping", {"recordingFilename": "D:/record.mkv"}),
                ("RecordingStopped", {"recordingFilename": "D:/record.mkv"}),
            ], None

        if req_type == "StartStopRecording":
            if st.record_active:
                return self._dispatch("StopRecording", {}, ws)
            return self._dispatch("StartRecording", {}, ws)

        if req_type == "PauseRecording":
            if not st.record_active:
                return None, None, "recording is not active"
            if st.record_paused:
                return None, None, "recording already paused"
            st.record_paused = True
            return None, [("RecordingPaused", {})], None

        if req_type == "ResumeRecording":
            if not st.record_active:
                return None, None, "recording is not active"
            if not st.record_paused:
                return None, None, "recording is not paused"
            st.record_paused = False
            return None, [("RecordingResumed", {})], None

        if req_type == "GetRecordingFolder":
            return {"rec-folder": st.record_directory}, None, None

        # ---- Streaming ----
        if req_type == "GetStreamingStatus":
            data = {
                "streaming": st.stream_active,
                "recording": st.record_active,
                "recording-paused": st.record_paused,
                "virtualcam": st.virtualcam_active,
                "preview-only": False,
            }
            if st.stream_active:
                data["stream-timecode"] = "00:00:30.000"
            if st.record_active:
                data["rec-timecode"] = "00:00:12.000"
            if st.virtualcam_active:
                data["virtualcam-timecode"] = "00:00:05.000"
            return data, None, None

        if req_type == "StartStreaming":
            if st.stream_active:
                return None, None, "streaming already active"
            st.stream_active = True
            return None, [("StreamStarting", {"preview-only": False}), ("StreamStarted", {})], None

        if req_type == "StopStreaming":
            if not st.stream_active:
                return None, None, "streaming not active"
            st.stream_active = False
            return None, [("StreamStopping", {"preview-only": False}), ("StreamStopped", {})], None

        if req_type == "StartStopStreaming":
            if st.stream_active:
                return self._dispatch("StopStreaming", {}, ws)
            return self._dispatch("StartStreaming", {}, ws)

        if req_type == "SendCaptions":
            if "text" not in fields:
                return None, None, "missing request parameters"
            # 忠实模拟真实 v4：没推流时 output 为空，**静默成功**（v5 会回 501）
            if st.stream_active:
                st.captions.append(str(fields.get("text", "")))
            return None, None, None

        # ---- Replay / VirtualCam ----
        if req_type == "GetReplayBufferStatus":
            return {"isReplayBufferActive": st.replay_buffer_active}, None, None
        if req_type == "StartReplayBuffer":
            if st.replay_buffer_active:
                return None, None, "replay buffer already active"
            st.replay_buffer_active = True
            return None, [("ReplayStarting", {}), ("ReplayStarted", {})], None
        if req_type == "StopReplayBuffer":
            if not st.replay_buffer_active:
                return None, None, "replay buffer not active"
            st.replay_buffer_active = False
            return None, [("ReplayStopping", {}), ("ReplayStopped", {})], None
        if req_type == "SaveReplayBuffer":
            # v4 没有 ReplayBufferSaved 事件
            return None, None, None

        if req_type == "GetVirtualCamStatus":
            data = {"isVirtualCam": st.virtualcam_active}
            if st.virtualcam_active:
                data["virtualCamTimecode"] = "00:00:05.000"
            return data, None, None
        if req_type == "StartVirtualCam":
            if st.virtualcam_active:
                return None, None, "virtual cam already active"
            st.virtualcam_active = True
            return None, [("VirtualCamStarted", {})], None
        if req_type == "StopVirtualCam":
            if not st.virtualcam_active:
                return None, None, "virtual cam not active"
            st.virtualcam_active = False
            return None, [("VirtualCamStopped", {})], None

        # ---- Transitions ----
        if req_type == "GetTransitionList":
            return {
                "current-transition": st.current_transition,
                "transitions": [{"name": name} for name in st.transitions],
            }, None, None

        if req_type == "GetCurrentTransition":
            data = {"name": st.current_transition}
            # 固定时长的转场（Cut）**不给 duration 字段**
            if st.current_transition != "Cut":
                data["duration"] = st.transition_duration
            return data, None, None

        if req_type == "SetCurrentTransition":
            name = str(fields.get("transition-name", "") or "")
            if name not in st.transitions:
                return None, None, "requested transition does not exist"
            st.current_transition = name
            return None, [("SwitchTransition", {"transition-name": name})], None

        if req_type == "SetTransitionDuration":
            ms = int(fields.get("duration", 0) or 0)
            st.transition_duration = ms
            return None, [("TransitionDurationChanged", {"new-duration": ms})], None

        if req_type == "GetTransitionDuration":
            return {"transition-duration": st.transition_duration}, None, None

        if req_type == "GetTransitionPosition":
            return {"position": st.tbar_position}, None, None

        if req_type in ("SetTBarPosition", "ReleaseTBar"):
            if not st.studio_mode:
                return None, None, "studio mode not enabled"
            if st.current_transition == "Cut":
                return None, None, "current transition doesn't support t-bar control"
            if req_type == "ReleaseTBar":
                st.tbar_transitioning = False
                st.tbar_position = 0.0
                return None, [("TransitionEnd", {"name": st.current_transition, "type": "fade_transition", "duration": st.transition_duration, "to-scene": st.current_scene})], None
            if "position" not in fields:
                return None, None, "missing request parameters"
            position = float(fields["position"])
            if position < 0.0 or position > 1.0:
                return None, None, "position is out of range"
            release = bool(fields.get("release", True))
            st.tbar_position = position
            events: list[tuple[str, dict]] = []
            if not st.tbar_transitioning:
                st.tbar_transitioning = True
                events.append(
                    ("TransitionBegin", {"name": st.current_transition, "type": "fade_transition", "duration": st.transition_duration, "to-scene": st.preview_scene})
                )
            if release and position >= 1.0 - T_BAR_CLAMP:
                st.current_scene = st.preview_scene
                st.tbar_transitioning = False
                st.tbar_position = 0.0
                events.append(
                    ("TransitionEnd", {"name": st.current_transition, "type": "fade_transition", "duration": st.transition_duration, "to-scene": st.current_scene})
                )
            return None, events, None

        # ---- Studio mode ----
        if req_type == "GetStudioModeStatus":
            return {"studio-mode": st.studio_mode}, None, None

        if req_type == "EnableStudioMode":
            if st.studio_mode:
                return None, None, "studio mode already active"
            st.studio_mode = True
            if not st.preview_scene:
                st.preview_scene = st.current_scene
            return None, [("StudioModeSwitched", {"new-state": True})], None

        if req_type == "DisableStudioMode":
            if not st.studio_mode:
                return None, None, "studio mode not active"
            st.studio_mode = False
            return None, [("StudioModeSwitched", {"new-state": False})], None

        if req_type == "ToggleStudioMode":
            st.studio_mode = not st.studio_mode
            return None, [("StudioModeSwitched", {"new-state": st.studio_mode})], None

        if req_type == "GetPreviewScene":
            if not st.studio_mode:
                return None, None, "studio mode not enabled"
            return {
                "name": st.preview_scene,
                "sources": self._scene_sources(st.preview_scene),
            }, None, None

        if req_type == "SetPreviewScene":
            if not st.studio_mode:
                return None, None, "studio mode not enabled"
            name = str(fields.get("scene-name", "") or "")
            if not name:
                return None, None, "missing request parameters"
            if name not in st.scenes:
                return None, None, "requested scene doesn't exist"
            st.preview_scene = name
            return None, [
                ("PreviewSceneChanged", {"scene-name": name, "sources": self._scene_sources(name)})
            ], None

        if req_type == "TransitionToProgram":
            if not st.studio_mode:
                return None, None, "studio mode not enabled"
            transition = fields.get("with-transition")
            if isinstance(transition, dict):
                tname = str(transition.get("name", "") or "")
                if tname:
                    if tname not in st.transitions:
                        return None, None, "specified transition doesn't exist"
                    st.current_transition = tname
                if isinstance(transition.get("duration"), int):
                    st.transition_duration = int(transition["duration"])
            st.current_scene = st.preview_scene
            return None, [
                ("TransitionBegin", {"name": st.current_transition, "type": "fade_transition", "duration": st.transition_duration, "to-scene": st.current_scene}),
                ("SwitchScenes", {"scene-name": st.current_scene, "sources": self._scene_sources(st.current_scene)}),
                ("TransitionEnd", {"name": st.current_transition, "type": "fade_transition", "duration": st.transition_duration, "to-scene": st.current_scene}),
            ], None

        # ---- Sources / audio ----
        if req_type == "GetSourcesList":
            # 真实 v4 把场景/输入/滤镜**全都**列出来
            sources = [
                {"name": name, "typeId": "scene", "type": "scene"} for name in st.scenes
            ]
            sources += [
                {"name": name, "typeId": kind, "type": "input"}
                for name, kind in st.audio_kinds.items()
            ]
            return {"sources": sources}, None, None

        if req_type == "GetSourceTypesList":
            return {
                "types": [
                    {
                        "typeId": kind,
                        "displayName": kind,
                        "type": "input",
                        "caps": {"hasAudio": has_audio, "hasVideo": True},
                    }
                    for kind, has_audio in (
                        ("wasapi_output_capture", True),
                        ("wasapi_input_capture", True),
                        ("ffmpeg_source", True),
                        ("dshow_input", True),
                        ("text_ft2_source", False),
                        ("game_capture", False),
                        ("image_source", False),
                    )
                ]
            }, None, None

        if req_type == "GetSpecialSources":
            return {"desktop-1": "桌面音频", "mic-1": "麦克风"}, None, None

        if req_type == "GetAudioActive":
            name = str(fields.get("sourceName", "") or "")
            return {"audioActive": name in st.volumes_db}, None, None

        if req_type == "GetVolume":
            name = str(fields.get("source", "") or "")
            if name not in st.volumes_db:
                return None, None, "specified source doesn't exist"
            db = st.volumes_db[name]
            use_db = bool(fields.get("useDecibel", False))
            value = db if use_db else 10.0 ** (db / 20.0)
            return {
                "name": name,
                "volume": value,
                "muted": bool(st.muted.get(name, False)),
            }, None, None

        if req_type == "SetVolume":
            name = str(fields.get("source", "") or "")
            if name not in st.volumes_db or "volume" not in fields:
                return None, None, "invalid request parameters"
            volume = float(fields["volume"])
            use_db = bool(fields.get("useDecibel", False))
            if use_db and volume > 26.0:
                return None, None, "invalid request parameters"
            if not use_db and (volume < 0.0 or volume > 20.0):
                return None, None, "invalid request parameters"
            db = volume if use_db else 20 * math.log10(max(volume, 1e-6))
            st.volumes_db[name] = db
            return None, [
                (
                    "SourceVolumeChanged",
                    {
                        "sourceName": name,
                        "volume": 10.0 ** (db / 20.0),
                        "volumeDb": db,
                    },
                )
            ], None

        if req_type == "GetMute":
            name = str(fields.get("source", "") or "")
            if name not in st.muted:
                return None, None, "specified source doesn't exist"
            return {"name": name, "muted": bool(st.muted[name])}, None, None

        if req_type in ("SetMute", "ToggleMute"):
            name = str(fields.get("source", "") or "")
            if name not in st.muted:
                return None, None, "specified source doesn't exist"
            if req_type == "ToggleMute":
                st.muted[name] = not st.muted[name]
            else:
                if "mute" not in fields:
                    return None, None, "missing request parameters"
                st.muted[name] = bool(fields["mute"])
            return None, [("SourceMuteStateChanged", {"sourceName": name, "muted": st.muted[name]})], None

        if req_type == "GetAudioMonitorType":
            name = str(fields.get("sourceName", "") or "")
            return {"monitorType": st.monitor_types.get(name, "none")}, None, None

        if req_type == "SetAudioMonitorType":
            name = str(fields.get("sourceName", "") or "")
            which = str(fields.get("monitorType", "") or "")
            if which not in ("none", "monitorOnly", "monitorAndOutput"):
                return None, None, "invalid monitorType"
            st.monitor_types[name] = which
            return None, None, None

        if req_type == "GetSyncOffset":
            name = str(fields.get("source", "") or "")
            if name not in st.sync_offset_ns:
                return None, None, "specified source doesn't exist"
            return {"name": name, "offset": st.sync_offset_ns[name]}, None, None

        if req_type == "SetSyncOffset":
            name = str(fields.get("source", "") or "")
            if name not in st.sync_offset_ns or "offset" not in fields:
                return None, None, "invalid request parameters"
            st.sync_offset_ns[name] = int(fields["offset"])
            return None, None, None

        if req_type == "GetTracks":
            name = str(fields.get("sourceName", "") or "")
            mask = st.tracks.get(name, 0)
            return {
                "name": name,
                **{f"track{i}": bool(mask & (1 << (i - 1))) for i in range(1, 7)},
            }, None, None

        if req_type == "SetTracks":
            name = str(fields.get("sourceName", "") or "")
            track = int(fields.get("track", 0) or 0)
            if name not in st.tracks or track < 1 or track > 6:
                return None, None, "invalid request parameters"
            mask = st.tracks[name]
            if bool(fields.get("active", False)):
                mask |= 1 << (track - 1)
            else:
                mask &= ~(1 << (track - 1))
            st.tracks[name] = mask
            return None, [
                (
                    "SourceAudioMixersChanged",
                    {
                        "sourceName": name,
                        "mixers": [
                            {"id": i, "enabled": bool(mask & (1 << (i - 1)))}
                            for i in range(1, 7)
                        ],
                        "hexMixersValue": hex(mask),
                    },
                )
            ], None

        # ---- Media ----
        if req_type in ("GetMediaState", "GetMediaDuration", "GetMediaTime"):
            name = str(fields.get("sourceName", "") or "")
            info = st.media.get(name)
            if info is None:
                return None, None, "specified source doesn't exist"
            if req_type == "GetMediaState":
                return {"mediaState": info["state"]}, None, None
            if req_type == "GetMediaDuration":
                return {"mediaDuration": int(info["duration"])}, None, None
            return {"timestamp": int(info["cursor"])}, None, None

        if req_type == "SetMediaTime":
            name = str(fields.get("sourceName", "") or "")
            info = st.media.get(name)
            if info is None:
                return None, None, "specified source doesn't exist"
            info["cursor"] = int(fields.get("timestamp", 0) or 0)
            return None, None, None

        if req_type == "ScrubMedia":
            name = str(fields.get("sourceName", "") or "")
            info = st.media.get(name)
            if info is None:
                return None, None, "specified source doesn't exist"
            info["cursor"] = max(0, int(info["cursor"]) + int(fields.get("timeOffset", 0) or 0))
            return None, None, None

        if req_type == "GetMediaSourcesList":
            return {
                "mediaSources": [
                    {"sourceName": name, "sourceKind": "ffmpeg_source", "mediaState": info["state"]}
                    for name, info in st.media.items()
                ]
            }, None, None

        if req_type in ("PlayPauseMedia", "RestartMedia", "StopMedia", "NextMedia", "PreviousMedia"):
            name = str(fields.get("sourceName", "") or "")
            info = st.media.get(name)
            if info is None:
                return None, None, "specified source doesn't exist"
            if req_type == "PlayPauseMedia":
                if "playPause" in fields:
                    pause = bool(fields["playPause"])
                else:
                    # 省略时**取反**（真实 v4 的读-改-写行为）
                    pause = info["state"] == "playing"
                info["state"] = "paused" if pause else "playing"
                event = "MediaPaused" if pause else "MediaPlaying"
            elif req_type == "RestartMedia":
                info["state"] = "playing"
                info["cursor"] = 0
                event = "MediaRestarted"
            elif req_type == "StopMedia":
                info["state"] = "stopped"
                info["cursor"] = 0
                event = "MediaStopped"
            elif req_type == "NextMedia":
                event = "MediaNext"
            else:
                event = "MediaPrevious"
            return None, [(event, {"sourceName": name, "sourceKind": "ffmpeg_source"})], None

        # ---- Screenshot ----
        if req_type == "TakeSourceScreenshot":
            if not fields.get("embedPictureFormat") and not fields.get("saveToFilePath"):
                return None, None, (
                    "At least 'embedPictureFormat' or 'saveToFilePath' must be specified"
                )
            st.screenshot_count += 1
            name = str(fields.get("sourceName", "") or st.current_scene)
            data: dict[str, Any] = {"sourceName": name}
            if fields.get("embedPictureFormat"):
                fmt = str(fields["embedPictureFormat"])
                fmt = "jpeg" if fmt == "jpg" else fmt
                import base64

                encoded = base64.b64encode(
                    make_png(shade=0x30 + (st.screenshot_count * 7) % 0x60)
                ).decode()
                # v4 回的是**完整 data URI**（v5 只回裸 base64）
                data["img"] = f"data:image/{fmt};base64,{encoded}"
            return data, None, None

        # ---- Collections / profiles ----
        if req_type == "ListSceneCollections":
            return {
                "scene-collections": [{"sc-name": n} for n in st.scene_collections]
            }, None, None
        if req_type == "GetCurrentSceneCollection":
            return {"sc-name": st.current_scene_collection}, None, None
        if req_type == "SetCurrentSceneCollection":
            name = str(fields.get("sc-name", "") or "")
            if name not in st.scene_collections:
                return None, None, "scene collection does not exist"
            st.current_scene_collection = name
            return None, [("SceneCollectionChanged", {"sceneCollection": name})], None
        if req_type == "ListProfiles":
            return {"profiles": [{"profile-name": n} for n in st.profiles]}, None, None
        if req_type == "GetCurrentProfile":
            return {"profile-name": st.current_profile}, None, None
        if req_type == "SetCurrentProfile":
            name = str(fields.get("profile-name", "") or "")
            if name not in st.profiles:
                return None, None, "profile does not exist"
            st.current_profile = name
            return None, [("ProfileChanged", {"profile": name})], None

        return {}, None, None

    # ---------------------------------------------------------------- 内部
    def _scene_sources(self, scene: str) -> list[dict[str, Any]]:
        """v4 的 GetSceneList.sources：**反序**（顶 -> 底），且带 render 等字段。"""
        items = list(self.state.items.get(scene, []))
        items.reverse()
        return [
            {
                "name": str(item["name"]),
                "id": int(item["id"]),
                "type": str(item["kind"]),
                "render": bool(self.state.visible.get(scene, {}).get(item["id"], True)),
                "muted": False,
                "locked": False,
                "x": 0.0,
                "y": 0.0,
                "cx": 1920.0,
                "cy": 1080.0,
                "source_cx": 1920,
                "source_cy": 1080,
                "volume": 1.0,
                "alignment": 5,
            }
            for item in items
        ]

    def _find_item(self, scene: str, item_id: int):
        return next(
            (i for i in self.state.items.get(scene, []) if int(i["id"]) == int(item_id)),
            None,
        )

    def _resolve_item(self, fields: dict):
        """解析 v4 的 `scene-name` + `item`（字符串名或 {name,id} 对象）。"""
        scene = str(fields.get("scene-name", "") or self.state.current_scene)
        if "item" not in fields:
            return scene, -1, "missing request parameters"
        item = fields["item"]
        if isinstance(item, dict):
            if item.get("id") is not None:
                item_id = int(item["id"])
                found = self._find_item(scene, item_id)
                if found is None:
                    return scene, item_id, "specified scene item doesn't exist"
                if item.get("name") and str(item["name"]) != str(found["name"]):
                    return scene, item_id, "specified scene item doesn't exist"
                return scene, item_id, None
            if item.get("name"):
                found = next(
                    (i for i in self.state.items.get(scene, []) if i["name"] == str(item["name"])),
                    None,
                )
                if found is None:
                    return scene, -1, "specified scene item doesn't exist"
                return scene, int(found["id"]), None
            return scene, -1, "missing request parameters"
        if isinstance(item, str):
            found = next(
                (i for i in self.state.items.get(scene, []) if i["name"] == item), None
            )
            if found is None:
                return scene, -1, "specified scene item doesn't exist"
            return scene, int(found["id"]), None
        return scene, -1, "missing request parameters"

    # ---------------------------------------------------------------- 事件
    async def broadcast(self, event_type: str, event_data: dict) -> None:
        """v4 把事件推给**所有**连接（已认证的；authRequired=false 时全部）。"""
        message = json.dumps({"update-type": event_type, **(event_data or {})})
        for ws in list(self._connections):
            if self.auth_required and not self._authed.get(ws, False):
                continue
            try:
                await ws.send(message)
            except Exception:  # noqa: BLE001
                pass

    def broadcast_threadsafe(self, event_type: str, event_data: dict) -> None:
        asyncio.run_coroutine_threadsafe(
            self.broadcast(event_type, event_data), self._loop
        ).result(3)

    def add_input(self, name: str, kind: str = "wasapi_input_capture") -> None:
        self.state.audio_kinds[name] = kind
        self.state.volumes_db.setdefault(name, 0.0)
        self.state.muted.setdefault(name, False)
        self.state.monitor_types.setdefault(name, "none")
        self.state.sync_offset_ns.setdefault(name, 0)
        self.state.tracks.setdefault(name, 1)
        self.broadcast_threadsafe(
            "SourceCreated",
            {"sourceName": name, "sourceType": "input", "sourceKind": kind, "sourceSettings": {}},
        )

    def remove_input(self, name: str) -> None:
        self.state.audio_kinds.pop(name, None)
        self.broadcast_threadsafe(
            "SourceDestroyed", {"sourceName": name, "sourceType": "input", "sourceKind": ""}
        )


if __name__ == "__main__":
    server = FakeObsV4Server()
    port = server.start()
    print(f"假 OBS v4 服务器已启动：127.0.0.1:{port}（密码 {DEFAULT_PASSWORD}）")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()

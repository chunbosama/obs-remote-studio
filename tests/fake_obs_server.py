"""极简 obs-websocket v5 服务器，仅供本地冒烟测试使用。

实现范围：Hello / Identify / Identified / Request / RequestResponse / Event
（不做 RequestBatch、不做真实编码，只维护一份可变状态）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import struct
import threading
import zlib
from typing import Any

import websockets

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

MONITOR_NONE = "OBS_MONITORING_TYPE_NONE"

# obs-websocket 的高频事件位（与 obsws_python.Subs 一致）
HIGH_VOLUME_METERS_BIT = 1 << 16

# G4：OBS 里 T 型推杆的完成/回退判定带 10% 量程的容差
# （T_BAR_CLAMP = T_BAR_PRECISION / 10，见 window-basic-main-transitions.cpp）
T_BAR_CLAMP = 0.1

STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"

DEFAULT_PASSWORD = "testpass"


def expected_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(
        hashlib.sha256(secret + challenge.encode()).digest()
    ).decode()


def make_png(width: int = 480, height: int = 270, shade: int = 0x40) -> bytes:
    """生成一张合法的小 PNG（测试里当缩略图用，避免依赖 Pillow）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + bytes([shade, shade, shade]) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeObsState:
    def __init__(self) -> None:
        self.scenes = [
            {"sceneName": "开场", "sceneIndex": 0},
            {"sceneName": "主画面", "sceneIndex": 1},
            {"sceneName": "结束", "sceneIndex": 2},
        ]
        self.current_scene = "主画面"
        # 注意：obs-websocket 返回的 sceneItems 顺序与 OBS 界面相反
        self.items: dict[str, list[dict[str, Any]]] = {
            "开场": [
                {"sceneItemId": 1, "sourceName": "背景", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "image_source"},
                {"sceneItemId": 2, "sourceName": "字幕", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "text_ft2_source"},
            ],
            "主画面": [
                {"sceneItemId": 10, "sourceName": "摄像头", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "dshow_input"},
                {"sceneItemId": 11, "sourceName": "桌面音频", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "wasapi_output_capture"},
                {"sceneItemId": 12, "sourceName": "游戏画面", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "game_capture"},
            ],
            "结束": [
                {"sceneItemId": 20, "sourceName": "结束画面", "sourceType": "OBS_SOURCE_TYPE_INPUT", "sourceKind": "image_source"},
            ],
        }
        self.enabled: dict[str, dict[int, bool]] = {
            scene: {item["sceneItemId"]: True for item in items}
            for scene, items in self.items.items()
        }
        self.record_active = False
        self.record_paused = False
        self.stream_active = False
        self.record_ms = 0
        self.stream_ms = 0
        # D7/D8：回放缓冲与虚拟摄像机
        self.replay_buffer_active = False
        self.saved_replay_count = 0
        self.virtualcam_active = False
        # G：演播室模式与转场
        self.studio_mode = False
        self.preview_scene = "主画面"
        self.transitions = [
            {"transitionKind": "cut_transition", "transitionName": "Cut", "transitionFixed": True},
            {"transitionKind": "fade_transition", "transitionName": "Fade", "transitionFixed": False},
            {"transitionKind": "swipe_transition", "transitionName": "Swipe", "transitionFixed": False},
        ]
        self.current_transition = "Fade"
        self.transition_duration = 300
        self.screenshot_count = 0
        # L：媒体源。key 是 inputName，只对 media/ffmpeg/vlc 类源有意义
        self.media: dict[str, dict[str, Any]] = {
            "媒体源2": {
                "state": "OBS_MEDIA_STATE_STOPPED",
                "duration": 180_000,
                "cursor": 0,
            }
        }
        self.media_actions: list[tuple[str, str]] = []
        # M：场景集合与配置文件
        self.scene_collections = ["未命名", "直播方案", "录屏方案"]
        self.current_scene_collection = "未命名"
        self.profiles = ["未命名", "高清推流", "低码率"]
        self.current_profile = "未命名"
        self.set_collection_calls = 0
        self.set_profile_calls = 0
        # G4：T 型推杆
        self.tbar_position = 0.0
        self.tbar_calls = 0
        # 推杆正把一次转场"推"在半途 —— 此时真实 OBS 处于 busy 状态
        self.tbar_transitioning = False
        # D17：录制目录（客户端据此算剩余空间）
        self.record_directory = "D:/record"
        # E：音频。混音器里"能出声"的源与纯视频源混在一起，用来验证过滤逻辑
        self.audio_kinds = {
            "桌面音频": "wasapi_output_capture",
            "麦克风": "wasapi_input_capture",
            "媒体源2": "ffmpeg_source",
            "摄像头": "dshow_input",
            "字幕": "text_ft2_source",  # 纯视频，不该出现在混音器
        }
        self.volumes_db = {"桌面音频": 0.0, "麦克风": -6.0, "媒体源2": -12.0, "摄像头": 0.0}
        self.muted = {"桌面音频": False, "麦克风": True, "媒体源2": False, "摄像头": False}
        self.monitor_types = {name: "OBS_MONITORING_TYPE_NONE" for name in self.audio_kinds}
        self.balance = {name: 0.5 for name in self.audio_kinds}
        self.sync_offset = {name: 0 for name in self.audio_kinds}
        self.tracks = {name: 0b000001 for name in self.audio_kinds}
        self.meter_events = 0
        self._meter_task = None


class FakeObsServer:
    """在独立线程里跑 asyncio 服务器。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        password: str = DEFAULT_PASSWORD,
        legacy_transitions: bool = False,
        hide_available_requests: bool = False,
        reject_requests: set[str] | None = None,
        send_meters: bool = True,
        pause_record: bool = True,
        replay_buffer: bool = True,
        virtualcam: bool = True,
        unavailable_requests: set[str] | None = None,
        scene_reorder: bool = True,
        media_inputs: bool = True,
        config_switch: bool = True,
        tbar: bool = True,
    ):
        """legacy_transitions=True：模拟 obs-websocket 5.0，只认 GetTransitionList。
        hide_available_requests=True：GetVersion 不上报 availableRequests，
        客户端只能靠“发了被拒”来发现该用哪个名字。
        reject_requests：即使上报了能力也照样回 204，用来验证客户端的自愈逻辑。
        pause_record / replay_buffer / virtualcam=False：模拟老 OBS 或缺驱动的机器，
        对应请求不出现在 availableRequests 里，用来验证按钮置灰。
        unavailable_requests：请求名**在** availableRequests 里（所以客户端会发），
        但服务端回 604 ResourceNotAvailable —— 对应"有这台机器没配回放缓冲"的真实场景，
        用来验证 604 不会弹错误框。
        """
        self.host = host
        self.port = port
        self.password = password
        self.legacy_transitions = legacy_transitions
        self.hide_available_requests = hide_available_requests
        self.reject_requests = set(reject_requests or ())
        self.send_meters = send_meters
        self.pause_record = pause_record
        self.replay_buffer = replay_buffer
        self.virtualcam = virtualcam
        self.unavailable_requests = set(unavailable_requests or ())
        self.scene_reorder = scene_reorder
        self.media_inputs = media_inputs
        self.config_switch = config_switch
        self.tbar = tbar
        self.state = FakeObsState()
        self.requests: list[str] = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ready = threading.Event()
        self._server: Any = None
        self._stop_future: Any = None
        self._connections: set[Any] = set()
        self._subs: dict[Any, int] = {}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> int:
        self._thread.start()
        self._ready.wait(10)
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
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async with websockets.serve(self._handler, self.host, self.port) as server:
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            self._stop_future = self._loop.create_future()
            self._meter_task = asyncio.ensure_future(self._meter_loop())
            await self._stop_future
            self._meter_task.cancel()

    async def _meter_loop(self) -> None:
        """周期性推送电平表（高频事件），模拟真实 OBS 的 ~20Hz。"""
        while True:
            await asyncio.sleep(0.05)
            if not self.send_meters:
                continue
            self.state.meter_events += 1
            await self.broadcast(
                "InputVolumeMeters",
                {
                    "inputs": [
                        {
                            "inputName": name,
                            "inputUuid": f"uuid-{name}",
                            "inputLevelsMul": [[level, level, level]],
                        }
                        for name, level in self._current_levels().items()
                    ]
                },
            )

    def _current_levels(self) -> dict[str, float]:
        return {
            "桌面音频": 0.62,
            "麦克风": 0.35,
            "媒体源2": 0.18,
        }

    def available_requests(self) -> list[str]:
        """对外声明支持的请求（真实 OBS 由 GetVersion.availableRequests 提供）。"""
        base = [
            "GetVersion",
            "GetSceneList",
            "SetCurrentProgramScene",
            "GetSceneItemList",
            "GetSceneItemEnabled",
            "SetSceneItemEnabled",
            # B5：场景增删改名
            "CreateScene",
            "RemoveScene",
            "SetSceneName",
            "StartRecord",
            "StopRecord",
            "GetRecordStatus",
            "StartStream",
            "StopStream",
            "GetStreamStatus",
            "GetStats",
            "GetVideoSettings",
            "GetSourceScreenshot",
            "GetStudioModeEnabled",
            "SetStudioModeEnabled",
            "SetCurrentPreviewScene",
            "TriggerStudioModeTransition",
            "GetCurrentSceneTransition",
            "SetCurrentSceneTransition",
            "SetCurrentSceneTransitionDuration",
            "GetInputList",
            "GetInputVolume",
            "SetInputVolume",
            "GetInputMute",
            "SetInputMute",
            "ToggleInputMute",
            "GetInputAudioMonitorType",
            "SetInputAudioMonitorType",
            "GetInputAudioBalance",
            "SetInputAudioBalance",
            "GetInputAudioSyncOffset",
            "SetInputAudioSyncOffset",
            "GetInputAudioTracks",
            "SetInputAudioTracks",
        ]
        # 5.0 只有 GetTransitionList；5.1+ 换成 GetSceneTransitionList
        base += ["GetTransitionList" if self.legacy_transitions else "GetSceneTransitionList"]
        # D6/D7/D8：能力缺失时不出现在列表里（真实 OBS 也是这样）
        if self.pause_record:
            base += ["PauseRecord", "ResumeRecord"]
        if self.replay_buffer:
            base += [
                "StartReplayBuffer",
                "StopReplayBuffer",
                "GetReplayBufferStatus",
                "SaveReplayBuffer",
            ]
        if self.virtualcam:
            base += ["StartVirtualcam", "StopVirtualcam", "GetVirtualcamStatus"]
        # B6：SetSceneIndex 在部分 OBS/协议版本里没有，用来验证拖拽的降级
        if self.scene_reorder:
            base.append("SetSceneIndex")
        # L：媒体源控制
        if self.media_inputs:
            base += [
                "GetMediaInputStatus",
                "TriggerMediaInputAction",
                "SetMediaInputCursor",
                "OffsetMediaInputCursor",
            ]
        # M：场景集合与配置文件
        if self.config_switch:
            base += [
                "GetSceneCollectionList",
                "SetCurrentSceneCollection",
                "GetProfileList",
                "SetCurrentProfile",
            ]
        # G4：T 型推杆。注意 obs-websocket 5.x **只有** SetTBarPosition
        if self.tbar:
            base.append("SetTBarPosition")
        base.append("GetRecordDirectory")
        return base

    # ---------------------------------------------------------------- 连接
    async def _handler(self, ws) -> None:
        self._connections.add(ws)
        self._subs[ws] = 0
        salt = "abcdefghijklmnop"
        challenge = "qrstuvwxyz123456"
        await ws.send(
            json.dumps(
                {
                    "op": OP_HELLO,
                    "d": {
                        "obsWebSocketVersion": "5.5.0",
                        "rpcVersion": 1,
                        "authentication": {"challenge": challenge, "salt": salt},
                    },
                }
            )
        )
        subs = 0
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == OP_IDENTIFY:
                    data = msg.get("d", {})
                    self._subs[ws] = int(data.get("eventSubscriptions", 0))
                    auth = data.get("authentication")
                    if auth != expected_auth(self.password, salt, challenge):
                        await ws.close(code=4008, reason="auth failed")
                        return
                    await ws.send(json.dumps({"op": OP_IDENTIFIED, "d": {"negotiatedRpcVersion": 1}}))
                elif op == OP_REQUEST:
                    await self._handle_request(ws, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(ws)
            self._subs.pop(ws, None)

    # ---------------------------------------------------------------- 请求
    async def _handle_request(self, ws, msg) -> None:
        data = msg.get("d", {})
        req_type = data.get("requestType")
        req_id = data.get("requestId")
        payload = data.get("requestData") or {}
        self.requests.append(req_type)

        # 跟真实 OBS 一致：请求类型不存在时返回 204，而不是默默返回空数据。
        if req_type not in self.available_requests() or req_type in self.reject_requests:
            await ws.send(
                json.dumps(
                    {
                        "op": OP_REQUEST_RESPONSE,
                        "d": {
                            "requestType": req_type,
                            "requestId": req_id,
                            "requestStatus": {
                                "result": False,
                                "code": 204,
                                "comment": "Your request type is not valid",
                            },
                        },
                    }
                )
            )
            return

        # 604 ResourceNotAvailable：请求名合法（所以客户端会照发），
        # 但对应的资源在这台机器上当前不可用。
        if req_type in self.unavailable_requests:
            await ws.send(
                json.dumps(
                    {
                        "op": OP_REQUEST_RESPONSE,
                        "d": {
                            "requestType": req_type,
                            "requestId": req_id,
                            "requestStatus": {
                                "result": False,
                                "code": 604,
                                "comment": f"{req_type} resource is not available",
                            },
                        },
                    }
                )
            )
            return

        response_data, event = self._dispatch(req_type, payload)
        # 业务错误：_dispatch 用 "__error__" 哨兵回一个 (code, comment)
        if isinstance(event, tuple) and event and event[0] == "__error__":
            code, comment = event[1]
            await ws.send(
                json.dumps(
                    {
                        "op": OP_REQUEST_RESPONSE,
                        "d": {
                            "requestType": req_type,
                            "requestId": req_id,
                            "requestStatus": {"result": False, "code": code, "comment": comment},
                        },
                    }
                )
            )
            return
        events = [] if event is None else ([event] if isinstance(event, tuple) else list(event))
        await ws.send(
            json.dumps(
                {
                    "op": OP_REQUEST_RESPONSE,
                    "d": {
                        "requestType": req_type,
                        "requestId": req_id,
                        "requestStatus": {"result": True, "code": 100},
                        "responseData": response_data or {},
                    },
                }
            )
        )
        for item in events:
            await self.broadcast(*item)

    def _dispatch(self, req_type: str, payload: dict) -> tuple[dict | None, tuple[str, dict] | None]:
        st = self.state
        if req_type == "GetVersion":
            return {
                "obsVersion": "31.0.0" if not self.legacy_transitions else "28.0.0",
                "obsWebSocketVersion": "5.5.0" if not self.legacy_transitions else "5.0.1",
                "rpcVersion": 1,
                "availableRequests": [] if self.hide_available_requests else self.available_requests(),
                "supportedImageFormats": ["jpeg"],
            }, None
        if req_type == "GetSceneList":
            return {
                "currentProgramSceneName": st.current_scene,
                "currentPreviewSceneName": st.preview_scene if st.studio_mode else st.current_scene,
                "scenes": st.scenes,
            }, None
        if req_type == "SetCurrentProgramScene":
            st.current_scene = payload["sceneName"]
            return None, ("CurrentProgramSceneChanged", {"sceneName": st.current_scene})
        # ---- B5：场景增删改名 ----
        if req_type == "CreateScene":
            name = str(payload["sceneName"])
            if any(s["sceneName"] == name for s in st.scenes):
                return None, ("__error__", (601, f"Scene {name} already exists"))
            index = max((s["sceneIndex"] for s in st.scenes), default=-1) + 1
            st.scenes.append({"sceneName": name, "sceneIndex": index})
            st.items.setdefault(name, [])
            st.enabled.setdefault(name, {})
            return None, (
                "SceneCreated",
                {"sceneName": name, "sceneUuid": f"uuid-{name}", "isGroup": False},
            )
        if req_type == "RemoveScene":
            name = str(payload["sceneName"])
            if not any(s["sceneName"] == name for s in st.scenes):
                return None, ("__error__", (600, f"Scene {name} does not exist"))
            st.scenes = [s for s in st.scenes if s["sceneName"] != name]
            st.items.pop(name, None)
            st.enabled.pop(name, None)
            if st.current_scene == name:
                st.current_scene = st.scenes[0]["sceneName"] if st.scenes else ""
            return None, ("SceneRemoved", {"sceneName": name, "sceneUuid": f"uuid-{name}"})
        if req_type == "SetSceneName":
            old = str(payload["sceneName"])
            new = str(payload["newSceneName"])
            if any(s["sceneName"] == new for s in st.scenes):
                return None, ("__error__", (601, f"Scene {new} already exists"))
            for scene in st.scenes:
                if scene["sceneName"] == old:
                    scene["sceneName"] = new
                    break
            else:
                return None, ("__error__", (600, f"Scene {old} does not exist"))
            st.items[new] = st.items.pop(old, [])
            st.enabled[new] = st.enabled.pop(old, {})
            if st.current_scene == old:
                st.current_scene = new
            if st.preview_scene == old:
                st.preview_scene = new
            return None, (
                "SceneNameChanged",
                {"sceneName": new, "oldSceneName": old, "sceneUuid": f"uuid-{new}"},
            )
        # ---- B6：场景排序 ----
        if req_type == "SetSceneIndex":
            name = str(payload["sceneName"])
            target = int(payload["newIndex"])
            moving = next((s for s in st.scenes if s["sceneName"] == name), None)
            if moving is None:
                return None, ("__error__", (600, f"Scene {name} does not exist"))
            others = [s for s in st.scenes if s["sceneName"] != name]
            target = max(0, min(target, len(others)))
            others.insert(target, moving)
            for position, scene in enumerate(others):
                scene["sceneIndex"] = position
            st.scenes = others
            return None, ("SceneListReindexed", {"scenes": [dict(s) for s in st.scenes]})
        if req_type == "GetSceneItemList":
            return {"sceneItems": st.items.get(payload["sceneName"], [])}, None
        if req_type == "GetSceneItemEnabled":
            scene = payload["sceneName"]
            item_id = payload["sceneItemId"]
            return {"sceneItemEnabled": st.enabled.get(scene, {}).get(item_id, True)}, None
        if req_type == "SetSceneItemEnabled":
            scene = payload["sceneName"]
            item_id = payload["sceneItemId"]
            enabled = bool(payload["sceneItemEnabled"])
            st.enabled.setdefault(scene, {})[item_id] = enabled
            return None, (
                "SceneItemEnableStateChanged",
                {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": enabled},
            )
        if req_type == "StartRecord":
            st.record_active = True
            st.record_paused = False
            return None, (
                "RecordStateChanged",
                {"outputActive": True, "outputState": STARTED, "outputPath": "D:/record.mkv"},
            )
        if req_type == "StopRecord":
            st.record_active = False
            st.record_paused = False
            return None, (
                "RecordStateChanged",
                {"outputActive": False, "outputState": STOPPED, "outputPath": "D:/record.mkv"},
            )
        if req_type == "GetRecordStatus":
            return {
                "outputActive": st.record_active,
                "outputPaused": st.record_paused,
                "outputTimecode": "00:00:12.000",
                "outputDuration": 12_000 if st.record_active else 0,
                "outputBytes": 1_234_567 if st.record_active else 0,
                "outputPath": "D:/record.mkv",
            }, None
        # ---- D6：录制暂停 / 继续 ----
        if req_type == "PauseRecord":
            st.record_paused = True
            return None, (
                "RecordStateChanged",
                {"outputActive": True, "outputState": PAUSED},
            )
        if req_type == "ResumeRecord":
            st.record_paused = False
            return None, (
                "RecordStateChanged",
                {"outputActive": True, "outputState": RESUMED},
            )
        if req_type == "StartStream":
            st.stream_active = True
            return None, (
                "StreamStateChanged",
                {"outputActive": True, "outputState": STARTED},
            )
        if req_type == "StopStream":
            st.stream_active = False
            return None, (
                "StreamStateChanged",
                {"outputActive": False, "outputState": STOPPED},
            )
        if req_type == "GetStreamStatus":
            return {
                "outputActive": st.stream_active,
                "outputReconnecting": False,
                "outputTimecode": "00:00:30.000",
                "outputDuration": 30_000 if st.stream_active else 0,
                "outputCongestion": 0.0,
                "outputBytes": 9_876_543 if st.stream_active else 0,
                "outputSkippedFrames": 3,
                "outputTotalFrames": 1800,
            }, None
        if req_type == "GetStats":
            return {
                "cpuUsage": 7.5,
                "memoryUsage": 512.0,
                "availableDiskSpace": 204_800.0,
                "activeFps": 60.0,
                "averageFrameRenderTime": 3.2,
                "renderSkippedFrames": 1,
                "renderTotalFrames": 3600,
                "outputSkippedFrames": 3,
                "outputTotalFrames": 1800,
            }, None
        # ---- E：音频 ----
        if req_type == "GetInputList":
            return {
                "inputs": [
                    {
                        "inputName": name,
                        "inputKind": kind,
                        "unversionedInputKind": kind,
                    }
                    for name, kind in st.audio_kinds.items()
                ]
            }, None
        if req_type == "GetInputVolume":
            name = payload["inputName"]
            db = st.volumes_db.get(name, 0.0)
            return {
                "inputVolumeMul": 10.0 ** (db / 20.0),
                "inputVolumeDb": db,
            }, None
        if req_type == "SetInputVolume":
            name = payload["inputName"]
            if "inputVolumeDb" in payload:
                st.volumes_db[name] = float(payload["inputVolumeDb"])
            elif "inputVolumeMul" in payload:
                st.volumes_db[name] = 20 * math.log10(max(float(payload["inputVolumeMul"]), 1e-6))
            db = st.volumes_db[name]
            return None, (
                "InputVolumeChanged",
                {
                    "inputName": name,
                    "inputVolumeMul": 10.0 ** (db / 20.0),
                    "inputVolumeDb": db,
                },
            )
        if req_type == "GetInputMute":
            name = payload["inputName"]
            return {"inputMuted": st.muted.get(name, False)}, None
        if req_type in ("SetInputMute", "ToggleInputMute"):
            name = payload["inputName"]
            if req_type == "ToggleInputMute":
                st.muted[name] = not st.muted.get(name, False)
            else:
                st.muted[name] = bool(payload["inputMuted"])
            return None, (
                "InputMuteStateChanged",
                {"inputName": name, "inputMuted": st.muted[name]},
            )
        if req_type == "GetInputAudioMonitorType":
            name = payload["inputName"]
            return {"monitorType": st.monitor_types.get(name, MONITOR_NONE)}, None
        if req_type == "SetInputAudioMonitorType":
            name = payload["inputName"]
            st.monitor_types[name] = payload["monitorType"]
            return {}, None
        if req_type == "GetInputAudioBalance":
            name = payload["inputName"]
            return {"inputAudioBalance": st.balance.get(name, 0.5)}, None
        if req_type == "SetInputAudioBalance":
            name = payload["inputName"]
            st.balance[name] = float(payload["inputAudioBalance"])
            return {}, None
        if req_type == "GetInputAudioSyncOffset":
            name = payload["inputName"]
            return {"inputAudioSyncOffset": st.sync_offset.get(name, 0)}, None
        if req_type == "SetInputAudioSyncOffset":
            name = payload["inputName"]
            st.sync_offset[name] = int(payload["inputAudioSyncOffset"])
            return {}, None
        if req_type == "GetInputAudioTracks":
            name = payload["inputName"]
            mask = st.tracks.get(name, 1)
            return {
                "inputAudioTracks": {
                    str(index): bool(mask & (1 << (index - 1))) for index in range(1, 7)
                }
            }, None
        if req_type == "SetInputAudioTracks":
            name = payload["inputName"]
            mask = 0
            for key, value in (payload.get("inputAudioTracks") or {}).items():
                if value:
                    mask |= 1 << (int(key) - 1)
            st.tracks[name] = mask
            return {}, None

        # ---- F2：缩略图 ----
        if req_type == "GetSourceScreenshot":
            st.screenshot_count += 1
            shade = 0x30 + (st.screenshot_count * 7) % 0x60
            # 尺寸贴近真实截图（OBS 端按请求的 imageWidth 出图），
            # 太小的图会让客户端的"缩放百分比"看起来离谱
            encoded = base64.b64encode(make_png(shade=shade)).decode()
            return {"imageData": f"data:image/png;base64,{encoded}"}, None

        # ---- G1/G2：转场 ----
        if req_type in ("GetTransitionList", "GetSceneTransitionList"):
            if req_type == "GetSceneTransitionList":
                # 5.1+ 一次返回列表与当前转场
                return {
                    "currentSceneTransitionKind": st.current_transition.lower() + "_transition",
                    "currentSceneTransitionName": st.current_transition,
                    "transitions": st.transitions,
                }, None
            return {"transitions": st.transitions}, None
        if req_type == "GetCurrentSceneTransition":
            configurable = not next(
                (t.get("transitionFixed", False) for t in st.transitions
                 if t.get("transitionName") == st.current_transition),
                False,
            )
            return {
                "transitionName": st.current_transition,
                "transitionKind": st.current_transition.lower() + "_transition",
                "transitionConfigurable": configurable,
                "transitionFixed": not configurable,
                "transitionDuration": st.transition_duration,
            }, None
        if req_type == "SetCurrentSceneTransition":
            st.current_transition = payload["transitionName"]
            return None, (
                "CurrentSceneTransitionChanged",
                {"transitionName": st.current_transition},
            )
        if req_type == "SetCurrentSceneTransitionDuration":
            st.transition_duration = int(payload["transitionDuration"])
            return None, (
                "CurrentSceneTransitionDurationChanged",
                {"transitionDuration": st.transition_duration},
            )

        # ---- G3：演播室模式 ----
        if req_type == "GetStudioModeEnabled":
            return {"studioModeEnabled": st.studio_mode}, None
        if req_type == "SetStudioModeEnabled":
            st.studio_mode = bool(payload["studioModeEnabled"])
            if st.studio_mode and not st.preview_scene:
                st.preview_scene = st.current_scene
            return None, (
                "StudioModeStateChanged",
                {"studioModeEnabled": st.studio_mode},
            )
        if req_type == "SetCurrentPreviewScene":
            st.preview_scene = payload["sceneName"]
            return None, (
                "CurrentPreviewSceneChanged",
                {"sceneName": st.preview_scene},
            )
        if req_type == "TriggerStudioModeTransition":
            st.current_scene = st.preview_scene
            return None, [
                ("SceneTransitionStarted", {"transitionName": st.current_transition}),
                ("CurrentProgramSceneChanged", {"sceneName": st.current_scene}),
                ("SceneTransitionEnded", {"transitionName": st.current_transition}),
            ]

        # ---- D7：回放缓冲区 ----
        if req_type == "StartReplayBuffer":
            st.replay_buffer_active = True
            return None, (
                "ReplayBufferStateChanged",
                {"outputActive": True, "outputState": STARTED},
            )
        if req_type == "StopReplayBuffer":
            st.replay_buffer_active = False
            return None, (
                "ReplayBufferStateChanged",
                {"outputActive": False, "outputState": STOPPED},
            )
        if req_type == "GetReplayBufferStatus":
            return {"outputActive": st.replay_buffer_active}, None
        if req_type == "SaveReplayBuffer":
            st.saved_replay_count += 1
            return None, (
                "ReplayBufferSaved",
                {"savedReplayPath": f"D:/replay{st.saved_replay_count}.mkv"},
            )

        # ---- D8：虚拟摄像机 ----
        if req_type == "StartVirtualcam":
            st.virtualcam_active = True
            return None, (
                "VirtualcamStateChanged",
                {"outputActive": True, "outputState": STARTED},
            )
        if req_type == "StopVirtualcam":
            st.virtualcam_active = False
            return None, (
                "VirtualcamStateChanged",
                {"outputActive": False, "outputState": STOPPED},
            )
        if req_type == "GetVirtualcamStatus":
            return {"outputActive": st.virtualcam_active}, None

        # ---- G4：T 型推杆 ----
        # 照搬 OBS 的真实语义（window-basic-main-transitions.cpp）：
        #   tBar 是**水平**滑块，值越大越接近完成（0 = 预览态，最大 = 已切到输出）；
        #   TBarChanged → obs_transition_set_manual_time(value / 1024)，所以推满即完成；
        #   TBarReleased → 距最大端 <= T_BAR_CLAMP(10% 量程) 完成；
        #                  距 0 端 <= T_BAR_CLAMP 回退；中间则**什么都不做**（转场保持挂起）。
        #   结束时 OBS 会把 tBar 归零。
        if req_type == "SetTBarPosition":
            position = max(0.0, min(float(payload["position"]), 1.0))
            release = bool(payload.get("release", False))
            st.tbar_position = position
            st.tbar_calls += 1
            events: list[tuple[str, dict]] = []
            if not release:
                if not st.tbar_transitioning:
                    st.tbar_transitioning = True
                    events.append(
                        ("SceneTransitionStarted",
                         {"transitionName": st.current_transition})
                    )
                if position >= 1.0 - 1e-6:
                    # 推满 → 手动时间到 1.0，转场就地完成
                    st.current_scene = st.preview_scene
                    st.tbar_transitioning = False
                    st.tbar_position = 0.0
                    events.append(
                        ("SceneTransitionEnded",
                         {"transitionName": st.current_transition})
                    )
            else:
                if position >= 1.0 - T_BAR_CLAMP:
                    st.current_scene = st.preview_scene
                    st.tbar_transitioning = False
                    st.tbar_position = 0.0
                    events.append(
                        ("SceneTransitionEnded",
                         {"transitionName": st.current_transition})
                    )
                elif position <= T_BAR_CLAMP:
                    st.tbar_transitioning = False
                    st.tbar_position = 0.0
                    events.append(
                        ("SceneTransitionEnded",
                         {"transitionName": st.current_transition})
                    )
                # 中间松手：OBS 两个分支都不进，转场保持挂起、位置不动
            return None, events

        # ---- L：媒体源 ----
        if req_type == "GetMediaInputStatus":
            name = str(payload["inputName"])
            info = st.media.get(name)
            if info is None:
                return None, ("__error__", (600, f"Input {name} is not a media source"))
            return {
                "mediaState": info["state"],
                "mediaDuration": info["duration"],
                "mediaCursor": info["cursor"],
            }, None
        if req_type == "TriggerMediaInputAction":
            name = str(payload["inputName"])
            action = str(payload["mediaAction"])
            info = st.media.get(name)
            if info is None:
                return None, ("__error__", (600, f"Input {name} is not a media source"))
            st.media_actions.append((name, action))
            event = None
            if action.endswith("_PLAY"):
                info["state"] = "OBS_MEDIA_STATE_PLAYING"
                event = ("MediaInputPlaybackStarted", {"inputName": name})
            elif action.endswith("_PAUSE"):
                info["state"] = "OBS_MEDIA_STATE_PAUSED"
            elif action.endswith("_STOP"):
                info["state"] = "OBS_MEDIA_STATE_STOPPED"
                info["cursor"] = 0
                event = (
                    "MediaInputActionTriggered",
                    {"inputName": name, "mediaAction": action},
                )
            elif action.endswith("_RESTART"):
                info["state"] = "OBS_MEDIA_STATE_PLAYING"
                info["cursor"] = 0
                event = ("MediaInputPlaybackStarted", {"inputName": name})
            else:
                event = (
                    "MediaInputActionTriggered",
                    {"inputName": name, "mediaAction": action},
                )
            return None, event
        if req_type == "SetMediaInputCursor":
            name = str(payload["inputName"])
            info = st.media.get(name)
            if info is None:
                return None, ("__error__", (600, f"Input {name} is not a media source"))
            info["cursor"] = int(payload["mediaCursor"])
            return None, None
        if req_type == "OffsetMediaInputCursor":
            name = str(payload["inputName"])
            info = st.media.get(name)
            if info is None:
                return None, ("__error__", (600, f"Input {name} is not a media source"))
            info["cursor"] = max(
                0, info["cursor"] + int(payload["mediaCursorOffset"])
            )
            return None, None

        # ---- M：场景集合与配置文件 ----
        if req_type == "GetSceneCollectionList":
            return {
                "currentSceneCollectionName": st.current_scene_collection,
                "sceneCollections": list(st.scene_collections),
            }, None
        if req_type == "SetCurrentSceneCollection":
            name = str(payload["sceneCollectionName"])
            if name not in st.scene_collections:
                return None, ("__error__", (600, f"Scene collection {name} not found"))
            st.current_scene_collection = name
            st.set_collection_calls += 1
            return None, (
                "CurrentSceneCollectionChanged",
                {"sceneCollectionName": name},
            )
        if req_type == "GetProfileList":
            return {
                "currentProfileName": st.current_profile,
                "profiles": list(st.profiles),
            }, None
        if req_type == "SetCurrentProfile":
            name = str(payload["profileName"])
            if name not in st.profiles:
                return None, ("__error__", (600, f"Profile {name} not found"))
            st.current_profile = name
            st.set_profile_calls += 1
            return None, ("CurrentProfileChanged", {"profileName": name})

        # ---- D12/D17：录制目录 ----
        if req_type == "GetRecordDirectory":
            return {"recordDirectory": st.record_directory}, None

        if req_type == "GetVideoSettings":
            return {
                "fpsNumerator": 60,
                "fpsDenominator": 1,
                "fpsInteger": 60,
                "baseWidth": 1920,
                "baseHeight": 1080,
                "outputWidth": 1920,
                "outputHeight": 1080,
            }, None
        return {}, None

    # ---------------------------------------------------------------- 事件
    async def broadcast(self, event_type: str, event_data: dict) -> None:
        message = json.dumps(
            {"op": OP_EVENT, "d": {"eventType": event_type, "eventData": event_data}}
        )
        # 只发给订阅了事件的连接：真实 OBS 不会对 subs=0 的连接推事件，
        # 否则事件帧会混进该连接的请求/响应流（客户端会解析错乱）。
        # 高频事件必须单独置位才推：真实 OBS 也是这样，
        # 只订阅了 Inputs 类别的连接收不到 InputVolumeMeters。
        needs_meter_bit = event_type == "InputVolumeMeters"
        for ws in list(self._connections):
            subs = self._subs.get(ws, 0)
            if not subs:
                continue
            if needs_meter_bit and not (subs & HIGH_VOLUME_METERS_BIT):
                continue
            try:
                await ws.send(message)
            except Exception:  # noqa: BLE001
                pass

    def add_input(self, name: str, kind: str = "wasapi_input_capture") -> None:
        """新增音频源并广播 InputCreated，用来验证客户端的增量刷新。"""
        self.state.audio_kinds[name] = kind
        self.state.volumes_db.setdefault(name, 0.0)
        self.state.muted.setdefault(name, False)
        self.broadcast_threadsafe(
            "InputCreated",
            {"inputName": name, "inputKind": kind, "unversionedInputKind": kind},
        )

    def remove_input(self, name: str) -> None:
        self.state.audio_kinds.pop(name, None)
        self.broadcast_threadsafe("InputRemoved", {"inputName": name})

    def broadcast_threadsafe(self, event_type: str, event_data: dict) -> None:
        asyncio.run_coroutine_threadsafe(
            self.broadcast(event_type, event_data), self._loop
        ).result(3)


if __name__ == "__main__":
    server = FakeObsServer()
    port = server.start()
    print(f"假 OBS 服务器已启动：127.0.0.1:{port}（密码 {DEFAULT_PASSWORD}）")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()

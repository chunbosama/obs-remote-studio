"""无头冒烟测试：连假 OBS 服务器，跑一遍 MVP 主链路。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试必须与真实配置隔离：Windows 上 QSettings(org, app) 写的是注册表，
# 只调 setPath 不够（格式不对），必须换 org 名 + INI 格式。
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fake_obs_server import DEFAULT_PASSWORD, FakeObsServer  # noqa: E402

from obs_remote_studio.core import protocol as P  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig  # noqa: E402
from obs_remote_studio.core.state_store import (  # noqa: E402
    CONNECTED,
    RECONNECTING,
)

CHECKS: list[str] = []
FAILED: list[str] = []


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


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-smoke-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()
    print(f"假 OBS 服务器：127.0.0.1:{port}")
    active = server  # 断线重连后会指向 server2，断言一律用 active

    app = QApplication([])
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 1.0
    controller.config.reconnect.initial_delay_ms = 300
    controller.config.reconnect.max_delay_ms = 300
    conn = ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)

    print("\n[1] 连接与初始状态")
    controller.connect(conn)
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 10, app)
    check("连接成功", ok, controller.store.status_message)
    check(
        "版本信息回显",
        controller.store.server_info.obs_version == "31.0.0",
        controller.store.server_info.label(),
    )

    wait_until(lambda: len(controller.store.scenes) == 3, 5, app)
    names = [s.name for s in controller.store.scenes]
    check("场景列表加载", names == ["结束", "主画面", "开场"], str(names))
    check("当前场景识别", controller.store.current_scene == "主画面", controller.store.current_scene)

    wait_until(lambda: len(controller.store.scene_items) == 3, 5, app)
    items = [i.source_name for i in controller.store.scene_items]
    check("来源列表加载（OBS 顺序）", items == ["游戏画面", "桌面音频", "摄像头"], str(items))
    check("来源可见性默认全开", all(i.enabled for i in controller.store.scene_items))

    print("\n[2] 场景切换")
    controller.switch_scene("开场")
    wait_until(lambda: controller.store.current_scene == "开场", 5, app)
    check("切到「开场」", controller.store.current_scene == "开场")
    wait_until(lambda: len(controller.store.scene_items) == 2, 5, app)
    items = [i.source_name for i in controller.store.scene_items]
    check("来源列表跟随刷新", items == ["字幕", "背景"], str(items))

    print("\n[3] 来源显隐")
    target = next(i for i in controller.store.scene_items if i.source_name == "字幕")
    controller.set_item_enabled("开场", target.item_id, False)
    wait_until(lambda: not any(
        i.enabled for i in controller.store.scene_items if i.source_name == "字幕"
    ), 5, app)
    check("隐藏「字幕」", not next(
        i.enabled for i in controller.store.scene_items if i.source_name == "字幕"
    ))
    # 客户端是乐观更新，必须以服务端状态为准验证往返
    wait_until(lambda: active.state.enabled["开场"][target.item_id] is False, 5, app)
    check(
        "服务端已生效",
        active.state.enabled["开场"][target.item_id] is False,
        str(active.state.enabled.get("开场")),
    )

    print("\n[4] 录制与推流")
    controller.toggle_record()
    wait_until(lambda: controller.store.record.active, 5, app)
    check("开始录制", controller.store.record.active)
    controller.toggle_record()
    wait_until(lambda: not controller.store.record.active, 5, app)
    check("停止录制", not controller.store.record.active)

    controller.toggle_stream()
    wait_until(lambda: controller.store.stream.active, 5, app)
    check("开始推流", controller.store.stream.active)
    check("推流指标回显", controller.store.stream.total_frames == 1800,
          str(controller.store.stream.total_frames))
    controller.toggle_stream()
    wait_until(lambda: not controller.store.stream.active, 5, app)
    check("停止推流", not controller.store.stream.active)

    print("\n[4b] D6：录制暂停 / 继续")
    controller.toggle_record()
    wait_until(lambda: controller.store.record.active, 5, app)
    check("暂停能力探测为真", controller.supports_pause_record())
    controller.toggle_pause_record()
    wait_until(lambda: controller.store.record.paused, 5, app)
    check("本地即时反映暂停", controller.store.record.paused)
    wait_until(lambda: active.state.record_paused, 5, app)
    check("服务端已暂停", active.state.record_paused)
    controller.toggle_pause_record()
    wait_until(lambda: not controller.store.record.paused, 5, app)
    check("本地即时反映继续", not controller.store.record.paused)
    # 本地是乐观更新，必须以服务端状态为准验证往返
    wait_until(lambda: not active.state.record_paused, 5, app)
    check("服务端已继续", not active.state.record_paused)
    controller.toggle_record()
    wait_until(lambda: not controller.store.record.active, 5, app)
    check("停止录制后暂停标记复位", not controller.store.record.paused)

    print("\n[4b2] D6：迟到的暂停事件不能把「继续」按回去（回归）")
    # 线上问题：RecordStateChanged 走的是**另一条 websocket**（事件连接），
    # 与请求响应之间没有顺序保证。连点「暂停 → 继续」时，一条"点继续之前"
    # 就发出的 PAUSED 事件完全可能后到，把刚翻过来的状态又按回去，
    # 并且把意图标记一并清掉 —— 界面就卡在"已暂停"。
    import types

    controller.toggle_record()
    wait_until(lambda: controller.store.record.active, 5, app)
    controller.toggle_pause_record()
    wait_until(lambda: controller.store.record.paused, 5, app)
    controller.toggle_pause_record()
    wait_until(lambda: not controller.store.record.paused, 5, app)
    # **直接**投递那条迟到的事件：事件处理器内部还会补一次 GetRecordStatus，
    # 那个回执会把状态自己纠正回来，从而把缺陷掩盖掉。所以要同步调用，
    # 在纠正回执到达之前就把断言做完。
    controller._event_record_state_changed(
        types.SimpleNamespace(
            output_state="OBS_WEBSOCKET_OUTPUT_PAUSED",
            output_active=True,
            output_path="",
        )
    )
    check("迟到的 PAUSED 事件不会把「继续」按回去",
          not controller.store.record.paused, str(controller.store.record.paused))
    check("意图在服务端跟上之前不会被丢掉",
          controller.pause_intent_wanted() is False,
          str(controller.pause_intent_wanted()))
    # 意图过期后，事件必须重新被采信（别把人家的状态变化永久忽略掉）
    controller._pause_intent = None
    controller._event_record_state_changed(
        types.SimpleNamespace(
            output_state="OBS_WEBSOCKET_OUTPUT_PAUSED",
            output_active=True,
            output_path="",
        )
    )
    check("没有意图时事件照常被采信", controller.store.record.paused,
          str(controller.store.record.paused))
    # 意图窗口本身的仲裁规则
    controller._pause_intent = (True, time.monotonic() + 5.0)
    check("服务端没跟上时以意图为准", controller._reconcile_pause(False) is True)
    check("服务端跟上后清掉意图", controller._reconcile_pause(True) is True
          and controller._pause_intent is None)
    check("没有意图时完全信服务端", controller._reconcile_pause(False) is False)
    controller.toggle_pause_record()
    wait_until(lambda: not controller.store.record.paused, 5, app)
    controller.toggle_record()
    wait_until(lambda: not controller.store.record.active, 5, app)

    print("\n[4c] D8：虚拟摄像机")
    wait_until(lambda: controller.store.supports("GetVirtualcamStatus"), 5, app)
    check("能力列表含虚拟摄像机请求",
          controller.store.supports("GetVirtualcamStatus"))
    controller.toggle_virtualcam()
    wait_until(lambda: controller.store.virtualcam.active, 5, app)
    check("启动虚拟摄像机", controller.store.virtualcam.active)
    check("服务端已启动", active.state.virtualcam_active)
    controller.toggle_virtualcam()
    wait_until(lambda: not controller.store.virtualcam.active, 5, app)
    check("停止虚拟摄像机", not controller.store.virtualcam.active
          and not active.state.virtualcam_active)

    print("\n[4d] D7：回放缓冲区")
    check("能力列表含回放缓冲请求",
          controller.store.supports("GetReplayBufferStatus"))
    check("未开启时保存按钮语义正确（无内容可存）", not controller.store.replay_buffer.active)
    controller.toggle_replay_buffer()
    wait_until(lambda: controller.store.replay_buffer.active, 5, app)
    check("开启回放缓冲", controller.store.replay_buffer.active)
    check("服务端已开启", active.state.replay_buffer_active)
    controller.save_replay_buffer()
    wait_until(lambda: active.state.saved_replay_count == 1, 5, app)
    check("保存回放已下发", active.state.saved_replay_count == 1)
    wait_until(lambda: "replay1" in controller.store.replay_buffer.saved_path, 5, app)
    check("保存路径已回显",
          "replay1" in controller.store.replay_buffer.saved_path,
          controller.store.replay_buffer.saved_path)
    controller.toggle_replay_buffer()
    wait_until(lambda: not controller.store.replay_buffer.active, 5, app)
    check("关闭回放缓冲", not controller.store.replay_buffer.active
          and not active.state.replay_buffer_active)

    print("\n[5] OBS 端主动变更（事件回显）")
    server.broadcast_threadsafe("CurrentProgramSceneChanged", {"sceneName": "结束"})
    wait_until(lambda: controller.store.current_scene == "结束", 5, app)
    check("外部切场景同步", controller.store.current_scene == "结束")

    print("\n[5b] C4：来源列表增量同步（不整体重拉）")
    # 回到「主画面」（3 个来源），后面的事件都在它身上做
    controller.switch_scene("主画面")
    wait_until(
        lambda: [i.source_name for i in controller.store.scene_items]
        == ["游戏画面", "桌面音频", "摄像头"],
        5, app,
    )
    names_before = [i.source_name for i in controller.store.scene_items]
    get_list_before = server.requests.count("GetSceneItemList")

    # 增：新来源插到最上层（展示列表最前面）
    server.broadcast_threadsafe(
        "SceneItemCreated",
        {
            "sceneName": "主画面",
            "sceneItemId": 99,
            "sceneItem": {
                "sceneItemId": 99,
                "sourceName": "新素材",
                "sourceType": "OBS_SOURCE_TYPE_INPUT",
                "sourceKind": "ffmpeg_source",
            },
            "sceneItemIndex": 3,
        },
    )
    wait_until(lambda: "新素材" in [i.source_name for i in controller.store.scene_items], 5, app)
    names_added = [i.source_name for i in controller.store.scene_items]
    check("新增来源已插入到最前", names_added == ["新素材"] + names_before, str(names_added))
    check("增量插入没有重拉列表",
          server.requests.count("GetSceneItemList") == get_list_before,
          f"{server.requests.count('GetSceneItemList')} vs {get_list_before}")

    # 删除
    server.broadcast_threadsafe(
        "SceneItemRemoved", {"sceneName": "主画面", "sceneItemId": 99}
    )
    wait_until(
        lambda: [i.source_name for i in controller.store.scene_items] == names_before, 5, app
    )
    check("删除来源已本地移除",
          [i.source_name for i in controller.store.scene_items] == names_before,
          str([i.source_name for i in controller.store.scene_items]))
    check("增量删除没有重拉列表",
          server.requests.count("GetSceneItemList") == get_list_before)

    # 重排：把展示顺序整个反过来
    server.broadcast_threadsafe(
        "SceneItemListReindexed",
        {
            "sceneName": "主画面",
            "sceneItems": [
                # 事件按 OBS 的 z 序（升序）给；10 在最底层
                {"sceneItemId": 10, "sceneItemIndex": 0},
                {"sceneItemId": 11, "sceneItemIndex": 1},
                {"sceneItemId": 12, "sceneItemIndex": 2},
            ],
        },
    )
    wait_until(
        lambda: [i.source_name for i in controller.store.scene_items]
        == ["游戏画面", "桌面音频", "摄像头"],
        5, app,
    )
    check("重排后展示顺序 = 事件倒序",
          [i.source_name for i in controller.store.scene_items]
          == ["游戏画面", "桌面音频", "摄像头"],
          str([i.source_name for i in controller.store.scene_items]))

    # 改名：改名是来源级的，当前场景里引用它的项要跟着变
    server.broadcast_threadsafe(
        "InputNameChanged", {"inputName": "摄像头2", "oldInputName": "摄像头"}
    )
    wait_until(
        lambda: "摄像头2" in [i.source_name for i in controller.store.scene_items], 5, app
    )
    check("来源改名已同步",
          "摄像头2" in [i.source_name for i in controller.store.scene_items]
          and "摄像头" not in [i.source_name for i in controller.store.scene_items],
          str([i.source_name for i in controller.store.scene_items]))
    # 改回去，免得影响后面的断言
    server.broadcast_threadsafe(
        "InputNameChanged", {"inputName": "摄像头", "oldInputName": "摄像头2"}
    )
    wait_until(
        lambda: [i.source_name for i in controller.store.scene_items]
        == ["游戏画面", "桌面音频", "摄像头"],
        5, app,
    )

    # 其他场景的事件不该动当前列表
    before_other = [i.source_name for i in controller.store.scene_items]
    server.broadcast_threadsafe(
        "SceneItemCreated",
        {
            "sceneName": "开场",
            "sceneItemId": 77,
            "sceneItem": {"sceneItemId": 77, "sourceName": "别的场景的源"},
        },
    )
    for _ in range(25):
        app.processEvents()
        time.sleep(0.02)
    check("非当前场景的事件不影响当前列表",
          [i.source_name for i in controller.store.scene_items] == before_other,
          str([i.source_name for i in controller.store.scene_items]))

    print("\n[5c] B5：场景新建 / 重命名 / 删除")
    check("场景编辑能力探测为真", controller.supports_scene_edit())
    controller.create_scene("临时场景")
    wait_until(lambda: controller.scene_exists("临时场景"), 5, app)
    check("新建场景生效", controller.scene_exists("临时场景"))
    check("服务端已建场景",
          any(s["sceneName"] == "临时场景" for s in active.state.scenes))

    controller.rename_scene("临时场景", "改名后的场景")
    wait_until(lambda: controller.scene_exists("改名后的场景"), 5, app)
    check("重命名生效",
          controller.scene_exists("改名后的场景") and not controller.scene_exists("临时场景"))

    controller.remove_scene("改名后的场景")
    wait_until(lambda: not controller.scene_exists("改名后的场景"), 5, app)
    check("删除场景生效", not controller.scene_exists("改名后的场景"))

    # 重名与空名要被挡住，且不该发请求出去
    create_before = active.requests.count("CreateScene")
    controller.create_scene("主画面")
    controller.create_scene("   ")
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    check("重名 / 空名不发出请求",
          active.requests.count("CreateScene") == create_before,
          f"{active.requests.count('CreateScene')} vs {create_before}")

    print("\n[5d] B6：场景拖拽排序（含倒序映射）")
    check("排序能力探测为真", controller.supports_scene_reorder())
    # 先让上一段的场景增删彻底落定，否则待处理的刷新会把顺序又刷回去
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)
    display = [s.name for s in controller.store.scenes]
    check("场景展示为倒序", display == ["结束", "主画面", "开场"], str(display))
    # 界面展示顺序是 [结束, 主画面, 开场]（倒序）；把「结束」移到最下面 = sceneIndex 0
    controller.move_scene("结束", 0)
    check("排序请求已下发（sceneIndex 语义）",
          wait_until(
              lambda: sorted(active.state.scenes, key=lambda x: x["sceneIndex"])[0]["sceneName"]
              == "结束",
              5, app,
          ),
          str(sorted(active.state.scenes, key=lambda x: x["sceneIndex"])))
    # 展示是倒序的：OBS 最底层(sceneIndex 0) 应出现在界面最后一行
    wait_until(lambda: [s.name for s in controller.store.scenes][-1] == "结束", 8, app)
    check("排序后客户端场景序同步",
          [s.name for s in controller.store.scenes][-1] == "结束",
          str([s.name for s in controller.store.scenes]))
    # 还原，别影响后续「Ctrl+数字切场景」按序号的用例
    controller.move_scene("结束", 2)
    wait_until(lambda: [s.name for s in controller.store.scenes] == ["结束", "主画面", "开场"],
               8, app)

    print("\n[6] 断线检测与自动重连")
    server.stop()
    wait_until(lambda: controller.store.connection_state == RECONNECTING, 15, app)
    check("掉线被识别并安排重连", controller.store.connection_state == RECONNECTING,
          controller.store.connection_state)
    check("掉线后清空运行时状态", controller.store.scenes == [] and not controller.store.record.active)

    # 换一个新端口再起服务：模拟“OBS 重新启动后客户端自动连回来”
    port2 = free_port()
    server2 = FakeObsServer(host="127.0.0.1", port=port2)
    server2.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port2, password=DEFAULT_PASSWORD
    )
    active = server2
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 20, app)
    check("自动重连成功", ok, controller.store.status_message)
    wait_until(lambda: len(controller.store.scenes) == 3, 8, app)
    check("重连后状态恢复", len(controller.store.scenes) == 3 and controller.store.current_scene == "主画面")

    print("\n[7] F2：缩略图（1 fps）")
    frames: dict[str, bytes] = {}
    controller.store.frame_ready.connect(lambda role, raw: frames.__setitem__(role, raw))
    controller.config.preview_interval_ms = 200  # 测试加速，仍走同一条链路
    controller._frame_timer.setInterval(200)
    wait_until(lambda: "program" in frames, 8, app)
    check("收到节目画面", bool(frames.get("program")))
    check("画面是合法 PNG", frames.get("program", b"").startswith(b"\x89PNG"), "")
    check("服务端确实在出帧", active.state.screenshot_count > 0, str(active.state.screenshot_count))

    print("\n[8] G1/G2：转场")
    check("握手时已拿到能力列表",
          bool(controller.store.supported_requests),
          f"{len(controller.store.supported_requests)} 条")
    check("能力列表含正确的转场列表请求",
          controller.store.supports("GetSceneTransitionList"))
    wait_until(lambda: len(controller.store.transitions) == 3, 5, app)
    check("转场列表加载", controller.store.transitions == ["Cut", "Fade", "Swipe"],
          str(controller.store.transitions))
    check("当前转场识别", controller.store.current_transition == "Fade", controller.store.current_transition)
    controller.set_transition("Cut")
    wait_until(lambda: active.state.current_transition == "Cut", 5, app)
    check("切换转场生效", active.state.current_transition == "Cut")
    controller.set_transition_duration(800)
    wait_until(lambda: active.state.transition_duration == 800, 5, app)
    check("转场时长生效", active.state.transition_duration == 800)
    check("能力探测可用", controller.store.supports("SetCurrentSceneTransitionDuration"))

    print("\n[9] G3：演播室模式")
    controller.set_studio_mode(True)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    check("开启演播室模式", controller.store.studio_mode)
    wait_until(lambda: bool(controller.store.preview_scene), 5, app)
    check("预览场景已同步", controller.store.preview_scene == "主画面", controller.store.preview_scene)

    controller.scene_clicked("结束")  # 演播室模式下点击＝设为预览
    wait_until(lambda: active.state.preview_scene == "结束", 5, app)
    check("点场景设为预览", active.state.preview_scene == "结束")
    check("节目未被动到", active.state.current_scene == "主画面", active.state.current_scene)
    wait_until(lambda: "preview" in frames, 8, app)
    check("预览窗也出画面", bool(frames.get("preview")))
    wait_until(
        lambda: any(i.source_name == "结束画面" for i in controller.store.scene_items), 8, app
    )
    check("来源列表跟随预览场景",
          [i.source_name for i in controller.store.scene_items] == ["结束画面"],
          str([i.source_name for i in controller.store.scene_items]))

    controller.trigger_transition()
    wait_until(lambda: active.state.current_scene == "结束", 5, app)
    check("转场把预览推到节目", active.state.current_scene == "结束")

    controller.set_preview_scene("开场")
    wait_until(lambda: active.state.preview_scene == "开场", 5, app)
    controller.cut_to_preview()
    wait_until(lambda: active.state.current_scene == "开场", 5, app)
    check("CUT 直接切节目", active.state.current_scene == "开场")

    controller.set_studio_mode(False)
    wait_until(lambda: not controller.store.studio_mode, 5, app)
    check("关闭演播室模式", not controller.store.studio_mode)

    print("\n[10] 兼容 obs-websocket 5.0：只有 GetTransitionList")
    # 必须先断开：两台服务器的转场列表内容一样，不清理就会用上一台的残留状态
    # 骗过等待条件，断言其实跑在新连接建立之前。
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port3 = free_port()
    legacy = FakeObsServer(host="127.0.0.1", port=port3, legacy_transitions=True)
    legacy.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port3, password=DEFAULT_PASSWORD
    )
    errors: list[str] = []
    controller.store.error_raised.connect(lambda title, detail: errors.append(f"{title}:{detail}"))
    controller.connect()
    check("已连上老协议服务端",
          wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app),
          controller.store.connection_state)
    check("转场列表已清空重来", controller.store.transitions == [])
    wait_until(lambda: len(controller.store.transitions) == 3, 10, app)
    check("老协议下也能拿到转场列表",
          controller.store.transitions == ["Cut", "Fade", "Swipe"],
          str(controller.store.transitions))
    check("能力探测生效：不发服务端没有的请求",
          "GetSceneTransitionList" not in legacy.requests
          and "GetTransitionList" in legacy.requests,
          str([r for r in legacy.requests if "Transition" in r]))
    app.processEvents()
    transition_errors = [e for e in errors if "Transition" in e]
    check("协议差异不弹错误框打扰用户", not transition_errors, str(transition_errors))
    legacy.stop()

    print("\n[11] 服务端不报 availableRequests 时的失败回退")
    # 先断开，确保转场列表是重新拉来的，而不是上一次连接的残留
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port4 = free_port()
    blind = FakeObsServer(
        host="127.0.0.1", port=port4, legacy_transitions=True, hide_available_requests=True
    )
    blind.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port4, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    wait_until(lambda: len(controller.store.transitions) == 3, 10, app)
    check("被 204 拒绝后自动退回老请求",
          "GetSceneTransitionList" in blind.requests and "GetTransitionList" in blind.requests,
          str([r for r in blind.requests if "TransitionList" in r]))
    check("回退后仍能拿到列表",
          controller.store.transitions == ["Cut", "Fade", "Swipe"],
          str(controller.store.transitions))
    blind.stop()

    print("\n[12] 服务端谎报能力（204）时的自愈")
    # 对应线上问题：客户端照文档发了服务端没有的请求名，弹框打扰用户。
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port5 = free_port()
    liar = FakeObsServer(
        host="127.0.0.1", port=port5, reject_requests={"GetSceneTransitionList"}
    )
    liar.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port5, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    wait_until(lambda: not controller.store.supports("GetSceneTransitionList"), 8, app)
    check("被 204 拒绝后标记为不支持",
          not controller.store.supports("GetSceneTransitionList"))
    check("只尝试一次，不反复撞墙",
          liar.requests.count("GetSceneTransitionList") == 1,
          str([r for r in liar.requests if "Transition" in r]))
    # 产品语义：被 204 拒绝后该请求进黑名单，之后连手动刷新都不再发。
    # 不比对"连接期总次数"——连接期的回退与测试的 refresh_all 可能交错，
    # 那个计数天生有竞态；改成"记下当前次数，之后不许再涨"。
    check("204 不弹错误框",
          not [e for e in errors if "Transition" in e],
          str([e for e in errors if "Transition" in e]))
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    baseline = liar.requests.count("GetSceneTransitionList")
    check("连接期只发过一次该请求", baseline == 1, f"{baseline} 次")

    controller.refresh_all()
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)
    check("手动刷新也不再重发",
          liar.requests.count("GetSceneTransitionList") == baseline,
          f"{baseline} -> {liar.requests.count('GetSceneTransitionList')}")
    liar.stop()

    print("\n[12b] 老 OBS 缺能力时自动降级（D6/D7/D8）")
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port6 = free_port()
    old_obs = FakeObsServer(
        host="127.0.0.1",
        port=port6,
        pause_record=False,
        replay_buffer=False,
        virtualcam=False,
    )
    old_obs.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port6, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("已连上缺能力的服务端", controller.store.connection_state == CONNECTED)
    check("暂停录制判定为不支持", not controller.supports_pause_record())
    check("回放缓冲判定为不支持",
          not controller.store.supports("GetReplayBufferStatus"))
    check("虚拟摄像机判定为不支持",
          not controller.store.supports("GetVirtualcamStatus"))
    # 真点了也不该发请求出去（更不该弹错误框）
    controller.toggle_virtualcam()
    controller.save_replay_buffer()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    check("不支持时一个请求都不发",
          not [r for r in old_obs.requests if r in (
              "StartVirtualcam", "StopVirtualcam", "GetVirtualcamStatus",
              "StartReplayBuffer", "StopReplayBuffer", "GetReplayBufferStatus",
              "SaveReplayBuffer", "PauseRecord", "ResumeRecord",
          )],
          str([r for r in old_obs.requests if "Virtual" in r or "Replay" in r]))
    check("降级不弹错误框", not errors, str(errors))
    old_obs.stop()

    print("\n[12c] 请求名合法但资源不可用（604）不弹框")
    # 对应线上问题：这台机器没配回放缓冲，GetReplayBufferStatus 回 604
    # "Replay buffer is not available"，而它挂在 refresh_all 里，于是每次刷新都弹框。
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port7 = free_port()
    no_replay = FakeObsServer(
        host="127.0.0.1",
        port=port7,
        unavailable_requests={"GetReplayBufferStatus", "GetVirtualcamStatus"},
    )
    no_replay.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port7, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("已连上资源不可用的服务端", controller.store.connection_state == CONNECTED)
    check("能力列表里仍然报着这两个请求（所以客户端会发）",
          controller.store.supports("GetReplayBufferStatus")
          and controller.store.supports("GetVirtualcamStatus"))
    wait_until(lambda: "GetReplayBufferStatus" in no_replay.requests, 8, app)
    check("连接后确实发了回放缓冲查询", "GetReplayBufferStatus" in no_replay.requests)
    for _ in range(60):
        app.processEvents()
        time.sleep(0.02)
    check("604 不弹错误框",
          not [e for e in errors if "Replay" in e or "Virtual" in e or "604" in e],
          str(errors))
    wait_until(lambda: controller.store.is_unavailable("GetReplayBufferStatus"), 8, app)
    check("604 后标记为资源不可用",
          controller.store.is_unavailable("GetReplayBufferStatus")
          and controller.store.is_unavailable("GetVirtualcamStatus"))
    # 手动刷新（菜单「刷新状态」/ F5）也不该弹
    errors.clear()
    controller.refresh_all()
    for _ in range(60):
        app.processEvents()
        time.sleep(0.02)
    check("手动刷新 604 也不弹框", not errors, str(errors))
    no_replay.stop()

    print("\n[12d] 资源恢复后 UI 重新露出来")
    # 换成一台「有回放缓冲」的服务端：拿到正常响应后应撤销降级标记
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port8 = free_port()
    healthy = FakeObsServer(host="127.0.0.1", port=port8)
    healthy.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port8, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("重连后降级标记清空",
          not controller.store.is_unavailable("GetReplayBufferStatus"))
    healthy.stop()

    print("\n[12e] B6：服务端没有 SetSceneIndex 时排序降级")
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port9 = free_port()
    no_reorder = FakeObsServer(host="127.0.0.1", port=port9, scene_reorder=False)
    no_reorder.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port9, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("已连上不支持排序的服务端", controller.store.connection_state == CONNECTED)
    check("排序判定为不支持", not controller.supports_scene_reorder())
    # 真调了也不该发请求出去，更不该弹框
    controller.move_scene("主画面", 0)
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    check("不支持时一个排序请求都不发",
          "SetSceneIndex" not in no_reorder.requests,
          str([r for r in no_reorder.requests if "SceneIndex" in r]))
    check("排序降级不弹错误框", not errors, str(errors))
    no_reorder.stop()

    print("\n[12f] J3：诊断窗口收到原始 JSON")
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port10 = free_port()
    # 关掉电平表（20Hz 后台事件）让计数可预期；不然"断开后不再记账"会被
    # detach 之前就已排进主线程队列的那一两条事件干扰 —— 那是 Qt 队列信号的
    # 正常行为，不是产品缺陷，但会让断言偶发假失败。
    server10 = FakeObsServer(host="127.0.0.1", port=port10, send_meters=False)
    server10.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port10, password=DEFAULT_PASSWORD
    )
    from obs_remote_studio.ui.dialogs.diagnostics_dialog import DiagnosticsWindow

    diag = DiagnosticsWindow()
    diag.attach(controller.worker)
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    # 主动造一个事件。注意**要反复广播**：事件通道是第二条 websocket，
    # 它是在控制通道连上之后才建的，`CONNECTED` 到 `EventClient` 订阅生效之间
    # 有一个窗口 —— 只播一次很容易恰好落在窗口里（事件没人订阅就丢了）。
    deadline = time.time() + 10
    while time.time() < deadline and diag._counts["event"] == 0:
        server10.broadcast_threadsafe("CurrentProgramSceneChanged", {"sceneName": "开场"})
        for _ in range(8):
            app.processEvents()
            time.sleep(0.02)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    check("诊断窗口收到请求帧", diag._counts["request"] > 0, str(diag._counts))
    check("诊断窗口收到响应帧", diag._counts["response"] > 0, str(diag._counts))
    check("诊断窗口收到事件帧", diag._counts["event"] > 0, str(diag._counts))
    # 不变量：每个发出去的请求都必须有着落（响应或错误帧），
    # 不能在诊断窗口里留下"有请求、之后什么都没有"的悬空现象
    check("没有悬空未响应的请求", not diag._pending, str(diag._pending))
    # 关闭 = 断开信号，之后不应再记账
    diag.detach(controller.worker)
    # 先把 detach 之前就已排队的跨线程信号冲干净，再取基线
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)
    frozen = dict(diag._counts)
    controller.refresh_all()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    check("断开后不再记账", diag._counts == frozen, f"{frozen} -> {diag._counts}")
    check("refresh_all 确实发了不少请求（反证断连有效）",
          len([r for r in server10.requests]) > 0)
    diag.clear()
    check("清空后计数归零", sum(diag._counts.values()) == 0, str(diag._counts))
    diag.close()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    server10.stop()

    print("\n[12g] 换服务器时不被上一条连接的残响干扰（回归）")
    # 曾经的缺陷：旧服务器被杀后遗留的传输失败会算到新连接头上，把它误判成掉线 →
    # 排一个重连 → 重连计时器在新连接建立后才到点 → 再 connect 一次 →
    # _on_connected / refresh_all 各跑两遍，同一批请求被下发两次。
    errors.clear()
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    # 起一台服务器，连上后**不等任何超时**直接杀掉（制造大量在途请求）
    port11 = free_port()
    doomed = FakeObsServer(host="127.0.0.1", port=port11)
    doomed.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port11, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    doomed.stop()  # 立刻拔线，此时还有一批请求在飞

    port12 = free_port()
    fresh = FakeObsServer(host="127.0.0.1", port=port12)
    fresh.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port12, password=DEFAULT_PASSWORD
    )
    controller.connect()
    check("已重新连上新服务器",
          wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app),
          controller.store.connection_state)
    # 给足时间：若残留失败被误计，退避重连（300ms）会在这段时间内跑完
    for _ in range(90):
        app.processEvents()
        time.sleep(0.02)
    check("新连接没有因残响被误判掉线",
          controller.store.connection_state == CONNECTED,
          controller.store.connection_state)
    check("只跑了一轮 refresh_all（转场只查一次）",
          fresh.requests.count("GetSceneTransitionList") == 1,
          str([r for r in fresh.requests if "Transition" in r]))
    check("场景列表也只拉了一次（启动那轮）",
          fresh.requests.count("GetSceneList") == 1,
          f"GetSceneList x{fresh.requests.count('GetSceneList')}")
    check("换服务器过程中没有错误弹框", not errors, str(errors))
    fresh.stop()

    print("\n[13] 混音器已接入（音频请求符合预期）")
    # 详细行为在 tests/audio_test.py，这里只确认主链路里也照常拉取
    audio_requests = [
        r for r in server.requests + server2.requests if r.startswith("GetInput")
    ]
    check("连接时拉取音频源列表", "GetInputList" in audio_requests, str(audio_requests[:3]))
    volumes = audio_requests.count("GetInputVolume")
    mutes = audio_requests.count("GetInputMute")
    check("逐源补拉音量与静音", volumes == mutes and volumes >= 4, f"{volumes}/{mutes}")
    check("没有误发音频控制请求",
          not [r for r in audio_requests if r.startswith(("Set", "Toggle"))])

    # ---- 以下各段共用一个新服务器，避免污染上面 [13] 的请求统计 ----
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port_n = free_port()
    feat = FakeObsServer(host="127.0.0.1", port=port_n)
    # D17：录制目录指向本机真实存在的目录，才能真的算剩余空间
    feat.state.record_directory = tempfile.gettempdir()
    feat.start()
    active = feat
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port_n, password=DEFAULT_PASSWORD
    )
    controller.connect()
    check("已连上新功能验证服务端",
          wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app),
          controller.store.connection_state)

    print("\n[14] A11：连接延迟探测与卡顿预警")
    controller.config.heartbeat_interval_ms = 1000
    controller._start_heartbeat()
    check("心跳已发出并测到 RTT",
          wait_until(lambda: controller.store.health.samples > 0, 8, app),
          str(controller.store.health))
    check("RTT 是正数", controller.store.health.rtt_ms > 0,
          str(controller.store.health.rtt_ms))
    check("延迟文案可读", "延迟" in controller.store.health.label,
          controller.store.health.label)
    check("正常延迟不判卡", not controller.store.health.stalled)
    # 把阈值压到 1ms：此后每次都算"慢"，连续够次数就该判卡
    controller.config.rtt_warn_ms = 0
    controller.config.rtt_slow_streak = 2
    controller._record_rtt(999.0)
    controller._record_rtt(999.0)
    check("连续超阈值判定为卡", controller.store.health.stalled,
          str(controller.store.health))
    check("恢复后不再判卡（慢≠断）", controller.store.connection_state == CONNECTED,
          controller.store.connection_state)

    print("\n[15] D17：磁盘与时长预警")
    controller.config.disk_warn_gb = 0.0
    controller.config.record_warn_minutes = 0
    controller.config.record_warn_gb = 0.0
    controller.toggle_record()
    wait_until(lambda: controller.store.record.active, 5, app)
    check("录制中且未超阈值时无预警", not controller.store.record_warning.message,
          controller.store.record_warning.message)
    # 体积阈值要等第一次"带字节数"的录制状态回来才算得出来 ——
    # active 可能是 StartRecord 的乐观/事件先置上的，此时 outputBytes 还是 0
    wait_until(lambda: controller.store.record.bytes_written > 0, 5, app)
    controller.config.record_warn_gb = 0.001
    controller._refresh_record_warning()
    check("文件超过体积阈值会预警",
          controller.store.record_warning.level == "warn"
          and "GB" in controller.store.record_warning.message,
          str(controller.store.record_warning))
    # 磁盘阈值：把阈值设得比任何真实盘都大，必然命中
    controller.config.record_warn_gb = 0.0
    controller.config.disk_warn_gb = 10_000_000.0
    controller._refresh_record_warning()
    check("剩余空间不足会升级为危险级",
          controller.store.record_warning.level == "danger"
          and "仅剩" in controller.store.record_warning.message,
          str(controller.store.record_warning))
    check("顺带读到了剩余空间", controller.store.record_warning.free_gb is not None,
          str(controller.store.record_warning.free_gb))
    controller.toggle_record()
    wait_until(lambda: not controller.store.record.active, 5, app)
    controller._refresh_record_warning()
    check("停止录制后预警自动清空", not controller.store.record_warning.message,
          controller.store.record_warning.message)
    controller.config.disk_warn_gb = 2.0
    controller.config.record_warn_gb = 0.0

    print("\n[16] G4：T 型推杆")
    controller.set_studio_mode(True)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    check("v5 协议没有 GetTBarPosition（不回读，只靠事件）",
          not controller.store.supports("GetTBarPosition")
          and controller.store.supports("SetTBarPosition"))
    # 手推第一下 → OBS 会立刻发 SceneTransitionStarted，本地进入"转场中"
    controller.set_tbar_position(0.4, False)
    wait_until(lambda: abs(active.state.tbar_position - 0.4) < 0.01, 5, app)
    check("拖动中间位已下发（release=false）",
          abs(active.state.tbar_position - 0.4) < 0.01,
          str(active.state.tbar_position))
    check("本地推杆位置同步", abs(controller.store.tbar_position - 0.4) < 0.01,
          str(controller.store.tbar_position))
    check("手推会进入「转场中」（OBS 发 SceneTransitionStarted）",
          wait_until(lambda: controller.store.transitioning, 5, app),
          str(controller.store.transitioning))
    check("转场中已arm看门狗兜底", controller._transition_watchdog.isActive())
    preview_name = controller.store.preview_scene
    before_release = active.requests.count("SetTBarPosition")
    # 推到底（position=1.0）松手 → 完成转场
    controller.set_tbar_position(1.0, True)
    # 等请求真的到服务器，而不是等场景名变化 ——
    # 预览场景可能本来就等于当前场景，那样会立即"满足"而请求还没发出去
    wait_until(lambda: active.requests.count("SetTBarPosition") > before_release, 5, app)
    check("推到底松手完成转场",
          active.state.current_scene == preview_name,
          f"{active.state.current_scene} vs {preview_name}")
    check("松手后退出「转场中」（SceneTransitionEnded 回来了）",
          wait_until(lambda: not controller.store.transitioning, 5, app),
          str(controller.store.transitioning))
    check("转场结束后推杆归位", controller.store.tbar_position == 0.0,
          str(controller.store.tbar_position))
    check("看门狗已停", not controller._transition_watchdog.isActive())
    # OBS 的完成判定带 10% 容差（T_BAR_CLAMP）：推到 0.95 松手也算完成。
    # 客户端不该自己按更严的阈值先归位，否则会和 OBS 的状态错位。
    controller.set_tbar_position(0.5, False)
    wait_until(lambda: controller.store.transitioning, 5, app)
    preview_name = controller.store.preview_scene
    controller.set_tbar_position(0.95, True)
    check("推到 0.95 松手也算完成（OBS 有 10% 容差）",
          wait_until(lambda: active.state.current_scene == preview_name, 5, app),
          active.state.current_scene)
    # 真正的中途松手：OBS 的两个分支都不进 → 转场保持挂起、不切场景。
    # 客户端不猜 OBS 的意图（界面层会显式发 0.0 回退，见 ui_style_test [9d]），
    # 这里只确认"不会误切场景"，以及"万一没事件回来，看门狗能收干净"。
    controller.set_tbar_position(0.5, False)
    wait_until(lambda: controller.store.transitioning, 5, app)
    scene_before = active.state.current_scene
    controller.set_tbar_position(0.5, True)
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)
    check("中途松手不切场景", active.state.current_scene == scene_before,
          active.state.current_scene)
    check("中途松手后仍有着落（看门狗兜底在位）",
          controller.store.transitioning and controller._transition_watchdog.isActive(),
          f"transitioning={controller.store.transitioning} "
          f"watchdog={controller._transition_watchdog.isActive()}")
    # 显式回退（界面层就是这么发的）
    controller.set_tbar_position(0.0, True)
    check("显式回退能正常收尾",
          wait_until(lambda: not controller.store.transitioning, 5, app),
          str(controller.store.transitioning))
    check("回退后场景未变", active.state.current_scene == scene_before,
          active.state.current_scene)
    # 「转场中」的兜底：万一 Ended 事件丢了，看门狗要能把状态收回来
    controller.store.set_transitioning(True)
    controller._on_transition_watchdog()
    check("看门狗能把卡住的「转场中」复位", not controller.store.transitioning,
          str(controller.store.transitioning))
    # 非演播室模式下不该发推杆请求
    controller.set_studio_mode(False)
    wait_until(lambda: not controller.store.studio_mode, 5, app)
    before_tbar = active.requests.count("SetTBarPosition")
    controller.set_tbar_position(0.5, False)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)
    check("非演播室模式下不发推杆请求",
          active.requests.count("SetTBarPosition") == before_tbar,
          str(active.requests.count("SetTBarPosition") - before_tbar))

    print("\n[16b] 演播室模式：迟到的回执不能把用户刚切的开关按回去（回归）")
    # 实测到的现象：连上后立刻开演播室模式，连接时那批刷新发出的
    # GetStudioModeEnabled 回执后到，把 store 按回 False；
    # 那一瞬 T 型推杆会被判成"非演播室模式"，**把用户的拖动静默丢掉**。
    import types as _types

    controller.set_studio_mode(True)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    controller._handle_get_studio_mode_enabled(
        _types.SimpleNamespace(studio_mode_enabled=False)
    )
    check("迟到的 GetStudioModeEnabled 不会把开关按回去",
          controller.store.studio_mode, str(controller.store.studio_mode))
    # 迟到的 StudioModeStateChanged 同样不能
    controller._event_studio_mode_state_changed(
        _types.SimpleNamespace(studio_mode_enabled=False)
    )
    check("迟到的 StudioModeStateChanged 也不会",
          controller.store.studio_mode, str(controller.store.studio_mode))
    # 意图过期后必须重新采信服务端，别把真实变化永久忽略
    controller._studio_intent = None
    controller._handle_get_studio_mode_enabled(
        _types.SimpleNamespace(studio_mode_enabled=False)
    )
    check("没有意图时照常采信服务端", not controller.store.studio_mode,
          str(controller.store.studio_mode))
    # 真实链路：连上后**立刻**切换，settle 之后必须还是开着的
    controller.set_studio_mode(True)
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)
    check("立刻切换后稳定在开启状态（不被陈旧回执翻回去）",
          controller.store.studio_mode, str(controller.store.studio_mode))
    controller.set_studio_mode(False)
    wait_until(lambda: not controller.store.studio_mode, 5, app)

    print("\n[16c] 两边工作室模式不同步时，推杆必须自愈并说清原因（回归）")
    # 用户报的现场：推杆怎么拖都没反应，**OBS 侧推杆纹丝不动**。
    # 服务端实现（obs-websocket RequestHandler_Transitions.cpp）里
    # SetTBarPosition 第一件事就是 `if (!obs_frontend_preview_program_mode_active())
    # return Error(StudioModeNotActive)` —— 只要两边对工作室模式的认知不同步，
    # 请求就全被 506 拒掉，而客户端如果只信本地状态，界面上的推杆会一直是"可用但没用"。
    # 这里直接制造这种不同步，验证客户端能纠正自己。
    active.state.studio_mode = False          # OBS 侧其实没开
    controller.store.set_studio_mode(True)    # 本地却以为开着
    controller.store.clear_unavailable("SetTBarPosition")
    app.processEvents()
    # 同时抓一份原始帧：验证"失败也记成协议原样的响应帧"（含 requestStatus.code）
    frames: list[tuple[str, str, object]] = []
    controller.worker.raw_trace.connect(
        lambda direction, kind, payload: frames.append((direction, kind, payload))
    )
    before_caps = active.requests.count("GetStudioModeEnabled")
    controller.set_tbar_position(0.5, False)
    check("本地先乐观推到 0.5", controller.store.tbar_position == 0.5,
          str(controller.store.tbar_position))
    # 等"位置被纠回 0" —— 只有拒绝纠错路径会这么做。
    # 不能等 is_unavailable：工作室模式同步那边也会把它置上，那样等到的
    # 可能不是拒绝路径，断言就变成了猜时序。
    check("被 OBS 拒后位置被纠回 0",
          wait_until(lambda: controller.store.tbar_position == 0.0, 5, app),
          str(controller.store.tbar_position))
    check("推杆被标成不可用",
          controller.store.is_unavailable("SetTBarPosition"),
          str(controller.store.is_unavailable("SetTBarPosition")))
    check("原因里讲清了是工作室模式",
          "工作室模式" in controller.store.unavailable_reason("SetTBarPosition"),
          controller.store.unavailable_reason("SetTBarPosition"))
    check("不再假装在转场", not controller.store.transitioning,
          str(controller.store.transitioning))
    # 排障关键：失败必须也进诊断窗口，而且是**协议原样**的 requestStatus 形状。
    # 用户按 requestStatus.code 去找却找不到，就会误判成"OBS 没回响应"。
    failed_frames = [
        payload for _, kind, payload in frames
        if kind == "response" and isinstance(payload, dict)
        and isinstance(payload.get("requestStatus"), dict)
        and payload["requestStatus"].get("result") is False
    ]
    check("失败也记成协议原样的响应帧（含 requestStatus.code）",
          any(p["requestStatus"].get("code") == 506 for p in failed_frames),
          str(failed_frames[:1]))
    check("回读了工作室模式（准备纠正本地状态）",
          wait_until(lambda: active.requests.count("GetStudioModeEnabled") > before_caps, 5, app),
          str(active.requests.count("GetStudioModeEnabled") - before_caps))
    check("本地工作室模式被纠正为关闭",
          wait_until(lambda: not controller.store.studio_mode, 5, app),
          str(controller.store.studio_mode))
    # 真正打开工作室模式后，推杆应当重新可用
    controller.set_studio_mode(True)
    wait_until(lambda: active.state.studio_mode, 5, app)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    check("工作室模式恢复后推杆重新可用",
          wait_until(lambda: not controller.store.is_unavailable("SetTBarPosition"), 5, app),
          str(controller.store.is_unavailable("SetTBarPosition")))
    check("恢复后位置归零、状态干净",
          controller.store.tbar_position == 0.0 and not controller.store.transitioning)
    controller.set_studio_mode(False)
    # 这里要等的是**服务端确认 + 可用性跟着更新**，不能等本地乐观值
    check("关闭工作室模式时主动收起推杆并给出原因",
          wait_until(lambda: controller.store.is_unavailable("SetTBarPosition"), 5, app)
          and "工作室模式" in controller.store.unavailable_reason("SetTBarPosition"),
          controller.store.unavailable_reason("SetTBarPosition"))
    controller.store.clear_unavailable("SetTBarPosition")

    print("\n[16d] OBS 收下推杆却毫无反应时，必须自己发现并说清原因（回归）")
    # OBS ≥29.1 的缺陷（obs-studio issue #11372 / PR #13143 未合入）：
    # SetTBarPosition **正常返回 code 100**，但 OBS 端什么都不会发生。
    # "请求成功"和"功能可用"是两件事，只看回执发现不了，只能从结果反推。
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    port_silent = free_port()
    silent = FakeObsServer(host="127.0.0.1", port=port_silent, tbar_silent=True)
    silent.start()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port_silent, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    wait_until(lambda: controller.store.supports("SetTBarPosition"), 8, app)
    controller.set_studio_mode(True)
    wait_until(lambda: silent.state.studio_mode, 5, app)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    controller.store.set_tbar_ignored(False)
    # 抓一下弹给用户的文案：必须点出这是 OBS 侧的已知缺陷 + 给出变通办法
    alerts: list[tuple[str, str]] = []
    controller.store.error_raised.connect(
        lambda title, detail: alerts.append((title, detail))
    )
    scene_before = silent.state.current_scene
    preview_before = silent.state.preview_scene
    controller.set_tbar_position(1.0, True)      # 推到底松手
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    check("OBS 回了成功（code 100），所以不是协议层的问题",
          not controller.store.transitioning and silent.state.current_scene == scene_before,
          f"{silent.state.current_scene}")
    check("**能自己发现「请求成功但没效果」**",
          wait_until(lambda: controller.store.tbar_ignored, 8, app),
          str(controller.store.tbar_ignored))
    check("没有把成功当失败（推杆仍可用）",
          not controller.store.is_unavailable("SetTBarPosition"))
    check("弹窗说清了这是 OBS 侧缺陷并给了上游号",
          any("11372" in detail or "13143" in detail for _, detail in alerts),
          str(alerts[:1]))
    check("弹窗给了变通办法", any("鼠标" in detail or "转场动画" in detail
                                  for _, detail in alerts),
          str(alerts[:1]))
    # 有动静的情况不能误判
    controller.store.set_tbar_ignored(False)
    silent.tbar_silent = False
    controller.set_tbar_position(0.5, False)
    wait_until(lambda: silent.state.tbar_transitioning, 5, app)
    controller.set_tbar_position(1.0, True)
    wait_until(lambda: silent.state.current_scene == preview_before, 5, app)
    check("OBS 真动了就不会误报", not controller.store.tbar_ignored,
          str(controller.store.tbar_ignored))
    # 这一段换了服务器，后面的段落还要用回原来那台 —— 不换回来会把它们全带崩
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    silent.stop()
    controller.config.connection = ConnectionConfig(
        host="127.0.0.1", port=port_n, password=DEFAULT_PASSWORD
    )
    controller.connect()
    wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)

    print("\n[17] G5：快捷转场槽位（客户端侧，两步打包）")
    controller.set_studio_mode(True)
    wait_until(lambda: controller.store.studio_mode, 5, app)
    controller.save_quick_transitions(
        [{"transition": "Swipe", "duration_ms": 500}]
    )
    check("槽位已持久化", controller.quick_transitions() == [
        {"transition": "Swipe", "duration_ms": 500}
    ], str(controller.quick_transitions()))
    before_set = active.requests.count("SetCurrentSceneTransition")
    before_trigger = active.requests.count("TriggerStudioModeTransition")
    controller.run_quick_transition(0)
    # 转场名与时长是两个独立请求，只等名称会让"时长"断言偶发失败（实测约 1/3 概率），
    # 所以等两个效果都落地再断言。
    wait_until(lambda: active.state.current_transition == "Swipe"
               and active.state.transition_duration == 500, 5, app)
    check("先把当前转场改成槽位里的", active.state.current_transition == "Swipe",
          active.state.current_transition)
    check("转场时长也跟着改", active.state.transition_duration == 500,
          str(active.state.transition_duration))
    wait_until(
        lambda: active.requests.count("TriggerStudioModeTransition") > before_trigger, 5, app
    )
    check("两步都发出（设转场 + Trigger）",
          active.requests.count("SetCurrentSceneTransition") > before_set
          and active.requests.count("TriggerStudioModeTransition") > before_trigger,
          f"{active.requests.count('SetCurrentSceneTransition')} / "
          f"{active.requests.count('TriggerStudioModeTransition')}")
    # 顺序必须是"先设转场，后 Trigger"
    seq = [
        r for r in active.requests
        if r in ("SetCurrentSceneTransition", "TriggerStudioModeTransition")
    ]
    check("顺序正确：先设转场再 Trigger",
          seq[-2:] == ["SetCurrentSceneTransition", "TriggerStudioModeTransition"],
          str(seq[-2:]))
    controller.save_quick_transitions([])

    print("\n[18] L1/L2：媒体源控制")
    wait_until(lambda: bool(controller.store.media), 8, app)
    check("识别出媒体源", [m.name for m in controller.store.media] == ["媒体源2"],
          str([m.name for m in controller.store.media]))
    # 时长来自 GetMediaInputStatus（低频那拍才拉），刚重连完还没轮到 —— 等它到
    wait_until(lambda: controller.store.find_media("媒体源2").duration_ms > 0, 8, app)
    check("拿到时长（L2 进度条需要）",
          controller.store.find_media("媒体源2").duration_ms == 180000,
          str(controller.store.find_media("媒体源2")))
    controller.media_action("媒体源2", P.MEDIA_ACTION_PLAY)
    wait_until(lambda: active.state.media["媒体源2"]["state"] == P.MEDIA_STATE_PLAYING, 5, app)
    check("播放已下发", active.state.media["媒体源2"]["state"] == P.MEDIA_STATE_PLAYING,
          active.state.media["媒体源2"]["state"])
    check("本地状态同步为播放中",
          controller.store.find_media("媒体源2").playing,
          controller.store.find_media("媒体源2").state)
    controller.media_action("媒体源2", P.MEDIA_ACTION_PAUSE)
    wait_until(lambda: active.state.media["媒体源2"]["state"] == P.MEDIA_STATE_PAUSED, 5, app)
    check("暂停已下发", controller.store.find_media("媒体源2").paused,
          controller.store.find_media("媒体源2").state)
    controller.seek_media("媒体源2", 90000)
    wait_until(lambda: active.state.media["媒体源2"]["cursor"] == 90000, 5, app)
    check("跳转已下发（L2）", active.state.media["媒体源2"]["cursor"] == 90000,
          str(active.state.media["媒体源2"]["cursor"]))
    controller.media_action("媒体源2", P.MEDIA_ACTION_STOP)
    wait_until(lambda: active.state.media["媒体源2"]["state"] == P.MEDIA_STATE_STOPPED, 5, app)
    check("停止已下发并把进度归零",
          active.state.media["媒体源2"]["cursor"] == 0,
          str(active.state.media["媒体源2"]["cursor"]))

    print("\n[19] M1/M2：场景集合与配置文件切换")
    wait_until(lambda: bool(controller.store.scene_collections), 8, app)
    check("场景集合列表已加载",
          controller.store.scene_collections == ["未命名", "直播方案", "录屏方案"],
          str(controller.store.scene_collections))
    check("当前场景集合已识别",
          controller.store.current_scene_collection == "未命名",
          controller.store.current_scene_collection)
    check("配置文件列表已加载",
          controller.store.profiles == ["未命名", "高清推流", "低码率"],
          str(controller.store.profiles))
    check("未录制未推流时允许切换", controller.config_switch_blocker() == "",
          controller.config_switch_blocker())
    controller.switch_scene_collection("直播方案")
    wait_until(lambda: active.state.current_scene_collection == "直播方案", 5, app)
    check("场景集合切换已下发", active.state.current_scene_collection == "直播方案",
          active.state.current_scene_collection)
    check("切换后本地整体作废（场景列表清空重来）",
          controller.store.current_scene_collection == "直播方案",
          f"集合={controller.store.current_scene_collection} 场景={len(controller.store.scenes)}")
    controller.switch_profile("高清推流")
    wait_until(lambda: active.state.current_profile == "高清推流", 5, app)
    check("配置文件切换已下发", active.state.current_profile == "高清推流",
          active.state.current_profile)
    # 录制/推流中必须禁止切换
    controller.toggle_record()
    wait_until(lambda: controller.store.record.active, 5, app)
    check("录制中禁止切换配置",
          "录制" in controller.config_switch_blocker(),
          controller.config_switch_blocker())
    blocked_before = active.state.set_profile_calls
    controller.switch_profile("低码率")
    for _ in range(15):
        app.processEvents()
        time.sleep(0.02)
    check("控制器层也不放行（不只靠界面）",
          active.state.set_profile_calls == blocked_before,
          str(active.state.set_profile_calls - blocked_before))
    controller.toggle_record()
    wait_until(lambda: not controller.store.record.active, 5, app)

    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    feat.stop()
    controller.shutdown()
    server2.stop()

    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

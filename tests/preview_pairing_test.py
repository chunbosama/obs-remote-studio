"""缩略图配对回归：预览/输出两块画面搞反。

跑法：`python tests/preview_pairing_test.py`

现场故障
--------
演播室模式下点一个场景，**输出窗切到了预览场景、预览窗不动**；退出重连才能恢复。

根因不在缩略图控件（那边是按 role 分派的，没问题），在传输层：
`obsws_python.ObsClient.req()` 是"发一条、收一帧"，**从不校验响应里的
`requestId`**。只要有一条请求超时，它那条迟到的响应就留在 socket 里，
被**下一次**请求当成自己的响应收下 —— 从此这条连接上每条响应都错位一格，
而且不会自愈。缩略图恰好是"预览一张、输出一张"连着发的，
错位一格正好表现为两块画面永久对调（预览窗拿到的是输出场景的图，反之亦然）。

本文件两部分
------------
[A] 传输层：迟到的响应不得被下一条请求认领（`core/client_v5.py`）。
    撤掉修复（换回 obsws-python 自带的 ReqClient）这一节必然变红。
[B] 控制器：画面角色只能来自"在途队列"，对不上的帧必须丢掉；
    流水线卡死要能作废这一拍重来，**但作废不许动队列**——旧请求的结局还在路上，
    把它们挤掉就等于每帧错一格（`[B4b]`–`[B8]` 就是这一条）。
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 与真实配置隔离（Windows 上 QSettings 默认写注册表）
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from obsws_python.error import OBSSDKTimeoutError  # noqa: E402

from fake_obs_server import (  # noqa: E402
    DEFAULT_PASSWORD,
    FakeObsServer,
    make_png,
    screenshot_shade,
)

from obs_remote_studio.core.client_v5 import VerifiedReqClient  # noqa: E402
from obs_remote_studio.core.controller import FRAME_STALE_SECONDS, Controller  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED  # noqa: E402

CHECKS: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    if condition:
        CHECKS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append(f"{name} {extra}".strip())
        print(f"  [FAIL] {name} {extra}")


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def expected_frame(scene: str) -> bytes:
    """该场景"应该"收到的那张图（假服务器按场景名定灰度，见 screenshot_shade）。"""
    return make_png(shade=screenshot_shade(scene))


def request_frame(client, scene: str) -> bytes:
    data = client.send(
        "GetSourceScreenshot",
        {"sourceName": scene, "imageFormat": "jpeg"},
        raw=True,
    )
    encoded = data["imageData"].split(",", 1)[-1]
    return base64.b64decode(encoded)


def _frame_holder(raw: bytes):
    class _Resp:
        image_data = "data:image/png;base64," + base64.b64encode(raw).decode()

    return _Resp()


def describe(frames: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    """把 (角色, 图) 说成人话：这块画面上**其实是哪个场景**的图。

    断言失败时只看 ["preview", "program"] 看不出问题 —— 角色一直是对的，
    错的是"这一格装的是谁的图"。直接照现场用户的话说：
    输出窗里出现了预览场景的画面。
    """
    names = {expected_frame(scene): scene for scene in ("预览A", "输出B", "主画面")}
    return [(role, names.get(raw, "?")) for role, raw in frames]


# ---------------------------------------------------------------- [A] 传输层
def test_transport_pairing() -> None:
    print("\n[A] 传输层：迟到的响应不得被下一条请求认领")
    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()

    stale_frames: list[dict] = []
    client = None
    try:
        client = VerifiedReqClient(
            host="127.0.0.1",
            port=port,
            password=DEFAULT_PASSWORD,
            subs=0,
            timeout=0.4,
        )
        client.on_stale = stale_frames.append

        # 正对照：假服务器按场景名定灰度，先确认这条映射本身是准的
        baseline = request_frame(client, "主画面")
        check("[A0] 正对照：拿到的画面就是所请求场景的画面",
              baseline == expected_frame("主画面"))

        # 只让**下一条**缩略图响应慢到超过客户端预算：这就是"错位"的起点
        server.slow_responses["GetSourceScreenshot"] = 0.9
        timed_out = False
        try:
            request_frame(client, "主画面")
        except OBSSDKTimeoutError:
            timed_out = True
        check("[A1] 慢响应确实让客户端超时（复现前提成立）", timed_out)

        # 等那条迟到的响应真的落到 socket 里
        time.sleep(0.7)

        frame = request_frame(client, "结束")
        check(
            "[A2] 第二条拿到的必须是**自己**那张图（不是迟到的「主画面」）",
            frame == expected_frame("结束"),
            "拿到的是别的场景的画面（响应被下一条请求认领了）",
        )
        check("[A3] 迟到的响应被丢弃并计数（诊断窗口可见）",
              client.stale_frames >= 1 and bool(stale_frames),
              f"stale_frames={client.stale_frames}, 回调 {len(stale_frames)} 次")
    finally:
        if client is not None:
            client.disconnect()
        server.stop()


# ---------------------------------------------------------------- [B] 控制器
def test_controller_roles(app: QApplication) -> None:
    print("\n[B] 控制器：画面角色只能来自在途队列")
    controller = Controller()
    sent: list[tuple[str, dict]] = []
    frames: list[tuple[str, bytes]] = []
    real_send = controller.send
    try:
        # 占住发送口：本文件不连服务器，只验证"发什么、回什么贴给谁"
        controller.send = lambda request_type, data=None: sent.append(
            (request_type, dict(data or {}))
        )
        controller.store.frame_ready.connect(
            lambda role, raw: frames.append((role, raw))
        )
        controller.store.set_connection_state(CONNECTED)
        controller.store.set_current_scene("输出B")
        controller.store.set_preview_scene("预览A")
        controller.store.set_studio_mode(True)

        controller._on_frame_tick()
        check(
            "[B1] 一次 tick 先要预览、再要输出，且分别要的是自己那个场景",
            [d.get("sourceName") for _, d in sent] == ["预览A", "输出B"],
            str([d.get("sourceName") for _, d in sent]),
        )

        preview_bytes = expected_frame("预览A")
        program_bytes = expected_frame("输出B")
        controller._handle_get_source_screenshot(_frame_holder(preview_bytes))
        controller._handle_get_source_screenshot(_frame_holder(program_bytes))
        check(
            "[B2] 回来的帧按 preview → program 贴角色",
            frames
            == [("preview", preview_bytes), ("program", program_bytes)],
            str(describe(frames)),
        )

        controller._handle_get_source_screenshot(_frame_holder(expected_frame("主画面")))
        check(
            "[B3] 队列里没有对应请求的帧被丢弃，不得冒充 program",
            len(frames) == 2,
            f"帧数 {len(frames)}",
        )

        # ---- 流水线卡死：某一路在途迟迟没下文 → 作废这一拍重来 ----
        # 现场：作废那一刻，那条旧请求的结局（回图 / 失败）**还在路上**。
        # 归属只看队列顺序（响应不带 sceneName），所以作废时如果连队列一起清掉，
        # 那些结局就只能去顶新请求的位置 —— 从此每帧错一格，表现正是
        # "输出窗显示预览场景、预览窗不动"（`c907080` 修过的现场的另一条路）。
        def arm_stale() -> None:
            """造出"一条请求已作废、结局还没到"的现场。"""
            controller._frame_queue.clear()
            controller._frame_pending = {
                "preview": time.monotonic() - FRAME_STALE_SECONDS - 1
            }
            controller._frame_orphans = 0
            controller._frame_queue.append("preview")
            sent.clear()
            frames.clear()
            controller._on_frame_tick()

        arm_stale()
        check(
            "[B4] 卡死的流水线作废后，下一拍照样重新要两张图",
            [d.get("sourceName") for _, d in sent] == ["预览A", "输出B"],
            str([d.get("sourceName") for _, d in sent]),
        )
        check(
            "[B4b] 作废只清在途标记：队列里那条旧请求仍留着（结局还没到）",
            list(controller._frame_queue) == ["preview", "preview", "program"]
            and getattr(controller, "_frame_orphans", None) == 1,
            f"queue={list(controller._frame_queue)} "
            f"orphans={getattr(controller, '_frame_orphans', None)}",
        )

        # 变体一：旧请求的**响应**迟到（服务器晚回，客户端还没超时）
        controller._handle_get_source_screenshot(_frame_holder(expected_frame("主画面")))
        check(
            "[B5] 作废前那条请求的响应迟到 → 只还账，不贴角色",
            frames == []
            and getattr(controller, "_frame_orphans", None) == 0
            and list(controller._frame_queue) == ["preview", "program"],
            f"frames={[role for role, _ in frames]} "
            f"orphans={getattr(controller, '_frame_orphans', None)} "
            f"queue={list(controller._frame_queue)}",
        )
        controller._handle_get_source_screenshot(_frame_holder(preview_bytes))
        controller._handle_get_source_screenshot(_frame_holder(program_bytes))
        check(
            "[B6] 之后两块画面仍各归各位（没有错一格）",
            frames == [("preview", preview_bytes), ("program", program_bytes)],
            str(describe(frames)),
        )

        # 变体二：旧请求的**失败**迟到（超时）—— 这才是现场那条路。
        # 旧实现盲取队首，消费掉的是**新请求**的在途标记：那一格再也不重发，
        # 之后每帧错一格。
        arm_stale()
        controller._on_request_failed("GetSourceScreenshot", "超时", True, 0)
        check(
            "[B7] 作废前那条请求的失败迟到 → 不得消费新请求的在途标记",
            frames == []
            and set(controller._frame_pending) == {"preview", "program"}
            and list(controller._frame_queue) == ["preview", "program"]
            and getattr(controller, "_frame_orphans", None) == 0,
            f"pending={sorted(controller._frame_pending)} "
            f"queue={list(controller._frame_queue)} "
            f"orphans={getattr(controller, '_frame_orphans', None)}",
        )
        controller._handle_get_source_screenshot(_frame_holder(preview_bytes))
        controller._handle_get_source_screenshot(_frame_holder(program_bytes))
        check(
            "[B8] 之后两块画面仍各归各位（画面不再对调）",
            frames == [("preview", preview_bytes), ("program", program_bytes)],
            str(describe(frames)),
        )
    finally:
        controller.send = real_send
        controller.shutdown()


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-pair-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)
    app = QApplication([])

    test_transport_pairing()
    test_controller_roles(app)

    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for item in FAILED:
        print(f"  - {item}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())

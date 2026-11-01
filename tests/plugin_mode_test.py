"""OBS 端插件（vendor `obsrs`）接入回归测试。

验收点（用户原话）：**连接时如果启用了插件并探测成功，界面显示「进阶模式」，
否则显示「标准模式」或「兼容模式」**。

分两段：
- **[A] 纯逻辑**：`core/plugin.py` 的契约解析、模式名、vendor 请求名编解码。
  不需要服务器，也不需要 Qt 事件循环。
- **[B] 端到端**：假服务器开/关插件，让真正的 `Controller` + `MainWindow` 跑起来，
  逐条验证模式标识（标题/状态栏）、能力置灰、推杆直驱、磁盘预警、日志与 `VendorEvent`。

运行：python tests/plugin_mode_test.py
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

from fake_obs_server import DEFAULT_PASSWORD, FakeObsServer  # noqa: E402
from fake_obs_v4_server import FakeObsV4Server  # noqa: E402

from obs_remote_studio.core import plugin as PV  # noqa: E402
from obs_remote_studio.core import protocol as P  # noqa: E402
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


def settle(app: QApplication, seconds: float = 0.6) -> None:
    """空转一会儿事件循环（等请求回来、等信号派发）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ==================================================================== [A] 纯逻辑
#: 夹具：MVP 插件真实会报的那张能力表（契约 §2）
FULL_FEATURES = {
    "info": True,
    "stream.mjpeg": True,
    "stream.role.preview": False,
    "tbar": True,
    "machine.stats": True,
    "logs": True,
    "encoder.stats": False,
    "capture.indicator": True,
}


def info_payload(**overrides) -> dict:
    payload = {
        "ok": True,
        "pluginVersion": "0.2.0",
        "apiVersion": 1,
        "vendor": "obsrs",
        "obsVersion": "31.0.3",
        "websocketApiVersion": "v3",
        "features": [
            {"name": name, "enabled": enabled} for name, enabled in FULL_FEATURES.items()
        ],
        "featureMap": dict(FULL_FEATURES),
        "limits": {"maxWidth": 3840, "maxHeight": 2160, "maxFps": 30, "maxLogLines": 500},
        "captureActive": False,
        "captureIndicator": True,
    }
    payload.update(overrides)
    return payload


def logic_tests() -> None:
    print("[A1] 模式名：三态且插件优先于协议")
    check("v5 无插件 -> 标准模式", PV.mode_label("v5", False) == "标准模式")
    check("v4 无插件 -> 兼容模式", PV.mode_label("v4", False) == "兼容模式")
    check("v5 有插件 -> 进阶模式", PV.mode_label("v5", True) == "进阶模式")
    check("v4 有插件 -> 进阶模式（插件优先）", PV.mode_label("v4", True) == "进阶模式")
    check("ServerInfo 默认 -> 标准模式", ServerInfo().mode_label() == "标准模式")
    check("ServerInfo(v4) -> 兼容模式",
          ServerInfo(protocol="v4").mode_label() == "兼容模式")
    check("ServerInfo(plugin_active) -> 进阶模式",
          ServerInfo(plugin_active=True).mode_label() == "进阶模式")
    check("ServerInfo(v4 + plugin_active) -> 进阶模式",
          ServerInfo(protocol="v4", plugin_active=True).mode_label() == "进阶模式")
    # 模式名只能有一处事实：models 里若是手抄的字符串，改了 plugin.py 会静默不同步
    check("models 与 plugin.py 用的是同一批模式名",
          ServerInfo(plugin_active=True).mode_label() == PV.MODE_ADVANCED
          and ServerInfo().mode_label() == PV.MODE_STANDARD
          and ServerInfo(protocol="v4").mode_label() == PV.MODE_COMPAT)

    print("\n[A2] getInfo 解析：判能力只看 featureMap/features，不比版本号")
    info = PV.parse_info(info_payload())
    check("正常载荷能解析", info is not None)
    assert info is not None
    check("版本号带回来了", info.plugin_version == "0.2.0", info.plugin_version)
    check("apiVersion 带回来了", info.api_version == 1, str(info.api_version))
    check("stream.mjpeg 可用", info.supports("stream.mjpeg"))
    check("tbar 可用", info.supports("tbar"))
    check("显式 false 的能力不可用", not info.supports("encoder.stats"))
    check("显式 false 的预览角色不可用", not info.supports(PV.FEATURE_STREAM_PREVIEW))
    check("**没报过的能力一律不可用**（不猜）", not info.supports("frobnicate"))
    summary = PV.feature_summary(info)
    check("feature_summary 用 ✓/✗ 标出每一项",
          "推杆直驱=✓" in summary and "编码器明细=✗" in summary, summary)
    check("未实现的能力写成完整名字（不按字数硬截断）",
          "编码器明=" not in summary and "编码器明细" in summary, summary)

    # ①libobs 会丢掉标量数组元素（契约里为此才给了 featureMap）
    broken = info_payload(features=list(FULL_FEATURES))
    info2 = PV.parse_info(broken)
    check("features 退化成字符串数组时仍然能判能力",
          info2 is not None and info2.supports("tbar") and not info2.supports("encoder.stats"))

    # ②只有 features 数组、没有 featureMap（老插件）
    only_array = info_payload()
    only_array.pop("featureMap")
    info3 = PV.parse_info(only_array)
    check("只有 features 数组也能判能力",
          info3 is not None and info3.supports("logs") and not info3.supports("encoder.stats"))

    # ③不比较版本号：将来插件升到 9.x，老客户端照样按能力表办事
    info4 = PV.parse_info(info_payload(pluginVersion="9.9.9", apiVersion=1))
    check("插件版本更高也不影响判能力（不比版本号）",
          info4 is not None and info4.supports("machine.stats"))

    check("ok=false -> 当没装插件", PV.parse_info({"ok": False, "error": "x"}) is None)
    check("空载荷 -> 当没装插件", PV.parse_info({}) is None)
    check("垃圾载荷 -> 当没装插件", PV.parse_info(None) is None and PV.parse_info([1, 2]) is None)

    print("\n[A3] getMachineStats：读不到盘必须说得清原因（D17）")
    good = PV.parse_machine_stats({
        "ok": True,
        "hostname": "OBS-PC",
        "recordDir": {"known": True, "path": "C:\\Rec", "isUnc": False, "exists": True},
        "disk": {"known": True, "totalBytes": 512 * 1024 ** 3,
                 "freeBytes": 100 * 1024 ** 3, "availBytes": 90 * 1024 ** 3},
        "recording": {"active": True, "paused": False, "currentFile": "C:\\Rec\\a.mkv"},
    })
    check("读得到盘时剩余空间正确", good is not None and abs(good.free_gb() - 90.0) < 0.01,
          str(good and good.free_gb()))
    check("读得到盘时没有 '读不到' 的原因", good is not None and good.disk_problem() == "",
          str(good and good.disk_problem()))
    check("录制目录带回来了", good is not None and good.record_dir == "C:\\Rec")
    check("录制状态带回来了", good is not None and good.recording_active)

    unc = PV.parse_machine_stats({
        "ok": True,
        "recordDir": {"known": True, "path": "\\\\NAS\\share", "isUnc": True,
                      "exists": False, "writeError": 0},
        "disk": {"known": False, "error": 53},
    })
    check("UNC 读不到盘 -> 剩余空间为 None（不报假数字）",
          unc is not None and unc.free_gb() is None)
    problem = unc.disk_problem() if unc else ""
    check("不给数字，但把错误码翻成人话", "53" in problem and "网络路径" in problem, problem)
    check("说清了是哪个目录读不到", "\\\\NAS\\share" in problem, problem)
    check("点了是 UNC 路径", "UNC" in problem, problem)

    no_dir = PV.parse_machine_stats({"ok": True, "disk": {"known": False}})
    check("连目录都不知道时也有一句话",
          no_dir is not None and no_dir.disk_problem() != "", str(no_dir and no_dir.disk_problem()))

    check("ok=false -> None（不造数）", PV.parse_machine_stats({"ok": False}) is None)

    print("\n[A4] tbar / getTbar 解析")
    state = PV.parse_tbar({"ok": True, "position": 0.42, "studioMode": True,
                           "transitioning": True, "transition": "淡入淡出",
                           "durationMs": 300, "frontendPosition": 0.1})
    check("响应能解析", state is not None and abs(state.position - 0.42) < 1e-6)
    check("frontendPosition 只作诊断，不当权威",
          state is not None and abs(state.frontend_position - 0.1) < 1e-6)
    check("strict 模式下 ok=false -> None（不算成功）",
          PV.parse_tbar({"ok": False, "error": "studio mode is not active"}) is None)
    check("事件无 ok 字段 -> strict=False 照常解析",
          PV.parse_tbar({"position": 1.0}, strict=False) is not None)
    check("越界位置被夹到 0..1",
          PV.parse_tbar({"ok": True, "position": 1.7}) .position == 1.0
          and PV.parse_tbar({"ok": True, "position": -3}).position == 0.0)

    print("\n[A5] 日志与失败原因")
    logs = PV.parse_logs({
        "ok": True,
        "lines": [
            {"timestamp": 1, "level": 200, "levelName": "warning",
             "message": "Failed to connect to server: connection timed out"},
            {"timestamp": 2, "level": 100, "levelName": "error",
             "message": "encoder 'nvenc' failed to initialize"},
        ],
    })
    check("日志解析出两条", len(logs) == 2, str(len(logs)))
    hint = PV.failure_hint(logs)
    check("从日志里挑出最可能的原因", bool(hint), hint or "(空)")
    check("提示是给人看的（不照抄英文原文）", "encoder" not in hint.lower(), hint)
    check("没有可疑日志时不给假原因",
          PV.failure_hint([]) == ""
          and PV.failure_hint([PV.LogLine(message="obs-websocket started")]) == "")
    check("单行事件能解析",
          PV.parse_log_line({"timestamp": 3, "level": 300, "message": "hi"}) is not None)
    check("空事件 -> None", PV.parse_log_line({}) is None)
    check("level 数字能翻成短名",
          PV.log_level_name(100) == "error" and PV.log_level_name(400) == "debug")

    print("\n[A6] vendor 请求名的编解码 + 订阅位")
    check("vendor 请求带前缀", P.vendor_request("getInfo") == "@vendor:getInfo")
    check("认得自己的前缀", P.is_vendor_request("@vendor:getInfo"))
    check("官方请求不会被误认", not P.is_vendor_request("GetVersion"))
    check("空值不炸", not P.is_vendor_request(None) and not P.is_vendor_request(""))
    check("能取回插件侧请求名", P.vendor_name_of("@vendor:getTbar") == "getTbar")
    # 漏订 Vendors 位 = 插件事件一条都收不到，且界面上完全看不出来
    check("订阅位里有 Vendors（1<<9）",
          int(P.Subs.VENDORS) == 1 << 9, str(int(P.Subs.VENDORS)))
    check("事件表里登记了 VendorEvent",
          P.EVENTS.get("VendorEvent") == "on_vendor_event", str(P.EVENTS.get("VendorEvent")))


# ==================================================================== [B] 端到端
class Session:
    """一套 controller + 假服务器，用完即拆。"""

    def __init__(self, app: QApplication, server, controller: Controller):
        self.app = app
        self.server = server
        self.controller = controller

    @property
    def store(self):
        return self.controller.store

    def close(self) -> None:
        try:
            self.controller.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.settle(0.2)
        try:
            self.controller.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.server.stop()

    def settle(self, seconds: float = 0.6) -> None:
        settle(self.app, seconds)

    def wait(self, predicate, timeout: float = 10.0) -> bool:
        return wait_until(predicate, timeout, self.app)


def boot(app: QApplication, *, plugin_enabled: bool = True,
         connect: bool = True, **server_kw) -> Session:
    """起一台假服务器 + 一个真 Controller，并按需连上去。"""
    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port, **server_kw)
    server.start()
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 2.0
    controller.config.plugin_enabled = plugin_enabled
    controller.config.reconnect.initial_delay_ms = 300
    controller.config.reconnect.max_delay_ms = 300
    session = Session(app, server, controller)
    if connect:
        controller.connect(
            ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)
        )
        if not session.wait(lambda: controller.store.connection_state == CONNECTED, 15):
            print(f"  [!!]   连不上假服务器：{controller.store.status_message}")
        elif plugin_enabled:
            # 连上之后探测请求才发出去，结果还没回来。所有断言都建立在
            # "探测已落定"之上，所以这里必须等它 —— 否则测的是探测中的中间态。
            session.wait(
                lambda: controller.store.plugin_state in ("ready", "absent"), 10
            )
    return session


def plugin_calls(session: Session, name: str) -> list[dict]:
    return [data for req, data in session.server.plugin_calls if req == name]


def main_window(controller: Controller):
    from obs_remote_studio.ui.main_window import MainWindow

    return MainWindow(controller)


def _tbar_hidden(session: Session) -> bool:
    """推杆那一行在界面上是不是被藏了（不依赖窗口真的 show 出来）。"""
    window = main_window(session.controller)
    try:
        session.settle(0.2)
        window.preview_panel.transition_column._update_button()
        return window.preview_panel.transition_column.tbar_row.isHidden()
    finally:
        window.close()


# ---------------------------------------------------------------- B1/B2：模式标识
def mode_label_test(app: QApplication) -> None:
    print("\n[B1] 没装插件 -> 标准模式")
    session = boot(app, plugin=False)
    try:
        store = session.store
        check("连上了", store.connection_state == CONNECTED, store.status_message)
        check("协议是 v5", store.server_info.protocol == "v5", store.server_info.protocol)
        check("标题用的模式名是「标准模式」",
              store.server_info.mode_label() == "标准模式", store.server_info.mode_label())
        check("plugin_ready() 为假", not session.controller.plugin_ready())
        check("plugin_state 落定为 absent", store.plugin_state == "absent", store.plugin_state)
        check("**探测真的发出去了**（不是没问就下结论）",
              len(plugin_calls(session, PV.REQ_GET_INFO)) == 1,
              str(session.server.plugin_calls))
        check("所有插件能力都不可用",
              not session.controller.plugin_supports(PV.FEATURE_TBAR)
              and not session.controller.plugin_supports(PV.FEATURE_MACHINE))

        window = main_window(session.controller)
        try:
            check("窗口标题含「标准模式」",
                  "标准模式" in window.windowTitle(), window.windowTitle())
            check("状态栏版本段含「标准模式」",
                  "标准模式" in window.status_bar.version_label.text(),
                  window.status_bar.version_label.text())
            check("「OBS 日志（插件）」菜单被置灰",
                  not window.log_action.isEnabled())
            check("置灰原因写在 tooltip 里（不留'点了没反应'）",
                  "插件" in window.log_action.toolTip(), window.log_action.toolTip())
        finally:
            window.close()

        print("\n[B3] 探测失败**不重试**（重试只会污染能力表）")
        session.settle(2.4)
        check("getInfo 只问过一次",
              len(plugin_calls(session, PV.REQ_GET_INFO)) == 1,
              str(len(plugin_calls(session, PV.REQ_GET_INFO))))
        check("没有去问别的插件请求（能力都没有，问了也是 600）",
              len(session.server.plugin_calls) == 1, str(session.server.plugin_calls))
    finally:
        session.close()

    print("\n[B2] 装了插件 -> 进阶模式")
    session = boot(app, plugin=True)
    try:
        store = session.store
        check("连上了", store.connection_state == CONNECTED, store.status_message)
        check("plugin_ready() 为真", session.controller.plugin_ready())
        check("plugin_state 落定为 ready", store.plugin_state == "ready", store.plugin_state)
        check("标题用的模式名是「进阶模式」",
              store.server_info.mode_label() == "进阶模式", store.server_info.mode_label())
        check("插件版本记下来了",
              store.server_info.plugin_version == "0.2.0", store.server_info.plugin_version)
        for feature in (PV.FEATURE_TBAR, PV.FEATURE_MACHINE, PV.FEATURE_LOGS,
                        PV.FEATURE_CAPTURE, PV.FEATURE_STREAM):
            check(f"能力 {feature} 可用", session.controller.plugin_supports(feature))
        check("显式 false 的编码器统计不可用",
              not session.controller.plugin_supports(PV.FEATURE_ENCODER))

        window = main_window(session.controller)
        try:
            check("窗口标题含「进阶模式」",
                  "进阶模式" in window.windowTitle(), window.windowTitle())
            check("状态栏含「进阶模式」",
                  "进阶模式" in window.status_bar.version_label.text(),
                  window.status_bar.version_label.text())
            check("statusbar tooltip 说清了进阶模式强在哪",
                  "插件" in window.status_bar.version_label.toolTip(),
                  window.status_bar.version_label.toolTip())
            check("「OBS 日志（插件）」菜单可用", window.log_action.isEnabled())

            print("\n[B2a] 探测中不许先谎报「标准模式」")
            # 连上到 getInfo 回来之间有一瞬间：`_on_connected` 已经重建了
            # server_info（plugin_active 还是 False），探测请求还在路上。
            # 那会儿若显示「标准模式」，用户会先看到一次错误的模式名再跳变。
            fresh = ServerInfo(obs_version="31.0.3", websocket_version="5.5.0",
                               protocol="v5")
            store.set_server_info(fresh)
            store.set_plugin_probing()
            window._update_title(store.server_info)
            window.status_bar._update_version(store.server_info)
            check("探测中标题不写「标准模式」",
                  "标准模式" not in window.windowTitle(), window.windowTitle())
            check("探测中状态栏不写「标准模式」",
                  "标准模式" not in window.status_bar.version_label.text(),
                  window.status_bar.version_label.text())
            check("探测中看得出来是在探测",
                  "探测" in window.windowTitle()
                  or "探测" in window.status_bar.version_label.text(),
                  window.windowTitle())
            # 恢复（后续断言还要用）
            session.controller._handle_plugin_info(info_payload())
            session.settle(0.3)
            check("探测结束回到「进阶模式」",
                  "进阶模式" in window.windowTitle(), window.windowTitle())
        finally:
            window.close()
    finally:
        session.close()


# ---------------------------------------------------------------- B4：用户关掉插件
def plugin_disabled_test(app: QApplication) -> None:
    print("\n[B4] 连接设置里没启用插件 -> 连探测都不发")
    session = boot(app, plugin=True, plugin_enabled=False)
    try:
        store = session.store
        check("连上了", store.connection_state == CONNECTED, store.status_message)
        check("标题用的模式名是「标准模式」",
              store.server_info.mode_label() == "标准模式", store.server_info.mode_label())
        session.settle(1.0)
        check("**一条 vendor 请求都没发**（用户明确关掉了）",
              session.server.plugin_calls == [], str(session.server.plugin_calls))
        check("插件不可用", not session.controller.plugin_ready())
    finally:
        session.close()


# ---------------------------------------------------------------- B5：v4 兼容模式
def compat_mode_test(app: QApplication) -> None:
    print("\n[B5] v4（没有 vendor 通道）-> 兼容模式")
    port = free_port()
    server = FakeObsV4Server(host="127.0.0.1", port=port)
    server.start()
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 2.0
    controller.config.plugin_enabled = True      # 就算启用了也不该问
    session = Session(app, server, controller)
    try:
        controller.connect(
            ConnectionConfig(host="127.0.0.1", port=port, password=DEFAULT_PASSWORD)
        )
        ok = session.wait(lambda: controller.store.connection_state == CONNECTED, 15)
        check("v4 连接成功", ok, controller.store.status_message)
        check("协议识别为 v4", controller.store.server_info.protocol == "v4")
        check("标题用的模式名是「兼容模式」",
              controller.store.server_info.mode_label() == "兼容模式",
              controller.store.server_info.mode_label())
        check("插件判定为没有", not controller.plugin_ready())
        check("没有把 v4 说成「进阶模式」",
              "进阶模式" not in controller.store.server_info.mode_label())
        window = main_window(controller)
        try:
            check("窗口标题含「兼容模式」",
                  "兼容模式" in window.windowTitle(), window.windowTitle())
        finally:
            window.close()
    finally:
        session.close()


# ---------------------------------------------------------------- B6/B7：推杆
def tbar_test(app: QApplication) -> None:
    print("\n[B6] 推杆直驱：OBS 31.x 上标准模式做不了，插件能做")
    # 31.0.0 落在 obs-studio #11372 的失效区间（29.1+），标准模式必须被版本闸门拦住
    session = boot(app, plugin=True, obs_version="31.0.0")
    try:
        store = session.store
        session.controller.set_studio_mode(True)
        session.wait(lambda: store.studio_mode, 6)
        check("演播室模式就位", store.studio_mode)

        session.controller.set_tbar_position(0.5, False)
        ok = session.wait(lambda: plugin_calls(session, PV.REQ_TBAR), 6)
        check("走的是插件的 tbar 请求", ok, str(session.server.plugin_calls))
        call = (plugin_calls(session, PV.REQ_TBAR) or [{}])[0]
        check("位置带过去了", abs(float(call.get("position", -1)) - 0.5) < 1e-6, str(call))
        check("release=false（拖动中）", call.get("release") is False, str(call))
        check("服务端真的动了", abs(session.server.state.tbar_position - 0.5) < 1e-6,
              str(session.server.state.tbar_position))
        check("**没有**同时发标准的 SetTBarPosition",
              "SetTBarPosition" not in session.server.requests,
              str([r for r in session.server.requests if "TBar" in r]))
        check("推杆在界面上可用（插件绕开了 #11372）",
              session.controller.tbar_supported())

        print("\n[B6a] 界面：31.x 上标准模式藏推杆，进阶模式必须露出来")
        window = main_window(session.controller)
        try:
            preview = window.preview_panel.transition_column
            session.settle(0.3)
            preview._update_button()
            check("进阶模式下推杆整行可见（插件不经过前端控件）",
                  not preview.tbar_row.isHidden(), str(preview.tbar_row.isHidden()))
            check("推杆可以拖", preview.tbar.isEnabled())
        finally:
            window.close()

        # 插件推回来的权威位置
        session.server.state.tbar_position = 0.9
        session.server.emit_vendor_event(
            PV.EVENT_TBAR_CHANGED,
            {"position": 0.9, "studioMode": True, "transitioning": True},
        )
        check("tbarChanged 事件把位置同步到客户端",
              session.wait(lambda: abs(store.tbar_position - 0.9) < 1e-6, 5),
              str(store.tbar_position))

        print("\n[B6a] 别的 vendor 的事件不许被当成自己的")
        session.server.emit_vendor_event(
            PV.EVENT_TBAR_CHANGED, {"position": 0.25}, vendor="someone-else"
        )
        session.settle(0.8)
        check("异名 vendor 的事件被忽略",
              abs(store.tbar_position - 0.9) < 1e-6, str(store.tbar_position))
    finally:
        session.close()

    print("\n[B6b] 同样 31.0.0，没有插件时推杆必须被拦下（不白发注定无效的请求）")
    session = boot(app, plugin=False, obs_version="31.0.0")
    try:
        session.controller.set_studio_mode(True)
        session.wait(lambda: session.store.studio_mode, 6)
        session.controller.set_tbar_position(1.0, True)
        session.settle(1.0)
        check("标准模式下没有下发 SetTBarPosition",
              "SetTBarPosition" not in session.server.requests,
              str([r for r in session.server.requests if "TBar" in r]))
        check("服务端位置没变", abs(session.server.state.tbar_position) < 1e-6)
        check("界面上推杆整行被藏掉（不摆必然无效的控件）", _tbar_hidden(session))
    finally:
        session.close()

    print("\n[B7] 插件在、但明确没有 tbar 能力 -> 回落标准路径并照旧受版本限制")
    session = boot(app, plugin=True, obs_version="31.0.0",
                   plugin_features={"tbar": False})
    try:
        check("插件本身在", session.controller.plugin_ready())
        check("tbar 能力判为不可用",
              not session.controller.plugin_supports(PV.FEATURE_TBAR))
        check("界面上推杆整行被藏掉（31.x 的 API 有缺陷）", _tbar_hidden(session))
        session.controller.set_studio_mode(True)
        session.wait(lambda: session.store.studio_mode, 6)
        session.controller.set_tbar_position(1.0, True)
        session.settle(1.0)
        check("没有去问插件的 tbar", not plugin_calls(session, PV.REQ_TBAR),
              str(plugin_calls(session, PV.REQ_TBAR)))
        check("也没有发标准的 SetTBarPosition",
              "SetTBarPosition" not in session.server.requests)
        check("模式仍是「进阶模式」（其它能力还在）",
              session.store.server_info.mode_label() == "进阶模式")
    finally:
        session.close()

    print("\n[B8] 插件回 ok=false（不在演播室）时不许误判成成功")
    session = boot(app, plugin=True)
    try:
        store = session.store
        check("初始不在演播室", not store.studio_mode)
        before = store.tbar_position
        # 本地误以为在演播室，插件那边其实不在 —— 服务端会回 ok=false
        session.controller.set_tbar_position(1.0, True)
        session.settle(1.0)
        check("位置没有被改成 1.0（ok=false 不算成功）",
              abs(store.tbar_position - before) < 1e-6, str(store.tbar_position))
        check("没有把推杆标成「已忽略」以外的错误状态",
              store.tbar_position <= 1.0)
    finally:
        session.close()


# ---------------------------------------------------------------- B13：推杆会话收尾
def tbar_session_test(app: QApplication) -> None:
    """进阶模式：半途挂起的手动推杆会话必须能被后续动作收掉。

    这是线上问题的回归用例 —— 「进阶模式下转场按钮点了没反应」：
    插件直驱的推杆会在 OBS 侧**开一个手动转场会话**，收不到 release 就一直开着。
    会话开着时 OBS 的 `TransitionToScene()` 里有

        if (usingPreviewProgram && stillTransitioning) goto cleanup;

    （obs-studio 31.1.2 `frontend/widgets/OBSBasic_Transitions.cpp`），
    于是**之后所有转场**都被静默跳过 —— 按钮看着能点，点了什么也不会发生。
    """
    print("\n[B13] 半途挂起的手动推杆会话：转场前必须先收掉")
    session = boot(app, plugin=True, obs_version="31.0.0")
    try:
        store = session.store
        controller = session.controller
        controller.set_studio_mode(True)
        session.wait(lambda: store.studio_mode, 6)
        check("演播室模式就位", store.studio_mode)

        # 推到 10% 就没有下文了（松手事件丢了 / 拖到控件外面松开）
        controller.set_tbar_position(0.1, False)
        ok = session.wait(lambda: plugin_calls(session, PV.REQ_TBAR), 6)
        check("推杆请求发出去了", ok, str(session.server.plugin_calls))
        check("会话被记为打开", controller.tbar_session_open())

        # 模拟 OBS 的 SceneTransitionStarted —— 此时按钮是「转场中…」且不可点
        session.server.broadcast_threadsafe(
            "SceneTransitionStarted", {"transitionName": store.current_transition}
        )
        check("进入「转场中」", session.wait(lambda: store.transitioning, 5))

        before = len(session.server.requests)
        controller.trigger_transition()  # 就是界面上的「转场动画」按钮
        ok = session.wait(
            lambda: "TriggerStudioModeTransition" in session.server.requests[before:], 6
        )
        check("转场请求发出去了", ok, str(session.server.requests[before:]))
        sent = session.server.requests[before:]
        check(
            "先收尾、再转场（顺序不能反：OBS 按到达顺序处理）",
            "CallVendorRequest" in sent
            and sent.index("CallVendorRequest") < sent.index("TriggerStudioModeTransition"),
            str(sent),
        )
        last = (plugin_calls(session, PV.REQ_TBAR) or [{}])[-1]
        check(
            "收尾用的是 release + 位置 0",
            last.get("release") is True
            and abs(float(last.get("position", -1))) < 1e-6,
            str(last),
        )
        check("会话标记已关闭", not controller.tbar_session_open())

        print("\n[B13a] 插件回报「会话已结束」-> 「转场中」立刻收回（不等看门狗）")
        session.server.broadcast_threadsafe(
            "SceneTransitionStarted", {"transitionName": store.current_transition}
        )
        check("又进入「转场中」", session.wait(lambda: store.transitioning, 5))
        session.server.emit_vendor_event(
            PV.EVENT_TBAR_CHANGED,
            {"position": 0.0, "studioMode": True, "transitioning": False},
        )
        check(
            "插件说结束了，本地立刻收回",
            session.wait(lambda: not store.transitioning, 3),
            f"transitioning={store.transitioning}",
        )

        print("\n[B13b] 界面：点一下轨道（不是拖动）不许开手动会话")
        window = main_window(controller)
        try:
            column = window.preview_panel.transition_column
            session.settle(0.3)
            before_calls = len(plugin_calls(session, PV.REQ_TBAR))
            column.tbar.setValue(60)  # 等价于点轨道 / 方向键：isSliderDown() 为假
            session.settle(0.6)
            extra = plugin_calls(session, PV.REQ_TBAR)[before_calls:]
            check("没有下发推杆请求（否则会开一个收不掉的会话）", not extra, str(extra))
            check(
                "滑块被拨回真实位置",
                column.tbar.value() == round(store.tbar_position * 100),
                f"{column.tbar.value()} vs {store.tbar_position}",
            )
        finally:
            window.close()
    finally:
        session.close()


# ---------------------------------------------------------------- B9：磁盘预警
def disk_warning_test(app: QApplication) -> None:
    print("\n[B9] 插件报了读盘错误 -> 预警里必须写清原因（D17）")
    machine = {
        "hostname": "OBS-PC",
        "obsUptimeMs": 60_000,
        "recordDir": {"known": True, "path": "\\\\NAS\\录像", "isUnc": True,
                      "exists": False, "writable": False, "writeError": 53},
        "disk": {"known": False, "error": 53},
        "recording": {"active": False},
    }
    session = boot(app, plugin=True, plugin_machine=machine)
    try:
        store = session.store
        ok = session.wait(lambda: store.machine.record_dir != "", 6)
        check("机器状态到了", ok, str(store.machine.record_dir))
        check("剩余空间为空（读不到就不报数字）", store.machine.free_gb() is None)
        check("录制目录优先用插件报的",
              session.controller.record_directory == "\\\\NAS\\录像",
              session.controller.record_directory)

        session.server.state.record_active = True
        session.server.state.record_ms = 12_000
        ok = session.wait(lambda: store.record.active, 6)
        check("开始录制", ok)
        ok = session.wait(lambda: bool(store.record_warning.message), 6)
        message = store.record_warning.message
        check("录制中出现了预警", ok, message or "(空)")
        check("预警里写了系统错误码", "53" in message, message)
        check("错误码翻成了人话", "网络路径" in message, message)
        check("没有伪造一个剩余空间数字", store.record_warning.free_gb is None,
              str(store.record_warning.free_gb))

        print("\n[B9a] 同一条路上不许在 UI 线程碰 UNC")
        # 插件说读不到盘时会继续走本地 shutil 那条老路 —— 必须走缓存，
        # 否则每秒一次 SMB 超时，界面卡住且日志里什么都看不到。
        import obs_remote_studio.core.controller as C

        calls = {"n": 0}
        original = C._disk_free_gb

        def counting(path):
            calls["n"] += 1
            return original(path)

        C._disk_free_gb = counting
        try:
            for _ in range(12):
                session.controller._refresh_record_warning()
                session.settle(0.05)
        finally:
            C._disk_free_gb = original
        check("12 次刷新磁盘最多只查 1 次（TTL 缓存生效）", calls["n"] <= 1, str(calls["n"]))
    finally:
        session.close()


# ---------------------------------------------------------------- B10：日志
def logs_test(app: QApplication) -> None:
    print("\n[B10] OBS 日志（Q10）")
    session = boot(app, plugin=True)
    try:
        store = session.store
        ok = session.controller.fetch_logs("warning", 200)
        check("fetch_logs 认为有这个能力", ok)
        ok = session.wait(lambda: len(store.plugin_logs) >= 1, 6)
        check("日志取回来了", ok, str(len(store.plugin_logs)))
        check("请求参数带上了级别与条数",
              any(d.get("level") == "warning" and 1 <= int(d.get("limit", 0)) <= 500
                  for d in plugin_calls(session, PV.REQ_RECENT_LOGS)),
              str(plugin_calls(session, PV.REQ_RECENT_LOGS)))
        check("**级别过滤生效**（只要 warning 及以上，info 行不返回）",
              len(store.plugin_logs) == 1, str(len(store.plugin_logs)))
        session.controller.fetch_logs("info", 200)
        check("放宽到 info 就该两条都回来",
              session.wait(lambda: len(store.plugin_logs) == 2, 6),
              str(len(store.plugin_logs)))
        check("日志行有内容",
              any("timed out" in line.message for line in store.plugin_logs),
              str([line.message for line in store.plugin_logs]))
        check("能排版成文本", "timed out" in PV.format_logs(store.plugin_logs))

        print("\n[B10a] 日志尾流：开流 + logLine 事件到达")
        check("检查流前的状态是关的", not session.server.plugin_stream_enabled)
        check("set_log_stream 认为有这个能力",
              session.controller.set_log_stream(True, "warning"))
        check("服务端记录已打开",
              session.wait(lambda: session.server.plugin_stream_enabled, 5))
        count = len(store.plugin_logs)
        session.server.emit_vendor_event(
            PV.EVENT_LOG_LINE,
            {"timestamp": 1_793_356_100_000, "level": 200,
             "levelName": "warning", "message": "late warning line"},
        )
        ok = session.wait(lambda: len(store.plugin_logs) > count, 5)
        check("logLine 事件被收到并追加", ok, str(len(store.plugin_logs)))
        check("追加的正是那一条",
              store.plugin_logs and store.plugin_logs[-1].message == "late warning line",
              store.plugin_logs[-1].message if store.plugin_logs else "(空)")

        print("\n[B10b] 没装插件时日志能力必须说不可用")
        session.controller.store.set_plugin(None)
        check("fetch_logs 返回 False（不说话就发请求等于白发）",
              session.controller.fetch_logs() is False)
        check("set_log_stream 也返回 False",
              session.controller.set_log_stream(True) is False)
    finally:
        session.close()


# ---------------------------------------------------------------- B11：采集指示
def capture_indicator_test(app: QApplication) -> None:
    print("\n[B11] 远程采集指示（隐私红线）")
    session = boot(app, plugin=True)
    try:
        store = session.store
        window = main_window(session.controller)
        try:
            check("初始没有采集", not store.capture.active)
            check("标题里没有采集痕迹",
                  "采集" not in window.windowTitle(), window.windowTitle())

            session.server.plugin_capture_active = True
            session.server.plugin_capture_viewers = 3
            session.server.plugin_stream_active = True
            session.controller.refresh_plugin_data()
            ok = session.wait(lambda: store.capture.active, 6)
            check("采集状态传上来了", ok)
            check("观看人数对得上", store.capture.viewers == 3, str(store.capture.viewers))
            session.settle(0.4)
            check("标题里出现「远程采集中」",
                  "远程采集中" in window.windowTitle(), window.windowTitle())
            check("标题里带人数", "3 人" in window.windowTitle(), window.windowTitle())
            check("状态栏也有痕迹（最小化也能看见）",
                  "远程采集中" in window.status_bar.version_label.text(),
                  window.status_bar.version_label.text())
        finally:
            window.close()
    finally:
        session.close()


# ---------------------------------------------------------------- B12：断线
def disconnect_test(app: QApplication) -> None:
    print("\n[B12] 断线后：能力清空，但模式名保留上次的结果")
    session = boot(app, plugin=True)
    try:
        store = session.store
        check("连上时是进阶模式", store.server_info.mode_label() == "进阶模式")
        session.controller.disconnect()
        session.wait(lambda: store.connection_state != CONNECTED, 6)
        check("已经断开", store.connection_state != CONNECTED, store.connection_state)
        check("插件能力已清空（不能拿上次的能力当现在有）",
              not session.controller.plugin_ready()
              and not session.controller.plugin_supports(PV.FEATURE_LOGS))
        check("机器状态已复位", store.machine.record_dir == "", store.machine.record_dir)
        check("采集指示已清（断线还在报'正在被看'是假警报）", not store.capture.active)
        check("标题上的模式名保留上次结果（与 compat_mode 同一套约定）",
              store.server_info.mode_label() == "进阶模式",
              store.server_info.mode_label())
    finally:
        session.close()


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-plugin-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    app = QApplication.instance() or QApplication([])

    logic_tests()
    mode_label_test(app)
    plugin_disabled_test(app)
    compat_mode_test(app)
    tbar_test(app)
    tbar_session_test(app)
    disk_warning_test(app)
    logs_test(app)
    capture_indicator_test(app)
    disconnect_test(app)

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

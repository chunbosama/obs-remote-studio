"""P3：场景级转场覆盖的回归测试。

守两类东西：

**一、协议语义**（照服务端 `RequestHandler_Scenes.cpp` 与 v4 协议文档核实过）
- v5：`Get` 回来没有覆盖时两个字段是 **null**；`Set` 的两个字段都可选、都允许 null，
  null = 删掉该项；两个都不给 → 400；名字不存在 → 600；时长越界 → 400。
- v4：**它也有这个能力**（4.8.0 起），但用 `transitionName=""` / `transitionDuration=-1`
  表示"无覆盖"，清除要走专门的 `RemoveSceneTransitionOverride`。
  翻译层必须把这两种语义对上 —— 这是本测试最要紧的一组断言。

**二、界面行为**
- 「无」是下拉第一项，对应清除；
- 覆盖时长在**没选转场名**时必须置灰并说明原因（OBS 只在覆盖转场存在时才读时长）；
- 服务端不支持时（老 v4）整组置灰并写明原因，不出现"点了没反应"。

运行：python tests/scene_override_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 与真实配置隔离（Windows 上 QSettings(org, app) 走注册表，必须换 org + INI）
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import socket  # noqa: E402

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fake_obs_server import DEFAULT_PASSWORD, FakeObsServer  # noqa: E402
from fake_obs_v4_server import FakeObsV4Server  # noqa: E402

from obs_remote_studio.app import apply_theme  # noqa: E402
from obs_remote_studio.core import protocol as P  # noqa: E402
from obs_remote_studio.core import protocol_v4 as P4  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED, StateStore  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402
from obs_remote_studio.ui.widgets.transitions_panel import OVERRIDE_NONE_LABEL  # noqa: E402

CHECKS: list[str] = []
FAILED: list[str] = []
SLOT_ERRORS: list[str] = []


def _slot_exception_hook(kind, value, traceback_) -> None:
    """接住 Qt 槽函数里抛的异常：Qt 只打印不中断，会让"功能坏了测试却全绿"。"""
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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout: float, app) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------- 纯逻辑：v4 翻译层
def test_v4_translation() -> None:
    print("\n[1] v4 翻译层：三条请求、两处语义差异")

    # 能力合成：三条齐备才算支持
    caps = P4.synthesize_capabilities(
        [
            "GetSceneTransitionOverride",
            "SetSceneTransitionOverride",
            "RemoveSceneTransitionOverride",
        ]
    )
    check("v4 三条齐备 -> 读到 v5 的 GET 能力",
          P.REQ_GET_SCENE_TRANSITION_OVERRIDE in caps)
    check("v4 三条齐备 -> 读到 v5 的 SET 能力",
          P.REQ_SET_SCENE_TRANSITION_OVERRIDE in caps)
    partial = P4.synthesize_capabilities(["GetSceneTransitionOverride"])
    check("v4 只有 Get（4.8.0 之前）-> 不算支持 SET",
          P.REQ_SET_SCENE_TRANSITION_OVERRIDE not in partial)
    check("v4 只有 Get -> 仍然提供 GET 能力",
          P.REQ_GET_SCENE_TRANSITION_OVERRIDE in partial)

    # 读：v4 的哨兵值必须翻成 v5 的 None
    calls: list[tuple] = []

    def fake_call(name, fields):
        calls.append((name, fields))
        return {"transitionName": "", "transitionDuration": -1}

    result = P4.RESOLVERS[P.REQ_GET_SCENE_TRANSITION_OVERRIDE](fake_call, {"sceneName": "开场"})
    check("v4 无覆盖（空串/-1）-> 翻成 None/None",
          result["transitionName"] is None and result["transitionDuration"] is None,
          str(result))
    check("v4 读请求名与字段正确",
          calls == [("GetSceneTransitionOverride", {"sceneName": "开场"})], str(calls))

    calls.clear()

    def fake_call_has(name, fields):
        calls.append((name, fields))
        return {"transitionName": "Fade", "transitionDuration": 500}

    result = P4.RESOLVERS[P.REQ_GET_SCENE_TRANSITION_OVERRIDE](
        fake_call_has, {"sceneName": "开场"}
    )
    check("v4 有覆盖 -> 原样翻成 v5 字段",
          result == {"transitionName": "Fade", "transitionDuration": 500}, str(result))

    # 写：v5 的 null -> v4 的 Remove
    calls.clear()
    P4.RESOLVERS[P.REQ_SET_SCENE_TRANSITION_OVERRIDE](
        lambda n, f: calls.append((n, f)), {"sceneName": "开场", "transitionName": None}
    )
    check("v5 传 null -> v4 走 Remove（不是 Set 空串）",
          [c[0] for c in calls] == ["RemoveSceneTransitionOverride"], str(calls))
    check("Remove 带对场景名", calls[0][1] == {"sceneName": "开场"}, str(calls[0][1]))

    # 写：给了名字 -> v4 的 Set，时长只在有值时带上
    calls.clear()
    P4.RESOLVERS[P.REQ_SET_SCENE_TRANSITION_OVERRIDE](
        lambda n, f: calls.append((n, f)),
        {"sceneName": "开场", "transitionName": "Fade", "transitionDuration": 700},
    )
    check("v5 给名字+时长 -> v4 Set 带上两个字段",
          calls == [("SetSceneTransitionOverride",
                     {"sceneName": "开场", "transitionName": "Fade",
                      "transitionDuration": 700})], str(calls))

    calls.clear()
    P4.RESOLVERS[P.REQ_SET_SCENE_TRANSITION_OVERRIDE](
        lambda n, f: calls.append((n, f)),
        {"sceneName": "开场", "transitionName": "Fade", "transitionDuration": None},
    )
    check("时长是 None 时不带该字段（让 v4 沿用现有值）",
          calls == [("SetSceneTransitionOverride",
                     {"sceneName": "开场", "transitionName": "Fade"})], str(calls))


# ---------------------------------------------------------------- 纯逻辑：状态层
def test_state_store() -> None:
    print("\n[2] 状态层：覆盖按场景存，两种「空」要分清")
    store = StateStore()
    seen: list[int] = []
    store.transition_override_changed.connect(lambda: seen.append(1))

    check("初始没有数据时 override_for 回 None（不是「无覆盖」）",
          store.override_for("开场") is None, str(store.override_for("开场")))

    store.set_transition_override("开场", "Fade", 450)
    check("设置后能读回", store.override_for("开场") == ("Fade", 450),
          str(store.override_for("开场")))
    check("发了一次信号", len(seen) == 1, str(len(seen)))

    store.set_transition_override("开场", "Fade", 450)
    check("同值不重复发信号", len(seen) == 1, str(len(seen)))

    check("别的场景仍是「未知」（None）",
          store.override_for("主画面") is None, str(store.override_for("主画面")))

    store.set_transition_override("开场", None, None)
    check("清除后是「确认无覆盖」(None, None) 而不是 None",
          store.override_for("开场") == (None, None), str(store.override_for("开场")))

    store.clear_transition_override()
    check("clear 后回到「未知」", store.override_for("开场") is None,
          str(store.override_for("开场")))

    store.set_transition_override("开场", "Fade", 450)
    store.reset_runtime_state()
    check("断开重连后覆盖被清空（不留旧值冒充）",
          store.override_for("开场") is None, str(store.override_for("开场")))


# ---------------------------------------------------------------- 端到端：v5
def test_v5_end_to_end(app: QApplication) -> None:
    print("\n[3] v5 端到端：读 / 设 / 清除 / 被拒")
    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()

    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 1.0
    controller.connect(ConnectionConfig("127.0.0.1", port, DEFAULT_PASSWORD))
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 10, app)
    check("连上假 OBS", ok, controller.store.status_message)
    if not ok:
        controller.shutdown()
        server.stop()
        return

    wait_until(lambda: bool(controller.store.scenes), 5, app)
    wait_until(lambda: controller.store.override_for(controller.store.current_scene)
               is not None, 5, app)

    # **先把错误弹窗接管掉**：下面会故意触发 600 被拒，而主窗口把
    # store.error_raised 接到了**模态** QMessageBox（`_show_error`）。
    # 离屏环境没人点那个框，测试会卡死（表现为"跑不完"而不是断言失败）。
    # 这里把信号转存到列表，顺便断言"确实给了用户提示"。
    errors: list[tuple[str, str]] = []
    controller.store.error_raised.connect(
        lambda title, detail: errors.append((title, detail))
    )

    # **覆盖下拉的候选取自转场列表**：转场列表到达时它必须跟着重填，
    # 否则用户点开只有一个「无」，根本没法选覆盖转场（曾经就是这样）。
    window = MainWindow(controller)
    # 主窗口自己又接了一次 error_raised -> 模态框，这里把它断开，
    # 只保留上面那个收集器（否则模态框照样会挡住）。
    controller.store.error_raised.disconnect(window._show_error)
    panel = window.transitions_panel
    app.processEvents()
    check("转场列表到达后覆盖下拉被填上",
          panel.override_combo.count() > 1,
          str([panel.override_combo.itemText(i)
               for i in range(panel.override_combo.count())]))
    check("覆盖下拉里有具体的转场可选项",
          panel.override_combo.findData("Fade") > 0,
          str(panel.override_combo.findData("Fade")))

    target = controller.store.current_scene
    check("目标场景=当前节目场景（非演播室模式）",
          controller.transition_override_target() == target,
          controller.transition_override_target())
    check("初始状态是「确认无覆盖」（还没动过任何东西）",
          controller.store.override_for(target) == (None, None),
          str(controller.store.override_for(target)))

    # 走真实的用户交互：activated 正是点击选择时发出的信号
    fade_index = panel.override_combo.findData("Fade")
    panel.override_combo.setCurrentIndex(fade_index)
    panel.override_combo.activated.emit(fade_index)
    check("从下拉选择后服务端收到覆盖",
          wait_until(lambda: server.state.transition_overrides.get(
              target, {}).get("transition") == "Fade", 6, app),
          str(server.state.transition_overrides))

    # 改时长：editingFinished 触发节流提交
    panel.override_duration_spin.setValue(850)
    panel.override_duration_spin.editingFinished.emit()
    check("改时长后落到服务端",
          wait_until(lambda: server.state.transition_overrides.get(
              target, {}).get("duration") == 850, 6, app),
          str(server.state.transition_overrides))

    # 选「无」清除
    none_index = panel.override_combo.findData(None)
    panel.override_combo.setCurrentIndex(none_index)
    panel.override_combo.activated.emit(none_index)
    check("从下拉选「无」即清除覆盖",
          wait_until(lambda: target not in server.state.transition_overrides, 6, app),
          str(server.state.transition_overrides))
    # **必须现在关掉**：本窗口把 store.error_raised 接到模态 QMessageBox，
    # 而下面几段会故意触发 600 被拒 —— 窗口还开着的话离屏环境下会卡在
    # 一个永远等不到点击的模态框上（测试表现为"跑不完"，不是断言失败）。
    # 注意只断主窗口那一个槽（上面已在 `disconnect(window._show_error)` 做过），
    # 不能用无参 `disconnect()` —— 那会把我自己的收集器也一起断掉。
    window.close()

    print("  -- 设置覆盖")
    controller.set_transition_override("Fade", 700)
    ok = wait_until(
        lambda: server.state.transition_overrides.get(target, {}).get("transition") == "Fade",
        5, app,
    )
    check("服务端收到覆盖（转场名）", ok, str(server.state.transition_overrides))
    check("服务端收到覆盖（时长）",
          server.state.transition_overrides.get(target, {}).get("duration") == 700,
          str(server.state.transition_overrides))
    check("本地回读后一致",
          controller.store.override_for(target) == ("Fade", 700),
          str(controller.store.override_for(target)))

    print("  -- 只改时长（名字跟着一起下发）")
    controller.set_transition_override("Fade", 900)
    ok = wait_until(
        lambda: server.state.transition_overrides.get(target, {}).get("duration") == 900,
        5, app,
    )
    check("时长改动落到服务端", ok, str(server.state.transition_overrides))

    print("  -- 时长越界要被本地夹住（服务端会 400）")
    controller.set_transition_override("Fade", 999_999)
    ok = wait_until(
        lambda: server.state.transition_overrides.get(target, {}).get("duration")
        == P.TRANSITION_OVERRIDE_MAX_MS, 5, app,
    )
    check("超上限被夹到 20000", ok, str(server.state.transition_overrides))
    controller.set_transition_override("Fade", 1)
    ok = wait_until(
        lambda: server.state.transition_overrides.get(target, {}).get("duration")
        == P.TRANSITION_OVERRIDE_MIN_MS, 5, app,
    )
    check("低于下限被夹到 50", ok, str(server.state.transition_overrides))

    print("  -- 转场名不存在（服务端 600）要被撤回")
    controller.set_transition_override("不存在的转场", 500)
    # 乐观值先在本地生效
    check("乐观更新先显示新值",
          controller.store.override_for(target)[0] == "不存在的转场",
          str(controller.store.override_for(target)))
    ok = wait_until(
        lambda: controller.store.override_for(target)[0] != "不存在的转场", 5, app
    )
    check("被拒后回读真实状态，不留下 OBS 没接受的覆盖", ok,
          str(controller.store.override_for(target)))
    check("被拒时给了用户可读的提示（且说的是「没有这个转场」）",
          any("转场" in detail for _title, detail in errors), str(errors[:3]))

    print("  -- 清除（「无」）")
    controller.set_transition_override("Fade", 700)
    wait_until(lambda: server.state.transition_overrides.get(target, {}).get("duration") == 700,
               5, app)
    controller.clear_transition_override()
    ok = wait_until(lambda: target not in server.state.transition_overrides, 5, app)
    check("清除后服务端不再有该场景的覆盖", ok, str(server.state.transition_overrides))
    check("本地变回「确认无覆盖」",
          controller.store.override_for(target) == (None, None),
          str(controller.store.override_for(target)))

    print("  -- 切场景要跟着换覆盖（不能串味）")
    controller.set_transition_override("Swipe", 600)
    wait_until(lambda: server.state.transition_overrides.get(target, {}).get("transition")
               == "Swipe", 5, app)
    other = next(s.name for s in controller.store.scenes if s.name != target)
    controller.switch_scene(other)
    ok = wait_until(
        lambda: controller.store.override_scene == other
        and controller.store.override_for(other) is not None,
        6, app,
    )
    check("切场景后覆盖跟着换成新场景的", ok,
          f"{controller.store.override_scene} / {controller.store.override_for(other)}")
    check("新场景没有覆盖 -> 显示为「无覆盖」",
          controller.store.override_for(other) == (None, None),
          str(controller.store.override_for(other)))
    check("旧场景的覆盖没被抹掉",
          server.state.transition_overrides.get(target, {}).get("transition") == "Swipe",
          str(server.state.transition_overrides))

    controller.shutdown()
    server.stop()


# ---------------------------------------------------------------- 端到端：v4
def test_v4_end_to_end(app: QApplication) -> None:
    print("\n[4] v4 端到端：能力可用时也能设置（走 Remove 清除）")
    port = free_port()
    server = FakeObsV4Server(host="127.0.0.1", port=port, transition_override=True)
    server.start()

    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 1.0
    controller.connect(ConnectionConfig("127.0.0.1", port, DEFAULT_PASSWORD))
    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 12, app)
    check("连上 v4 假 OBS", ok, controller.store.status_message)
    if not ok:
        controller.shutdown()
        server.stop()
        return
    check("识别为兼容模式", controller.store.compat_mode is True)

    wait_until(lambda: bool(controller.store.scenes), 5, app)
    supported = controller.store.supports(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)
    check("v4 4.8.0+ 被识别为支持场景覆盖", supported,
          str(sorted(controller.store.supported_requests)[:5]))

    if supported:
        wait_until(
            lambda: controller.store.override_for(controller.store.current_scene)
            is not None, 5, app,
        )
        target = controller.store.current_scene
        check("v4 无覆盖时本地是「确认无覆盖」（-1 已翻成 None）",
              controller.store.override_for(target) == (None, None),
              str(controller.store.override_for(target)))

        controller.set_transition_override("Swipe", 800)
        ok = wait_until(
            lambda: server.state.transition_overrides.get(target, {}).get("transition")
            == "Swipe", 6, app,
        )
        check("v4 设置覆盖生效", ok, str(server.state.transition_overrides))
        check("v4 时长一并下发",
              server.state.transition_overrides.get(target, {}).get("duration") == 800,
              str(server.state.transition_overrides))

        # 清除必须走 RemoveSceneTransitionOverride（v4 没有 null 语义）
        server.state.override_remove_calls.clear()
        controller.clear_transition_override()
        ok = wait_until(lambda: bool(server.state.override_remove_calls), 6, app)
        check("v4 清除走 RemoveSceneTransitionOverride", ok,
              str(server.state.override_remove_calls))
        check("Remove 带对场景名", server.state.override_remove_calls[:1] == [target],
              str(server.state.override_remove_calls))
        ok = wait_until(lambda: target not in server.state.transition_overrides, 5, app)
        check("v4 清除后服务端不再有覆盖", ok, str(server.state.transition_overrides))

    controller.shutdown()
    server.stop()


# ---------------------------------------------------------------- 界面
def test_ui(app: QApplication) -> None:
    print("\n[5] 界面：置灰与「无」的语义")
    controller = Controller()
    window = MainWindow(controller)
    panel = window.transitions_panel
    store = controller.store

    check("下拉有「无（跟随全局）」且是第一项",
          panel.override_combo.count() >= 1
          and panel.override_combo.itemText(0) == OVERRIDE_NONE_LABEL,
          panel.override_combo.itemText(0) if panel.override_combo.count() else "(空)")
    check("「无」的数据是 None（供清除用）",
          panel.override_combo.itemData(0) is None)

    store.set_transitions(["Cut", "Fade", "Swipe"])
    store.set_current_scene("开场")
    store.set_connection_state(CONNECTED)
    store.set_capabilities(
        [P.REQ_GET_SCENE_TRANSITION_OVERRIDE, P.REQ_SET_SCENE_TRANSITION_OVERRIDE]
    )
    app.processEvents()
    check("支持时下拉可用", panel.override_combo.isEnabled())

    # 覆盖时长在没选转场名时必须置灰
    store.set_transition_override("开场", None, None)
    app.processEvents()
    check("无覆盖时时长框置灰（OBS 只在覆盖转场存在时读时长）",
          not panel.override_duration_spin.isEnabled())
    check("时长框写明了为什么不能改",
          "覆盖转场" in panel.override_duration_spin.toolTip(),
          panel.override_duration_spin.toolTip())
    check("提示说明用全局转场",
          "全局" in panel.override_hint.text()
          or "全局" in panel.override_hint.toolTip(),
          panel.override_hint.text())

    # 选了转场名后时长可改
    store.set_transition_override("开场", "Fade", 650)
    app.processEvents()
    check("有覆盖时时长框可用", panel.override_duration_spin.isEnabled())
    check("时长回显正确", panel.override_duration_spin.value() == 650,
          str(panel.override_duration_spin.value()))
    check("下拉选中该转场", panel.override_combo.currentData() == "Fade",
          str(panel.override_combo.currentData()))
    check("提示写明用了哪个转场",
          "Fade" in panel.override_hint.text()
          or "Fade" in panel.override_hint.toolTip(),
          panel.override_hint.text())

    # 还没查到时的文案与「无覆盖」要区分开
    store.clear_transition_override()
    app.processEvents()
    check("未查到时不写成「无覆盖」",
          "无覆盖" not in panel.override_hint.text(),
          panel.override_hint.text())
    check("未查到时给出读取中提示",
          "读取" in panel.override_hint.text(),
          panel.override_hint.text())

    # 服务端不支持 -> 整组置灰并写明原因
    store.set_capabilities(["GetSceneTransitionList"])
    app.processEvents()
    check("不支持时下拉置灰", not panel.override_combo.isEnabled())
    check("不支持时时长框置灰", not panel.override_duration_spin.isEnabled())
    check("不支持时写明原因", bool(panel.override_hint.text().strip()),
          panel.override_hint.text())
    check("不支持时 tooltip 也说明原因", bool(panel.override_combo.toolTip().strip()),
          panel.override_combo.toolTip())
    check("不支持时说明文字给的是完整原因",
          len(panel.override_hint.toolTip()) > len(panel.override_hint.text()),
          panel.override_hint.toolTip())

    # 版面预算：这个面板默认只有约 200px 高，新增控件不能把内容挤出可视区。
    # （曾经因为多加了"标签+下拉+标签+数字框+说明"五行而溢出，覆盖控件看不见。）
    check("面板内容能放进默认 dock 高度",
          panel.minimumSizeHint().height() <= 200,
          f"min={panel.minimumSizeHint().height()}")
    check("面板最小宽度没被文案顶宽",
          panel.minimumSizeHint().width() <= 260,
          f"min_w={panel.minimumSizeHint().width()}")

    # 老 v4（4.8.0 之前）的置灰原因要说得准：v4 **不是**从来没有这项能力，
    # 而是 4.8.0 才引入 —— 措辞必须给出"升级 v4 插件"这条出路，
    # 否则用户会以为只能换 OBS。
    check("登记了 v4 的最低版本",
          P4.V4_REQUEST_SINCE.get(P.REQ_SET_SCENE_TRANSITION_OVERRIDE) == "4.8.0",
          str(P4.V4_REQUEST_SINCE.get(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)))
    store.set_capabilities(["GetTransitionList", "GetCurrentTransition"])
    store.set_compat_mode(True)
    reason_text = store.support_reason(P.REQ_SET_SCENE_TRANSITION_OVERRIDE)
    check("兼容模式置灰原因提到 4.8.0", "4.8.0" in reason_text, reason_text)
    check("原因给出可操作的出路（升级 v4 插件）", "升级" in reason_text, reason_text)
    check("不笼统地说成「v4 没有这项能力」",
          "v4（兼容模式）没有这项能力" not in reason_text, reason_text)
    store.set_compat_mode(False)

    window.close()
    controller.shutdown()


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-override-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    app = QApplication([])
    app.setStyle("Fusion")
    apply_theme(app, "dark")

    test_v4_translation()
    test_state_store()
    test_ui(app)
    test_v5_end_to_end(app)
    test_v4_end_to_end(app)

    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    if SLOT_ERRORS:
        print(f"\n警告：测试期间有 {len(SLOT_ERRORS)} 个槽函数抛异常（Qt 默认会吞掉）")
        for item in SLOT_ERRORS[:3]:
            print("  " + item.splitlines()[-1])
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

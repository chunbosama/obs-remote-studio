"""E 模块（混音器）专项测试：音频源识别、音量、静音、电平表、高级音频属性。

运行：python tests/audio_test.py
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fake_obs_server import DEFAULT_PASSWORD, FakeObsServer  # noqa: E402
from obs_remote_studio.core import audio  # noqa: E402
from obs_remote_studio.core import protocol as P  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig  # noqa: E402
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


def wait_until(predicate, timeout: float = 8.0, app: QApplication | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.02)
    return predicate()


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_controller(port: int, **overrides) -> Controller:
    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.request_timeout_s = 1.0
    for key, value in overrides.items():
        setattr(controller.config, key, value)
    controller.config.connection = ConnectionConfig(
        "127.0.0.1", port, DEFAULT_PASSWORD
    )
    return controller


def main() -> int:
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp())

    app = QApplication([])

    print("[0] 纯计算：推子映射")
    for db in (0.0, -6.0, -30.0, 26.0, -100.0):
        position = audio.db_to_fader(db)
        check(f"{db:g} dB 往返一致", abs(audio.fader_to_db(position) - db) < 0.01,
              f"{audio.fader_to_db(position):.2f}")
    check("0 dB 落在推子 3/4 处", abs(audio.db_to_fader(0.0) - 0.75) < 0.01,
          f"{audio.db_to_fader(0.0):.3f}")
    check("静音显示 -∞", audio.format_db(audio.FADER_MIN_DB) == "-∞ dB")
    check("电平表解析（3 层嵌套）", audio.parse_meter_levels([[0.5, 0.7, 0.3]]) == 0.7)
    check("电平表解析（标量）", audio.parse_meter_levels([0.25]) == 0.25)
    check("电平表解析（空）", audio.parse_meter_levels(None) == 0.0)

    print("\n[0b] 配置坏值不许把程序带崩（回归）")
    # 病因：load_config() 在 app.py 里**窗口还没建**时就被调用，而所有数字项
    # 都是裸 int()/float()。配置是用户可编辑的本地数据（手工改 INI、写入被截断、
    # 旧版残留），一个非数字值就会让 ValueError 冒到顶层 —— 程序直接起不来，
    # 用户还会以为"双击了没反应"。坏值只该回落默认值。
    from obs_remote_studio.core.settings import AppSettings

    bad = AppSettings()
    bad._qs.setValue("poll/interval_ms", "abc")
    bad._qs.setValue("connection/port", "not-a-port")
    bad._qs.setValue("poll/request_timeout_s", "")
    bad._qs.setValue("health/heartbeat_ms", "5s")
    bad._qs.setValue("record/disk_warn_gb", "两G")
    bad._qs.setValue("transitions/quick", '[{"transition": "Fade", "duration_ms": "很久"}]')
    bad._qs.setValue(
        "connection/recent", '[{"host": "127.0.0.1", "port": "abc"}]'
    )
    bad._qs.sync()
    try:
        cfg = AppSettings().load_config()
        loaded = True
        detail = ""
    except BaseException as exc:  # noqa: BLE001 - 这里就是要证明它不抛
        cfg = None
        loaded = False
        detail = f"{type(exc).__name__}: {exc}"
    check("坏值配置仍能加载（不再启动即崩）", loaded, detail)
    if loaded:
        check("poll_interval_ms 回落默认 1000", cfg.poll_interval_ms == 1000,
              str(cfg.poll_interval_ms))
        check("port 回落默认 4455", cfg.connection.port == 4455,
              str(cfg.connection.port))
        check("request_timeout_s 回落默认 3.0", cfg.request_timeout_s == 3.0,
              str(cfg.request_timeout_s))
        check("heartbeat_interval_ms 回落默认 5000",
              cfg.heartbeat_interval_ms == 5000, str(cfg.heartbeat_interval_ms))
        check("disk_warn_gb 回落默认 2.0", cfg.disk_warn_gb == 2.0,
              str(cfg.disk_warn_gb))
        check("快捷转场槽位 duration_ms 回落 300",
              cfg.quick_transitions and cfg.quick_transitions[0]["duration_ms"] == 300,
              str(cfg.quick_transitions))
    # 最近连接的坏 port 也不能炸
    try:
        recent = AppSettings().recent_connections()
        check("最近连接的坏 port 回落 4455",
              recent and recent[0].port == 4455, str(recent))
    except BaseException as exc:  # noqa: BLE001
        check("最近连接的坏 port 回落 4455", False, f"{type(exc).__name__}: {exc}")
    # 好值不能被这条兜底改坏
    good = AppSettings()
    good._qs.setValue("poll/interval_ms", 750)
    good._qs.sync()
    check("合法值照常读取（兜底没把好值吃掉）",
          AppSettings().load_config().poll_interval_ms == 750,
          str(AppSettings().load_config().poll_interval_ms))

    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()
    controller = make_controller(port)
    controller.connect()

    ok = wait_until(lambda: controller.store.connection_state == CONNECTED, 15, app)
    check("连接成功", ok, controller.store.status_message)
    wait_until(lambda: len(controller.store.audio_inputs) >= 4, 10, app)
    # 列表到手后音量/静音是逐个补拉的，要等回填完再断言
    wait_until(
        lambda: all(
            item.volume_mul != 1.0 or item.muted or item.name == "桌面音频"
            for item in controller.store.audio_inputs
        ),
        8,
        app,
    )
    pump(app, 0.4)

    print("\n[1] E1 音频源识别")
    names = [item.name for item in controller.store.audio_inputs]
    check("列出音频源", set(names) == {"桌面音频", "麦克风", "媒体源2", "摄像头"}, str(names))
    check("纯视频源被过滤掉（字幕）", "字幕" not in names)

    print("\n[2] E2/E3/E4 音量与静音回显")
    mic = controller.store.find_audio_input("麦克风")
    check("音量回显", mic is not None and abs(mic.volume_db + 6.0) < 0.01,
          f"{mic.volume_db if mic else '?'}")
    check("静音回显", mic is not None and mic.muted)

    controller.set_input_volume("桌面音频", -20.0)
    wait_until(lambda: abs(server.state.volumes_db["桌面音频"] + 20.0) < 0.01, 5, app)
    check("设置音量生效", abs(server.state.volumes_db["桌面音频"] + 20.0) < 0.01,
          f"{server.state.volumes_db['桌面音频']}")

    controller.set_input_mute("桌面音频", True)
    wait_until(lambda: server.state.muted["桌面音频"], 5, app)
    check("设置静音生效", server.state.muted["桌面音频"])
    controller.toggle_input_mute("桌面音频")
    wait_until(lambda: not server.state.muted["桌面音频"], 5, app)
    check("切换静音生效", not server.state.muted["桌面音频"])
    # 本地是我们乐观更新的，随后 OBS 的 InputMuteStateChanged 会回来校准；
    # 两次写操作挨得太近时，前一次的回声可能后到，所以这里等它收敛而不是立刻断言。
    converged = wait_until(
        lambda: not controller.store.find_audio_input("桌面音频").muted, 5, app
    )
    local_muted = controller.store.find_audio_input("桌面音频").muted
    check("本地状态与服务端收敛", converged, f"本地 muted={local_muted}")

    print("\n[3] E5 电平表（高频事件）")
    wait_until(lambda: bool(controller.store.meters), 8, app)
    meters = controller.store.meters
    check("收到电平数据", bool(meters), str(meters))
    check("桌面音频电平 ≈0.62", abs(meters.get("桌面音频", 0) - 0.62) < 0.01,
          str(meters.get("桌面音频")))
    check("服务端确实在推高频事件", server.state.meter_events > 0, str(server.state.meter_events))

    print("\n[3b] E9 批量静音的失败统计不许漏记（回归）")
    # 病因：判据是 `self._mute_all_pending`（裸计数），而收尾定时器会把它清零。
    # 清零之后到达的失败就掉出这个分支 —— 换成通用错误分支，把内部请求名
    # `SetInputMute` 直接弹给用户；更糟的是失败数就此丢掉，
    # "报了 3 个源、前 2 个失败、最后一个失败没人知道"。
    # 现在改用时间窗口判定，收尾后仍留一段宽限期。
    from obs_remote_studio.core.controller import _request_label

    sent_mutes: list[str] = []
    real_send = controller.send
    controller.send = lambda rt, data=None: sent_mutes.append(rt) or real_send(rt, data)
    try:
        controller.set_all_muted(True)
    finally:
        controller.send = real_send
    check("批量静音逐源下发", sent_mutes.count("SetInputMute") >= 1, str(sent_mutes[:5]))
    check("下发后处于累计窗口内", controller._mute_all_in_window())

    controller._on_request_failed("SetInputMute", "boom", False, 0)
    controller._on_request_failed("SetInputMute", "boom", False, 0)
    controller._flush_mute_all()          # 收尾定时器到点
    after_settle = controller._mute_all_failures
    controller._on_request_failed("SetInputMute", "boom", False, 0)  # 迟到的失败
    check("收尾后迟到的失败仍被计入（不再被静默丢掉）",
          controller._mute_all_failures == after_settle + 1,
          f"{after_settle} -> {controller._mute_all_failures}")
    controller._flush_mute_all()

    # 用户可读标签：内部请求名不该出现在给用户看的文案里
    check("内部请求名有用户可读名称",
          _request_label("SetInputMute") == "切换静音",
          _request_label("SetInputMute"))
    check("未登记的请求名回落中性说法（不泄漏内部名）",
          _request_label("SomeInternalRequest") == "操作",
          _request_label("SomeInternalRequest"))

    print("\n[4] E6 高级音频属性")
    controller.fetch_advanced_audio("媒体源2")
    wait_until(lambda: controller.store.find_audio_input("媒体源2").tracks is not None, 8, app)
    item = controller.store.find_audio_input("媒体源2")
    check("监听类型回填", item.monitor_type == P.MONITOR_NONE, str(item.monitor_type))
    check("声道平衡回填", item.balance is not None and abs(item.balance - 0.5) < 0.01,
          str(item.balance))
    check("同步偏移回填", item.sync_offset_ms == 0, str(item.sync_offset_ms))
    check("混音轨回填", item.tracks == 0b000001, bin(item.tracks or 0))

    controller.set_monitor_type("媒体源2", P.MONITOR_AND_OUTPUT)
    wait_until(lambda: server.state.monitor_types["媒体源2"] == P.MONITOR_AND_OUTPUT, 5, app)
    check("设置监听类型生效", server.state.monitor_types["媒体源2"] == P.MONITOR_AND_OUTPUT)

    controller.set_balance("媒体源2", 0.8)
    wait_until(lambda: abs(server.state.balance["媒体源2"] - 0.8) < 0.01, 5, app)
    check("设置声道平衡生效", abs(server.state.balance["媒体源2"] - 0.8) < 0.01)

    controller.set_sync_offset("媒体源2", 120)
    wait_until(lambda: server.state.sync_offset["媒体源2"] == 120, 5, app)
    check("设置同步偏移生效", server.state.sync_offset["媒体源2"] == 120)

    controller.set_tracks("媒体源2", 0b000011)
    wait_until(lambda: server.state.tracks["媒体源2"] == 0b000011, 5, app)
    check("设置混音轨生效", server.state.tracks["媒体源2"] == 0b000011)

    print("\n[5] E1 手动隐藏噪声源 + 增量刷新")
    controller.set_input_hidden("摄像头", True)
    wait_until(lambda: "摄像头" not in [i.name for i in controller.store.audio_inputs], 8, app)
    check("隐藏后从列表消失",
          "摄像头" not in [i.name for i in controller.store.audio_inputs])
    check("隐藏项写入配置", "摄像头" in controller.config.mixer_hidden_inputs)
    controller.set_input_hidden("摄像头", False)
    wait_until(lambda: "摄像头" in [i.name for i in controller.store.audio_inputs], 8, app)
    check("取消隐藏后回来", "摄像头" in [i.name for i in controller.store.audio_inputs])

    server.add_input("新麦克风")
    wait_until(
        lambda: "新麦克风" in [i.name for i in controller.store.audio_inputs], 8, app
    )
    check("新建输入自动进混音器",
          "新麦克风" in [i.name for i in controller.store.audio_inputs])
    server.remove_input("新麦克风")
    wait_until(
        lambda: "新麦克风" not in [i.name for i in controller.store.audio_inputs], 8, app
    )
    check("删除输入自动移除",
          "新麦克风" not in [i.name for i in controller.store.audio_inputs])

    print("\n[6] 关掉电平表时不订阅高频事件")
    check("掩码含高频位", bool(P.subscription_mask(True) & HIGH_VOLUME_BIT))
    check("掩码不含高频位", not (P.subscription_mask(False) & HIGH_VOLUME_BIT))
    check("基础位仍然齐全",
          P.subscription_mask(False) == P.subscription_mask(True) - HIGH_VOLUME_BIT)
    controller.disconnect()
    wait_until(lambda: controller.store.connection_state != CONNECTED, 5, app)
    controller.shutdown()

    quiet = make_controller(port, audio_meters=False)
    quiet.connect()
    wait_until(lambda: quiet.store.connection_state == CONNECTED, 15, app)
    wait_until(lambda: bool(quiet.store.audio_inputs), 8, app)
    pump(app, 1.0)
    check("音频源仍能列出", bool(quiet.store.audio_inputs))
    check("但没有电平表数据", not quiet.store.meters, str(quiet.store.meters))
    quiet.shutdown()

    server.stop()
    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


HIGH_VOLUME_BIT = 1 << 16

if __name__ == "__main__":
    raise SystemExit(main())

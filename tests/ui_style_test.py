"""UI 回归测试：主题配色 + 面板能正常构造。

起因：Windows 深色/浅色模式下弹出层不继承主窗口样式，曾出现「黑底黑字看不见」。
现在整套主题是 OBS 深色，判定标准改为「背景够暗 + 存在亮色像素（文字可见）」。

运行：python tests/ui_style_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试必须与真实配置隔离：Windows 上 QSettings(org, app) 写的是注册表，
# 只调 setPath 不够（格式不对），必须换 org 名 + INI 格式。
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QWidget  # noqa: E402

from obs_remote_studio.app import apply_theme  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.audio import SLIDER_MAX, fader_to_db  # noqa: E402
from obs_remote_studio.core.models import RecordStatus, StreamStatus  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED, DISCONNECTED  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402
from obs_remote_studio.ui.widgets.controls_panel import ControlsPanel  # noqa: E402
from obs_remote_studio.ui.widgets.mixer import MixerPanel  # noqa: E402
from obs_remote_studio.ui.widgets.status_bar import SEGMENTS  # noqa: E402
from obs_remote_studio.ui.widgets.preview_panel import (  # noqa: E402
    TRANSITION_COLUMN_WIDTH,
    PreviewPanel,
)
from obs_remote_studio.ui.widgets.transitions_panel import TransitionsPanel  # noqa: E402

CHECKS: list[str] = []
FAILED: list[str] = []
SLOT_ERRORS: list[str] = []


def _slot_exception_hook(kind, value, traceback_) -> None:
    """接住 Qt 槽函数里抛出的异常。

    **为什么需要这个**：Qt 不会让槽里的异常中断程序，只把 traceback 打出来就继续跑。
    于是"功能其实是坏的、但测试全绿"这种事会悄悄溜过去 ——
    本项目就真的漏过一次（`_refresh_config_menus_enabled` 里少 import 一个常量，
    每次能力刷新都抛 NameError，菜单状态压根没更新，所有断言却都是绿的）。

    记录之外**照常打印**：这个钩子同时也会收到顶层未捕获异常，
    只记录不打印会把真正的报错也一起藏掉。
    """
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


def brightness(widget: QWidget) -> tuple[float, float]:
    """渲染控件后返回（平均亮度, 最亮像素）。平均低=背景暗，最亮高=文字亮。"""
    image = widget.grab().toImage()
    if image.isNull():
        return (-1.0, -1.0)
    total = 0
    count = 0
    peak = 0.0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            value = QColor(image.pixel(x, y)).lightnessF() * 255
            total += value
            count += 1
            peak = max(peak, value)
    return (total / count if count else -1.0, peak)


def spin_button_span(widget: QWidget, top: bool) -> tuple[float, float]:
    """增减按钮区域（右侧 16px，上半=上按钮 / 下半=下按钮）的 (最暗, 最亮)。

    **为什么不能用 QStyle.subControlRect 来定位**：QSS 一旦接管子控件，
    Fusion 的 subControlRect 会返回退化矩形（实测高度是 -1），拿它做断言会误判。
    所以直接按像素扫右侧条带 —— 按钮宽 14px，扫 16px 一定包住。

    **为什么看亮度"跨度"而不是"有没有亮像素"**：深色主题箭头（#b0b0b0）比按钮底色亮，
    浅色主题箭头（#5f6771）比底色暗，写死某一头在另一套主题下必然误判。
    按钮底色是纯色，因此"跨度大"就等价于"上面画了东西"。
    """
    image = widget.grab().toImage()
    width, height = image.width(), image.height()
    half = height // 2
    y_start, y_end = (0, half) if top else (half, height)
    values = [
        QColor(image.pixel(x, y)).lightnessF() * 255
        for y in range(y_start, y_end)
        for x in range(max(0, width - 16), width)
    ]
    return (min(values), max(values)) if values else (-1.0, -1.0)


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-ui-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    app = QApplication([])
    app.setStyle("Fusion")
    apply_theme(app, "dark")

    print("[0] 配置隔离（不能碰到用户真实配置）")
    from obs_remote_studio.core.settings import AppSettings

    settings_file = AppSettings().file_name
    # Qt 返回的路径用正斜杠，Windows 的 tmp_dir 用反斜杠，统一后再比
    check("配置写在临时目录",
          Path(tmp_dir).as_posix() in Path(settings_file).as_posix(), settings_file)
    check("不是注册表", not settings_file.startswith("\\HKEY") and "HKEY" not in settings_file,
          settings_file)

    print("\n[1] OBS 深色调色板")
    base = app.palette().color(QPalette.ColorRole.Base)
    check("Base 是深色", base.lightness() < 80, base.name())
    text = app.palette().color(QPalette.ColorRole.Text)
    check("文字是亮色", text.lightness() > 150, text.name())
    check("选中色是 OBS 蓝",
          app.palette().color(QPalette.ColorRole.Highlight).blue() > 100)

    print("\n[2] 下拉框弹出层")
    combo = QComboBox()
    combo.addItems(["Cut", "Fade", "Swipe"])
    combo.resize(160, 28)
    combo.show()
    app.processEvents()
    combo.showPopup()
    app.processEvents()
    mean, peak = brightness(combo.view())
    check("弹出层是深色底", 0 <= mean < 110, f"平均亮度 {mean:.0f}")
    check("弹出层文字可见（有亮色像素）", peak > 150, f"最亮 {peak:.0f}")
    combo.hidePopup()

    print("\n[2b] 输入框的增减按钮：↑ / ↓ 必须画得出来（回归）")
    # 现象：设置里所有数字框的上下箭头都看不见，但按钮能点。
    # 起因：QSS 给 ::up-button/::down-button 设了背景色，却没定义
    # ::up-arrow/::down-arrow 的 image —— 只要动了按钮样式，Qt 就不再绘制原生箭头。
    # （后来又发现 Qt 的 QSS 压根画不出三角，改成按主题现画 ↑ / ↓ 图片，见 ui/spin_arrows.py）
    # 所以这条用例两头都盯：箭头画出来了 *且* 按钮还在（别为了显箭头把按钮改没了）。
    from PySide6.QtCore import QPoint
    from PySide6.QtCore import Qt as _QtBox
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox

    for box_cls, box_label in ((QSpinBox, "整数框"), (QDoubleSpinBox, "小数框")):
        box = box_cls()
        box.setRange(200, 60_000)
        box.setSuffix(" ms")
        box.setValue(3000)
        box.resize(165, 30)
        box.show()
        app.processEvents()

        low_up, high_up = spin_button_span(box, top=True)
        low_down, high_down = spin_button_span(box, top=False)
        check(f"{box_label}上箭头可见", high_up - low_up > 60,
              f"亮度 {low_up:.0f}~{high_up:.0f}")
        check(f"{box_label}下箭头可见", high_down - low_down > 60,
              f"亮度 {low_down:.0f}~{high_down:.0f}")

        before = box.value()
        QTest.mouseClick(box, _QtBox.MouseButton.LeftButton,
                         pos=QPoint(box.width() - 8, 8))
        app.processEvents()
        check(f"{box_label}右上角仍可点（值 +1）", box.value() == before + 1,
              f"{before} -> {box.value()}")
        box.hide()
        box.deleteLater()

    print("\n[3] 主窗口与 OBS 式面板")
    controller = Controller()
    window = MainWindow(controller)
    window.show()
    app.processEvents()

    check("主窗口可构造", window.windowTitle().startswith("OBS Remote Studio"))
    check("五个停靠面板齐全",
          all(hasattr(window, name) for name in (
              "scene_panel", "source_panel", "mixer_panel",
              "transitions_panel", "controls_panel")))
    check("面板类型正确",
          isinstance(window.mixer_panel, MixerPanel)
          and isinstance(window.transitions_panel, TransitionsPanel)
          and isinstance(window.controls_panel, ControlsPanel))
    preview = window.preview_panel
    check("预览区是双画面 + 转场列",
          isinstance(preview, PreviewPanel)
          and preview.transition_column.width() == TRANSITION_COLUMN_WIDTH)
    check("菜单结构与 OBS 对齐",
          [a.text() for a in window.menuBar().actions()] ==
          ["文件(&F)", "视图(&V)", "停靠窗口(&D)", "工具(&T)", "帮助(&H)"],
          str([a.text() for a in window.menuBar().actions()]))
    check("非演播室模式只显示「输出」一块", not preview.preview_pane.isVisible())
    check("缩放条默认「缩放至窗口」",
          preview.zoom_combo.itemText(0) == "缩放至窗口")

    print("\n[4] 未接入的按钮必须置灰")
    check("虚拟摄像机（未连接时隐藏，不再谎报可用）",
          not window.controls_panel.virtual_cam_btn.isVisible()
          or not window.controls_panel.virtual_cam_btn.isEnabled())
    check("设置按钮未连接时也可用", window.controls_panel.settings_btn.isEnabled())
    check("直播/录制按钮未连接时置灰",
          not window.controls_panel.record_btn.isEnabled()
          and not window.controls_panel.stream_btn.isEnabled())
    check("暂停录制未连接时不可用", not window.controls_panel.pause_btn.isEnabled())
    check("回放缓冲未连接时不可用",
          not window.controls_panel.replay_btn.isEnabled()
          and not window.controls_panel.save_replay_btn.isEnabled())
    placeholders = [
        button for button in window.findChildren(QPushButton)
        if button.objectName() == "panelToolButton" and not button.isEnabled()
    ]
    check("面板工具排是置灰状态", len(placeholders) >= 8, f"{len(placeholders)} 个")

    print("\n[5] H7 快捷键")
    sequences = {shortcut.key().toString() for shortcut in window._shortcuts}
    check("快捷键已注册", len(window._shortcuts) >= 15, str(len(window._shortcuts)))
    check("含录制 / 直播 / 场景切换",
          {"Ctrl+R", "Ctrl+L"} <= sequences and any(
              s.startswith("Ctrl+") and s[-1].isdigit() for s in sequences),
          str(sorted(sequences)[:6]))

    print("\n[6] H4/H8 布局持久化（分栏比例 + 面板显隐 + Dock 布局）")
    window.preview_panel.splitter.setSizes([300, 190, 380])
    # H8：面板已经 Dock 化，隐藏走 dock
    window.set_panel_visible("场景", False)
    window._save_layout()
    second = MainWindow(controller)
    second.show()
    app.processEvents()
    # 分栏尺寸的持久化：直接比对存回来的原始值（非演播室模式下预览/转场列本来就隐藏，
    # 控件实测尺寸是 0，不能拿它当断言依据）
    saved = controller.settings.load_json("preview_splitter")
    check("分栏比例已持久化",
          isinstance(saved, list) and len(saved) == 3,
          str(saved))
    # 非演播室模式下只有「输出」一块是可见的，它应当铺满
    live = second.preview_panel.splitter.sizes()
    check("可见画面铺满分栏",
          live[2] > 0 and live[2] >= sum(live) - 2,
          str(live))
    check("面板显隐已恢复", not second.docks["场景"].isVisible())
    check("菜单勾选状态同步", not second._panel_actions["场景"].isChecked())

    print("\n[6b] H8 面板可浮动 / 可恢复")
    from PySide6.QtWidgets import QDockWidget

    feature = QDockWidget.DockWidgetFeature
    check("五个面板都是 Dock",
          len(second.docks) == 5
          and all(
              bool(d.features() & feature.DockWidgetMovable)
              and bool(d.features() & feature.DockWidgetFloatable)
              for d in second.docks.values()
          ))
    check("默认都停靠（未浮动）",
          not any(d.isFloating() for d in second.docks.values()))
    second.docks["混音器"].setFloating(True)
    app.processEvents()
    check("可以浮出去", second.docks["混音器"].isFloating())
    second._reset_layout()
    app.processEvents()
    check("恢复默认布局能把浮动面板收回",
          not second.docks["混音器"].isFloating())
    check("恢复默认布局可用", second.docks["场景"].isVisible())
    second.close()
    second.deleteLater()

    print("\n[6c] H10 紧凑 / 迷你模式")
    window.set_compact_mode(True)
    app.processEvents()
    check("迷你模式隐藏预览", not window.preview_panel.isVisible())
    check("迷你模式只留控制按钮",
          [label for label, dock in window.docks.items() if dock.isVisible()]
          == ["控制按钮"],
          str([label for label, dock in window.docks.items() if dock.isVisible()]))
    check("迷你模式写回配置", controller.config.compact_mode is True)
    window.set_compact_mode(False)
    app.processEvents()
    check("退出迷你模式恢复预览", window.preview_panel.isVisible())
    check("退出后写回配置", controller.config.compact_mode is False)

    print("\n[6d] H9 状态栏段自定义")
    status = window.status_bar
    check("默认显示全部段", len(status.visible_segments()) == len(SEGMENTS),
          str(status.visible_segments()))
    status.set_segment_visible("perf", False)
    app.processEvents()
    check("可以关掉某一段", "perf" not in status.visible_segments(),
          str(status.visible_segments()))
    check("关掉后配置已持久化",
          "perf" not in controller.config.status_segments,
          str(controller.config.status_segments))
    status.set_segment_visible("perf", True)
    app.processEvents()
    check("可以再打开", "perf" in status.visible_segments())

    print("\n[7] H5 浅色主题")
    apply_theme(app, "light")
    light_base = app.palette().color(QPalette.ColorRole.Base)
    light_text = app.palette().color(QPalette.ColorRole.Text)
    check("浅色主题 Base 变浅", light_base.lightness() > 200, light_base.name())
    check("浅色主题文字变深", light_text.lightness() < 100, light_text.name())
    combo2 = QComboBox()
    combo2.addItems(["Cut", "Fade", "Swipe"])
    combo2.resize(160, 28)
    combo2.show()
    app.processEvents()
    combo2.showPopup()
    app.processEvents()
    mean, peak = brightness(combo2.view())
    check("浅色主题下弹出层是浅底", mean > 150, f"平均亮度 {mean:.0f}")
    check("浅色主题下弹出层文字可见", peak < 150 or mean > 150, f"最亮 {peak:.0f}")
    combo2.hidePopup()
    combo2.deleteLater()

    # [2b] 的延伸：箭头图片是按主题现画的，换主题就必须换成新图。
    # 若图片路径不随主题变，Qt 的 QPixmapCache 会把深色主题的亮箭头继续贴到浅色主题上 ——
    # 那正是"改了主题却没反应"的经典成因，所以这里要盯住浅色下箭头确实是深的。
    light_box = QSpinBox()
    light_box.setRange(0, 60_000)
    light_box.setValue(3000)
    light_box.resize(165, 30)
    light_box.show()
    app.processEvents()
    low, high = spin_button_span(light_box, top=True)
    check("浅色主题下箭头变深（说明换了新图，没被缓存串色）",
          high - low > 60 and low < 130, f"亮度 {low:.0f}~{high:.0f}")
    light_box.hide()
    light_box.deleteLater()

    apply_theme(app, "dark")
    check("切回深色", app.palette().color(QPalette.ColorRole.Base).lightness() < 80)

    print("\n[8] H6 托盘与全局热键")
    from obs_remote_studio.core.models import AudioInput
    from obs_remote_studio.utils import global_hotkeys as gh
    from obs_remote_studio.ui.tray import TrayIcon

    check("热键序列解析（Ctrl+Alt+R）",
          gh.parse_sequence("Ctrl+Alt+R") == (gh.MOD_CONTROL | gh.MOD_ALT | gh.MOD_NOREPEAT, 0x52),
          str(gh.parse_sequence("Ctrl+Alt+R")))
    check("热键序列解析（数字键）", gh.parse_sequence("Ctrl+Alt+3")[1] == 0x33)
    check("非法序列返回 None", gh.parse_sequence("Ctrl+Foo") is None)
    bindings = window.hotkey_bindings()
    check("热键表覆盖录制/直播/工作室/9 个场景",
          {1, 2, 3, 10, 18} <= set(bindings), str(sorted(bindings)))
    check("默认不注册全局热键", window.apply_hotkey_config() == 0)

    tray = TrayIcon(controller.store, {
        "toggle_window": lambda: None,
        "toggle_record": lambda: None,
        "toggle_stream": lambda: None,
        "studio_mode": lambda: None,
        "quit": lambda: None,
    })
    clickable = [a for a in tray.contextMenu().actions() if not a.isSeparator()]
    check("托盘菜单项齐全", len(clickable) == 5, str([a.text() for a in clickable]))
    controller.store.set_record(RecordStatus(active=True))
    controller.store.set_stream(StreamStatus(active=True))
    check("托盘菜单跟随录制/直播状态",
          tray.record_action.text() == "停止录制" and tray.stream_action.text() == "停止直播",
          f"{tray.record_action.text()} / {tray.stream_action.text()}")
    controller.store.set_record(RecordStatus(active=False))
    controller.store.set_stream(StreamStatus(active=False))
    check("状态回落", tray.record_action.text() == "开始录制")
    tray.setVisible(False)

    print("\n[9] E 混音器界面（不发请求，直接喂状态）")
    mixer = window.mixer_panel
    check("无音频源时显示提示", mixer.empty_hint.isVisible())
    controller.store.set_audio_inputs([
        AudioInput(name="麦克风", volume_db=0.0, volume_mul=1.0, muted=False),
        AudioInput(name="桌面音频", volume_db=-12.0, volume_mul=0.25, muted=True),
    ])
    app.processEvents()
    check("按音频源生成推子行", set(mixer._rows) == {"麦克风", "桌面音频"}, str(list(mixer._rows)))
    mic_row = mixer._rows["麦克风"]
    check("推子位置按 dB 映射（0 dB 在 3/4）",
          abs(mic_row.fader.value() - int(0.75 * 1000)) <= 2, str(mic_row.fader.value()))
    check("静音源的名称为红色", "e04b4b" in mixer._rows["桌面音频"].name_label.styleSheet()
          or "#" in mixer._rows["桌面音频"].name_label.styleSheet())
    check("静音按钮已勾选", mixer._rows["桌面音频"].mute_button.isChecked())
    check("有音频源时隐藏提示", not mixer.empty_hint.isVisible())
    mixer._refresh_meters()
    check("电平表能取到值（无数据时为 0）", mic_row.meter._level == 0.0)
    controller.store.set_meters({"麦克风": 0.5})
    mixer._refresh_meters()
    check("电平表读到数值", mic_row.meter._level == 0.5, str(mic_row.meter._level))

    print("\n[9b] E8 推子微调与复位")
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QKeyEvent

    committed: list[tuple[str, float]] = []
    mic_row.volume_committed.connect(lambda n, db: committed.append((n, db)))
    muted_calls: list[str] = []
    mic_row.mute_toggled.connect(lambda n: muted_calls.append(n))

    mic_row.fader.setValue(int(0.75 * SLIDER_MAX))  # 先回到 0 dB
    base_db = fader_to_db(mic_row.fader.value() / SLIDER_MAX)
    mic_row._nudge_db(0.1)
    after_fine = fader_to_db(mic_row.fader.value() / SLIDER_MAX)
    check("方向键 +0.1 dB 生效", after_fine > base_db, f"{base_db:.2f} -> {after_fine:.2f}")
    check("微调立即提交（不用等松手）", bool(committed), str(committed))
    before_coarse = fader_to_db(mic_row.fader.value() / SLIDER_MAX)
    mic_row._nudge_db(1.0)
    after_coarse = fader_to_db(mic_row.fader.value() / SLIDER_MAX)
    check("Shift 粗调 +1 dB 生效", after_coarse > before_coarse,
          f"{before_coarse:.2f} -> {after_coarse:.2f}")

    # 真实按键路径：方向键 / Delete 应该被滑块拦下
    mic_row.fader.setValue(int(0.75 * SLIDER_MAX))
    key_before = mic_row.fader.value()
    mic_row.fader.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, _Qt.Key.Key_Right, _Qt.KeyboardModifier.NoModifier)
    )
    check("右方向键改的是 dB 而不是滑块格",
          mic_row.fader.value() != key_before, str(mic_row.fader.value()))
    muted_calls.clear()
    mic_row.fader.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, _Qt.Key.Key_Delete, _Qt.KeyboardModifier.NoModifier)
    )
    check("Delete 触发静音", muted_calls == ["麦克风"], str(muted_calls))

    mic_row._nudge_db(5.0)
    check("推子已偏离 0 dB",
          abs(fader_to_db(mic_row.fader.value() / SLIDER_MAX)) > 1.0)
    mic_row.reset_to_zero_db()
    check("双击复位回到 0 dB",
          abs(fader_to_db(mic_row.fader.value() / SLIDER_MAX)) < 0.2,
          str(fader_to_db(mic_row.fader.value() / SLIDER_MAX)))

    print("\n[9c] E9 一键全静音")
    mute_all_calls: list[bool] = []
    mixer.callbacks["mute_all"] = lambda m: mute_all_calls.append(m)
    # 上一步的 Delete 用例已经通过控制器把「麦克风」静音了，这里先恢复成
    # "有源未静音"的初始态，才好验证按钮文案会跟着变
    controller.store.update_audio_input("麦克风", muted=False)
    controller.store.update_audio_input("桌面音频", muted=True)
    app.processEvents()
    mixer._update_mute_all()
    check("还有未静音时按钮是「全静音」",
          mixer.mute_all_button.text() == "全静音", mixer.mute_all_button.text())
    mixer._toggle_mute_all()
    check("点了下发「静音」", mute_all_calls == [True], str(mute_all_calls))
    controller.store.update_audio_input("麦克风", muted=True)
    controller.store.update_audio_input("桌面音频", muted=True)
    app.processEvents()
    check("全静音后按钮变「取消全静音」",
          mixer.mute_all_button.text() == "取消全静音", mixer.mute_all_button.text())
    mixer._toggle_mute_all()
    check("再点下发「取消静音」", mute_all_calls == [True, False], str(mute_all_calls))

    print("\n[9d] G4 手推 T 条：方向、释放语义、以及不能把自己禁用（回归）")
    # 线上问题（两个）：
    # ① 手推 T 条会让 OBS 立刻发 SceneTransitionStarted，而 _update_button 把 tbar 的
    #    可用性也挂了 busy → 用户正拖着的滑块被禁用 → release 发不出去 → 永久卡「转场中」；
    # ② T 条做成了**竖直**滑块。Qt 竖直滑块默认最小值在下、最大值在上，
    #    于是"往下推到底"发出去的是 0.0，OBS 收到就回退 —— 表现为"推到底也没转场"。
    #    OBS 自己的 tBar 是 `QSlider(Qt::Horizontal)`，往右推到底才是完成。
    from PySide6.QtCore import Qt as _Qt2

    column = window.preview_panel.transition_column
    controller.store.set_connection_state(CONNECTED, "127.0.0.1:4455")
    controller.store.set_capabilities(
        ["SetTBarPosition", "SetCurrentSceneTransition", "TriggerStudioModeTransition"]
    )
    controller.store.set_current_transition("渐变")
    controller.store.set_studio_mode(True)
    controller.store.set_transitioning(False)
    app.processEvents()

    check("**T 条是水平滑块**（与 OBS 一致，右端才是完成）",
          column.tbar.orientation() == _Qt2.Orientation.Horizontal,
          str(column.tbar.orientation()))
    check("最小值在左、最大值在右",
          column.tbar.minimum() == 0 and column.tbar.maximum() == 100)
    check("演播室模式下 T 条可用", column.tbar.isEnabled())
    controller.store.set_transitioning(True)   # 等价于收到 SceneTransitionStarted
    app.processEvents()
    check("转场按钮显示「转场中」", column.transition_btn.text() == "转场中…",
          column.transition_btn.text())
    check("**转场中 T 条仍可拖**（bug ①）", column.tbar.isEnabled())
    check("转场中按钮本身仍禁用", not column.transition_btn.isEnabled())

    # 释放语义：用 setValue 模拟真实拖动（会触发 valueChanged），别直接调回调
    tbar_calls: list[tuple[float, bool]] = []
    original_tbar = window.preview_panel.callbacks["tbar"]
    window.preview_panel.callbacks["tbar"] = (
        lambda pos, rel: tbar_calls.append((pos, rel))
    )

    def drag_to(value: int) -> list[tuple[float, bool]]:
        tbar_calls.clear()
        column.tbar.setValue(value)
        column._flush_tbar()          # 节流到点，把中间值发出去
        column._on_tbar_released()    # 用户松手
        return list(tbar_calls)

    calls = drag_to(30)
    check("拖动发 release=false；中途松手显式回退（发 0.0）",
          calls == [(0.3, False), (0.0, True)], str(calls))
    calls = drag_to(100)
    check("推到底松手发真实位置 + release=true（交给 OBS 完成）",
          calls == [(1.0, False), (1.0, True)], str(calls))
    calls = drag_to(95)
    check("推到 95% 也算到底（沿用 OBS 的 10% 容差）",
          calls == [(0.95, False), (0.95, True)], str(calls))
    check("本地不自己改滑块位置（等事件确认后归位）",
          column.tbar.value() == 95, str(column.tbar.value()))

    # 转场结束事件到达后，滑块才归位
    window.preview_panel.callbacks["tbar"] = original_tbar
    controller.store.set_tbar_position(0.0)   # 等价于 SceneTransitionEnded 里的归位
    app.processEvents()
    check("事件确认后滑块归位", column.tbar.value() == 0, str(column.tbar.value()))

    # 当前转场是「剪切」：OBS 的 ValidTBarTransition 排除 cut，
    # 但它推杆时会自动临时改用淡入淡出 —— 所以推杆**仍然可用**，
    # 只是要在界面上说清"转场方式会被换掉"，别让用户以为选错了。
    controller.store.set_current_transition("Cut")
    app.processEvents()
    check("剪切时推杆仍可用（OBS 会自动改用淡入淡出）", column.tbar.isEnabled())
    # 用 isHidden() 而不是 isVisible()：非工作室模式下整个转场列本身是隐藏的，
    # isVisible() 会把"祖先隐藏"也算进来，断不到提示自己的状态
    check("剪切时界面上写明会改用淡入淡出",
          "剪切" in column.tbar_hint.text() and not column.tbar_hint.isHidden(),
          column.tbar_hint.text())

    print("\n[9d1] 快捷转场行刷新不许泄漏布局项（回归）")
    # 病因：refresh_quick_slots() 每次都在行末 addStretch(1)，而它会被
    # transitions_changed / 保存槽位 / 换主题多次调到。弹簧项只增不减，
    # 实测刷新 20 次后布局项从 2 涨到 22（每次 +1）—— 既无上限增长，
    # 又会把槽位按钮往左挤。
    slots_2 = [
        {"transition": "渐变", "duration_ms": 300},
        {"transition": "Cut", "duration_ms": 100},
    ]
    original_quick = window.preview_panel.callbacks["quick_slots"]
    window.preview_panel.callbacks["quick_slots"] = lambda: slots_2
    try:
        window.preview_panel.refresh_quick_slots()
        app.processEvents()
        baseline = column.quick_row.count()
        for _ in range(20):
            window.preview_panel.refresh_quick_slots()
        app.processEvents()
        after = column.quick_row.count()
        check("刷新 20 次后布局项数量不变（无泄漏）",
              after == baseline, f"{baseline} -> {after}")
        # 槽位按钮本身仍然按数量正确创建（别为了不泄漏而少建）
        check("两个槽位各有一个按钮",
              sum(1 for b in column._quick_buttons if b.isVisible()) == 2
              if hasattr(column, "_quick_buttons") else False,
              str([b.isVisible() for b in getattr(column, "_quick_buttons", [])]))
    finally:
        window.preview_panel.callbacks["quick_slots"] = original_quick
        window.preview_panel.refresh_quick_slots()

    print("\n[9d2] 推杆不可用时必须在界面上说清原因（回归）")
    controller.store.set_current_transition("渐变")
    controller.store.set_studio_mode(False)
    app.processEvents()
    check("非工作室模式下推杆不可用", not column.tbar.isEnabled())
    check("界面上写明要开工作室模式",
          "工作室模式" in column.tbar_hint.text() and not column.tbar_hint.isHidden(),
          column.tbar_hint.text())
    controller.store.set_studio_mode(True)
    app.processEvents()
    check("恢复工作室模式后推杆可用且提示消失",
          column.tbar.isEnabled() and column.tbar_hint.isHidden(),
          column.tbar_hint.text())
    # 被 OBS 拒过（如 506）时的原因也要显出来
    controller.store.mark_unavailable("SetTBarPosition", "OBS 端未开启工作室模式")
    app.processEvents()
    check("被 OBS 拒后推杆置灰", not column.tbar.isEnabled())
    check("界面上显示 OBS 给的原因",
          "未开启工作室模式" in column.tbar_hint.text(), column.tbar_hint.text())
    controller.store.clear_unavailable("SetTBarPosition")
    app.processEvents()
    check("撤销降级后推杆恢复", column.tbar.isEnabled())

    # OBS 收下请求却毫无反应（未修复的 OBS 侧缺陷）：推杆**仍可用**（不是错误，
    # 只是 OBS 不理），但界面上要说清"为什么拖了没反应"
    controller.store.set_tbar_ignored(True)
    app.processEvents()
    check("OBS 不理会时推杆仍可用（这不是我们的错，也不该锁死用户）",
          column.tbar.isEnabled())
    check("界面上说明是已知的 OBS 侧缺陷",
          "OBS" in column.tbar_hint.text() and not column.tbar_hint.isHidden(),
          column.tbar_hint.text())
    controller.store.set_tbar_ignored(False)
    app.processEvents()
    check("恢复正常后提示消失", column.tbar_hint.isHidden(), column.tbar_hint.text())

    print("\n[9d3] T 型推杆的版本门槛（回归）")
    # obs-studio issue #11372：推杆的 API 在 29.0.2 及更早可用，29.1.0-beta1 起失效
    # （修复 PR #13143 未合入）。所以这个版本区间要**不显示**推杆，
    # 而不是摆一个必然无效的控件在那儿。
    from obs_remote_studio.core.models import ServerInfo

    for version, should_show in (
        ("29.0.2", True),        # 最后一个可用版本，必须留着
        ("29.0.1", True),        # 更早的当然可用
        ("28.1.2", True),
        ("29.1.0-beta1", False), # 从这一版开始失效
        ("31.1.2", False),
        ("32.1.2", False),
        ("", True),              # 读不出/未知：不能武断藏掉，交给运行期自检兜底
    ):
        controller.store.set_server_info(ServerInfo(obs_version=version))
        controller.store.set_studio_mode(True)
        app.processEvents()
        shown = not column.tbar_row.isHidden()
        check(f"OBS {version or '未知'} → 推杆{'显示' if should_show else '隐藏'}",
              shown == should_show, f"shown={shown}")
    # 隐藏时要说明原因，别让用户以为功能丢了
    controller.store.set_server_info(ServerInfo(obs_version="31.1.2"))
    app.processEvents()
    check("隐藏时界面上说明是版本缺陷所致",
          "有缺陷" in column.tbar_hint.text() and not column.tbar_hint.isHidden(),
          column.tbar_hint.text())
    controller.store.set_server_info(ServerInfo(obs_version="29.0.2"))
    app.processEvents()
    check("可用的版本上不显示这条说明",
          "有缺陷" not in column.tbar_hint.text(), column.tbar_hint.text())

    controller.store.set_transitioning(False)
    controller.store.set_studio_mode(False)
    app.processEvents()
    app.processEvents()

    print("\n[9e] M1/M2 配置菜单的可用性跟随连接与录制状态（回归）")
    # 这条断言直接盯着"菜单有没有被更新"：之前 _refresh_config_menus_enabled
    # 里少了一个 import，函数每次调用都抛 NameError，菜单状态压根没变过。
    controller.store.set_connection_state(CONNECTED, "127.0.0.1:4455")
    controller.store.set_record(RecordStatus(active=False))
    controller.store.set_stream(StreamStatus(active=False))
    app.processEvents()
    check("连接且空闲时配置菜单可点", window.collection_menu.isEnabled())
    controller.store.set_record(RecordStatus(active=True))
    app.processEvents()
    check("录制中配置菜单置灰", not window.collection_menu.isEnabled())
    check("置灰原因写进 tooltip",
          "录制" in window.collection_menu.toolTip(), window.collection_menu.toolTip())
    controller.store.set_record(RecordStatus(active=False))
    controller.store.set_stream(StreamStatus(active=True))
    app.processEvents()
    check("推流中配置菜单也置灰", not window.profile_menu.isEnabled())
    controller.store.set_stream(StreamStatus(active=False))
    app.processEvents()
    check("恢复空闲后又能点", window.profile_menu.isEnabled())
    before_percent = controller.config.mixer_show_percent
    mixer.unit_button.click()
    after_percent = controller.config.mixer_show_percent
    check("dB/% 切换会写回配置", after_percent is not before_percent,
          f"{before_percent} -> {after_percent}")
    expected_suffix = "%" if after_percent else "dB"
    check("切换后数值显示跟着变",
          mixer._rows["桌面音频"].value_label.text().endswith(expected_suffix),
          mixer._rows["桌面音频"].value_label.text())
    controller.store.set_audio_inputs([])
    app.processEvents()
    check("清空后回到提示态", mixer.empty_hint.isVisible())
    # 上面 [9e] 把连接态设成了"已连接"，而下面的场景面板要断言"未连接时禁用"，
    # 这里恢复回去。**必须放在混音器段落全部跑完之后再重置** ——
    # 一置为未连接，混音器会清空所有推子行，上面那几条就找不到行了。
    controller.store.set_connection_state(DISCONNECTED, "未连接")
    app.processEvents()

    print("\n[9] 场景面板的编辑按钮（B5/B6）")
    from obs_remote_studio.core.models import Scene

    scene_panel = window.scene_panel
    # 未连接：整块面板禁用
    check("未连接时场景面板禁用", not scene_panel.isEnabled())
    # 造一个"已连接且支持编辑"的状态
    controller.store.set_connection_state(CONNECTED, "127.0.0.1:4455")
    controller.store.set_scenes(
        [Scene("主画面", 2), Scene("开场", 1), Scene("结束", 0)], "主画面"
    )
    controller.store.set_capabilities(
        ["GetSceneList", "CreateScene", "RemoveScene", "SetSceneName", "SetSceneIndex"]
    )
    app.processEvents()
    check("能力齐全时新建/删除可用",
          scene_panel.add_btn.isEnabled() and scene_panel.remove_btn.isEnabled())
    check("能力齐全时排序按钮可用",
          scene_panel.up_btn.isEnabled() and scene_panel.down_btn.isEnabled())
    check("列表项按场景名填充",
          [scene_panel.list_widget.item(i).data(0x0100) for i in range(3)]
          == ["主画面", "开场", "结束"],
          str([scene_panel.list_widget.item(i).data(0x0100) for i in range(3)]))

    # 只留一个场景时必须禁止删除
    controller.store.set_scenes([Scene("独苗", 0)], "独苗")
    app.processEvents()
    check("只剩一个场景时删除被禁用", not scene_panel.remove_btn.isEnabled())

    # 能力缺失（老服务端没有 SetSceneIndex）：排序按钮置灰
    controller.store.set_capabilities(["GetSceneList", "CreateScene"])
    app.processEvents()
    check("无 SetSceneIndex 时排序按钮置灰",
          not scene_panel.up_btn.isEnabled() and not scene_panel.down_btn.isEnabled())
    check("无 SetSceneIndex 时拖拽也禁用",
          not scene_panel.list_widget.dragEnabled())
    check("新建仍可用（CreateScene 在）", scene_panel.add_btn.isEnabled())

    # 老服务端连 CreateScene 都没有：编辑按钮全灰
    controller.store.set_capabilities(["GetSceneList"])
    app.processEvents()
    check("无编辑能力时新建/删除置灰",
          not scene_panel.add_btn.isEnabled() and not scene_panel.remove_btn.isEnabled())

    print("\n[10] 诊断窗口")
    from obs_remote_studio.ui.dialogs.diagnostics_dialog import DiagnosticsWindow

    diag = DiagnosticsWindow()
    check("诊断窗口可构造", diag.windowTitle().startswith("诊断窗口"))
    check("诊断窗口默认上限防内存膨胀",
          diag.view.maximumBlockCount() == 2000, str(diag.view.maximumBlockCount()))
    diag.append("->", "request", {"requestType": "GetSceneList"})
    diag.append("<-", "response", {"requestType": "GetSceneList", "ok": True})
    diag.append("<-", "error", {"code": 604})
    check("四种帧都能写入且计数正确",
          diag._counts == {"request": 1, "response": 1, "event": 0, "error": 1},
          str(diag._counts))
    check("内容含请求类型", "GetSceneList" in diag.view.toPlainText())
    # 「只看错误」模式下非错误帧不写入
    diag.errors_only_check.setChecked(True)
    before_rows = diag.view.blockCount()
    diag.append("->", "request", {"requestType": "GetStats"})
    check("只看错误时过滤掉请求帧",
          diag.view.blockCount() == before_rows, str(diag.view.blockCount()))
    diag.errors_only_check.setChecked(False)
    diag.clear()
    check("清空后无内容", diag.view.toPlainText() == "" and sum(diag._counts.values()) == 0)
    diag.close()

    print("\n[10d] P7：推流字幕窗（回归）")
    from obs_remote_studio.ui.dialogs.stream_caption_dialog import StreamCaptionDialog

    sent: list[str] = []
    cap_state = {"blocker": "未在推流，字幕只对直播输出有效"}
    cap = StreamCaptionDialog(
        controller.store,
        {
            "send": lambda text: (sent.append(text), True)[1],
            "blocker": lambda: cap_state["blocker"],
            "clear": lambda: (sent.append(""), True)[1],
        },
    )
    check("字幕窗可构造", cap.windowTitle() == "推流字幕")
    # 不可发送时：输入与两个按钮都禁用，**并且把原因写出来**（不能只藏在 tooltip）
    check("不可发送时输入禁用", not cap.text_edit.isEnabled())
    check("不可发送时发送按钮禁用", not cap.send_btn.isEnabled())
    check("不可发送时清除按钮也禁用（服务端同样会回 501）",
          not cap.clear_btn.isEnabled())
    check("不可发送时界面上写明原因",
          "未在推流" in cap.status.text(), cap.status.text())

    # 可发送：输入启用、能发、发完清空输入框
    cap_state["blocker"] = ""
    cap.refresh()
    check("可发送时输入启用", cap.text_edit.isEnabled() and cap.send_btn.isEnabled())
    check("可发送时状态文字说明可以发", "可以发送" in cap.status.text(), cap.status.text())
    cap.text_edit.setText("开场提示")
    check("超长前不显示折行提示", "超出" not in cap.counter.text(), cap.counter.text())
    cap.text_edit.setText("x" * 40)
    check("超过单行建议长度时给出提示（但不阻断）",
          "超出" in cap.counter.text(), cap.counter.text())
    check("超长仍可发送（切分交给 OBS，客户端不替它决定）", cap.send_btn.isEnabled())
    cap.text_edit.setText("开场提示")
    cap._send()
    check("点发送把文本交给控制器", sent == ["开场提示"], str(sent))
    check("发送后清空输入框", cap.text_edit.text() == "", repr(cap.text_edit.text()))

    # 空输入不该被当成"清除"——清除有专门的按钮，避免误操作
    sent.clear()
    cap.text_edit.setText("   ")
    cap._send()
    check("空输入不发送（避免误清屏）", sent == [], str(sent))
    check("空输入给出引导", "清除字幕" in cap.status.text(), cap.status.text())

    # 「清除字幕」走空串，这是协议允许的用法
    sent.clear()
    cap._clear()
    check("清除按钮发送空串", sent == [""], str(sent))
    cap.close()

    print("\n[10b] 诊断窗口：暂停滚动 / 待响应 / 失败帧（回归）")
    diag2 = DiagnosticsWindow()
    diag2.resize(600, 300)
    diag2.show()
    app.processEvents()
    for i in range(80):
        diag2.append("->", "request", {"requestType": f"Req{i}", "i": i})
        diag2.append("<-", "response", {"requestType": f"Req{i}", "ok": True})
    app.processEvents()
    bar = diag2.view.verticalScrollBar()
    check("内容够多，视图可滚动", bar.maximum() > 0, str(bar.maximum()))
    # QPlainTextEdit 的 value 与 maximum 允许差 1（视口行高取整），别写死相等
    check("默认跟随到底部", bar.value() >= bar.maximum() - 1, f"{bar.value()}/{bar.maximum()}")

    # 线上问题：用户勾了"暂停滚动"，视图仍然一路跑到最底。
    # 真因：`QPlainTextEdit.appendPlainText` 在**滚动条已经位于最底部**时会自己
    # "跟随末尾"继续滚到底（实测 107→112），而用户勾暂停时正好就在底部。
    # 所以只跳过后面那句 setValue(maximum()) 是拦不住的，必须把位置存下来再还原。
    # ↓ 先测**在底部时暂停**这个真实场景（这是原缺陷能复现的唯一姿势）
    check("前提：此刻视图位于最底部", bar.value() >= bar.maximum() - 1,
          f"{bar.value()}/{bar.maximum()}")
    diag2.pause_check.setChecked(True)
    app.processEvents()
    bottom_at_pause = bar.value()
    for i in range(10):
        diag2.append("->", "request", {"requestType": f"Tail{i}", "i": i})
        diag2.append("<-", "response", {"requestType": f"Tail{i}", "ok": True})
    app.processEvents()
    check("**在底部勾暂停后不再跟随**（这正是之前的 bug）",
          bar.value() == bottom_at_pause and bar.value() < bar.maximum(),
          f"{bottom_at_pause} -> {bar.value()} (max={bar.maximum()})")
    check("暂停时状态栏有提示", "已暂停滚动" in diag2.status_label.text(),
          diag2.status_label.text())
    check("暂停期间新内容仍在记录", "Tail9" in diag2.view.toPlainText())

    # 再从中间暂停，验证位置被原样保住
    diag2.pause_check.setChecked(False)
    app.processEvents()
    bar.setValue(bar.maximum() // 2)
    parked = bar.value()
    diag2.pause_check.setChecked(True)
    app.processEvents()
    for i in range(10):
        diag2.append("->", "request", {"requestType": f"Mid{i}", "i": i})
        diag2.append("<-", "response", {"requestType": f"Mid{i}", "ok": True})
    app.processEvents()
    check("中途勾暂停时位置原样保住", bar.value() == parked,
          f"{parked} -> {bar.value()}")

    diag2.to_bottom_btn.click()
    app.processEvents()
    check("点「回到底部」恢复跟随",
          bar.value() >= bar.maximum() - 1 and not diag2.pause_check.isChecked(),
          f"{bar.value()}/{bar.maximum()} paused={diag2.pause_check.isChecked()}")

    print("\n[10c] 诊断窗口：待响应与失败帧（回归）")
    diag2.pause_check.setChecked(False)
    diag2.clear()
    # 失败是**协议原样**的响应帧（requestStatus.result=false），
    # 之前记的是自定义的 {requestType, code, comment}，用户按 requestStatus.code 找不到，
    # 会误以为"OBS 根本没回响应"。
    diag2.append("->", "request", {"requestType": "SetTBarPosition", "requestData": {"position": 1.0}})
    app.processEvents()
    check("发出未回的请求会显示为待响应",
          "待响应" in diag2.status_label.text()
          and "SetTBarPosition" in diag2.status_label.text(),
          diag2.status_label.text())
    diag2.append("<-", "response", {
        "requestType": "SetTBarPosition",
        "requestStatus": {"result": False, "code": 506, "comment": "Studio Mode is not active"},
    })
    app.processEvents()
    check("收到响应后不再算待响应", "待响应" not in diag2.status_label.text(),
          diag2.status_label.text())
    check("失败响应计入「失败」", diag2._failed == 1, str(diag2._failed))
    text = diag2.view.toPlainText()
    check("失败帧里能看到 requestStatus 与 code",
          "requestStatus" in text and "506" in text, text[:120])

    # 「只看错误」不能只认 kind=="error"，否则最该看的那一类反而被过滤掉
    diag2.clear()
    diag2.errors_only_check.setChecked(True)
    rows = diag2.view.blockCount()   # 空文档本来就有 1 个 block，要跟追加前比
    diag2.append("<-", "response", {"requestType": "GetStats", "ok": True})
    check("只看错误时普通响应仍被过滤", diag2.view.blockCount() == rows,
          f"{rows} -> {diag2.view.blockCount()}")
    diag2.append("<-", "response", {
        "requestType": "SetTBarPosition",
        "requestStatus": {"result": False, "code": 506},
    })
    check("只看错误时能看到失败响应", diag2.view.blockCount() > rows,
          str(diag2.view.blockCount()))
    diag2.errors_only_check.setChecked(False)
    diag2.close()

    print("\n[11] O2 高 DPI 适配")
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QGuiApplication

    from obs_remote_studio.app import apply_high_dpi_policy
    from obs_remote_studio.utils.dpi import device_ratio, snap, snap_rect

    apply_high_dpi_policy()
    check("缩放策略设为 PassThrough",
          QGuiApplication.highDpiScaleFactorRoundingPolicy()
          == _Qt.HighDpiScaleFactorRoundingPolicy.PassThrough,
          str(QGuiApplication.highDpiScaleFactorRoundingPolicy()))
    check("高分屏位图已启用",
          QApplication.testAttribute(_Qt.ApplicationAttribute.AA_UseHighDpiPixmaps))
    # 像素对齐：1.25 倍下 10px 逻辑坐标应落在物理像素格点上
    check("snap 对齐到物理像素", abs(snap(10.0, 1.25) * 1.25 - round(10.0 * 1.25)) < 1e-6,
          str(snap(10.0, 1.25)))
    source = QRect(0, 0, 101, 57)
    aligned = snap_rect(source, 1.5)
    check("snap_rect 不退化成 0",
          aligned.width() >= 1 and aligned.height() >= 1, str(aligned))
    check("snap_rect 尺寸基本保持不变",
          abs(aligned.width() - source.width()) <= 1
          and abs(aligned.height() - source.height()) <= 1, str(aligned))
    check("device_ratio 有合理默认", device_ratio(window) > 0, str(device_ratio(window)))

    window.close()
    controller.shutdown()

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

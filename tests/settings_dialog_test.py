"""设置面板（仿 OBS 分页式设置）回归测试。

守的是这几件事：
1. 结构与 OBS 原版一致 —— 九个页面、顺序、左侧列表文案、确定/取消/应用三个按钮；
2. 「做不到的项一律置灰」这条硬规矩真的执行了，而且**每一条置灰都写了原因**；
3. 能让用户改的项真的能改，并且真的写回 AppConfig（不是摆样子）；
4. 「应用」就地生效但不关窗，「确定」返回配置；
5. 置灰项不允许"看着能用、点了没反应" —— 逐个断言 enabled=False + 非空 tooltip。

运行：python tests/settings_dialog_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 与真实配置隔离（Windows 上 QSettings(org, app) 走注册表，必须换 org + INI）
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from obs_remote_studio.app import apply_theme  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.ui import theme  # noqa: E402
from obs_remote_studio.ui.dialogs.settings_ui import form as F  # noqa: E402
from obs_remote_studio.ui.dialogs.settings_ui.dialog import SettingsDialog  # noqa: E402
from obs_remote_studio.ui.dialogs.settings_ui.pages import PAGE_ORDER  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402

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


def page_widgets(page) -> list:
    """页面里**我们自己建的**可交互控件。

    **为什么不能直接 findChildren**：QSpinBox / QComboBox 内部各自带一个
    QLineEdit（`qt_spinbox_lineedit`），而"颜色块 + 按钮"这类复合行也带子控件。
    父级被 setEnabled(False) 之后子控件同样变成 disabled，于是批量检查会把
    这些内部子控件当成"置灰项"，再因为它们本来没有独立 tooltip 而误报
    "没写原因"。所以这里只保留最外层的那一批：祖先里已经有同类控件的就跳过。
    """
    from PySide6.QtWidgets import QDoubleSpinBox

    kinds = (QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QLineEdit)
    found = [w for w in page.findChildren(QWidget) if isinstance(w, kinds)]
    outermost = []
    for widget in found:
        parent = widget.parent()
        nested = False
        while parent is not None and parent is not page:
            if isinstance(parent, kinds):
                nested = True
                break
            parent = parent.parent()
        if not nested:
            outermost.append(widget)
    return outermost


def check_disabled_reasons(dialog) -> tuple[list, list, list]:
    """返回（全部置灰项, 缺原因的, "可用却写着暂不可用"的）。"""
    disabled: list = []
    missing: list[str] = []
    liars: list[str] = []
    for key, page in dialog.pages.items():
        for widget in page_widgets(page):
            tooltip = widget.toolTip().strip()
            if not widget.isEnabled():
                disabled.append((key, widget))
                if not tooltip:
                    missing.append(f"{key}/{widget.__class__.__name__}")
            elif "暂不可用" in tooltip:
                liars.append(f"{key}/{widget.__class__.__name__}")
    return disabled, missing, liars


def main() -> int:
    tmp_dir = tempfile.mkdtemp(prefix="obsrs-settings-")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp_dir)

    app = QApplication([])
    app.setStyle("Fusion")
    apply_theme(app, "dark")

    controller = Controller()
    window = MainWindow(controller)

    def make_dialog():
        # 与主窗口 _open_settings_dialog 的真实接线保持一致：
        # rows/summary 给快捷键页，record_directory 给「输出 → 录像路径」只读对照。
        return SettingsDialog(
            controller.config,
            window,
            store=controller.store,
            theme_name=theme.current_name(),
            hotkeys={
                "rows": window._hotkey_rows(),
                "summary": window._hotkey_summary(),
                "record_directory": lambda: controller.record_directory,
            },
            log_dir_opener=window._open_log_dir,
        )

    dialog = make_dialog()
    dialog.resize(920, 680)
    dialog.show()
    app.processEvents()

    # ------------------------------------------------------------ [1] 结构
    print("\n[1] 结构与 OBS 原版一致")
    expected_labels = [label for _key, label, _icon in PAGE_ORDER]
    actual_labels = [dialog.nav.item(i).text() for i in range(dialog.nav.count())]
    check("左侧九个页面", dialog.nav.count() == 9, str(dialog.nav.count()))
    check("页面顺序与 OBS 一致",
          actual_labels == ["常规", "外观", "直播", "输出", "音频",
                            "视频", "快捷键", "无障碍环境", "高级"],
          str(actual_labels))
    check("页面 key 与文案对应", expected_labels == actual_labels, str(actual_labels))
    check("每个页面都有图标",
          all(not dialog.nav.item(i).icon().isNull() for i in range(dialog.nav.count())))
    check("右侧堆叠页数量与列表一致", dialog.stack.count() == 9, str(dialog.stack.count()))
    check("按钮是 确定 / 取消 / 应用",
          [dialog.ok_btn.text(), dialog.cancel_btn.text(), dialog.apply_btn.text()]
          == ["确定", "取消", "应用"])
    check("默认选中第一页「常规」", dialog.nav.currentRow() == 0)
    check("页面标题跟着选中项走", dialog.page_title.text() == "常规",
          dialog.page_title.text())

    # 逐页切换，标题与堆叠页都要跟着动
    mismatched = []
    for row in range(dialog.nav.count()):
        dialog.nav.setCurrentRow(row)
        app.processEvents()
        if dialog.page_title.text() != actual_labels[row]:
            mismatched.append((actual_labels[row], dialog.page_title.text()))
        if dialog.stack.currentIndex() != row:
            mismatched.append((f"stack@{row}", str(dialog.stack.currentIndex())))
    check("切换页面时标题与内容同步", not mismatched, str(mismatched))
    dialog.nav.setCurrentRow(0)

    # ------------------------------------------------------------ [2] 布局观感
    print("\n[2] 仿 OBS 的排版栅格")
    general = dialog.pages["general"]
    labels = [w for w in general.findChildren(QLabel)
              if w.objectName() == "settingsFieldLabel"]
    check("表单项标签用专用 objectName（QSS 才管得到）", len(labels) >= 5, str(len(labels)))
    check("标签右对齐",
          all(w.alignment() & Qt.AlignmentFlag.AlignRight for w in labels))
    sections = [w for w in general.findChildren(QLabel)
                if w.objectName() == "settingsSectionTitle"]
    check("分组标题存在", len(sections) >= 4, str(len(sections)))
    rules = [w for w in general.findChildren(QFrame)
             if w.objectName() == "settingsRule"]
    check("有分隔线", len(rules) >= 3, str(len(rules)))
    # 栅格：标签列固定宽，控件列吃剩余空间
    grid = general._grid
    check("标签列有固定最小宽",
          grid.columnMinimumWidth(0) == F.LABEL_COLUMN_WIDTH,
          str(grid.columnMinimumWidth(0)))
    check("控件列可伸缩", grid.columnStretch(1) == 1, str(grid.columnStretch(1)))
    check("页面内容可滚动收在 QScrollArea 里",
          dialog.stack.currentWidget().__class__.__name__ == "QScrollArea",
          dialog.stack.currentWidget().__class__.__name__)

    # ------------------------------------------------------------ [3] 置灰规矩
    print("\n[3] 做不到的项一律置灰，且必须写明原因")
    disabled, missing_reason, liars = check_disabled_reasons(dialog)
    check("确实存在被置灰的项", len(disabled) >= 25, str(len(disabled)))
    check("每个置灰项都有悬停原因", not missing_reason, str(missing_reason[:5]))
    check("置灰原因用统一措辞",
          all("暂不可用：" in w.toolTip() for _k, w in disabled),
          str([w.toolTip() for _k, w in disabled][:3]))
    check("没有「可用状态却写着暂不可用」的控件", not liars, str(liars[:5]))

    # 三类原因都要出现，说明分类真的用上了（而不是一律一句话）
    all_tips = " ".join(w.toolTip() for _k, w in disabled)
    for name, reason in (
        ("只有 OBS 本机能改", F.REASON_REMOTE),
        ("本阶段未接入", F.REASON_NOT_WIRED),
        ("只读对照", F.REASON_READONLY),
    ):
        check(f"置灰原因含「{name}」", reason in all_tips)
    # 反向断言：不能在没核对过的情况下自造"协议没有"这一类
    check("置灰原因不含自造的「协议没有」措辞",
          "协议没有" not in all_tips and "REASON_PROTOCOL" not in all_tips)

    # 逐页统计：可用项与置灰项都记下来，供第 4 步复核
    per_page = {
        key: (sum(1 for w in page_widgets(page) if w.isEnabled()),
              sum(1 for w in page_widgets(page) if not w.isEnabled()))
        for key, page in dialog.pages.items()
    }
    check("九个页面都建出来了", len(per_page) == 9, str(sorted(per_page)))
    check("每页都有控件", all(a + b > 0 for a, b in per_page.values()), str(per_page))
    check("「常规」页可用项最多（本客户端偏好主要在这）",
          per_page["general"][0] >= 6, str(per_page["general"]))

    # ------------------------------------------------------------ [4] 可用项真的能用
    print("\n[4] 本客户端能改的项真的写回配置")
    # 各项散落在不同页面，这里逐个改一遍再收配置
    dialog.pages["general"].poll_spin.setValue(1500)
    dialog.pages["general"].timeout_spin.setValue(2.5)
    dialog.pages["general"].heartbeat_spin.setValue(8000)
    dialog.pages["general"].attempts_spin.setValue(5)
    dialog.pages["general"].confirm_stop_check.setChecked(False)
    dialog.pages["general"].auto_check.setChecked(True)
    dialog.pages["general"].reconnect_check.setChecked(False)
    dialog.pages["general"].tray_check.setChecked(False)
    dialog.pages["general"].close_to_tray_check.setChecked(True)
    dialog.pages["general"].preview_spin.setValue(2500)
    dialog.pages["general"].preview_quality_spin.setValue(80)
    dialog.pages["general"].preview_width_spin.setValue(640)
    dialog.pages["audio"].meters_check.setChecked(False)
    dialog.pages["audio"].percent_check.setChecked(True)
    dialog.pages["output"].disk_warn_spin.setValue(5.5)
    dialog.pages["output"].record_minutes_spin.setValue(120)
    dialog.pages["output"].record_warn_gb_spin.setValue(4.0)
    dialog.pages["advanced"].log_level_combo.setCurrentIndex(0)
    dialog.pages["advanced"].log_to_file_check.setChecked(True)
    dialog.pages["advanced"].compact_check.setChecked(True)

    config = dialog.result_config()
    check("轮询间隔写回", config.poll_interval_ms == 1500, str(config.poll_interval_ms))
    check("请求超时写回", abs(config.request_timeout_s - 2.5) < 1e-6,
          str(config.request_timeout_s))
    check("心跳间隔写回", config.heartbeat_interval_ms == 8000,
          str(config.heartbeat_interval_ms))
    check("最大重试次数写回", config.reconnect.max_attempts == 5,
          str(config.reconnect.max_attempts))
    check("自动重连开关写回", config.reconnect.enabled is False)
    check("启动自动连接写回", config.auto_connect_on_startup is True)
    check("停止直播确认写回", config.confirm_stop_stream is False)
    check("托盘开关写回", config.tray_enabled is False)
    check("关闭到托盘写回", config.close_to_tray is True)
    check("缩略图间隔写回", config.preview_interval_ms == 2500,
          str(config.preview_interval_ms))
    check("缩略图质量写回", config.preview_quality == 80, str(config.preview_quality))
    check("缩略图宽度写回", config.preview_width == 640, str(config.preview_width))
    check("电平表订阅写回", config.audio_meters is False)
    check("推子百分比写回", config.mixer_show_percent is True)
    check("磁盘预警写回", abs(config.disk_warn_gb - 5.5) < 1e-6,
          str(config.disk_warn_gb))
    check("时长预警写回", config.record_warn_minutes == 120,
          str(config.record_warn_minutes))
    check("文件大小预警写回", abs(config.record_warn_gb - 4.0) < 1e-6,
          str(config.record_warn_gb))
    check("日志级别写回", config.log_level == "DEBUG", str(config.log_level))
    check("日志落盘写回", config.log_to_file is True)
    check("紧凑模式写回", config.compact_mode is True)

    # 可用项不应被误禁，也要真的 enabled
    check("可用项未被误禁",
          dialog.pages["general"].poll_spin.isEnabled()
          and dialog.pages["audio"].percent_check.isEnabled()
          and dialog.pages["general"].tray_check.isEnabled())
    check("可用项不带「暂不可用」提示",
          not dialog.pages["general"].poll_spin.toolTip().startswith("暂不可用"),
          dialog.pages["general"].poll_spin.toolTip())

    # ------------------------------------------------------------ [5] 应用 / 确定
    print("\n[5] 「应用」就地生效且不关窗，「确定」返回配置")
    applied: list = []
    themes: list[str] = []
    dialog.applied.connect(applied.append)
    dialog.theme_selected.connect(themes.append)

    dialog.pages["general"].poll_spin.setValue(777)
    dialog.apply_changes()
    check("应用后发出 applied", len(applied) == 1, str(len(applied)))
    check("应用后配置已更新", dialog.config.poll_interval_ms == 777,
          str(dialog.config.poll_interval_ms))
    check("应用不关闭窗口", dialog.isVisible())
    check("应用不动主题", not themes, str(themes))

    # 换主题 → 应用 → 发 theme_selected
    dialog.pages["appearance"].theme_combo.setCurrentIndex(1)
    dialog.apply_changes()
    check("换主题后发 theme_selected", themes == ["light"], str(themes))
    check("applied 又发了一次", len(applied) == 2, str(len(applied)))

    # 「确定」不重复发 applied（否则主窗口会白跑一遍副作用）
    before = len(applied)
    result = dialog.result_config()
    check("确定不重复发 applied", len(applied) == before, str(len(applied)))
    check("确定返回一份配置", result is not None and result.poll_interval_ms == 777)

    # 主题下拉的选中项与当前主题一致性
    check("外观页有主题下拉", isinstance(dialog.pages["appearance"].theme_combo, QComboBox))
    check("外观页能选到两套主题",
          dialog.pages["appearance"].theme_combo.count() == 2,
          str(dialog.pages["appearance"].theme_combo.count()))

    dialog.close()

    # ------------------------------------------------------------ [6] 与主窗口联动
    print("\n[6] 主窗口接线：应用后配置真的落到 controller")
    # 走主窗口真实的那条路径：applied 接到 _apply_settings。
    # 只调 dialog.apply_changes() 不会碰 controller —— 接线在主窗口那边，
    # 所以这里必须把接线补上，否则测的只是"对话框自己算对了"。
    dialog2 = make_dialog()
    dialog2.applied.connect(window._apply_settings)
    dialog2.pages["general"].poll_spin.setValue(1234)
    dialog2.apply_changes()
    check("controller 配置已同步", controller.config.poll_interval_ms == 1234,
          str(controller.config.poll_interval_ms))
    check("配置已落盘（QSettings）",
          controller.settings.load_config().poll_interval_ms == 1234,
          str(controller.settings.load_config().poll_interval_ms))
    check("非紧凑模式下按应用不会误开紧凑模式", window._compact is False)

    # 高级页的「打开日志目录」必须真的接着回调。
    # **不能用 receivers() 判断**：它只数 Qt slot，Python 可调用对象一律算 0，
    # 拿它断言会把"已接好"误判成"没接"。这里换成真的点一下。
    log_calls: list[str] = []
    dialog2._log_dir_opener = lambda: log_calls.append("opened")
    dialog2._wire_log_button()
    dialog2.pages["advanced"].open_log_btn.click()
    app.processEvents()
    check("点「打开日志目录」真的触发回调", log_calls == ["opened"], str(log_calls))

    # 紧凑模式联动：勾上应用 → 主窗口真的进紧凑模式
    # 用 isHidden() 而不是 isVisible()：本测试没有 show() 顶层窗口，
    # isVisible() 恒为 False，会把"已恢复显示"误判成失败。
    dialog2.pages["advanced"].compact_check.setChecked(True)
    dialog2.apply_changes()
    check("勾紧凑模式后主窗口进入紧凑模式", window._compact is True, str(window._compact))
    check("紧凑模式下预览区被显式隐藏", window.preview_panel.isHidden())
    dialog2.pages["advanced"].compact_check.setChecked(False)
    dialog2.apply_changes()
    check("取消后退出紧凑模式", window._compact is False, str(window._compact))
    check("预览区不再被隐藏", not window.preview_panel.isHidden())

    dialog2.applied.disconnect(window._apply_settings)
    dialog2.close()

    # 主题切换经主窗口要走 save_theme（否则下次启动又变回去）
    saved: list[str] = []
    original = controller.settings.save_theme

    def spy(name: str) -> None:
        saved.append(name)
        original(name)

    controller.settings.save_theme = spy
    window._apply_settings_theme("light")
    check("主题切换写进 QSettings", saved == ["light"], str(saved))
    check("主题真的切了", theme.current_name() == "light", theme.current_name())
    controller.settings.save_theme = original
    apply_theme(app, "dark")

    # ------------------------------------------------------------ [7] 快捷键页
    print("\n[7] 快捷键页列出真实注册的键位")
    hotkeys_page = dialog.pages["hotkeys"]
    table = hotkeys_page.table
    check("有热键表格", table is not None)
    rows = [(table.item(r, 0).text(), table.item(r, 1).text())
            for r in range(table.rowCount())]
    check("键位数与主窗口一致", len(rows) == len(window._hotkey_rows()),
          f"{len(rows)} vs {len(window._hotkey_rows())}")
    check("含录制 / 直播 / 工作室模式",
          any("录制" in name for name, _s in rows)
          and any("直播" in name for name, _s in rows)
          and any("工作室" in name for name, _s in rows),
          str(rows[:4]))
    check("键位非空", all(sequence for _n, sequence in rows), str(rows[:3]))
    check("全局热键开关在快捷键页", isinstance(hotkeys_page.hotkey_check, QCheckBox))

    # ------------------------------------------------------------ [8] 视频页只读对照
    print("\n[8] 视频页：分辨率只读对照，不冒充可写")
    video_page = dialog.pages["video"]
    check("视频页有画布分辨率下拉", isinstance(video_page.base_combo, QComboBox))
    check("视频页有输出分辨率下拉", isinstance(video_page.output_combo, QComboBox))
    check("分辨率下拉被置灰（不能写）", not video_page.base_combo.isEnabled())
    check("分辨率下拉写明原因",
          "暂不可用" in video_page.base_combo.toolTip(),
          video_page.base_combo.toolTip())
    hints = [w.text() for w in video_page.findChildren(QLabel)
             if "当前 OBS 端取值" in w.text()]
    check("给出 OBS 端实际取值作对照", len(hints) >= 2, str(len(hints)))

    # 只读项要真的"有东西可读"：录像路径接的是 controller.record_directory
    output_page = dialog.pages["output"]
    check("输出页录像路径是只读框",
          output_page.record_path_edit.isReadOnly(),
          str(output_page.record_path_edit.isReadOnly()))
    check("录像路径被置灰并写明只读原因",
          not output_page.record_path_edit.isEnabled()
          and F.REASON_READONLY in output_page.record_path_edit.toolTip(),
          output_page.record_path_edit.toolTip())
    # controller 还没连过 OBS，应当显示"拿不到"而不是一个空框
    check("拿不到录像路径时明确写出来",
          output_page.record_path_edit.text() == "未连接 / OBS 未上报",
          output_page.record_path_edit.text())
    # 模拟 OBS 报来真实目录后，设置窗应能显示它
    controller._record_directory = r"D:\OBS\录像"
    dialog3 = make_dialog()
    check("拿到录像路径后如实显示",
          dialog3.pages["output"].record_path_edit.text() == r"D:\OBS\录像",
          dialog3.pages["output"].record_path_edit.text())
    dialog3.close()
    controller._record_directory = ""

    # ------------------------------------------------------------ [9] 无障碍页
    print("\n[9] 无障碍环境整页置灰（只作用在 OBS 本机的项）")
    accessibility = dialog.pages["accessibility"]
    widgets = page_widgets(accessibility)
    check("无障碍页有控件", len(widgets) >= 6, str(len(widgets)))
    check("无障碍页全部置灰", all(not w.isEnabled() for w in widgets),
          str([w.isEnabled() for w in widgets]))
    check("无障碍页每条都写了原因", all(w.toolTip().strip() for w in widgets))
    check("九个配色项齐全（与 OBS 一致）", len(accessibility.swatches) == 9,
          str(len(accessibility.swatches)))

    # 嵌套在容器里的按钮也必须自解释，不能只给外层容器写 tooltip
    nested_missing = []
    for key, page in dialog.pages.items():
        for widget in page.findChildren(QWidget):
            if not widget.isEnabled() and not widget.toolTip().strip():
                nested_missing.append(f"{key}/{widget.__class__.__name__}")
    check("被禁用的嵌套子控件也带说明", not nested_missing, str(nested_missing[:5]))

    # ------------------------------------------------------------ [10] 文案对齐 OBS
    print("\n[10] 条目文案与 OBS 31 zh-CN 原文一致（抽查）")

    def page_labels(page) -> set[str]:
        """页面里所有可见文字：QLabel 的 text、复选框/按钮的 text 都算。

        QCheckBox 不是 QLabel 的子类，只收 QLabel 会把所有复选框文案漏掉。
        """
        texts = {w.text() for w in page.findChildren(QLabel)}
        texts |= {w.text() for w in page.findChildren(QCheckBox)}
        texts |= {w.text() for w in page.findChildren(QPushButton)}
        return {t for t in texts if t}

    general_labels = page_labels(dialog.pages["general"])
    for text in ("语言", "更新通道", "启动时自动检查更新", "开始直播时弹窗确认",
                 "停止直播时弹窗确认", "停止录制时弹窗确认", "直播时自动录制",
                 "停止直播后继续录制", "直播时自动启动回放缓存",
                 "停止直播后保持回放缓存开启", "源对齐吸附", "吸附敏感度",
                 "吸附源到屏幕边缘", "吸附源到其他的源"):
        check(f"常规页含「{text}」", text in general_labels)
    # OBS 原文是「在屏幕采集中隐藏OBS窗口」（没有空格）
    check("常规页含「在屏幕采集中隐藏OBS窗口」",
          "在屏幕采集中隐藏OBS窗口" in general_labels)
    check("常规页不给「屏幕采集」乱加空格",
          "在屏幕采集中隐藏 OBS 窗口" not in general_labels)

    video_labels = page_labels(dialog.pages["video"])
    for text in ("基础（画布）分辨率", "输出 (缩放) 分辨率", "缩小算法",
                 "常用帧率", "分子：", "分母："):
        check(f"视频页含「{text}」", text in video_labels)

    advanced_labels = page_labels(dialog.pages["advanced"])
    for text in ("进程优先级", "渲染器", "视频适配器", "色彩格式", "色彩空间",
                 "色彩范围", "SDR 白电平", "HDR 标称峰值电平", "绑定到 IP", "IP 族",
                 "开启网络优化", "开启 TCP pacing", "快捷键与窗口焦点"):
        check(f"高级页含「{text}」", text in advanced_labels)

    output_labels = page_labels(dialog.pages["output"])
    for text in ("输出模式", "录像路径", "录像质量", "录像格式", "视频编码器",
                 "音频编码器", "音轨", "回放时长上限", "最大内存"):
        check(f"输出页含「{text}」", text in output_labels)

    audio_labels = page_labels(dialog.pages["audio"])
    for text in ("采样率", "声道", "全局音频设备", "桌面音频", "桌面音频 2",
                 "麦克风/辅助音频", "麦克风/辅助音频 4", "电平表", "衰减速率",
                 "峰值计类型"):
        check(f"音频页含「{text}」", text in audio_labels)

    stream_labels = page_labels(dialog.pages["stream"])
    for text in ("服务", "终点", "服务器", "推流码", "获取推流码", "使用推流码",
                 "使用身份认证", "用户名", "密码", "开启带宽测试模式",
                 "忽略流媒体服务的推荐设置"):
        check(f"直播页含「{text}」", text in stream_labels)

    appearance_labels = page_labels(dialog.pages["appearance"])
    for text in ("主题", "样式", "字体大小", "密度",
                 "此样式不提供某些外观选项。"):
        check(f"外观页含「{text}」", text in appearance_labels)
    check("外观页的密度是四个可选按钮",
          all(b.isCheckable() for b in
              dialog.pages["appearance"].density_row.findChildren(QPushButton)),
          str([b.text() for b in
               dialog.pages["appearance"].density_row.findChildren(QPushButton)]))

    window.close()
    controller.shutdown()

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

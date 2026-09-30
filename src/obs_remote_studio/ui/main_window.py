"""主窗口：布局对齐 OBS Studio。

    ┌ 菜单栏 ──────────────────────────────────────────────┐
    │ 预览：场景 │ 转场列 │ 输出：场景 2                    │
    │            - 57% + [缩放至窗口 ▼]                    │
    ├ 停靠区（可浮动 / 可重排）─────────────────────────────┤
    │ 场景 │ 来源 │ 混音器 │ 转场动画 │ 控制按钮            │
    ├ 状态栏 ─────────────────────────────────────────────┤

H8：底部五个面板是真正的 QDockWidget —— 默认依然排成一行停靠在底部（外观与之前一致），
但用户可以拖动重排、拖出来浮动到副屏。浮动窗口的位置由 `saveState()` 一起持久化。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..core.controller import Controller
from ..core.models import ConnectionConfig
from ..core.state_store import CONNECTED
from . import theme
from .dialogs.advanced_audio_dialog import AdvancedAudioDialog
from .dialogs.connect_dialog import ConnectDialog
from .dialogs.diagnostics_dialog import DiagnosticsWindow
from .dialogs.quick_transitions_dialog import QuickTransitionsDialog
from .dialogs.settings_dialog import SettingsDialog
from .widgets.controls_panel import ControlsPanel
from .widgets.dock_panel import DockGrip
from .widgets.mixer import MixerPanel
from .widgets.preview_panel import PreviewPanel
from .widgets.scene_list import ScenePanel
from .widgets.source_list import SourcePanel
from .widgets.status_bar import StatusBar
from .tray import TrayIcon
from .widgets.transitions_panel import TransitionsPanel
from ..utils import global_hotkeys
from ..utils.global_hotkeys import GlobalHotkeys

# 底部面板初始宽度，比例参照 OBS 默认布局
DOCK_SIZES = (130, 240, 280, 190, 200)
# 恢复布局时低于这个尺寸就视为"折叠过的脏数据"，整体不采用
MIN_SPLITTER_SIZE = 40
# restoreState 的版本号：改了 dock 结构就 +1，老的布局数据自动作废而不是错乱
DOCK_LAYOUT_VERSION = 3
# H10：迷你模式默认尺寸
COMPACT_SIZE = (380, 260)
# 迷你模式下保留的面板
COMPACT_KEEP = ("控制按钮",)


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.store = controller.store
        self.setWindowTitle("OBS Remote Studio")
        self.resize(1100, 720)

        self._advanced_dialog: AdvancedAudioDialog | None = None
        self._diagnostics_dialog: DiagnosticsWindow | None = None
        self._tray: TrayIcon | None = None
        self._hotkeys = GlobalHotkeys(self)
        # H10：迷你模式标志必须在 _build_layout 之前就位（_save_layout 会读它）
        self._compact = False

        self._build_layout()
        self._build_menu()
        self._build_shortcuts()
        self._build_tray()
        self._hotkeys.triggered.connect(self._on_hotkey)
        self.apply_hotkey_config()

        self.store.error_raised.connect(self._show_error)
        self.store.server_info_changed.connect(self._update_title)

        self._restore_layout()
        if controller.config.auto_connect_on_startup and controller.config.connection.host:
            controller.connect()

    # ---------------------------------------------------------------- 布局
    def _build_layout(self) -> None:
        callbacks = {
            "toggle_record": self.controller.toggle_record,
            "toggle_stream": self._toggle_stream_guarded,
            "pause_record": self.controller.toggle_pause_record,
            "pause_supported": self.controller.supports_pause_record,
            "virtualcam": self.controller.toggle_virtualcam,
            "replay_toggle": self.controller.toggle_replay_buffer,
            "replay_save": self.controller.save_replay_buffer,
            "studio_mode": self.controller.set_studio_mode,
            "transition": self.controller.set_transition,
            "duration": self.controller.set_transition_duration,
            "trigger_transition": self.controller.trigger_transition,
            # G4：T 型推杆（position, release）
            "tbar": self.controller.set_tbar_position,
            # G5：快捷转场槽位（客户端侧）
            "quick_slots": self.controller.quick_transitions,
            "quick_run": self.controller.run_quick_transition,
            "quick_manage": self._manage_quick_transitions,
            "settings": self._open_settings_dialog,
            # 混音器（E）
            "volume_preview": self.controller.set_input_volume_preview,
            "volume_commit": self.controller.set_input_volume,
            "toggle_mute": self.controller.toggle_input_mute,
            "mute_all": self.controller.set_all_muted,  # E9
            "hide": self.controller.set_input_hidden,
            "advanced": self.open_advanced_audio,
            "show_percent": lambda: self.controller.config.mixer_show_percent,
            "set_show_percent": self._set_mixer_unit,
        }

        self.preview_panel = PreviewPanel(self.store, callbacks)
        self.scene_panel = ScenePanel(
            self.store,
            self.controller.scene_clicked,
            on_reorder=self.controller.move_scene,
            on_create=self.controller.create_scene,
            on_rename=self.controller.rename_scene,
            on_remove=self.controller.remove_scene,
        )
        self.source_panel = SourcePanel(
            self.store,
            self.controller.set_item_enabled,
            media_callbacks={
                # L1/L2：媒体源播放控制
                "media_action": self.controller.media_action,
                "media_seek": self.controller.seek_media,
            },
        )
        self.mixer_panel = MixerPanel(self.store, callbacks)
        self.transitions_panel = TransitionsPanel(self.store, callbacks)
        self.controls_panel = ControlsPanel(self.store, callbacks)

        # 中央区就是预览区；底部五个面板改成 dock，可浮动可重排
        self.setCentralWidget(self.preview_panel)
        self.docks: dict[str, QDockWidget] = {}
        self._build_docks()

        self.status_bar = StatusBar(
            self.store,
            {
                # H9：段显隐交给主窗口持久化
                "segments": lambda: self.controller.config.status_segments,
                "segments_changed": self._save_status_segments,
                "rtt_warn_ms": lambda: self.controller.config.rtt_warn_ms,
            },
        )
        self.setStatusBar(self.status_bar)

    # ---------------------------------------------------------------- H8：Dock 化
    def _build_docks(self) -> None:
        for label, widget in self._panels().items():
            dock = QDockWidget(label, self)
            # objectName 是 restoreState 的键，必须唯一且稳定
            dock.setObjectName(f"dock_{label}")
            dock.setWidget(widget)
            # 可拖动、可浮动；不给 Closable —— 隐藏统一走「停靠窗口」菜单，
            # 免得用户手滑把面板关掉后找不到恢复入口
            dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            )
            # 面板自带标题条（DockPanel），dock 自己再画一个就重复了。
            # 换成一条细抓手：既能拖，又不重复显示名字。
            dock.setTitleBarWidget(DockGrip(label, dock, self))
            self.docks[label] = dock
        self.arrange_docks()

    def arrange_docks(self) -> None:
        """把五个面板重新排成底部一行（也是「恢复默认布局」的底座）。"""
        previous: QDockWidget | None = None
        for label in self._panels():
            dock = self.docks[label]
            dock.setFloating(False)
            if previous is None:
                self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
            else:
                self.splitDockWidget(previous, dock, Qt.Orientation.Horizontal)
            previous = dock
        sizes = list(DOCK_SIZES)[: len(self.docks)]
        self.resizeDocks(list(self.docks.values()), sizes, Qt.Orientation.Horizontal)

    def _build_menu(self) -> None:
        # 菜单名与 OBS 保持一致，但只放本项目真的能做事的项；
        # OBS 的「配置文件 / 场景集合」属编辑类能力，MVP 不做，故不设这两个菜单。
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction("连接设置…", self._open_connect_dialog)
        file_menu.addAction("断开连接", self.controller.disconnect)
        file_menu.addSeparator()
        # M1/M2：切换会重载 OBS 全部场景，放在"文件"菜单下（与 OBS 一致的位置）
        self.collection_menu = file_menu.addMenu("场景集合")
        self.profile_menu = file_menu.addMenu("配置文件")
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        self.store.collections_changed.connect(self._rebuild_collection_menu)
        self.store.profiles_changed.connect(self._rebuild_profile_menu)
        self.store.record_changed.connect(self._refresh_config_menus_enabled)
        self.store.stream_changed.connect(self._refresh_config_menus_enabled)
        self.store.capabilities_changed.connect(lambda: self._refresh_config_menus_enabled())
        self._rebuild_collection_menu()
        self._rebuild_profile_menu()

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction("刷新状态", self.controller.refresh_all)
        view_menu.addSeparator()
        self.studio_action = view_menu.addAction("工作室模式", self._toggle_studio_mode)
        self.studio_action.setCheckable(True)
        # H10：紧凑 / 迷你模式
        self._compact_action = view_menu.addAction("紧凑模式", self._toggle_compact_mode)
        self._compact_action.setCheckable(True)
        self._compact_action.setChecked(self.controller.config.compact_mode)
        self._compact_action.setToolTip("只保留控制按钮与状态栏的小窗口，适合副屏 / 触摸屏")
        view_menu.addAction("缩放至窗口", self._zoom_to_fit)
        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("主题")
        self._theme_group = QActionGroup(self)
        for name in theme.available():
            action = theme_menu.addAction(theme.THEME_LABELS.get(name, name))
            action.setCheckable(True)
            action.setChecked(name == theme.current_name())
            action.setData(name)
            action.triggered.connect(self._on_theme_selected)
            self._theme_group.addAction(action)
        self.store.studio_changed.connect(self._sync_studio_action)

        dock_menu = self.menuBar().addMenu("停靠窗口(&D)")
        self._panel_actions: dict[str, object] = {}
        for label in self._panels():
            action = dock_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, name=label: self.set_panel_visible(name, checked)
            )
            self._panel_actions[label] = action
        dock_menu.addSeparator()
        dock_menu.addAction("恢复默认布局", self._reset_layout)

        tools_menu = self.menuBar().addMenu("工具(&T)")
        tools_menu.addAction("高级音频属性…", lambda: self.open_advanced_audio(None))
        tools_menu.addAction("诊断窗口（原始 JSON）…", self.open_diagnostics)
        tools_menu.addSeparator()
        tools_menu.addAction("设置…", self._open_settings_dialog)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction("快捷键", self._show_shortcuts)
        help_menu.addAction("关于", self._show_about)

    def _build_shortcuts(self) -> None:
        """H7：应用内快捷键（与 OBS 的习惯尽量一致）。"""
        bindings = [
            ("F5", self.controller.refresh_all),
            ("Ctrl+R", self.controller.toggle_record),
            ("Ctrl+L", self._toggle_stream_guarded),
            ("Ctrl+P", self.controller.toggle_pause_record),
            ("Ctrl+M", lambda: self.controller.set_studio_mode(not self.store.studio_mode)),
            ("Ctrl+T", self.controller.trigger_transition),
            ("Ctrl+Shift+A", lambda: self.open_advanced_audio(None)),
            ("Ctrl+," , self._open_settings_dialog),
        ]
        # 场景 1~9：Ctrl+数字
        bindings += [
            (f"Ctrl+{index}", lambda i=index: self._switch_scene_by_index(i))
            for index in range(1, 10)
        ]
        # G5：快捷转场槽位 1~9（应用内）
        bindings += [
            (f"Ctrl+Alt+Shift+{index}", lambda i=index: self._run_quick_transition(i - 1))
            for index in range(1, 10)
        ]
        self._shortcuts: list[QShortcut] = []
        for sequence, slot in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _switch_scene_by_index(self, index: int) -> None:
        scenes = self.store.scenes
        if 1 <= index <= len(scenes):
            self.controller.scene_clicked(scenes[index - 1].name)

    # ---------------------------------------------------------------- H6：托盘与全局热键
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = TrayIcon(
            self.store,
            {
                "toggle_window": self._toggle_window,
                "toggle_record": self.controller.toggle_record,
                "toggle_stream": self._toggle_stream_guarded,
                "studio_mode": self.controller.set_studio_mode,
                "quit": self._quit,
            },
            self,
        )
        self._tray.setVisible(self.controller.config.tray_enabled)
        self.store.record_changed.connect(self._notify_record)
        self.store.stream_changed.connect(self._notify_stream)

    def _toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()
        if self._tray is not None:
            self._tray.set_window_visible(self.isVisible())

    def _quit(self) -> None:
        self._force_quit = True
        self.close()

    def _notify_record(self) -> None:
        """窗口不在前台时才弹气泡，免得正在操作时被打扰。"""
        if self._tray is None or self.isVisible():
            return
        record = self.store.record
        self._tray.notify("录制", "开始录制" if record.active else "已停止录制")

    def _notify_stream(self) -> None:
        if self._tray is None or self.isVisible():
            return
        self._tray.notify("直播", "开始直播" if self.store.stream.active else "已停止直播")

    # 全局热键编号：1~2 是录制/直播，3 是工作室模式，10+ 是场景 1~9，20+ 是快捷转场 1~9
    HOTKEY_ACTIONS = {1: "record", 2: "stream", 3: "studio"}
    QUICK_HOTKEY_BASE = 20

    def hotkey_bindings(self) -> dict[int, str]:
        bindings = {1: "Ctrl+Alt+R", 2: "Ctrl+Alt+L", 3: "Ctrl+Alt+M"}
        for index in range(1, 10):
            bindings[9 + index] = f"Ctrl+Alt+{index}"
        # G5：快捷转场。用 Ctrl+Alt+Shift+数字，和场景热键分开，
        # 免得演播室模式下"切场景"和"推转场"互相抢键
        for index in range(1, 10):
            bindings[self.QUICK_HOTKEY_BASE + index] = f"Ctrl+Alt+Shift+{index}"
        return bindings

    def apply_hotkey_config(self) -> int:
        """按配置注册/注销全局热键，返回成功注册的条数。"""
        self._hotkeys.unregister()
        if not self.controller.config.global_hotkeys or not global_hotkeys.available():
            return 0
        count = self._hotkeys.register(self.hotkey_bindings())
        return count

    def _on_hotkey(self, hotkey_id: int) -> None:
        action = self.HOTKEY_ACTIONS.get(hotkey_id)
        if action == "record":
            self.controller.toggle_record()
        elif action == "stream":
            self._toggle_stream_guarded()
        elif action == "studio":
            self.controller.set_studio_mode(not self.store.studio_mode)
        elif hotkey_id >= self.QUICK_HOTKEY_BASE:
            self._run_quick_transition(hotkey_id - self.QUICK_HOTKEY_BASE - 1)
        elif hotkey_id > 9:
            self._switch_scene_by_index(hotkey_id - 9)

    # ---------------------------------------------------------------- 交互
    def _open_connect_dialog(self) -> None:
        dialog = ConnectDialog(self.controller.settings, self.controller.config.connection, self)
        if dialog.exec():
            config: ConnectionConfig = dialog.result_config()
            self.controller.connect(config)

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.controller.config, self)
        if dialog.exec():
            self.controller.apply_config(dialog.result_config())
            # H6：设置里改了托盘/全局热键，立刻生效
            if self._tray is not None:
                self._tray.setVisible(self.controller.config.tray_enabled)
            self.apply_hotkey_config()
            # I3：日志级别 / 落盘开关也立即生效
            from ..app import apply_logging

            apply_logging(self.controller.config)

    def _toggle_studio_mode(self, checked: bool) -> None:
        self.controller.set_studio_mode(checked)

    def _toggle_stream_guarded(self) -> None:
        """D10：停止直播要二次确认。

        按钮、托盘菜单、全局热键三条路径都汇到这里，避免只有某一路有保护。
        确认框只在"正在推流 → 要停"且开关打开时出现。
        """
        if self.store.stream.active and self.controller.config.confirm_stop_stream:
            answer = QMessageBox.question(
                self,
                "停止直播",
                "确定要停止推流吗？观众会立刻断流。\n\n"
                "（可在「工具 → 设置」里关掉这个确认）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.controller.toggle_stream()

    def _sync_studio_action(self) -> None:
        self.studio_action.blockSignals(True)
        self.studio_action.setChecked(self.store.studio_mode)
        self.studio_action.blockSignals(False)

    def _zoom_to_fit(self) -> None:
        self.preview_panel.zoom_combo.setCurrentIndex(0)
        self.preview_panel._on_zoom_picked(0)

    def open_advanced_audio(self, name: str | None = None) -> None:
        """E6：高级音频属性，非模态，允许多次打开同一个窗口。"""
        if self._advanced_dialog is None:
            self._advanced_dialog = AdvancedAudioDialog(
                self.store,
                {
                    "monitor": self.controller.set_monitor_type,
                    "balance": self.controller.set_balance,
                    "sync": self.controller.set_sync_offset,
                    "tracks": self.controller.set_tracks,
                    "fetch": self.controller.fetch_advanced_audio,
                },
                self,
            )
        self._advanced_dialog.show()
        self._advanced_dialog.raise_()
        self._advanced_dialog.activateWindow()
        if name:
            self._advanced_dialog.select_source(name)

    def open_diagnostics(self) -> None:
        """J3：诊断窗口。非模态，关闭时断开信号避免空转。"""
        if self._diagnostics_dialog is None:
            self._diagnostics_dialog = DiagnosticsWindow(self)
        dialog = self._diagnostics_dialog
        dialog.attach(self.controller.worker)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _set_mixer_unit(self, show_percent: bool) -> None:
        self.controller.config.mixer_show_percent = show_percent
        self.controller.settings.save_config(self.controller.config)
        self.mixer_panel.rebuild()

    def _save_status_segments(self, names: list[str]) -> None:
        """H9：状态栏段显隐。复用 H4 的持久化通道（save_config）。"""
        self.controller.config.status_segments = list(names)
        self.controller.settings.save_config(self.controller.config)

    # ---------------------------------------------------------------- G5：快捷转场
    def _manage_quick_transitions(self) -> None:
        dialog = QuickTransitionsDialog(
            self.controller.quick_transitions(), self.store.transitions, self
        )
        if dialog.exec():
            self.controller.save_quick_transitions(dialog.result_slots())
            self.preview_panel.refresh_quick_slots()

    def _run_quick_transition(self, index: int) -> None:
        self.controller.run_quick_transition(index)

    # ---------------------------------------------------------------- M1/M2：场景集合与配置文件
    def _rebuild_collection_menu(self) -> None:
        self._fill_config_menu(
            self.collection_menu,
            self.store.scene_collections,
            self.store.current_scene_collection,
            self._switch_scene_collection,
        )

    def _rebuild_profile_menu(self) -> None:
        self._fill_config_menu(
            self.profile_menu,
            self.store.profiles,
            self.store.current_profile,
            self._switch_profile,
        )

    def _fill_config_menu(self, menu, names, current, handler) -> None:
        menu.clear()
        if not names:
            action = menu.addAction("（未连接或服务端不支持）")
            action.setEnabled(False)
            return
        for name in names:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == current)
            action.setEnabled(name != current)
            action.triggered.connect(lambda _checked=False, n=name: handler(n))
        self._refresh_config_menus_enabled()

    def _refresh_config_menus_enabled(self) -> None:
        """录制 / 推流中禁用 —— 与 controller 的拦截保持一致，别让用户点了才知道。"""
        supported = self.store.connection_state == CONNECTED
        # 只在连接且不在录制/推流时才可点；原因写进 tooltip，界面和控制器用的是同一个判断
        reason = self.controller.config_switch_blocker()
        for menu, label in (
            (self.collection_menu, "场景集合"),
            (self.profile_menu, "配置文件"),
        ):
            menu.setEnabled(supported and not reason)
            menu.setToolTip(
                f"{label}：{reason}" if reason
                else (f"切换{label}" if supported else f"{label}：未连接")
            )

    def _switch_scene_collection(self, name: str) -> None:
        if not self._confirm_config_switch("场景集合", name):
            return
        self.controller.switch_scene_collection(name)

    def _switch_profile(self, name: str) -> None:
        if not self._confirm_config_switch("配置文件", name, profile=True):
            return
        self.controller.switch_profile(name)

    def _confirm_config_switch(self, kind: str, name: str, profile: bool = False) -> bool:
        """M1/M2 的第一条约束：必须二次确认。

        配置文件比场景集合更重 —— 它会连带改输出 / 编码器设置，
        所以文案要写得更明确，别让人以为是"只换个名字"。
        """
        blocker = self.controller.config_switch_blocker()
        if blocker:
            QMessageBox.warning(self, f"无法切换{kind}", blocker)
            return False
        if profile:
            detail = (
                f"确定要把 OBS 的配置文件切换到「{name}」吗？\n\n"
                "配置文件里包含输出、编码器、串流等设置，切换后这些都会跟着变。\n"
                "正在进行的推流/录制会被禁止切换；切换完成后客户端会重新读取全部状态。"
            )
            icon = QMessageBox.Icon.Warning
        else:
            detail = (
                f"确定要把场景集合切换到「{name}」吗？\n\n"
                "OBS 会重新加载整套场景，当前界面上的场景与来源都会换成新的那一套。"
            )
            icon = QMessageBox.Icon.Question
        box = QMessageBox(icon, f"切换{kind}", detail, parent=self)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "快捷键",
            "F5            刷新状态\n"
            "Ctrl+R        开始 / 停止录制\n"
            "Ctrl+P        暂停 / 继续录制\n"
            "Ctrl+L        开始 / 停止直播\n"
            "Ctrl+M        切换工作室模式\n"
            "Ctrl+T        执行转场\n"
            "Ctrl+1 ~ 9    切换第 1~9 个场景（演播室模式下为设为预览）\n"
            "Ctrl+Shift+A  高级音频属性\n"
            "Ctrl+,        设置\n\n"
            "场景面板：双击切换；拖拽或用 ▲▼ 调整顺序；右键新建 / 重命名 / 删除",
        )

    def _show_error(self, title: str, detail: str) -> None:
        QMessageBox.warning(self, title, detail)

    def _show_about(self) -> None:
        info = self.store.server_info
        QMessageBox.information(
            self,
            "关于",
            "OBS Remote Studio\n"
            "通过 obs-websocket v5 远程控制 OBS Studio。\n\n"
            f"已连接服务端：{info.label()}\n"
            "订阅事件：Scenes / Outputs / SceneItems / Transitions / Ui\n"
            "界面中置灰的按钮对应 OBS 的编辑类功能，本阶段未接入。",
        )

    def _update_title(self, info) -> None:
        self.setWindowTitle(f"OBS Remote Studio — {info.label()}")

    # ---------------------------------------------------------------- 主题
    def _on_theme_selected(self) -> None:
        action = self._theme_group.checkedAction()
        name = action.data() if action is not None else None
        if not name or name == theme.current_name():
            return
        # 局部导入：app 模块反过来要 import main_window，避免循环依赖
        from ..app import apply_theme

        apply_theme(QApplication.instance(), name)
        self.controller.settings.save_theme(name)
        self._refresh_theme_icons()

    def _refresh_theme_icons(self) -> None:
        """图标是手绘的像素图，换主题后要重画一遍。"""
        self.source_panel.refresh()
        self.controls_panel.refresh_icons()
        self.status_bar.refresh_icons()
        self.mixer_panel.rebuild()

    # ---------------------------------------------------------------- 生命周期
    def _restore_layout(self) -> None:
        """H4 + H8：窗口几何、dock 布局（含浮动位置）、面板显隐都要记住。"""
        settings = self.controller.settings
        geometry = settings.load_geometry()
        state = settings.load_state()
        if geometry:
            self.restoreGeometry(geometry)
        if state:
            # 带版本号：dock 结构变了（比如这轮把面板 Dock 化）老数据直接作废，
            # 否则会还原出一个"看着像对但缺面板"的布局
            self.restoreState(state, DOCK_LAYOUT_VERSION)

        sizes = settings.load_json("preview_splitter")
        splitter = self.preview_panel.splitter
        # 保存时若某个画面是隐藏的，分栏尺寸里会出现 0；
        # 直接套用会得到一个 0 宽的画面，看起来像"画面丢了"。所以有过小值就整体忽略。
        if (
            isinstance(sizes, list)
            and len(sizes) == splitter.count()
            and all(int(value) >= MIN_SPLITTER_SIZE for value in sizes)
        ):
            splitter.setSizes([int(value) for value in sizes])

        self._restore_dock_visibility()
        # H10：上次是迷你模式就接着迷你
        if self.controller.config.compact_mode:
            self.set_compact_mode(True, remember=False)

    def _panel_order(self) -> list[str]:
        return list(self._panels().keys())

    def _restore_dock_visibility(self) -> None:
        visibility = self.controller.settings.load_json("panels", {})
        for label, dock in self.docks.items():
            visible = True
            if isinstance(visibility, dict):
                visible = bool(visibility.get(label, True))
            dock.setVisible(visible)
            action = self._panel_actions.get(label)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(visible)
                action.blockSignals(False)

    def set_panel_visible(self, label: str, visible: bool) -> None:
        dock = self.docks.get(label)
        if dock is None:
            return
        dock.setVisible(visible)
        action = self._panel_actions.get(label)
        if action is not None:
            action.blockSignals(True)
            action.setChecked(visible)
            action.blockSignals(False)

    def _panels(self) -> dict[str, QWidget]:
        return {
            "场景": self.scene_panel,
            "来源": self.source_panel,
            "混音器": self.mixer_panel,
            "转场动画": self.transitions_panel,
            "控制按钮": self.controls_panel,
        }

    def _reset_layout(self) -> None:
        """H8：把浮出去的面板全部收回，再按默认比例重排。"""
        self.set_compact_mode(False, remember=False)
        for label in self.docks:
            self.set_panel_visible(label, True)
        self.preview_panel.setVisible(True)
        self.arrange_docks()
        self.preview_panel.splitter.setSizes([420, 190, 420])

    def _save_layout(self) -> None:
        settings = self.controller.settings
        if self._compact:
            # H10：迷你模式的几何单独存，别覆盖普通模式的
            settings.save_compact_geometry(self.saveGeometry())
            return
        settings.save_layout(self.saveGeometry(), self.saveState())
        settings.save_json("preview_splitter", self.preview_panel.splitter.sizes())
        settings.save_json(
            "panels", {label: dock.isVisible() for label, dock in self.docks.items()}
        )

    # ---------------------------------------------------------------- H10：迷你模式
    def set_compact_mode(self, enabled: bool, remember: bool = True) -> None:
        """只留控制按钮 + 状态栏的小窗，适合副屏与触摸屏。

        与 H4 的普通几何**分开存**：两套尺寸互相覆盖的话，
        来回切一次就会把正常布局毁掉。
        """
        enabled = bool(enabled)
        if enabled == self._compact and remember:
            return
        settings = self.controller.settings
        if enabled:
            if not self._compact:
                settings.save_layout(self.saveGeometry(), self.saveState())
            self._compact = True
            self.preview_panel.setVisible(False)
            for label, dock in self.docks.items():
                dock.setVisible(label in COMPACT_KEEP)
            self._compact_action.blockSignals(True)
            self._compact_action.setChecked(True)
            self._compact_action.blockSignals(False)
            geometry = settings.load_compact_geometry()
            if geometry:
                self.restoreGeometry(geometry)
            else:
                self.resize(*COMPACT_SIZE)
        else:
            was_compact = self._compact
            self._compact = False
            if was_compact:
                settings.save_compact_geometry(self.saveGeometry())
            self.preview_panel.setVisible(True)
            self._restore_dock_visibility()
            self._compact_action.blockSignals(True)
            self._compact_action.setChecked(False)
            self._compact_action.blockSignals(False)
            if was_compact:
                geometry = settings.load_geometry()
                if geometry:
                    self.restoreGeometry(geometry)
        if remember:
            self.controller.config.compact_mode = enabled
            settings.save_config(self.controller.config)

    def _toggle_compact_mode(self, checked: bool) -> None:
        self.set_compact_mode(checked)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_layout()
        # J3：诊断窗口是独立顶层窗口，主窗口收起来时一并收掉并断开信号
        if self._diagnostics_dialog is not None:
            self._diagnostics_dialog.detach(self.controller.worker)
            self._diagnostics_dialog.close()
        # H6：可配置"关闭时最小化到托盘"
        if (
            not getattr(self, "_force_quit", False)
            and self.controller.config.close_to_tray
            and self._tray is not None
        ):
            event.ignore()
            self.hide()
            self._tray.set_window_visible(False)
            self._tray.notify("OBS Remote Studio", "已最小化到托盘，可从托盘菜单退出")
            return
        self._hotkeys.unregister()
        if self._tray is not None:
            self._tray.setVisible(False)
        super().closeEvent(event)

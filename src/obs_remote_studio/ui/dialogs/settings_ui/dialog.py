"""仿 OBS 原版的「设置」对话框（左侧页面列表 + 右侧滚动页 + 确定/取消/应用）。

结构完全照 OBS 31 的设置窗口来：
    ┌ 标题栏「设置」 ────────────────────────────────────────┐
    │ 常规      │  常规                                       │
    │ 外观      │  ───────────────────────────────────────    │
    │ 直播      │  语言            [ 简体中文            ▾ ]  │
    │ 输出      │  ☐ 启动时打开统计对话框                     │
    │ …         │  ☐ 在屏幕采集中隐藏 OBS 窗口                │
    ├───────────┴─────────────────────────────────────────┤
    │                                    [确定] [取消] [应用] │
    └──────────────────────────────────────────────────────┘

**「应用」不是摆设**：OBS 里它是"就地生效、窗口不关"，
所以这里也真的这么走 —— 按下即把当前页面（全部页面）的改动写回配置并触发回调，
只是不关闭窗口。这样用户能一边调缩略图间隔一边看主窗口的变化。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import theme
from ....utils import icons
from .form import REASON_REMOTE
from .pages import PAGE_CLASSES, PAGE_ORDER

# 左侧列表宽度：OBS 原版差不多是这么宽（能放下「无障碍环境」四个字 + 图标）
NAV_WIDTH = 168
# 内容区最小尺寸，保证「基础（画布）分辨率」这类长标签不被压折
PAGE_MIN_WIDTH = 560
PAGE_MIN_HEIGHT = 460


class SettingsContext:
    """设置页要用到的外部依赖，集中一处，页面不直接摸 controller。

    只传页面真的需要的东西（配置、状态源、主题名、热键回调），
    而不是整个 Controller —— 页面因此可以脱离网络与线程单独构造，便于测试。
    """

    def __init__(self, config, store, theme_name: str, hotkeys=None, log_dir_opener=None):
        self.config = config
        self.store = store
        self.theme_name = theme_name
        self.hotkeys = hotkeys or {}
        self.log_dir_opener = log_dir_opener

    def title_for(self, key: str) -> str:
        for page_key, label, _icon in PAGE_ORDER:
            if page_key == key:
                return label
        return key

    @property
    def hotkey_summary(self) -> str:
        return str(self.hotkeys.get("summary", "Ctrl+Alt+R 录制 / +L 直播 / +1~9 场景"))

    def record_directory_text(self) -> str:
        """录像路径的只读文本：带上 OBS 端报来的实际目录。

        拿不到（未连接 / 还没问过 / OBS 在另一台机器）就说清楚，别显示空框让人
        以为读到了空值。
        """
        getter = self.hotkeys.get("record_directory")
        value = ""
        if callable(getter):
            try:
                value = str(getter() or "")
            except Exception:  # noqa: BLE001 - 读不到不该让设置窗打不开
                value = ""
        return value or "未连接 / OBS 未上报"

    def hotkey_rows(self) -> list[tuple[str, str]]:
        rows = self.hotkeys.get("rows")
        return list(rows) if rows else []


class SettingsDialog(QDialog):
    """返回 `result_config()` 的模态设置窗（与旧版 API 保持一致）。"""

    # 点「应用」时发出：主窗口借此立刻让托盘/热键/日志生效
    applied = Signal(object)
    # 主题换了要单独说一下：它不存进 AppConfig，而是走 ui/theme 这条持久化通道
    theme_selected = Signal(str)

    def __init__(self, config, parent=None, store=None, theme_name=None,
                 hotkeys=None, log_dir_opener=None):
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle("设置")
        self.config = config
        self._log_dir_opener = log_dir_opener

        self.context = SettingsContext(
            config=config,
            store=store,
            theme_name=theme_name or theme.current_name(),
            hotkeys=hotkeys,
            log_dir_opener=log_dir_opener,
        )

        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(NAV_WIDTH)
        self.nav.setIconSize(QSize(16, 16))
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.stack = QStackedWidget()

        self.pages: dict[str, QWidget] = {}
        for page_key, label, icon_name in PAGE_ORDER:
            page_class = self._page_class(page_key)
            page = page_class(self.context)
            self.pages[page_key] = page
            self.stack.addWidget(self._scrollable(page))
            item = QListWidgetItem(icons.settings_icon(icon_name), label)
            item.setData(Qt.ItemDataRole.UserRole, page_key)
            self.nav.addItem(item)

        self.nav.currentRowChanged.connect(self._on_page_changed)

        # 页面名固定在内容区顶部（不随内容滚动）—— OBS 原版就是这样：
        # 左侧选中「常规」，右侧顶上写一行「常规」，下面是可滚动的分组。
        self.page_title = QLabel("")
        self.page_title.setObjectName("settingsPageTitle")
        header = QFrame()
        header.setObjectName("settingsHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 14, 8)
        header_layout.addWidget(self.page_title)
        header_layout.addStretch(1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(header)
        content_layout.addWidget(self.stack, 1)

        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self.nav)
        body_layout.addWidget(content, 1)

        self.button_box = QDialogButtonBox()
        self.ok_btn = self.button_box.addButton(
            "确定", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.cancel_btn = self.button_box.addButton(
            "取消", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.apply_btn = QPushButton("应用")
        self.apply_btn.setObjectName("applyButton")
        self.button_box.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.ApplyRole)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.apply_btn.clicked.connect(self.apply_changes)

        footer = QHBoxLayout()
        footer.setContentsMargins(10, 6, 10, 8)
        footer.addStretch(1)
        footer.addWidget(self.button_box)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.body, 1)
        layout.addLayout(footer)

        # 打开日志目录这件事由主窗口提供的回调做（要弹系统文件管理器）
        self._wire_log_button()
        self.setMinimumSize(NAV_WIDTH + PAGE_MIN_WIDTH, PAGE_MIN_HEIGHT)
        self.nav.setCurrentRow(0)

    # ---------------------------------------------------------------- 组装
    @staticmethod
    def _page_class(page_key: str):
        for page_class in PAGE_CLASSES:
            if page_class.key == page_key:
                return page_class
        raise KeyError(page_key)

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setObjectName("settingsPane")
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(page)
        return area

    def _wire_log_button(self) -> None:
        page = self.pages.get("advanced")
        opener = self._log_dir_opener
        if page is not None and opener is not None and hasattr(page, "open_log_btn"):
            page.open_log_btn.clicked.connect(opener)

    def _on_page_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.nav.item(row)
        page_key = str(item.data(Qt.ItemDataRole.UserRole) or "")
        index = list(self.pages).index(page_key) if page_key in self.pages else 0
        self.stack.setCurrentIndex(index)
        self.page_title.setText(item.text())

    # ---------------------------------------------------------------- 结果
    def _collect(self):
        """把全部页面的改动收进一份新 AppConfig。

        主题要在这里发一次信号：它不存进 AppConfig，而是走
        `settings.save_theme` + `apply_theme` 这条通道（与「视图 → 主题」相同）。
        「确定」和「应用」都走这里，所以两条路径都不会漏掉换主题。
        """
        config = self.config
        for page in self.pages.values():
            config = page.apply_to(config)
        self.config = config
        appearance = self.pages.get("appearance")
        chosen = getattr(appearance, "theme_combo", None)
        if chosen is not None and chosen.currentData() != theme.current_name():
            self.theme_selected.emit(str(chosen.currentData()))
        return config

    def apply_changes(self) -> None:
        """「应用」：写回配置并发出信号，但**不关窗口**（对应 OBS 的「应用」）。"""
        self.applied.emit(self._collect())

    def result_config(self):
        """「确定」的返回：静默收一次（避免与「应用」重复生效），由调用方落实。"""
        return self._collect()

__all__ = ["SettingsDialog", "SettingsContext", "REASON_REMOTE"]

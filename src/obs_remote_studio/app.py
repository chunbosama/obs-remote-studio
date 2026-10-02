"""应用入口。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from .core.controller import Controller
from .ui import theme
from .ui.main_window import MainWindow

ORG_NAME = "obs-remote-studio"
APP_NAME = "OBS Remote Studio"


def apply_high_dpi_policy() -> None:
    """O2：显示缩放策略。**必须在 QApplication 构造之前调用**。

    Qt6 默认按 125% / 150% 这类分数缩放**四舍五入到整数倍**，
    副屏（常见 125%、150%）上会把界面放大成 2 倍，看起来"字巨大、界面被撑开"。
    `PassThrough` 直接用真实比例渲染，配合 Qt6 自带的高 DPI 位图支持，
    在 125%/150% 的屏上才是正常的观感。
    """
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # Qt6 默认已开，这里显式声明一次，避免将来有人改了默认值
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)


def stylesheet_path() -> Path:
    return Path(__file__).parent / "ui" / "resources" / "style.qss"


def _load_stylesheet(app: QApplication) -> None:
    """style.qss 是模板，@TOKEN@ 按当前主题替换。"""
    try:
        template = stylesheet_path().read_text(encoding="utf-8")
    except OSError:
        return
    # 数字框的 ↑ / ↓ 图标是按当前主题现画的（QSS 画不出三角形，见 spin_arrows 说明）
    from .ui import spin_arrows

    app.setStyleSheet(theme.render_stylesheet(template, spin_arrows.arrow_image_tokens()))


def apply_theme(app: QApplication, name: str | None = None) -> str:
    """切换主题：调色板 + 样式表一次换掉，返回生效的主题名。"""
    if name is not None:
        theme.set_theme(name)
    _apply_theme_palette(app)
    _load_stylesheet(app)
    return theme.current_name()


def apply_logging(config) -> None:
    """I3：设置里改了日志级别 / 落盘开关后立刻生效（无需重启）。"""
    from .utils.logging_setup import setup_logging

    setup_logging(
        level=config.log_level,
        debug_obsws="--debug-obsws" in sys.argv,
        log_to_file=config.log_to_file,
    )


# 优先使用带中文的界面字体，避免默认字体缺字时整屏方块
PREFERRED_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "PingFang SC",
    "WenQuanYi Micro Hei",
)


def _apply_font(app: QApplication) -> None:
    families = set(QFontDatabase.families())
    for family in PREFERRED_FONTS:
        if family in families:
            font = app.font()
            font.setFamily(family)
            app.setFont(font)
            return


def _apply_theme_palette(app: QApplication) -> None:
    """按当前主题设置调色板。

    不能只靠 QSS：弹出层（下拉列表、菜单、Tooltip）是独立顶层窗口，
    不继承主窗口的样式；而且 Qt 会跟随 Windows 的浅色/深色模式给出不同调色板。
    这里从调色板层面兜底，QSS 再对具体控件做细节覆盖。
    """
    colors = theme.current()
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: colors["BG"],
        QPalette.ColorRole.WindowText: colors["TEXT"],
        QPalette.ColorRole.Base: colors["SURFACE"],
        QPalette.ColorRole.AlternateBase: colors["SURFACE_ALT"],
        QPalette.ColorRole.ToolTipBase: colors["SURFACE_ALT"],
        QPalette.ColorRole.ToolTipText: colors["TEXT"],
        QPalette.ColorRole.Text: colors["TEXT"],
        QPalette.ColorRole.Button: colors["BUTTON"],
        QPalette.ColorRole.ButtonText: colors["TEXT"],
        QPalette.ColorRole.BrightText: colors["TEXT_ON_ACCENT"],
        QPalette.ColorRole.Highlight: colors["SELECT"],
        QPalette.ColorRole.HighlightedText: colors["TEXT_ON_ACCENT"],
        QPalette.ColorRole.PlaceholderText: colors["TEXT_DIM"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    # 置灰态单独设：Qt6 把 ColorGroup 与 ColorRole 合并了，
    # 没有 ColorRole.Disabled，要按「组 + 角色」两个参数设。
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        palette.setColor(
            QPalette.ColorGroup.Disabled, role, QColor(colors["TEXT_DISABLED"])
        )
    app.setPalette(palette)


def main(argv: list[str] | None = None) -> int:
    debug_obsws = "--debug-obsws" in (argv or sys.argv)
    # 日志配置存在 QSettings 里，先读一次；之后 Controller 会再读一次，
    # 但那时日志已经就位，设置里改动通过 apply_logging 即时生效。
    from .core.settings import AppSettings
    from .utils.logging_setup import setup_logging

    startup = AppSettings().load_config()
    setup_logging(
        level="DEBUG" if debug_obsws else startup.log_level,
        debug_obsws=debug_obsws,
        log_to_file=startup.log_to_file,
    )
    # O2：必须在 QApplication 构造之前设显示缩放策略
    apply_high_dpi_policy()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    _apply_font(app)
    apply_theme(app)

    controller = Controller()
    # 主题跟着配置走
    apply_theme(app, controller.settings.load_theme())
    window = MainWindow(controller)
    window.show()

    code = app.exec()
    controller.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

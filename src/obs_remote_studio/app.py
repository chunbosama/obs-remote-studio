"""应用入口。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from .core.controller import Controller
from .ui import theme
from .ui.main_window import MainWindow
from .utils.resources import resource_path

ORG_NAME = "obs-remote-studio"
APP_NAME = "OBS Remote Studio"

# J5：打包产物自检开关。**只给构建脚本/回归用**，正常启动不经过这里。
SELFTEST_FLAG = "--selftest"
SELFTEST_MS = 1200          # 事件循环跑多久再退出（让轮询/定时器有机会炸出来）


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
    """style.qss 的位置。

    不能写 `Path(__file__).parent / ...`：打包成 exe 后代码与资源的实际落点
    都和源码树不同，必须由 `utils/resources.py` 统一解析（打包踩坑记在 J5）。
    """
    return resource_path("ui", "resources", "style.qss")


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


def _selftest_report_path() -> Path:
    """自检结果写在这儿。**不写 stdout**：exe 是无控制台窗口的，print 没人接得住。"""
    base = Path(tempfile.gettempdir())
    return base / "obs-remote-studio-selftest.txt"


def _collect_selftest_problems(app: QApplication, window: MainWindow) -> list[str]:
    """查的就是「打包最容易漏掉的东西」：随包资源、样式替换、界面是否真的建起来。

    特意不查那些跑源码也一定成立的东西（协议、网络）—— 自检是为了守打包，
    不是第二套单元测试。
    """
    problems: list[str] = []

    # 样式表：不看「有没有 @ 之类的蛛丝马迹」（style.qss 头部注释里就有 @TOKEN@ 当示例，
    # 按特征猜必然误判），而是**拿模板现渲染一遍，和真正应用在 app 上的逐字比对** ——
    # 一次同时验掉「文件读到了」「token 替换完成」「真的 setStyleSheet 了」。
    qss = stylesheet_path()
    if not qss.is_file():
        problems.append(f"样式表没打进包里：{qss}")
    else:
        from .ui import spin_arrows

        try:
            expected = theme.render_stylesheet(
                qss.read_text(encoding="utf-8"), spin_arrows.arrow_image_tokens()
            )
        except (OSError, KeyError) as exc:
            problems.append(f"样式表渲染失败：{exc}")
        else:
            if app.styleSheet() != expected:
                problems.append("样式表没真正生效（当前应用上的与重新渲染的结果不一致）")

        # 数字框箭头是启动时现画到临时目录的 PNG，QSS 用绝对路径引用它。
        # 临时目录不可写 / 冻结后画不出图时，界面会静默少掉箭头 —— 这里盯一眼。
        missing = [
            name
            for name, path in spin_arrows.arrow_image_tokens().items()
            if not Path(path).is_file()
        ]
        if missing:
            problems.append(f"数字框箭头图没生成：{missing}")

    if window.menuBar() is None:
        problems.append("菜单栏没建起来")
    if not window.windowTitle():
        problems.append("窗口标题为空")

    return problems


def run_selftest(app: QApplication, window: MainWindow) -> int:
    """跑一小段事件循环，然后把结论落到结果文件并返回退出码。

    要真跑事件循环：界面能建起来不代表轮询/重连那些 `QTimer` 不炸，
    而「打包后一联网就崩」正是这类问题。窗口会闪约 1 秒，属自检的正常现象。
    """
    window.show()
    app.processEvents()
    problems = _collect_selftest_problems(app, window)

    if problems:
        return _finish_selftest(problems)

    # 有连接尝试时才有东西可跑；没配 OBS 也会走重连退避，不阻塞
    QTimer.singleShot(SELFTEST_MS, app.quit)
    app.exec()
    return _finish_selftest(_collect_selftest_problems(app, window))


def _finish_selftest(problems: list[str]) -> int:
    lines = ["SELFTEST OK：资源与界面均正常"] if not problems else [
        "SELFTEST FAIL：" + item for item in problems
    ]
    report = _selftest_report_path()
    try:
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    # 有控制台（源码运行、-Console）时也顺手打一份，两边都看得到。
    # 包 try：无窗口的 exe 里 sys.stderr 可能是 None，print 会直接抛。
    try:
        for line in lines:
            print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001 - 打不了日志不该影响退出码
        pass
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv)
    selftest = SELFTEST_FLAG in args
    # --selftest 是我们自己的开关，别透给 Qt（Qt 会为不认识的参数打警告）
    qt_args = [arg for arg in args if arg != SELFTEST_FLAG]

    debug_obsws = "--debug-obsws" in args
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
    app = QApplication(qt_args)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    _apply_font(app)
    apply_theme(app)

    controller = Controller()
    # 主题跟着配置走
    apply_theme(app, controller.settings.load_theme())
    window = MainWindow(controller)

    if selftest:
        code = run_selftest(app, window)
        controller.shutdown()
        return code

    window.show()

    code = app.exec()
    controller.shutdown()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

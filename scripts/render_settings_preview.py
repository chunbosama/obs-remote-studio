"""把「设置」对话框离屏渲染成 PNG，用于和 OBS 原版设置窗口逐页对照。

用法：
    python scripts/render_settings_preview.py [输出目录] [--light] [--page 常规]

不连 OBS —— 设置面板本身不依赖连接（只有「视频」页会去读 state_store 的
画布信息，拿不到就显示"未连接 / 未上报"）。所以这里连假服务器都不需要起，
渲染速度快且结果稳定，方便每次改版前后对照。

说明：走 QT_QPA_PLATFORM=offscreen，不弹窗；离屏平台没有系统字体库，
中文会渲染成方块，所以手动挂载 Windows 字体（与 render_ui_preview.py 一致）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试必须与真实配置隔离（Windows 上 QSettings(org, app) 走注册表）
os.environ["OBSRS_SETTINGS_ORG"] = "obs-remote-studio-tests"
os.environ["OBSRS_SETTINGS_INI"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from obs_remote_studio.app import _apply_font, apply_theme  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.ui import theme  # noqa: E402
from obs_remote_studio.ui.dialogs.settings_ui.dialog import SettingsDialog  # noqa: E402
from obs_remote_studio.ui.dialogs.settings_ui.pages import PAGE_ORDER  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402

SYSTEM_FONTS = ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf")

# 页面中文名 → 渲染文件名（英文，避免中文路径在别的机器上出问题）
SLUGS = {
    "常规": "general",
    "外观": "appearance",
    "直播": "stream",
    "输出": "output",
    "音频": "audio",
    "视频": "video",
    "快捷键": "hotkeys",
    "无障碍环境": "accessibility",
    "高级": "advanced",
}


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    theme_name = "light" if "--light" in sys.argv else "dark"
    only = None
    for index, value in enumerate(sys.argv):
        if value == "--page" and index + 1 < len(sys.argv):
            only = sys.argv[index + 1]
    out_dir = Path(args[0]).resolve() if args else ROOT / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp())

    app = QApplication([])
    for font_file in SYSTEM_FONTS:
        QFontDatabase.addApplicationFont(font_file)
    app.setStyle("Fusion")
    _apply_font(app)
    apply_theme(app, theme_name)

    controller = Controller()
    window = MainWindow(controller)
    dialog = SettingsDialog(
        controller.config,
        window,
        store=controller.store,
        theme_name=theme.current_name(),
        hotkeys={"rows": window._hotkey_rows(), "summary": window._hotkey_summary()},
        log_dir_opener=window._open_log_dir,
    )
    dialog.resize(920, 680)
    dialog.show()
    app.processEvents()

    suffix = "-light" if theme_name == "light" else ""
    saved = 0
    for row, (_key, label, _icon) in enumerate(PAGE_ORDER):
        if only and label != only:
            continue
        dialog.nav.setCurrentRow(row)
        app.processEvents()
        path = out_dir / f"settings-{SLUGS.get(label, row)}{suffix}.png"
        if dialog.grab().save(str(path)):
            saved += 1
            print(f"已保存：{path}")
        else:
            print(f"保存失败：{path}")

    dialog.close()
    controller.shutdown()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())

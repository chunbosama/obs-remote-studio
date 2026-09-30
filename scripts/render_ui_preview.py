"""把主窗口连上假 OBS 后离屏渲染成 PNG，用于比对 OBS 界面 / 改版前后对照。

用法：
    python scripts/render_ui_preview.py [输出路径] [--no-studio] [--light]

说明：
- 走 QT_QPA_PLATFORM=offscreen，不需要真实桌面，也不会弹窗。
- 离屏平台没有系统字体库，中文会渲染成方块，所以这里手动挂载 Windows 字体
  （真实运行时由 windows 平台插件负责，无需关心）。
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
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fake_obs_server import DEFAULT_PASSWORD, FakeObsServer  # noqa: E402
from obs_remote_studio.app import _apply_font, apply_theme  # noqa: E402
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402

SYSTEM_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    studio = "--no-studio" not in sys.argv
    theme_name = "light" if "--light" in sys.argv else "dark"
    out_path = Path(args[0]).resolve() if args else ROOT / "logs" / "ui-preview.png"

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp())

    app = QApplication([])
    # 注意：addApplicationFont 必须在 QApplication 之后调用，
    # 否则平台插件还没初始化，直接段错误。
    for font_file in SYSTEM_FONTS:
        QFontDatabase.addApplicationFont(font_file)
    app.setStyle("Fusion")
    _apply_font(app)
    apply_theme(app, theme_name)

    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()

    controller = Controller()
    controller.config.poll_interval_ms = 300
    window = MainWindow(controller)
    window.resize(1100, 720)
    window.show()

    Controller.connect(
        controller, ConnectionConfig("127.0.0.1", port, DEFAULT_PASSWORD)
    )
    deadline = time.time() + 15
    while time.time() < deadline and controller.store.connection_state != CONNECTED:
        app.processEvents()
        time.sleep(0.02)

    if studio:
        controller.set_studio_mode(True)
        controller.set_preview_scene("开场")
    else:
        controller.switch_scene("开场")
    pump(app, 4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    saved = window.grab().save(str(out_path))
    store = controller.store
    print(f"已保存：{out_path}（{'成功' if saved else '失败'}）")
    print(f"连接={store.connection_state}  场景={[s.name for s in store.scenes]}")
    print(f"来源={[i.source_name for i in store.scene_items]}")
    print(f"转场={store.transitions} 当前={store.current_transition}")
    print(f"主题={theme_name}  音频源={[i.name for i in store.audio_inputs]}")

    controller.shutdown()
    server.stop()
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())

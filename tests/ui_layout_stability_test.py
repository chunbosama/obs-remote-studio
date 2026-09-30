"""布局稳定性回归测试。

起因（用户反馈）：连上 OBS 后，右边框每秒往右挪一点，窗口越变越宽。

机制：`QLabel.setPixmap()` 会把图片尺寸算进 sizeHint，
而缩略图是「按当前控件大小缩放」得到的 —— 控件变大 → 图变大 → sizeHint 变大
→ 布局要求更大 → 控件又变大，形成正反馈，顶层窗口的 minimumSize 被一路顶大。

判定：跑若干帧缩略图后，窗口尺寸与各面板尺寸必须保持不变。

运行：python tests/ui_layout_stability_test.py
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
from obs_remote_studio.app import (  # noqa: E402
    _apply_font,
    _apply_theme_palette,
    _load_stylesheet,
)
from obs_remote_studio.core.controller import Controller  # noqa: E402
from obs_remote_studio.core.models import ConnectionConfig  # noqa: E402
from obs_remote_studio.core.state_store import CONNECTED  # noqa: E402
from obs_remote_studio.ui.main_window import MainWindow  # noqa: E402

FRAME_INTERVAL_MS = 120
OBSERVE_SECONDS = 3.0

CHECKS: list[str] = []
FAILED: list[str] = []


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


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def snapshot(window: MainWindow) -> dict[str, object]:
    preview = window.preview_panel
    # H8：面板已 Dock 化，用每个 dock 的宽度来观察布局是否漂移
    dock_sizes = tuple(
        (label, dock.width(), dock.height()) for label, dock in window.docks.items()
    )
    return {
        "window": (window.width(), window.height()),
        "window_min": (window.minimumWidth(), window.minimumHeight()),
        "docks": dock_sizes,
        "preview_splitter": tuple(preview.splitter.sizes()),
        "video_area": (preview.output_pane.video_area.width(),
                       preview.output_pane.video_area.height()),
    }


def main() -> int:
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tempfile.mkdtemp())

    app = QApplication([])
    for font_file in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/segoeui.ttf"):
        QFontDatabase.addApplicationFont(font_file)
    app.setStyle("Fusion")
    _apply_font(app)
    _apply_theme_palette(app)
    _load_stylesheet(app)

    port = free_port()
    server = FakeObsServer(host="127.0.0.1", port=port)
    server.start()

    controller = Controller()
    controller.config.poll_interval_ms = 200
    controller.config.preview_interval_ms = FRAME_INTERVAL_MS
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

    frames = 0
    controller.store.frame_ready.connect(lambda *_: None)
    pump(app, 0.6)  # 先让首帧落地
    before = snapshot(window)
    frames = server.state.screenshot_count

    print(f"[1] 连跑 {OBSERVE_SECONDS:.0f} 秒缩略图后布局是否稳定")
    pump(app, OBSERVE_SECONDS)
    after = snapshot(window)
    frames_after = server.state.screenshot_count
    check("期间确实在持续出帧", frames_after > frames, f"{frames} → {frames_after}")

    for key in before:
        check(f"{key} 未变化", before[key] == after[key], f"{before[key]} → {after[key]}")

    print("[2] 窗口本身没有被顶大")
    check("窗口尺寸未变", before["window"] == after["window"],
          f"{before['window']} → {after['window']}")
    check("窗口最小尺寸未变", before["window_min"] == after["window_min"],
          f"{before['window_min']} → {after['window_min']}")

    print("[3] 演播室模式（双画面）同样稳定")
    controller.set_studio_mode(True)
    pump(app, 1.0)
    before = snapshot(window)
    frames = server.state.screenshot_count
    pump(app, OBSERVE_SECONDS * 2)
    after = snapshot(window)
    check("双画面期间确实在持续出帧", server.state.screenshot_count > frames,
          f"{frames} → {server.state.screenshot_count}")
    for key in before:
        check(f"{key} 未变化（演播室）", before[key] == after[key],
              f"{before[key]} → {after[key]}")

    print("[4] 缩放条与画面联动正常")
    window.resize(900, 600)
    pump(app, 0.5)
    check("缩小窗口后 fit 百分比下降",
          int(window.preview_panel.zoom_label.text().rstrip("%") or 0) > 0,
          window.preview_panel.zoom_label.text())
    window.resize(1100, 720)
    pump(app, 0.5)
    check("恢复尺寸后仍能出图",
          window.preview_panel.output_pane.view.displayed_percent() > 0,
          str(window.preview_panel.output_pane.view.displayed_percent()))

    window.close()
    controller.shutdown()
    server.stop()

    print(f"\n通过 {len(CHECKS)} 项，失败 {len(FAILED)} 项")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

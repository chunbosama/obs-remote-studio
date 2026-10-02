"""数字框增减按钮上的 ↑ / ↓ 图标。

**为什么不用 QSS 画三角**：Qt 的 QSS 不会像浏览器那样把相邻边框斜切成三角形 ——
实测四种写法（带/不带 width·height、只留一条边、放大到 6px）渲染出来
都是同一块 6×4 实心小块（详见 style.qss 里的说明）。所以改成用字体渲染
↑ / ↓ 两个字符，生成 PNG 交给 QSS 的 `image` 引用。

**为什么文件名要带颜色指纹**：QSS 的 `image: url(...)` 会被 Qt 记进 QPixmapCache，
路径不变时换了主题也不会重新读图，颜色就换不过来了。把颜色算进文件名，
换主题自然换一张新图，缓存问题不存在。

图片落在系统临时目录（不是用户配置目录），且只在不存在时才生成。
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

from . import theme

UP = "\u2191"      # ↑
DOWN = "\u2193"    # ↓

# 与 QComboBox 的箭头同一观感：小、居中、不抢戏。
# BOX 要和 style.qss 里 `::up-arrow` 的 width/height 对齐 —— QSS 的 image 不缩放，
# 图片比 subcontrol 大会被裁掉。字号取满格并加粗，否则 ↑ 的细笔画被抗锯齿摊淡成灰线。
BOX = 12
FONT_PX = 12
BOLD = True


def _cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "obs-remote-studio" / "spin-arrows"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ui_font() -> QFont:
    """用界面字体渲染，保证 ↑ / ↓ 字形一定存在（雅黑、Segoe UI 都有）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    font = QFont(app.font()) if app is not None else QFont()
    font.setPixelSize(FONT_PX)
    font.setBold(BOLD)
    return font


def _render(char: str, color: str, path: Path) -> None:
    pixmap = QPixmap(BOX, BOX)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setFont(_ui_font())
    painter.setPen(QColor(color))
    painter.drawText(QRect(0, 0, BOX, BOX), Qt.AlignmentFlag.AlignCenter, char)
    painter.end()
    pixmap.save(str(path), "PNG")


def _image(char: str, color: str, tag: str) -> str:
    # 指纹里既要有颜色（换主题要换图），也要有渲染参数（调字号要重画），
    # 否则改了参数却复用旧缓存，界面"改了没反应"。
    seed = f"{color}|{BOX}|{FONT_PX}|{BOLD}"
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]
    path = _cache_dir() / f"{tag}-{digest}.png"
    if not path.exists() or path.stat().st_size == 0:
        _render(char, color, path)
    return path.as_posix()


def arrow_image_tokens(theme_name: str | None = None) -> dict[str, str]:
    """当前（或指定）主题下四个箭头图的路径，供 QSS 模板做 @TOKEN@ 替换。

    四个 = 上/下 × 正常/置灰。到上下限时按钮会被禁用，那时箭头也要跟着变暗，
    否则"灰按钮 + 亮箭头"自相矛盾。
    """
    original = theme.current_name()
    if theme_name is not None:
        theme.set_theme(theme_name)
    try:
        normal = theme.color("ARROW")
        disabled = theme.color("TEXT_DISABLED")
        return {
            "SPIN_UP_IMAGE": _image(UP, normal, "up"),
            "SPIN_DOWN_IMAGE": _image(DOWN, normal, "down"),
            "SPIN_UP_IMAGE_DISABLED": _image(UP, disabled, "up-disabled"),
            "SPIN_DOWN_IMAGE_DISABLED": _image(DOWN, disabled, "down-disabled"),
        }
    finally:
        theme.set_theme(original)

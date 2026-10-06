"""手绘小图标。

OBS 界面里用了眼睛、锁、时钟、信号格这些图标，Qt 内置图标集里没有对应的，
又不想为一个 MVP 引入图片资源，所以直接用 QPainter 画。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

from ..ui import theme

SIZE = 16


def _new_pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    return pixmap


def _painter(pixmap: QPixmap, color: str, width: float = 1.3) -> QPainter:
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    return painter


def eye_icon(visible: bool, size: int = SIZE, color: str | None = None) -> QIcon:
    """可见性图标：睁眼 / 闭眼（斜杠）。"""
    pixmap = _new_pixmap(size)
    painter = _painter(pixmap, color or (theme.color("TEXT") if visible else theme.color("TEXT_DISABLED")))
    scale = size / 16.0
    eye = QRectF(2 * scale, 5 * scale, 12 * scale, 6 * scale)
    painter.drawEllipse(eye)
    painter.setBrush(QColor(painter.pen().color()))
    painter.drawEllipse(QPointF(8 * scale, 8 * scale), 1.6 * scale, 1.6 * scale)
    if not visible:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(3 * scale, 13 * scale), QPointF(13 * scale, 3 * scale))
    painter.end()
    return QIcon(pixmap)


def lock_icon(locked: bool, size: int = SIZE, color: str | None = None) -> QIcon:
    """锁定图标：锁上 / 打开（锁梁偏到一侧）。"""
    pixmap = _new_pixmap(size)
    painter = _painter(pixmap, color or theme.color("TEXT_DIM"))
    scale = size / 16.0
    body = QRectF(4 * scale, 8 * scale, 8 * scale, 6 * scale)
    painter.drawRoundedRect(body, 1.2 * scale, 1.2 * scale)
    shank = QRectF(5.5 * scale, 4.5 * scale, 5 * scale, 5 * scale)
    if locked:
        painter.drawArc(shank, 0, 180 * 16)
        painter.drawLine(QPointF(5.5 * scale, 7 * scale), QPointF(5.5 * scale, 8.5 * scale))
        painter.drawLine(QPointF(10.5 * scale, 7 * scale), QPointF(10.5 * scale, 8.5 * scale))
    else:  # 打开：锁梁整体抬起并右移
        painter.drawArc(QRectF(8 * scale, 3.5 * scale, 5 * scale, 5 * scale), 0, 180 * 16)
        painter.drawLine(QPointF(13 * scale, 6 * scale), QPointF(13 * scale, 8.5 * scale))
    painter.end()
    return QIcon(pixmap)


def dot_icon(color: str = theme.color("RED"), size: int = 12) -> QIcon:
    """实心圆点：直播 / 录制按钮前面的指示灯。"""
    pixmap = _new_pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    offset = size * 0.2
    painter.drawEllipse(QRectF(offset, offset, size - 2 * offset, size - 2 * offset))
    painter.end()
    return QIcon(pixmap)


def speaker_icon(muted: bool, size: int = SIZE, color: str | None = None) -> QIcon:
    """扬声器：静音时画一道斜杠（OBS 混音器每行右侧那个按钮）。"""
    pixmap = _new_pixmap(size)
    pen_color = theme.color("TEXT_DISABLED") if muted else theme.color("TEXT")
    painter = _painter(pixmap, color or pen_color, 1.2)
    scale = size / 16.0
    # 音箱主体：一个梯形 + 矩形
    body = [
        QPointF(3 * scale, 6 * scale),
        QPointF(6 * scale, 6 * scale),
        QPointF(9 * scale, 3.5 * scale),
        QPointF(9 * scale, 12.5 * scale),
        QPointF(6 * scale, 10 * scale),
        QPointF(3 * scale, 10 * scale),
    ]
    painter.drawPolygon(QPolygonF(body))
    if muted:
        painter.drawLine(QPointF(11 * scale, 5 * scale), QPointF(14 * scale, 11 * scale))
        painter.drawLine(QPointF(14 * scale, 5 * scale), QPointF(11 * scale, 11 * scale))
    else:
        painter.drawArc(QRectF(8.5 * scale, 5 * scale, 6 * scale, 6 * scale), -60 * 16, 120 * 16)
    painter.end()
    return QIcon(pixmap)


def clock_icon(size: int = 12, color: str | None = None) -> QIcon:
    """时钟：状态栏的计时。"""
    pixmap = _new_pixmap(size)
    painter = _painter(pixmap, color or theme.color("TEXT_DIM"), 1.1)
    scale = size / 12.0
    painter.drawEllipse(QRectF(1 * scale, 1 * scale, 10 * scale, 10 * scale))
    painter.drawLine(QPointF(6 * scale, 3.5 * scale), QPointF(6 * scale, 6 * scale))
    painter.drawLine(QPointF(6 * scale, 6 * scale), QPointF(8.5 * scale, 6 * scale))
    painter.end()
    return QIcon(pixmap)


def app_icon(size: int = 64) -> QIcon:
    """应用/托盘图标：深色圆底 + 录制红环，风格与 OBS 的圆形图标接近。"""
    pixmap = _new_pixmap(size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    scale = size / 64.0
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#2b2b2b"))
    painter.drawEllipse(QRectF(1 * scale, 1 * scale, 62 * scale, 62 * scale))
    pen = QPen(QColor(theme.color("RED")))
    pen.setWidthF(7 * scale)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(18 * scale, 18 * scale, 28 * scale, 28 * scale))
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------- 设置面板导航图标
# OBS 的设置窗口左侧每个页面都有一个 16px 图标。这里按同样的语义手绘：
# 常规=齿轮、外观=显示器、直播=信号塔、输出=推流箭头、音频=喇叭、
# 视频=摄像机、快捷键=键盘、无障碍环境=人形、高级=叉形扳手、更新=循环箭头。
# 全部只用线段/圆/多边形画，不引入任何图片资源（与上面几个图标同一套做法）。
SETTINGS_ICON_SIZE = 16


def _settings_icon(kind: str, size: int = SETTINGS_ICON_SIZE,
                   color: str | None = None) -> QIcon:
    """设置面板左侧的页面图标。`kind` 见下面的分发表。"""
    pixmap = _new_pixmap(size)
    painter = _painter(pixmap, color or theme.color("HEADER_TEXT"), 1.2)
    s = size / 16.0

    if kind == "general":  # 齿轮
        painter.drawEllipse(QPointF(8 * s, 8 * s), 4.2 * s, 4.2 * s)
        painter.drawEllipse(QPointF(8 * s, 8 * s), 1.5 * s, 1.5 * s)
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            painter.drawLine(
                QPointF((8 + dx * 4.6) * s, (8 + dy * 4.6) * s),
                QPointF((8 + dx * 6.4) * s, (8 + dy * 6.4) * s),
            )
    elif kind == "appearance":  # 显示器
        painter.drawRect(QRectF(1.5 * s, 3 * s, 13 * s, 8.5 * s))
        painter.drawLine(QPointF(6 * s, 14 * s), QPointF(10 * s, 14 * s))
        painter.drawLine(QPointF(8 * s, 11.5 * s), QPointF(8 * s, 14 * s))
    elif kind == "stream":  # 信号塔
        painter.drawEllipse(QPointF(8 * s, 12 * s), 1.5 * s, 1.5 * s)
        painter.drawLine(QPointF(8 * s, 10.5 * s), QPointF(8 * s, 4 * s))
        painter.drawArc(QRectF(5 * s, 1 * s, 6 * s, 6 * s), 20 * 16, 140 * 16)
        painter.drawArc(QRectF(2.5 * s, -1 * s, 11 * s, 11 * s), 20 * 16, 140 * 16)
    elif kind == "output":  # 出框箭头（推流 / 输出）
        painter.drawRect(QRectF(1.5 * s, 5 * s, 9 * s, 9 * s))
        painter.drawLine(QPointF(8 * s, 9 * s), QPointF(8 * s, 1.5 * s))
        painter.drawLine(QPointF(8 * s, 1.5 * s), QPointF(5.2 * s, 4.3 * s))
        painter.drawLine(QPointF(8 * s, 1.5 * s), QPointF(10.8 * s, 4.3 * s))
    elif kind == "audio":  # 喇叭
        body = [
            QPointF(2 * s, 6 * s), QPointF(5 * s, 6 * s),
            QPointF(8 * s, 3 * s), QPointF(8 * s, 13 * s),
            QPointF(5 * s, 10 * s), QPointF(2 * s, 10 * s),
        ]
        painter.drawPolygon(QPolygonF(body))
        painter.drawArc(QRectF(7.5 * s, 4.5 * s, 6 * s, 7 * s), -60 * 16, 120 * 16)
    elif kind == "video":  # 摄像机
        painter.drawRect(QRectF(1.5 * s, 5 * s, 9 * s, 7 * s))
        painter.drawPolygon(QPolygonF([
            QPointF(10.5 * s, 8.5 * s), QPointF(14.5 * s, 6 * s),
            QPointF(14.5 * s, 11 * s), QPointF(10.5 * s, 8.5 * s),
        ]))
        painter.drawLine(QPointF(1.5 * s, 3 * s), QPointF(6 * s, 3 * s))
    elif kind == "hotkeys":  # 键盘
        painter.drawRect(QRectF(1 * s, 4 * s, 14 * s, 8.5 * s))
        painter.drawLine(QPointF(1 * s, 8 * s), QPointF(15 * s, 8 * s))
        for x in (4.0, 8.0, 12.0):
            painter.drawLine(QPointF(x * s, 4 * s), QPointF(x * s, 8 * s))
        painter.drawLine(QPointF(4.5 * s, 12.5 * s), QPointF(11.5 * s, 12.5 * s))
    elif kind == "accessibility":  # 无障碍环境：人形
        painter.drawEllipse(QPointF(8 * s, 2.8 * s), 1.7 * s, 1.7 * s)
        painter.drawLine(QPointF(8 * s, 4.5 * s), QPointF(8 * s, 10 * s))
        painter.drawLine(QPointF(3.5 * s, 7 * s), QPointF(12.5 * s, 7 * s))
        painter.drawLine(QPointF(8 * s, 10 * s), QPointF(5 * s, 14.5 * s))
        painter.drawLine(QPointF(8 * s, 10 * s), QPointF(11 * s, 14.5 * s))
    elif kind == "advanced":  # 叉形扳手
        painter.drawLine(QPointF(2 * s, 12 * s), QPointF(9 * s, 5 * s))
        painter.drawLine(QPointF(2 * s, 12 * s), QPointF(10.5 * s, 13.5 * s))
        painter.drawEllipse(QRectF(9 * s, 1.5 * s, 5.5 * s, 5.5 * s))
        painter.drawLine(QPointF(11.8 * s, 7 * s), QPointF(11.8 * s, 9 * s))
    elif kind == "update":  # 循环箭头
        painter.drawArc(QRectF(2 * s, 2 * s, 12 * s, 12 * s), 40 * 16, 280 * 16)
        painter.drawPolygon(QPolygonF([
            QPointF(12.5 * s, 1.5 * s), QPointF(14 * s, 5.5 * s), QPointF(10 * s, 5 * s),
        ]))
    else:  # 未知类型就画个方块，至少不崩
        painter.drawRect(QRectF(2.5 * s, 2.5 * s, 11 * s, 11 * s))
    painter.end()
    return QIcon(pixmap)


def settings_icon(kind: str, size: int = SETTINGS_ICON_SIZE) -> QIcon:
    """设置面板左侧列表的页面图标（见 `_settings_icon` 的 kind 分发）。"""
    return _settings_icon(kind, size)


def signal_icon(level: int, size: int = 16, color: str | None = None) -> QIcon:
    """信号格子（level 为点亮格数 0~4）。"""
    pixmap = _new_pixmap(size)
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    scale = size / 16.0
    for index in range(4):
        height = (3 + index * 3) * scale
        rect = QRectF((2 + index * 3.4) * scale, (14 * scale) - height, 2.4 * scale, height)
        painter.setBrush(QColor(color if index < level else theme.color("TEXT_DISABLED")))
        painter.drawRoundedRect(rect, 0.6 * scale, 0.6 * scale)
    painter.end()
    return QIcon(pixmap)

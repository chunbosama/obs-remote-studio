"""O2：高 DPI 辅助。

自绘控件（预览画面、电平表、推子刻度）如果直接用逻辑坐标画，
在 125% / 150% 这类**分数缩放**下会落在物理像素中间，
表现为边缘发虚、1px 线时有时无。这里统一做一次"对齐到物理像素网格"。

注意：Qt6 的布局与命中测试都用逻辑坐标，所以**只在对齐时**做这一次换算，
不要拿换算后的值去算布局尺寸。
"""

from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QWidget


def device_ratio(widget: QWidget) -> float:
    """控件所在屏幕的物理/逻辑像素比。异常值兜回 1.0。"""
    try:
        ratio = float(widget.devicePixelRatioF())
    except (AttributeError, TypeError):
        return 1.0
    return ratio if ratio > 0 else 1.0


def snap(value: float, ratio: float) -> float:
    """把逻辑坐标对齐到物理像素网格。"""
    if ratio <= 0:
        return value
    return round(value * ratio) / ratio


def snap_rect(rect: QRect, ratio: float) -> QRect:
    """把一个矩形对齐到物理像素网格。

    注意：QRect 用的是整数逻辑坐标，而 125%/150% 这类比例下
    "整数逻辑宽度"并不总能对应整物理像素（1.5 倍下每 2 个逻辑像素才 3 个物理像素）。
    所以这里只保证**左上角落在物理像素格点上**、尺寸不退化成 0，
    不去追求"宽高也是整物理像素"——那在整数逻辑坐标下根本做不到。
    """
    if ratio <= 0:
        return rect
    left = round(rect.left() * ratio) / ratio
    top = round(rect.top() * ratio) / ratio
    width = max(1.0, round(rect.width() * ratio) / ratio)
    height = max(1.0, round(rect.height() * ratio) / ratio)
    return QRect(round(left), round(top), round(width), round(height))


def icon_pixmap(icon, size: int, widget: QWidget):
    """按控件的设备像素比取图标位图，避免高分屏上图标发虚。"""
    from PySide6.QtCore import QSize

    ratio = device_ratio(widget)
    return icon.pixmap(QSize(size, size), ratio)

"""把代码画的应用图标渲染成 exe 用的多尺寸 .ico（J5）。

图标不是图片文件，而是 `utils/icons.py: app_icon()` 用 QPainter 画的（界面/托盘用的
就是它）。所以这里做两件事：按几个尺寸离屏渲染出 PNG，再手工拼成 ICO 容器。

**为什么不用 Pillow**：ICO 的容器格式非常薄 —— 6 字节目录头 + 每个图块 16 字节目录项
+ 图块本体；Vista 之后图块允许直接塞 PNG。PyInstaller 读 ICO 时也是按目录结构原样
拷进资源段（`utils/win32/icon.py` 只解结构、不解码图像），所以手工拼完全够用，
不值得为一个图标给构建流程加一个图像库依赖。

用法：
    python scripts/packaging/make_icon.py <输出.ico>
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

# 必须在导入 Qt 之前设：离屏渲染不需要真窗口，构建机上也稳
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# 任务栏/资源管理器/大图标视图都要看，按 Windows 的常用档位出图
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

# 256 在 ICO 目录项里用 0 表示（一个字节存不下 256）
ICO_SIZE_256 = 0


def _png_bytes(pixmap) -> bytes:
    """QPixmap → PNG 字节。"""
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pixmap.save(buffer, "PNG"):
        raise RuntimeError("QPixmap 存 PNG 失败")
    return bytes(buffer.data())


def render_pngs() -> list[tuple[int, bytes]]:
    from PySide6.QtGui import QGuiApplication

    # QPixmap/QPainter 需要一个 QGuiApplication 实例（离屏也必须有）
    app = QGuiApplication.instance() or QGuiApplication([])
    assert app is not None

    from obs_remote_studio.utils import icons

    out: list[tuple[int, bytes]] = []
    for size in SIZES:
        pixmap = icons.app_icon(size).pixmap(size, size)
        out.append((size, _png_bytes(pixmap)))
    return out


def pack_ico(images: list[tuple[int, bytes]]) -> bytes:
    """手工拼 ICO。

    布局：ICONDIR(6B) + N × ICONDIRENTRY(16B) + 依次排列的图块。
    所有图块都是 32 位带 alpha 的 PNG（PNG 图块时 planes/bitCount 只是占位）。
    """
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    offset = len(header) + 16 * len(images)

    entries = bytearray()
    payload = bytearray()
    for size, data in images:
        if not 0 < size <= 256:
            raise ValueError(f"尺寸超出 ICO 能表示的范围：{size}")
        dim = ICO_SIZE_256 if size == 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dim,            # 宽
            dim,            # 高
            0,              # 调色板色数（真彩填 0）
            0,              # 保留位
            1,              # 颜色平面
            32,             # 位深
            len(data),      # 图块字节数
            offset,         # 图块偏移
        )
        payload += data
        offset += len(data)

    return bytes(header + bytes(entries) + bytes(payload))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_icon.py <output.ico>", file=sys.stderr)
        return 2

    target = Path(argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    images = render_pngs()
    target.write_bytes(pack_ico(images))
    print(f"图标已生成：{target}（{len(images)} 个尺寸，{target.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

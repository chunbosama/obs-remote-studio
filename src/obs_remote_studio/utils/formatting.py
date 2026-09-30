"""展示层格式化工具。"""

from __future__ import annotations


def format_duration(ms: int) -> str:
    """毫秒 -> HH:MM:SS（超过一天也照常累加小时）。"""
    if not ms or ms < 0:
        return "00:00:00"
    total = int(ms // 1000)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_bitrate(bytes_written: int, duration_ms: int) -> str:
    """由已写字节与时长估算 kbps。"""
    if not duration_ms or duration_ms <= 0:
        return "0 kbps"
    kbps = (bytes_written * 8) / duration_ms
    return f"{kbps:,.0f} kbps".replace(",", " ")


def format_bytes(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_fps(value: float) -> str:
    return f"{value:.1f}" if value else "—"


def dropped_ratio(skipped: int, total: int) -> str:
    if not total:
        return "0.00%"
    return f"{skipped / total * 100:.2f}%"

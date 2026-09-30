"""音量 / 推子 / 电平表的纯计算，便于单独测。

OBS 的推子不是线性的：0 dB 大约在推子 3/4 处，再往上到 +26 dB 只占最后一小段。
这里用锚点做分段线性插值，视觉手感与 OBS 基本一致。
"""

from __future__ import annotations

import math

FADER_MIN_DB = -100.0
FADER_MAX_DB = 26.0

# (推子位置 0~1, 对应 dB)
_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.0, FADER_MIN_DB),
    (0.25, -60.0),
    (0.50, -30.0),
    (0.65, -15.0),
    (0.75, 0.0),
    (1.0, FADER_MAX_DB),
)

SLIDER_MAX = 1000  # 推子用整型滑块的刻度数，够精细又不抖


def fader_to_db(position: float) -> float:
    """推子位置 0~1 -> dB。"""
    position = min(max(position, 0.0), 1.0)
    for (x0, y0), (x1, y1) in zip(_ANCHORS, _ANCHORS[1:]):
        if position <= x1:
            ratio = 0.0 if x1 == x0 else (position - x0) / (x1 - x0)
            return y0 + (y1 - y0) * ratio
    return FADER_MAX_DB


def db_to_fader(db: float) -> float:
    """dB -> 推子位置 0~1。"""
    db = min(max(db, FADER_MIN_DB), FADER_MAX_DB)
    for (x0, y0), (x1, y1) in zip(_ANCHORS, _ANCHORS[1:]):
        if db <= y1:
            ratio = 0.0 if y1 == y0 else (db - y0) / (y1 - y0)
            return x0 + (x1 - x0) * ratio
    return 1.0


def db_to_mul(db: float) -> float:
    """dB -> 线性增益（0 dB = 1.0，-100 dB 视作静音 0）。"""
    if db <= FADER_MIN_DB:
        return 0.0
    return 10.0 ** (db / 20.0)


def mul_to_db(mul: float) -> float:
    """线性增益 -> dB（0 或负数视作 -100 dB，即 OBS 的静音下限）。"""
    if mul <= 0:
        return FADER_MIN_DB
    return max(20.0 * math.log10(mul), FADER_MIN_DB)


def format_db(db: float) -> str:
    """OBS 里最低那一档显示成 -inf。"""
    if db <= FADER_MIN_DB + 0.01:
        return "-∞ dB"
    return f"{db:.1f} dB"


def format_percent(mul: float) -> str:
    return f"{mul * 100:.0f}%"


def parse_meter_levels(raw) -> float:
    """把 InputVolumeMeters 里某个源的 inputLevelsMul 折成一个 0~1+ 的峰值。

    协议里它是「声道数组」，每个声道又可能是标量或若干个数（电平/峰值/输入峰值）。
    不同版本字段形状略有差异，这里一律取最大，不做严格假设。
    """

    def flatten(value) -> list[float]:
        if isinstance(value, (int, float)):
            return [float(value)]
        if isinstance(value, (list, tuple)):
            out: list[float] = []
            for item in value:
                out.extend(flatten(item))
            return out
        return []

    levels = flatten(raw)
    return max(levels) if levels else 0.0


def parse_meters_event(event_data) -> dict[str, float]:
    """InputVolumeMeters 事件 -> {源名: 峰值}。"""
    inputs = getattr(event_data, "inputs", None)
    if inputs is None and isinstance(event_data, dict):
        inputs = event_data.get("inputs")
    result: dict[str, float] = {}
    for item in inputs or []:
        if isinstance(item, dict):
            name = item.get("inputName")
            levels = item.get("inputLevelsMul")
        else:
            name = getattr(item, "input_name", None)
            levels = getattr(item, "input_levels_mul", None)
        if name:
            result[str(name)] = parse_meter_levels(levels)
    return result

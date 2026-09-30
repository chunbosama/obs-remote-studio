"""全局热键（H6，仅 Windows）。

用 user32.RegisterHotKey 注册系统级热键，通过 QAbstractNativeEventFilter 收 WM_HOTKEY。
非 Windows 或注册失败时 `available()` 为 False，调用方降级处理即可，不会抛异常。

为什么不引第三方库：全局热键在别的库（keyboard / pynput）里要么需要管理员权限，
要么靠轮询 GetAsyncKeyState，反而更重；这里 60 行 ctypes 足够。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# 热键名 -> 虚拟键码
_VK: dict[str, int] = {
    "R": 0x52,
    "L": 0x4C,
    "M": 0x4D,
    "T": 0x54,
}
for _digit in range(1, 10):
    _VK[str(_digit)] = 0x30 + _digit


def available() -> bool:
    return sys.platform == "win32"


def parse_sequence(sequence: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+R' -> (modifiers, vk)；无法解析返回 None。"""
    parts = [part.strip().upper() for part in sequence.split("+") if part.strip()]
    if not parts:
        return None
    key = parts[-1]
    if key not in _VK:
        return None
    modifiers = MOD_NOREPEAT
    for part in parts[:-1]:
        if part in ("CTRL", "CONTROL"):
            modifiers |= MOD_CONTROL
        elif part == "ALT":
            modifiers |= MOD_ALT
        elif part == "SHIFT":
            modifiers |= MOD_SHIFT
        else:
            return None
    return modifiers, _VK[key]


class _Filter(QAbstractNativeEventFilter):
    """独立的小过滤器：避免和 QObject 多重继承带来的元类问题。"""

    def __init__(self, hotkeys: "GlobalHotkeys"):
        super().__init__()
        self._hotkeys = hotkeys

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if event_type == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
            except Exception:  # noqa: BLE001
                return False, 0
            if msg.message == WM_HOTKEY:
                self._hotkeys.triggered.emit(int(msg.wParam))
        return False, 0


class GlobalHotkeys(QObject):
    """给一串 (id, 序列, 名称) 注册全局热键，按下时发 triggered(id)。"""

    triggered = Signal(int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter = _Filter(self)
        self._registered: set[int] = set()
        self._installed = False
        self._user32 = ctypes.windll.user32 if available() else None

    def register(self, bindings: dict[int, str]) -> int:
        """注册热键，返回成功条数。已注册过的会先注销。"""
        if not available() or self._user32 is None:
            return 0
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if not self._installed and app is not None:
            app.installNativeEventFilter(self._filter)
            self._installed = True

        self.unregister()
        for hotkey_id, sequence in bindings.items():
            parsed = parse_sequence(sequence)
            if parsed is None:
                logger.warning("无法解析全局热键：%s", sequence)
                continue
            modifiers, vk = parsed
            if self._user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
                self._registered.add(hotkey_id)
            else:
                # 常见原因：被别的程序占了
                logger.warning("全局热键注册失败（可能被占用）：%s", sequence)
        return len(self._registered)

    def unregister(self) -> None:
        if not available() or self._user32 is None:
            return
        for hotkey_id in list(self._registered):
            self._user32.UnregisterHotKey(None, hotkey_id)
        self._registered.clear()

    @property
    def count(self) -> int:
        return len(self._registered)

"""随包资源定位（J5）。

开发时读资源用 `Path(__file__)` 天经地义，打包后就不成立了：
PyInstaller 会把代码解包/放到别处，`__file__` 指的位置和源码树完全不同。
所以凡是「读随包附带的文件」都统一走这里，别各写各的。

两种打包形态下 `sys._MEIPASS` 都指向解包目录：
- **单文件（onefile）**：exe 启动时先把内容释放到临时目录，资源就在那儿；
- **目录版（onedir）**：默认是 exe 同级的 `_internal/`。

⚠️ 单文件模式下这个目录**只在本次运行期间存在**（退出即删），
所以它只读不写 —— 要落盘的东西（日志、缓存）走 `QStandardPaths`，
例如 `utils/logging_setup.py` 的日志目录。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

PACKAGE_NAME = "obs_remote_studio"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打好的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


@lru_cache(maxsize=1)
def bundle_root() -> Path:
    """随包资源的根目录：源码态是 `src/`，冻结态是解包目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # 本文件 = src/obs_remote_studio/utils/resources.py → parents[2] = src/
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """包内资源路径，例如 `resource_path("ui", "resources", "style.qss")`。"""
    return bundle_root().joinpath(PACKAGE_NAME, *parts)

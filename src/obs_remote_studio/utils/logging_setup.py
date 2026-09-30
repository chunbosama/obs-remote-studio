"""日志初始化（I3）。

两件事：
1. 控制台按配置的级别输出（默认 INFO）；
2. 「写入日志文件」打开时，额外挂一个按大小滚动的文件 handler，
   排障时不用靠用户复现截图，直接看文件。

文件放在 `QStandardPaths.AppLocalDataLocation/logs/`，Windows 上大致是
`%LOCALAPPDATA%\\obs-remote-studio\\OBS Remote Studio\\logs\\`。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LEVEL = "INFO"
LEVEL_LABELS = {
    "DEBUG": "调试（最详细）",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
}

MAX_BYTES = 1_000_000  # 单文件 1 MB
BACKUP_COUNT = 3
LOG_FILENAME = "obs-remote-studio.log"

# 记住自己装的 handler，重配时先摘掉，避免重复写同一行
_console_handler: logging.Handler | None = None
_file_handler: logging.Handler | None = None


def log_directory() -> Path:
    """日志目录。取不到系统路径就退化到当前目录，不让日志功能把应用拖挂。"""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    root = Path(base) if base else Path.cwd()
    return root / "logs"


def log_file_path() -> Path:
    return log_directory() / LOG_FILENAME


def resolve_level(name: str | int) -> int:
    if isinstance(name, int):
        return name
    return getattr(logging, str(name or DEFAULT_LEVEL).upper(), logging.INFO)


def setup_logging(
    level: int | str = DEFAULT_LEVEL,
    debug_obsws: bool = False,
    log_to_file: bool = False,
) -> Path | None:
    """配置根 logger，返回日志文件路径（未启用落盘时返回 None）。"""
    global _console_handler, _file_handler

    _console_handler = logging.StreamHandler(sys.stderr)
    _console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))

    path: Path | None = None
    _file_handler = None
    if log_to_file:
        try:
            directory = log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / LOG_FILENAME
            _file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            _file_handler.setFormatter(
                logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
            )
        except OSError:
            # 目录不可写不该让程序起不来，退回纯控制台
            _file_handler = None
            path = None

    root = logging.getLogger()
    # 只摘自己上一次装的：别人（pytest、宿主程序）挂的 handler 别动
    for existing in list(root.handlers):
        if existing in (_console_handler, _file_handler):
            continue
        root.removeHandler(existing)

    if _file_handler is not None:
        root.addHandler(_file_handler)
    root.addHandler(_console_handler)
    root.setLevel(resolve_level(level))

    # obsws-python 默认 INFO 会打印带密码的连接串；连不上时还会抛整屏 traceback。
    # 连接失败我们自己分类并弹提示，所以默认把它压到 CRITICAL。
    logging.getLogger("obsws_python").setLevel(
        logging.DEBUG if debug_obsws else logging.CRITICAL
    )
    logging.getLogger("websocket").setLevel(logging.CRITICAL)

    return path

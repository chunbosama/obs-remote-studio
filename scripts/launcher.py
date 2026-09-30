"""pythonw 静默启动辅助脚本（由 scripts/start.ps1 调用，一般不用手动执行）。

用法：
    pythonw.exe scripts/launcher.py <日志文件路径> [--debug-obsws]

做两件事：
1. 把 stderr 接到日志文件（pythonw 没有控制台，否则报错会彻底看不见）；
2. 像 `python -m obs_remote_studio` 一样运行主模块。

之所以需要它：PowerShell 5.1 的 Start-Process 在某些环境下无法使用
-RedirectStandardError（详见 start.ps1 里的注释），只能由 Python 自己写日志。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
APP_MODULE = "obs_remote_studio"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: launcher.py <logfile> [extra args...]", file=sys.stderr)
        return 2

    log_path = Path(sys.argv[1])
    extra_args = sys.argv[2:]

    # 源码路径优先，免除必须 pip install -e 才能 import 的限制
    sys.path.insert(0, str(SRC_DIR))

    log_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)

    sys.argv = [APP_MODULE, *extra_args]
    runpy.run_module(APP_MODULE, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

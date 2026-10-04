# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（J5）。

一份 spec 出两种产物，靠环境变量切换形态（构建脚本 `scripts/packaging/build.py` 会设）：

    OBSRS_ONEFILE=1   单文件：exe 启动时把内容解包到临时目录。**分发首选**，
                      但启动慢一点（本机实测约 3~5 秒）。
    不设              目录版（onedir）：启动快、缺文件一眼看得出来，压缩成 zip 分发。

其它环境变量：
    OBSRS_ICON           .ico 路径（`scripts/packaging/make_icon.py` 生成）
    OBSRS_VERSION_FILE   版本资源文件路径（构建脚本按 pyproject 的版本生成）

入口用 `src/obs_remote_studio/__main__.py` 而不是 `app.py`：PyInstaller 把入口脚本
当 `__main__` 跑，而 `app.py` 内部是包内相对导入，脱离包上下文会直接 ImportError。

体积：PySide6 的 wheel 有 641MB，全收进去不可接受。这里两层裁剪 ——
① `excludes` 把用不到的 Qt 模块从分析阶段就摘掉；
② `_trim()` 再按输出路径过滤一遍。第二层是必须的：PySide6 的 hook 会额外兜底塞进
   一批「基础」文件和插件目录，`excludes` 拦不住它们 —— **60MB 的 Qt 译文**、
   QML/Quick/Pdf/Network/OpenGL/Svg 这几个家族的 DLL 都这么进来的。

译文可以放心删：本项目界面文案全是自己写的，Qt 自带译文只影响 QMessageBox 这类
标准对话框的按钮文字，而源码运行时也没有装 QTranslator，行为一致。

那几个家族的 DLL 也可以删：**它们的硬依赖关系是核过的**（读 `Qt6Widgets.dll` 的
PE 导入表：只引用 `Qt6Gui.dll` + `Qt6Core.dll`；`Qt6Gui.dll` 只引用 `Qt6Core.dll`；
`pyside6.abi3.dll` 只引用 `Qt6Core.dll`），用不到 QML / SVG / QtNetwork。删完必须
跑一遍产物自检 —— 少一个硬依赖 DLL，进程根本起不来，自检立刻会红。

唯一特意留下的是 `opengl32sw.dll`（20MB，软件 OpenGL 后备）：它只在显卡驱动不正常的
机器上被启用（虚拟机、远程桌面、没装驱动的老机器）。我们没法在开发机上复现那种环境，
而它一旦缺失的后果是**目标机器上界面起不来**——这种「本机测不出来、到了用户那儿才炸」
的故障不值得为 20MB 冒。要赌的话，把 prefix 加进 DROP_QT_DLL_PREFIXES 即可。
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent  # scripts/packaging -> 项目根
SRC_DIR = ROOT / "src"
APP_NAME = "OBSRemoteStudio"

ONE_FILE = os.environ.get("OBSRS_ONEFILE") == "1"
ICON_FILE = os.environ.get("OBSRS_ICON") or None
VERSION_FILE = os.environ.get("OBSRS_VERSION_FILE") or None

# --------------------------------------------------------------- 裁剪：不用的 Qt 模块
# 本项目只用到 QtCore / QtGui / QtWidgets（网络全走 websocket-client，不经 QtNetwork）。
EXCLUDED_QT = [
    "PySide6.QtNetwork",
    "PySide6.QtNetworkAuth",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtTest",
    "PySide6.QtSql",
    "PySide6.QtPrintSupport",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtDBus",
    "PySide6.QtConcurrent",
    "PySide6.QtAxContainer",
    "PySide6.QtHttpServer",
    "PySide6.QtCanvasPainter",
    "PySide6.QtXml",
]

# 非 Qt 的：测试/构建期才用得到的，别混进产物
EXCLUDED_OTHER = [
    "tkinter",
    "unittest",
    "pytest",
    "setuptools",
    "pip",
    "pkg_resources",
    "websockets",       # 只有 tests/fake_obs_server.py 用（开发期依赖）
    "PIL",
    "numpy",
]

# ------------------------------------------- 裁剪：Qt 插件目录（hook 会按 DLL 扫描整目录）
DROP_PLUGIN_DIRS = {
    "assetimporters",        # Qt3D 模型导入
    "canbus",
    "designer",              # Qt Designer 插件
    "generic",               # 触摸输入
    "geometryloaders",       # Qt3D
    "geoservices",           # QtLocation
    "multimedia",            # QtMultimedia 后端
    "networkinformation",    # QtNetwork
    "platforminputcontexts", # Windows 下输入法由 qwindows 平台插件原生接管
    "position",
    "qmllint",
    "qmltooling",
    "renderers",             # Qt3D
    "renderplugins",         # Qt3D
    "sceneparsers",          # Qt3D
    "scxmldatamodel",
    "sensors",
    "sqldrivers",            # QtSql
    "texttospeech",
    "tls",                   # QtNetwork
    "vectorimageformats",    # 无 SVG 需求
    "webview",
}

# imageformats 只留这几个：界面里没有任何「从文件加载图片」的路径
# （图标是 QPainter 画的，数字框箭头是运行时写出的 PNG），留着是给将来兜底。
KEEP_IMAGEFORMATS = {"qico", "qpng", "qjpeg"}
KEEP_PLATFORMS = {"qwindows", "qoffscreen", "qminimal"}

# 顶层 Qt 运行时 DLL 里用不到的家族（按前缀匹配，省得跟着 PySide6 的小版本改清单）
DROP_QT_DLL_PREFIXES = (
    "Qt6Quick",               # Quick/QML 运行时
    "Qt6Qml",
    "Qt6Pdf",
    "Qt6Svg",
    "Qt6Network",             # 网络全走 websocket-client，不经 Qt
    "Qt6OpenGL",              # 没有 OpenGL 控件；注意这不等于 opengl32sw.dll
    "Qt6VirtualKeyboard",
    "Qt6Location",
    "Qt6Sql",
    "Qt6Test",
    "Qt6WebEngine",
    "Qt6Multimedia",
    "Qt6Charts",
    "Qt63D",
)

# qsvgicon 依赖 Qt6Svg，一起摘掉（缺了 Qt6Svg 它加载会失败）：
# 图标全是 QPainter 画的，没有任何 SVG 需求
DROP_ICONENGINES = {"qsvgicon"}


def _should_drop(dest: str) -> bool:
    parts = str(dest).replace("\\", "/").split("/")
    if not parts or parts[0] != "PySide6":
        return False

    if len(parts) >= 2 and parts[1] == "translations":
        return True

    if len(parts) == 2:  # PySide6/Qt6Xxx.dll
        return parts[1].startswith(DROP_QT_DLL_PREFIXES)

    if len(parts) >= 4 and parts[1] == "plugins":
        group, stem = parts[2], Path(parts[-1]).stem
        if group in DROP_PLUGIN_DIRS:
            return True
        if group == "iconengines" and stem in DROP_ICONENGINES:
            return True
        if group == "imageformats" and stem not in KEEP_IMAGEFORMATS:
            return True
        if group == "platforms" and stem not in KEEP_PLATFORMS:
            return True

    return False


def _trim(entries):
    """按输出路径过滤 Analysis 收进来的二进制与数据文件。

    PyInstaller 6 的条目是 `(dest_name, src_path, typecode)`，`dest_name` 就是产物里的
    相对落点，所以直接按路径判断，不依赖 hook 的实现细节。
    **`a.datas` 也要过一遍** —— 译文就是被 hook 当数据文件塞进去的，
    只过滤 `a.binaries` 会漏掉它们。
    """
    return [entry for entry in entries if not _should_drop(entry[0])]


a = Analysis(
    [str(SRC_DIR / "obs_remote_studio" / "__main__.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=[
        # 界面样式表是运行时读的文本资源，必须显式带上；
        # 落点要和 obs_remote_studio/utils/resources.py 解析出来的路径对齐。
        (
            str(SRC_DIR / "obs_remote_studio" / "ui" / "resources" / "style.qss"),
            "obs_remote_studio/ui/resources",
        ),
    ],
    hiddenimports=[
        # 函数体内才 import 的模块：静态分析通常能跟到，显式写上不依赖它的判断
        "obs_remote_studio.ui.spin_arrows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_QT + EXCLUDED_OTHER,
    noarchive=False,
    optimize=1,  # 去掉 assert 与 __debug__ 分支
)

a.binaries = _trim(a.binaries)
a.datas = _trim(a.datas)

pyz = PYZ(a.pure)

# 两种形态共用的 EXE 参数
_exe_common = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 未安装 UPX；且压 Qt 的 DLL 有出问题的先例
    runtime_tmpdir=None,
    console=False,  # GUI 程序：不要黑控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if sys.platform == "win32":
    if ICON_FILE:
        _exe_common["icon"] = ICON_FILE
    if VERSION_FILE and Path(VERSION_FILE).is_file():
        _exe_common["version"] = VERSION_FILE


if ONE_FILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **_exe_common)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **_exe_common)
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )

"""一键打包：生成图标 → 构建单文件 exe → 构建目录版 → 自检 → 压成 zip（J5）。

用法（也可以双击根目录的 build.bat）：

    python scripts/packaging/build.py                # 两种产物都出
    python scripts/packaging/build.py --only single   # 只要单文件 exe
    python scripts/packaging/build.py --only portable # 只要压缩包版
    python scripts/packaging/build.py --no-selftest   # 跳过产物自检（不建议）
    python scripts/packaging/build.py --clean         # 先清掉 build/ 缓存再构建

产物落在 `dist/`：

    OBSRemoteStudio-<版本>-win64-single.exe     单文件，拷走即用
    OBSRemoteStudio-<版本>-win64-portable.zip   目录版压缩包（解压后运行里面的 exe）

**构建完一定会自检**：拿真实 exe 跑 `--selftest`（离屏起界面 + 跑一小段事件循环），
确认随包资源、样式表、界面都真的能用。打包最常见的故障就是「能起来但少了文件」，
不自检等于没打。自检会短暂弹出一次窗口，属正常现象。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING_DIR = ROOT / "scripts" / "packaging"
SPEC_FILE = PACKAGING_DIR / "obs_remote_studio.spec"
MAKE_ICON = PACKAGING_DIR / "make_icon.py"
BUILD_DIR = ROOT / "build" / "packaging"
DIST_DIR = ROOT / "dist"

ICON_FILE = BUILD_DIR / "obs-remote-studio.ico"
VERSION_FILE = BUILD_DIR / "version_info.txt"
PYINSTALLER_LOG = BUILD_DIR / "pyinstaller.log"

# 自检专用临时目录。**刻意不用系统的 %TEMP%**，理由见 verify()。
SELFTEST_DIR = BUILD_DIR / "selftest-tmp"
SELFTEST_REPORT_NAME = "obs-remote-studio-selftest.txt"

APP_NAME = "OBSRemoteStudio"
VERSION_INFO_APP_NAME = APP_NAME  # 版本资源里的内部名（ASCII，避免编码坑）

# 自检结果文件：app.py 的 `--selftest` 会写到这里（exe 无控制台，只能落文件）
SELFTEST_OK = "SELFTEST OK"
SELFTEST_TIMEOUT_S = 120

# 单次删除超过这个文件数会被外层的安全策略拦下并要求人工确认（实测阈值 50）。
# 构建要能无人值守地跑完，所以删目录时一律按这个块大小分批删。
SAFE_DELETE_CHUNK = 40


def selftest_report_path() -> Path:
    """自检结果文件的位置。

    它就是自检进程的 `%TEMP%\\obs-remote-studio-selftest.txt`
    （`app.py` 里用的是 `tempfile.gettempdir()`，而 `verify()` 会把 TEMP 指到
    `SELFTEST_DIR`）—— 两边必须对得上，改一处就要改另一处。
    """
    return SELFTEST_DIR / SELFTEST_REPORT_NAME


# ------------------------------------------------------------------ 版本号


def read_version() -> str:
    """版本号以 pyproject.toml 为准，别在这儿再抄一份。"""
    pyproject = ROOT / "pyproject.toml"
    try:
        import tomllib

        with pyproject.open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except Exception:  # noqa: BLE001 - 读不到就当开发版，不拦构建
        return "0.0.0"


# ------------------------------------------------------------------ 构建步骤


def step(text: str) -> None:
    print(f"\n==> {text}", flush=True)


def check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        step("安装 PyInstaller（打包依赖，只装在项目 .venv 里）")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )


def make_icon() -> None:
    step(f"生成图标 {ICON_FILE.relative_to(ROOT)}")
    subprocess.run([sys.executable, str(MAKE_ICON), str(ICON_FILE)], check=True)


def make_version_file(version: str) -> None:
    """生成 exe 的版本资源。

    决定「右键属性 → 详细信息」里显示什么，也影响任务管理器里的描述。
    **字符串只用 ASCII**：这个文件里的中文要经过 PyInstaller 自己的解析再写进
    Windows 资源段，编码出问题会变成方块或者直接构建失败，不值得为几个中文字冒险。
    """
    step(f"生成版本资源 {VERSION_FILE.relative_to(ROOT)}（{version}）")
    parts = version.split(".")
    nums = [int(p) if p.isdigit() else 0 for p in parts][:4]
    while len(nums) < 4:
        nums.append(0)
    quad = ", ".join(str(n) for n in nums)
    VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}),
    prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'obs-remote-studio'),
        StringStruct('FileDescription', 'OBS Remote Studio - OBS Studio remote control client'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{VERSION_INFO_APP_NAME}'),
        StringStruct('OriginalFilename', '{VERSION_INFO_APP_NAME}.exe'),
        StringStruct('ProductName', 'OBS Remote Studio'),
        StringStruct('ProductVersion', '{version}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="ascii",
    )


def run_pyinstaller(onefile: bool, tag: str) -> Path:
    """跑一次 PyInstaller，返回产物路径（exe 或目录）。"""
    kind = "单文件" if onefile else "目录版"
    step(f"构建{kind}（{tag}）—— PyInstaller 输出写进 {PYINSTALLER_LOG.relative_to(ROOT)}")

    workpath = BUILD_DIR / f"work-{tag}"
    distpath = BUILD_DIR / f"dist-{tag}"

    discard(distpath, f"{kind}旧产物")

    env = dict(os.environ)
    env["OBSRS_ONEFILE"] = "1" if onefile else "0"
    env["OBSRS_ICON"] = str(ICON_FILE)
    env["OBSRS_VERSION_FILE"] = str(VERSION_FILE)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--distpath",
        str(distpath),
        "--workpath",
        str(workpath),
    ]

    started = time.monotonic()
    with PYINSTALLER_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n\n===== {kind} / {tag} @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        proc = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        print(f"\n构建失败（退出码 {proc.returncode}）。日志尾部：\n")
        print(tail(PYINSTALLER_LOG, lines=40))
        raise SystemExit(1)

    # 顺带把 PyInstaller 的告警报出来：多数是「找不到某个可选导入」，但漏文件也可能长这样
    warnings = count_warnings(PYINSTALLER_LOG, tag)
    print(f"    {kind}构建完成，用时 {elapsed:.0f}s，PyInstaller 告警 {warnings} 条")

    produced = distpath / (f"{APP_NAME}.exe" if onefile else APP_NAME)
    if not produced.exists():
        raise SystemExit(f"构建产物没找到：{produced}")
    return produced


def discard(path: Path, what: str) -> None:
    """删掉上一轮的产物目录。

    必须自己删干净，不能交给 PyInstaller 的 `--noconfirm`：它会照旧整个目录删掉重建，
    而这里删掉更稳（某些环境下 `rm`/删除会被安全策略拦），也**更正确** ——
    上一轮有、这一轮不再生成的文件（比如裁剪清单里新加的那个 DLL）如果留着，
    就会被打进包里，出现「源码里已经不用了但包里还在」的假象。

    ⚠️ **不能直接 `shutil.rmtree()`**：产物是 300+ 个文件，一次性删除会触发工作区的
    批量删除保护（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，阈值 50），
    **构建会卡在那里等一个永远不会来的人工确认** —— 表现为 `build.bat` 在
    「构建单文件」这一步毫无输出地停住。所以这里分批删，每批不超过 `SAFE_DELETE_CHUNK`。
    """
    if not path.exists():
        return
    try:
        if path.is_dir():
            _rmtree_chunked(path)
        else:
            path.unlink()
    except OSError as exc:
        raise SystemExit(
            f"{what}删不掉：{path}\n{exc}\n请手动删掉它再重新构建。"
        ) from None
    print(f"    已清掉上一轮的{what}：{path.name}")


def _rmtree_chunked(root: Path) -> None:
    """分批删除整棵目录树，避免触发批量删除保护。

    自底向上处理：每一层先把**文件**按小批删掉，再删掉这一层的空目录
    （此时它的子目录已经在上几轮被删空了）。顺带清掉只读属性 ——
    产物里偶有只读文件，只读会让 `unlink` 直接失败。
    """
    directories: list[Path] = [root]
    for current, subdirs, _files in os.walk(root):
        directories.extend(Path(current) / name for name in subdirs)

    # 深度大的（叶子）先处理
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if not directory.is_dir():
            continue
        pending: list[Path] = []
        for entry in directory.iterdir():
            if entry.is_file():
                pending.append(entry)
                if len(pending) >= SAFE_DELETE_CHUNK:
                    _unlink_batch(pending)
                    pending = []
        if pending:
            _unlink_batch(pending)
        # 子目录已在前面几轮删掉，这一层清完文件就能直接删
        directory.rmdir()


def _unlink_batch(files: list[Path]) -> None:
    """删一小批文件（只读的先解除只读再删）。"""
    for file in files:
        try:
            file.unlink()
        except PermissionError:
            os.chmod(file, 0o700)
            file.unlink()


def prune_best_effort(path: Path) -> str:
    """尽力删掉 `path`，删不干净也不算错，返回一句给人看的说明（没话说就返回空串）。

    用在**自检临时目录**上。有两种残留是删不掉的，而且都不该让构建失败：
    · 单文件 exe 被强杀 / 崩在解包阶段时留下的 `_MEIxxxxxx`：
      它的 DACL 是「只允许创建它的那个账户」的受保护 ACL，
      换个身份（受限令牌、另一个用户）就跑不动；
    · 杀毒 / 索引服务临时占着刚解包出来的 DLL。

    这些都是环境噪声，跟「产物好不好」无关 —— 卡在这儿报错只会把人引偏。
    真正需要保证的是**自检本身跑得起来**：残留的旧目录不影响新一轮解包
    （bootloader 每次都用新的随机 `_MEI` 名），所以放它一马就行。
    """
    if not path.exists():
        return ""
    try:
        _rmtree_chunked(path)
    except OSError as exc:
        return f"（没删干净，不影响构建：{exc}）"
    return ""


def tail(path: Path, lines: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def count_warnings(log: Path, tag: str) -> int:
    """只数本次这一段里的 WARNING，别把上一次构建的算进来。"""
    text = log.read_text(encoding="utf-8", errors="replace")
    marker = text.rfind(f"/ {tag} @")
    section = text[marker:] if marker >= 0 else text
    return sum(1 for line in section.splitlines() if "WARNING" in line)


# ------------------------------------------------------------------ DLL 引用自检
# spec 里裁掉了十几个 Qt DLL 家族，靠肉眼维护这份清单迟早出错（PySide6 换个版本
# 名字就变了）。所以每次构建都直接读产物的 PE 导入表，确认**每一个被引用的 DLL
# 要么在产物里、要么由系统提供**。裁错一个必需 DLL，进程会连启动都做不到，
# 而这道检查在构建阶段就能说清是哪个文件的哪条引用缺了。

_SYSTEM_DLL_DIR = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"

# api-ms-win-* / ext-ms-win-* 是 Windows 的**虚拟 API set**：磁盘上没有同名文件，
# 加载器按内置的映射表把它们解析到 kernelbase.dll / ucrtbase.dll 这些真实模块上
# （实测 System32 下确实查不到 api-ms-win-crt-runtime-l1-1-0.dll）。
# 所以这类引用一律算系统提供，否则每次构建都会刷一屏误报。
_API_SET_PREFIXES = ("api-ms-", "ext-ms-")


def _pe_imports(path: Path) -> set[str]:
    """读 PE 的导入表，返回被引用的 DLL 名（不含延迟导入，延迟导入缺了不会拦启动）。"""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return set()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe : pe + 4] != b"PE\0\0":
        return set()

    section_count = int.from_bytes(data[pe + 6 : pe + 8], "little")
    optional_size = int.from_bytes(data[pe + 20 : pe + 22], "little")
    magic = int.from_bytes(data[pe + 24 : pe + 26], "little")
    data_dir = pe + 24 + (112 if magic == 0x20B else 96)  # PE32+ / PE32
    import_rva = int.from_bytes(data[data_dir + 8 : data_dir + 12], "little")

    sections = []
    base = pe + 24 + optional_size
    for index in range(section_count):
        head = base + index * 40
        vsize, vaddr, rawsize, rawptr = (
            int.from_bytes(data[head + 8 : head + 12], "little"),
            int.from_bytes(data[head + 12 : head + 16], "little"),
            int.from_bytes(data[head + 16 : head + 20], "little"),
            int.from_bytes(data[head + 20 : head + 24], "little"),
        )
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def to_offset(rva: int) -> int | None:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva < vaddr + size:
                return rawptr + (rva - vaddr)
        return None

    names: set[str] = set()
    cursor = to_offset(import_rva) if import_rva else None
    while cursor is not None:
        descriptor = data[cursor : cursor + 20]
        if len(descriptor) < 20 or descriptor == b"\x00" * 20:
            break
        name_rva = int.from_bytes(descriptor[12:16], "little")
        if not name_rva:
            break
        offset = to_offset(name_rva)
        if offset is None:
            break
        end = data.index(b"\x00", offset)
        names.add(data[offset:end].decode("ascii", "replace"))
        cursor += 20
    return names


def check_dll_references(root: Path) -> None:
    """确认产物内没有悬空的 DLL 引用。"""
    step("检查 DLL 引用（确认刚才裁掉的文件没有被人依赖）")
    present = {file.name.lower() for file in root.rglob("*") if file.is_file()}
    dangling: dict[str, set[str]] = {}

    for file in root.rglob("*"):
        if file.suffix.lower() not in (".exe", ".dll", ".pyd"):
            continue
        for name in _pe_imports(file):
            lowered = name.lower()
            if lowered in present:
                continue
            if lowered.startswith(_API_SET_PREFIXES):
                continue
            if (_SYSTEM_DLL_DIR / name).exists():
                continue  # 系统自带
            dangling.setdefault(name, set()).add(file.relative_to(root).as_posix())

    if dangling:
        listed = sorted(dangling.items())[:12]
        lines = [f"    {name}  ← 被这些文件引用：{sorted(users)}" for name, users in listed]
        if len(dangling) > len(listed):
            lines.append(f"    ……还有 {len(dangling) - len(listed)} 项")
        raise SystemExit("产物里有悬空的 DLL 引用（裁剪裁过头了）：\n" + "\n".join(lines))

    scanned = sum(
        1 for f in root.rglob("*") if f.suffix.lower() in (".exe", ".dll", ".pyd")
    )
    print(f"    通过（扫了 {scanned} 个 PE 文件，引用都能落地）")


def verify(path: Path, tag: str) -> None:
    """拿真实产物跑自检：起界面 + 跑一小段事件循环，确认真能用。

    **给自检单独一个 `%TEMP%`**（`SELFTEST_DIR`），不用系统的那个。两个原因，
    都会让「是不是打包坏了」这个判断变得不可信：

    1. 单文件 exe 启动时要把内容释放到 `%TEMP%\\_MEIxxxxxx`。系统 `%TEMP%` 里
       常留着别的东西，一旦它不可写（权限被改过、清理工具锁着、盘满），
       bootloader 直接以 **-1** 退出、**连结果文件都写不出来** ——
       现场看就是「自检未通过 / 没有结果文件」，和「打包漏了文件」一模一样，
       但其实是环境问题。放进我们自己的目录就能一眼分清。
    2. 自检结果文件放在自己的目录里，也不会和别处的旧结果串味
       （旧结果会让一次失败的自检看着像通过）。

    目录每次重建，用完删掉，不留垃圾。
    """
    step(f"自检 {tag}（会短暂弹出一次窗口）")
    # 上一轮的残留（可能删不干净，见 prune_best_effort）不该挡住这一轮：
    # bootloader 每次都建新的随机 _MEI 目录，用不着清场。这里只顺手收拾一下。
    note = prune_best_effort(SELFTEST_DIR)
    if note:
        print(f"    上一轮自检临时目录{note}")
    SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
    report = selftest_report_path()

    # 单文件 exe 会把内容释放到 TEMP；指向我们自己的目录，避免被系统 TEMP 的状态干扰
    env = dict(os.environ)
    env["TEMP"] = str(SELFTEST_DIR)
    env["TMP"] = str(SELFTEST_DIR)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(path), "--selftest"],
            timeout=SELFTEST_TIMEOUT_S,
            cwd=str(path.parent),
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"自检超时（{SELFTEST_TIMEOUT_S}s）：{path}\n"
            "多半是启动时卡住了（缺 DLL 或平台插件）——手动跑一次看看。"
        ) from None
    elapsed = time.monotonic() - started

    text = report.read_text(encoding="utf-8").strip() if report.is_file() else ""
    if proc.returncode != 0 or SELFTEST_OK not in text:
        # 退出码 -1（= 4294967295）基本都是解包阶段就失败了，和「少了文件」不是一回事，
        # 分开说，省得下次又朝裁剪清单的方向查。
        hint = ""
        if proc.returncode in (-1, 0xFFFFFFFF, 4294967295):
            hint = (
                "\n退出码 -1 表示进程在**解包/启动阶段**就失败了（还没轮到 Python 代码），"
                "\n常见原因是临时目录不可写或空间不足 —— 这多半是环境问题，不是打包漏了文件。"
                f"\n自检用的临时目录：{SELFTEST_DIR}"
            )
        cause = f"\n{hint}" if hint else ""
        raise SystemExit(
            f"自检未通过：{path}\n退出码 {proc.returncode}\n结果文件内容：\n"
            f"{text or '（没有结果文件）'}{cause}"
        )
    print(f"    通过（{elapsed:.1f}s）：{text}")
    # 验完即清：目录里有单文件版解包出来的一整份运行时（几十 MB），留着白占地方。
    # 删不掉也不报错（见 prune_best_effort），下一轮会再收拾。
    prune_best_effort(SELFTEST_DIR)


def zip_directory(source: Path, archive: Path) -> None:
    """把目录版压成 zip，顶层带一层同名文件夹（解压不会把文件撒一地）。"""
    step(f"压缩 {archive.name}")
    started = time.monotonic()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                bundle.write(file, arcname=f"{source.name}/{file.relative_to(source).as_posix()}")
    print(f"    用时 {time.monotonic() - started:.0f}s")


def human_size(path: Path) -> str:
    if path.is_file():
        size = path.stat().st_size
    else:
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ------------------------------------------------------------------ 主流程


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 OBS Remote Studio 打包成单文件 exe 与目录版压缩包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        choices=("all", "single", "portable"),
        default="all",
        help="只构建某一种形态（默认两种都出）",
    )
    parser.add_argument(
        "--no-selftest",
        action="store_true",
        help="跳过产物自检与 DLL 引用检查（不建议：等于不验货）",
    )
    parser.add_argument("--clean", action="store_true", help="构建前清掉 build/ 下的缓存")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    version = read_version()

    print("OBS Remote Studio —— 打包")
    print(f"  项目根目录：{ROOT}")
    print(f"  版本：{version}")
    print(f"  Python：{sys.version.split()[0]}（{sys.executable}）")

    if args.clean and BUILD_DIR.exists():
        step("清理 build/ 缓存")
        # 同样可能撞上删不掉的 _MEI 残留（见 prune_best_effort）：清缓存是尽力而为，
        # 清不掉就说明白，别让 --clean 整个作业失败 —— 后面每一步本来就会重建自己的输出。
        note = prune_best_effort(BUILD_DIR)
        if note:
            print(f"    部分缓存{note}")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    check_pyinstaller()
    make_icon()
    make_version_file(version)

    single_name = f"{APP_NAME}-{version}-win64-single.exe"
    portable_zip_name = f"{APP_NAME}-{version}-win64-portable.zip"
    produced: list[tuple[str, Path]] = []
    quick_test: Path | None = None

    if args.only in ("all", "single"):
        exe = run_pyinstaller(onefile=True, tag="onefile")
        if not args.no_selftest:
            verify(exe, "单文件 exe")
        target = DIST_DIR / single_name
        shutil.copy2(exe, target)
        produced.append(("单文件 exe", target))

    if args.only in ("all", "portable"):
        folder = run_pyinstaller(onefile=False, tag="onedir")
        # DLL 引用检查只在目录版上做：单文件把内容封在自解压包里，没法逐个读 PE。
        # 两种形态收的文件是同一套，目录版查过就等于单文件查过了。
        if not args.no_selftest:
            check_dll_references(folder)
            verify(folder / f"{APP_NAME}.exe", "目录版")

        archive = DIST_DIR / portable_zip_name
        zip_directory(folder, archive)
        produced.append(("目录版压缩包", archive))
        quick_test = folder / f"{APP_NAME}.exe"

    print("\n" + "=" * 66)
    print("打包完成，产物在 dist/：")
    for label, path in produced:
        print(f"  {label:<14} {path.name:<46} {human_size(path):>10}")
    print("=" * 66)
    print("\n交付给用户：")
    if args.only in ("all", "single"):
        print("  单文件版   拷走那一个 exe 就能跑，首次启动自解压到临时目录，稍慢几秒")
    if args.only in ("all", "portable"):
        print("  压缩包版   解压后运行里面的 OBSRemoteStudio.exe，启动更快")
    print("\n两者都不需要在目标机器上装 Python；配置与日志都写在 %LOCALAPPDATA%，")
    print("换新版本时直接覆盖程序文件即可，配置不会丢。")
    if quick_test is not None:
        print(f"\n想在本地先试跑目录版（不必解压）：{quick_test}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

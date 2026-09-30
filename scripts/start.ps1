<#
.SYNOPSIS
    OBS Remote Studio 一键启动脚本（Windows）。

.DESCRIPTION
    自动完成：定位项目根目录 -> 准备 .venv -> 按需安装依赖 -> 检查 OBS 是否运行
    -> 启动 GUI 并把 stderr 写入 logs\studio-<时间戳>.log。

    双击项目根目录的 start.bat 即可，无需手动敲命令。

.PARAMETER Console
    用 python.exe 前台启动并保留控制台，实时看日志，适合排错。默认用 pythonw.exe
    静默启动，不弹黑色命令行窗口。

.PARAMETER Reinstall
    强制重新安装依赖（pip install -e ".[dev]"）。

.PARAMETER SkipDeps
    跳过依赖检查与安装，直接启动（环境确定没问题时的最快路径）。

.PARAMETER SkipObsCheck
    不检查本机是否已运行 OBS。

.PARAMETER DebugObsWs
    透传 --debug-obsws，打开 obsws-python 的 DEBUG 日志（注意会打印含密码的连接串）。

.PARAMETER SmokeTest
    启动 GUI 前先跑一遍 tests\smoke_test.py。

.EXAMPLE
    .\scripts\start.ps1
    .\scripts\start.ps1 -Console -DebugObsWs
#>
[CmdletBinding()]
param(
    [switch]$Console,
    [switch]$Reinstall,
    [switch]$SkipDeps,
    [switch]$SkipObsCheck,
    [switch]$DebugObsWs,
    [switch]$SmokeTest
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 路径与常量
# start.ps1 位于 <项目根>\scripts\，所以根目录是它的上一级
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$VenvPythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
$SrcDir = Join-Path $Root 'src'
$LogDir = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir ('studio-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$AppModule = 'obs_remote_studio'

Set-Location $Root

function Write-Step { param([string]$Text) Write-Host "[start] $Text" -ForegroundColor Cyan }
function Write-Ok { param([string]$Text) Write-Host "[ ok  ] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "[warn ] $Text" -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host "[fail ] $Text" -ForegroundColor Red }

# ---------------------------------------------------------------- 1. 虚拟环境
if (-not (Test-Path $VenvPython)) {
    Write-Step '未找到 .venv，正在创建虚拟环境...'

    $sysPython = $null
    foreach ($cmd in @('py', 'python')) {
        $found = Get-Command $cmd -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $sysPython = $found.Source; break }
    }
    if (-not $sysPython) {
        Write-Fail '没找到 Python。请先安装 Python >= 3.10 并勾选 "Add to PATH"。'
        exit 1
    }

    if ((Split-Path -Leaf $sysPython) -eq 'py.exe') {
        & $sysPython -3 -m venv (Join-Path $Root '.venv')
    }
    else {
        & $sysPython -m venv (Join-Path $Root '.venv')
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Fail '虚拟环境创建失败。'
        exit 1
    }
    Write-Ok '虚拟环境已创建'
}

# 让源码无需 pip install -e 也能被 import
$env:PYTHONPATH = $SrcDir
$env:PYTHONIOENCODING = 'utf-8'
# 关掉 PySide6 的 WebEngine 沙箱噪音（本项目用不到）
$env:QTWEBENGINE_DISABLE_SANDBOX = '1'

# ---------------------------------------------------------------- 2. 依赖检查
if ($SkipDeps) {
    Write-Step '已指定 -SkipDeps，跳过依赖检查'
}
else {
    $missing = $false
    if ($Reinstall) {
        $missing = $true
    }
    else {
        $probe = @'
import importlib, sys
for name in ("PySide6", "obsws_python", "obs_remote_studio"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        print("MISSING", name, exc)
        sys.exit(1)
print("ALL_OK")
'@
        $probeFile = Join-Path $env:TEMP 'obsrs_dep_probe.py'
        Set-Content -Path $probeFile -Value $probe -Encoding UTF8
        $out = & $VenvPython $probeFile 2>&1
        Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -ne 0) { $missing = $true }
    }

    if ($missing) {
        Write-Step '安装依赖：pip install -e ".[dev]" （首次会比较慢）'
        & $VenvPython -m pip install --upgrade pip
        & $VenvPython -m pip install -e '.[dev]'
        if ($LASTEXITCODE -ne 0) {
            Write-Fail '依赖安装失败，请看上面的 pip 输出。'
            exit 1
        }
        Write-Ok '依赖安装完成'
    }
    else {
        Write-Ok '依赖已就绪'
    }
}

# ---------------------------------------------------------------- 3. 冒烟测试
if ($SmokeTest) {
    Write-Step '运行 smoke_test.py ...'
    & $VenvPython (Join-Path $Root 'tests\smoke_test.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Fail '冒烟测试未通过，已中止启动。'
        exit 1
    }
    Write-Ok '冒烟测试通过'
}

# ---------------------------------------------------------------- 4. OBS 检查
if (-not $SkipObsCheck) {
    $obsProc = Get-Process -Name 'obs64', 'obs32', 'obs' -ErrorAction SilentlyContinue
    if (-not $obsProc) {
        Write-Warn '没有检测到 OBS Studio 进程。'
        Write-Warn '请先在 OBS 里："工具 -> WebSocket 服务器设置" 启用服务器并设置密码，'
        Write-Warn '否则应用能启动但连不上（v5 协议强制鉴权）。'
    }
    else {
        Write-Ok ('检测到 OBS 进程：{0} (PID {1})' -f $obsProc[0].ProcessName, $obsProc[0].Id)
    }
}

# ---------------------------------------------------------------- 5. 启动应用
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$appArgs = @('-m', $AppModule)
$extraArgs = @()
if ($DebugObsWs) { $appArgs += '--debug-obsws'; $extraArgs += '--debug-obsws' }

if ($Console) {
    Write-Step '以控制台模式启动（Ctrl+C 退出）...'
    & $VenvPython @appArgs
    exit $LASTEXITCODE
}

Write-Step '正在启动 OBS Remote Studio ...'

# PowerShell 5.1 的 Start-Process 在使用 -RedirectStandardError 时会重建环境变量字典，
# 一旦环境里同时存在 http_proxy / HTTP_PROXY 这类只差大小写的变量就会直接抛异常。
# 优先用原生重定向，失败了就走 scripts\launcher.py（由 Python 自己把 stderr 写进日志）。
$proc = $null
try {
    $proc = Start-Process -FilePath $VenvPythonW `
        -ArgumentList $appArgs `
        -RedirectStandardError $LogFile `
        -PassThru
}
catch {
    Write-Warn '本机环境变量存在重名项，改用 launcher.py 记录日志'
    $launcher = Join-Path $Root 'scripts\launcher.py'
    $proc = Start-Process -FilePath $VenvPythonW `
        -ArgumentList (@($launcher, $LogFile) + $extraArgs) `
        -PassThru
}

Start-Sleep -Seconds 3

if ($proc.HasExited) {
    Write-Fail ('应用启动后立刻退出（退出码 {0}）。' -f $proc.ExitCode)
    if (Test-Path $LogFile) {
        Write-Host "---- $LogFile ----" -ForegroundColor DarkGray
        Get-Content $LogFile -Tail 30
        Write-Host '------------------' -ForegroundColor DarkGray
    }
    Write-Host '排错建议：用 .\scripts\start.ps1 -Console 前台运行看完整报错。' -ForegroundColor Yellow
    exit 1
}

Write-Ok ('已启动，PID {0}' -f $proc.Id)
Write-Host "      运行日志：$LogFile" -ForegroundColor DarkGray
Write-Host '      首次使用请在菜单「文件 -> 连接设置」填写地址/端口/密码。' -ForegroundColor DarkGray
exit 0

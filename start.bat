@echo off
REM ============================================================
REM  OBS Remote Studio 一键启动器
REM  双击本文件即可启动；逻辑全部在 scripts\start.ps1
REM  额外参数会原样透传，例如：
REM      start.bat -Console        保留控制台窗口，实时看日志（排错用）
REM      start.bat -Reinstall      重装依赖
REM      start.bat -SmokeTest      启动前先跑冒烟测试
REM ============================================================
setlocal
chcp 65001 >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*

if errorlevel 1 (
    echo.
    echo 启动失败，请看上面的错误信息。
    pause
)
endlocal

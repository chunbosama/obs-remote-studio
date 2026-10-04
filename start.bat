@echo off
REM ============================================================
REM  OBS Remote Studio - one-click launcher
REM
REM  Double-click this file to start the app. All the real logic
REM  lives in scripts\start.ps1. Extra arguments are passed through:
REM      start.bat -Console      keep the console, watch logs live
REM      start.bat -Reinstall    reinstall dependencies
REM      start.bat -SmokeTest    run the smoke test before starting
REM
REM  NOTE: keep this file ASCII-only.
REM  cmd.exe parses .bat files using the console codepage (936 here), so
REM  UTF-8 Chinese text is decoded as GBK: each 3-byte character leaves a
REM  stray byte that pairs up with the next byte (often the newline), which
REM  splits and merges lines. The result is that otherwise-fine lines get
REM  chopped up and executed as garbage commands. chcp cannot fix this
REM  because cmd has already been reading the file with the old codepage.
REM  All user-facing messages (Chinese) belong in scripts\start.ps1,
REM  which PowerShell reads as UTF-8.
REM ============================================================
setlocal
chcp 65001 >nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*

if errorlevel 1 (
    echo.
    echo [start] FAILED - see the error message above.
    pause
)
endlocal

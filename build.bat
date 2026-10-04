@echo off
REM ============================================================
REM  OBS Remote Studio - build script (Windows)
REM
REM  Double-click to: make icon -> build single-file exe ->
REM  build portable folder -> verify -> zip.  Takes ~2-4 min.
REM  Output goes to the dist\ folder.
REM
REM  Optional arguments (pass on the command line):
REM      build.bat --only single     build only the single-file exe
REM      build.bat --only portable   build only the portable zip
REM      build.bat --clean           clear build\ cache first
REM      build.bat --no-selftest     skip product verification
REM
REM  NOTE: keep this file ASCII-only. cmd.exe parses .bat files using the
REM  console codepage, so UTF-8 non-ASCII text shifts the byte stream and
REM  turns later lines into garbage commands. All user-facing messages
REM  (Chinese) live in scripts\packaging\build.py instead.
REM ============================================================
setlocal

REM Prefer the project venv: PyInstaller and PySide6 live there.
REM Keep parentheses out of these echo lines - an unescaped ")" closes the
REM "if not exist (" block early and cmd then chokes on the rest of the line.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [build] .venv not found:
    echo [build]   %PY%
    echo [build] Prepare the virtualenv first, see README, or run start.bat once.
    pause
    exit /b 1
)

"%PY%" "%~dp0scripts\packaging\build.py" %*
if errorlevel 1 (
    echo.
    echo [build] FAILED - see the error message above.
    pause
)
endlocal

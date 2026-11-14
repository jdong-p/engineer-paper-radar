@echo off
rem Engineer Paper Radar: double-click to open the Setup & Control Center (STARTUP.html) with working buttons.
rem It runs "python briefing.py --gui": a small server on http://127.0.0.1 (this computer only) that opens
rem your browser. Keep this window open while you use the page; close it (or Ctrl+C) to stop.
rem It first looks for Python 3.8 or newer (py -3, then python, then python3) and explains the fix if there is none.
setlocal
cd /d "%~dp0"
set "PY="
set "FOUND="
for %%c in ("py -3" "python" "python3") do call :try %%~c
if not defined PY goto too_old
%PY% briefing.py --gui
if not errorlevel 1 goto :eof
echo.
echo The Control Center stopped with an error (see the lines above).
pause
goto :eof

rem :try CMD...  sets PY to the first command that is Python 3.8+, and FOUND to the first Python seen.
rem The check below runs under any Python (2 or 3) and has no quotes in it, so cmd can't misread it.
:try
if defined PY exit /b 0
set "HV="
set "VER="
for /f "usebackq tokens=1,2" %%a in (`%* -c "import sys; sys.stdout.write(str(sys.hexversion) + chr(32) + sys.version.split()[0])" 2^>nul`) do (
    set "HV=%%a"
    set "VER=%%b"
)
if not defined HV exit /b 0
echo %HV%| findstr /r "^[0-9][0-9]*$" >nul || exit /b 0
if not defined FOUND set "FOUND=Python %VER% (%*)"
rem 50855936 = 0x03080000, Python 3.8.0
if %HV% GEQ 50855936 set "PY=%*"
exit /b 0

:too_old
set "CP="
for /f "tokens=2 delims=:" %%p in ('chcp') do set /a CP=%%p 2>nul
chcp 65001 >nul
if not defined FOUND set "FOUND=no Python found / 未找到 Python"
echo.
echo   Engineer Paper Radar needs Python 3.8 or newer. Found: %FOUND%
echo   Engineer Paper Radar 需要 Python 3.8 或更新版本。当前：%FOUND%
echo.
echo   Install a current Python with one line (in this or a new terminal) / 一行命令安装新版 Python：
echo.
echo       winget install Python.Python.3.12
echo.
echo   Or download it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
echo   或从 python.org 下载安装（勾选 "Add python.exe to PATH"）。
echo   Then double-click this file again. / 然后重新双击本文件。
echo.
pause
if defined CP chcp %CP% >nul
exit /b 1

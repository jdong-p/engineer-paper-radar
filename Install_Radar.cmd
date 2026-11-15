@echo off
rem Engineer Paper Radar: double-click to install what is missing (Git, Python 3.8+ via winget), update this
rem folder (git pull) and open the setup (install.py --setup), which ends in STARTUP.html.
rem The same as the one-line installer:  irm https://raw.githubusercontent.com/jdong-p/engineer-paper-radar/main/install.ps1 | iex
rem -ExecutionPolicy Bypass applies to this one script only; nothing on the computer is changed.
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
    echo.
    echo The installer stopped with an error, see the lines above.
    pause
)

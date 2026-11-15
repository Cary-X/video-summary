@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-raccoon.ps1" -NoBrowser
if errorlevel 1 goto failed
"runtime\python\python.exe" -c "import tkinterdnd2"
if errorlevel 1 goto failed
start "" "runtime\python\pythonw.exe" "%~dp0desktop_widget.py"
exit /b
:failed
echo Raccoon Drop requires the bundled runtime\python folder.
pause

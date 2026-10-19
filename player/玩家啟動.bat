@echo off
setlocal
cd /d "%~dp0"
rem Launch the GUI launcher with the bundled portable Python (no console).
if exist "%~dp0python\pythonw.exe" (
  start "" "%~dp0python\pythonw.exe" "%~dp0玩家啟動器.py"
  exit /b
)
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0玩家啟動器.py"
  exit /b
)
echo [ERROR] Python not found.
echo Put the "python" folder (from the pack) next to this file, or install Python.
echo.
pause

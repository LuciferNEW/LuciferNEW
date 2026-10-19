@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0python\pythonw.exe" (
  start "" "%~dp0python\pythonw.exe" "%~dp0SF2.py"
  exit /b
)
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw "%~dp0SF2.py"
  exit /b
)
echo [ERROR] Python not found. Put the "python" folder next to this file.
pause

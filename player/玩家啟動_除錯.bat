@echo off
cd /d "%~dp0"
echo === SF2 Launcher - DEBUG ===
echo.
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" "%~dp0SF2.py"
  goto done
)
where python >nul 2>&1
if %errorlevel%==0 (
  python "%~dp0SF2.py"
  goto done
)
echo [ERROR] No Python found. Put the "python" folder next to this .bat.
:done
echo.
echo === done. If a red Traceback shows above, screenshot it. ===
pause

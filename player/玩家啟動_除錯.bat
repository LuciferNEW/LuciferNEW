@echo off
cd /d "%~dp0"
echo === SF2 Launcher - DEBUG (errors will show below) ===
echo.
if exist "%~dp0python\python.exe" (
  echo [OK] found bundled python
  "%~dp0python\python.exe" "%~dp0玩家啟動器.py"
  goto done
)
where python >nul 2>&1
if %errorlevel%==0 (
  echo [OK] found system python
  python "%~dp0玩家啟動器.py"
  goto done
)
echo [ERROR] No Python found.
echo The "python" folder must sit NEXT TO this .bat (copy it from the pack),
echo or install Python from python.org.
:done
echo.
echo === done. If you see a red Traceback above, screenshot the whole thing. ===
pause

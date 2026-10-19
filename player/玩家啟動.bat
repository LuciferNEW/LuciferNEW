@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 用整合包內的免安裝 Python 執行啟動器（玩家不用另外裝 Python）
if exist "%~dp0python\pythonw.exe" (
  start "" "%~dp0python\pythonw.exe" "%~dp0玩家啟動器.py"
  exit /b
)
where pythonw >nul 2>&1
if %errorlevel%==0 ( start "" pythonw "%~dp0玩家啟動器.py" & exit /b )
echo 找不到 Python。請確認「python」資料夾和本檔在同一層。
pause

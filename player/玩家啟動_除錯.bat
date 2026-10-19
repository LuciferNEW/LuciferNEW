@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === SF2 啟動器 · 除錯模式（會顯示錯誤訊息）===
echo.
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" "%~dp0玩家啟動器.py"
) else (
  where python >nul 2>&1 && ( python "%~dp0玩家啟動器.py" ) || ( echo [錯誤] 找不到 Python。請把整合包的 python 資料夾放到本檔旁邊，或在電腦裝 Python。 )
)
echo.
echo === 已結束。若上面出現紅字 Traceback 錯誤，整段截圖給我 ===
pause

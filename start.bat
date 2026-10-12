@echo off
rem Local Board launcher. Keep ASCII only: Cyrillic text + chcp breaks cmd.exe parsing.
cd /d "%~dp0"
echo.
echo  Local Board - http://localhost:8090
echo  Keep this window open while using the board.
echo  Press Ctrl+C (or close the window) to stop the server.
echo.
where python >nul 2>nul && (python serve.py) || (py serve.py)
echo.
echo  Server stopped.
pause

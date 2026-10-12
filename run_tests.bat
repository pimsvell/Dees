@echo off
rem Automated tests launcher. Keep ASCII only (see start.bat note).
cd /d "%~dp0"
echo Running board tests (headless Edge)...
echo.
where python >nul 2>nul && (python tests\test_app.py) || (py tests\test_app.py)
echo.
pause

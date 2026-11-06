@echo off
rem Double-click to start Plane Watch in the background. The dashboard opens in your browser.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python planewatch.py --background %*
) else (
  py -3 planewatch.py --background %*
)
if errorlevel 1 pause

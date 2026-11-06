@echo off
rem Double-click to stop a Plane Watch that is running in the background.
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python planewatch.py --stop
) else (
  py -3 planewatch.py --stop
)
timeout /t 3 >nul

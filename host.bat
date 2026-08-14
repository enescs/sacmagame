@echo off
REM Host a game. Everyone else runs play.bat and picks it from the list.
REM Windows Firewall will ask on the first run - allow it on private networks.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :nosetup
".venv\Scripts\python.exe" -m sacma.server %*
exit /b %errorlevel%

:nosetup
echo.
echo No .venv found - run setup.bat first.
echo.
pause
exit /b 1

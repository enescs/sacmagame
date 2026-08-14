@echo off
REM Join a game. With no arguments it scans the LAN and shows what it finds.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :nosetup
".venv\Scripts\python.exe" -m sacma.client %*

REM Keep the console open if it fell over, so the error is readable when this
REM was launched by double-clicking rather than from a prompt.
if errorlevel 1 (
    echo.
    echo The game exited with an error - see above.
    echo.
    pause
)
exit /b %errorlevel%

:nosetup
echo.
echo No .venv found - run setup.bat first.
echo.
pause
exit /b 1

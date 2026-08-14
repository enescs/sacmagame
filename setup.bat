@echo off
REM One-time setup: build a local venv and install pygame into it.
setlocal
cd /d "%~dp0"

REM Prefer the py launcher; fall back to python on PATH. Bare "python" is often
REM the Microsoft Store stub, which fails in a way that is hard to read.
set "PY=py -3"
%PY% --version >nul 2>nul || set "PY=python"
%PY% --version >nul 2>nul || goto :nopython

echo Creating .venv ...
%PY% -m venv .venv || goto :fail

echo Installing pygame ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail

echo.
echo done.
echo.
echo   play:  play.bat
echo   host:  host.bat
echo.
pause
exit /b 0

:nopython
echo.
echo Python was not found. Install Python 3.10 or newer from python.org and
echo tick "Add python.exe to PATH" during setup, then run this again.
echo.
pause
exit /b 1

:fail
echo.
echo Setup failed - see the error above.
echo.
pause
exit /b 1

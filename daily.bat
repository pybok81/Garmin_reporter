@echo off
rem Double-click to: collect Garmin data -> build report -> open it in the browser.
rem Optional: run "daily.bat 2026-10-20" from cmd to do a past date.
setlocal

rem === Edit this line if your virtual environment is somewhere else (use an ASCII-only path) ===
set "VENV=C:\venvs\garmin"

chcp 65001 >nul
cd /d "%~dp0"

if not exist "%VENV%\Scripts\activate.bat" (
  echo Virtual environment not found: %VENV%
  echo Edit the VENV line in daily.bat.
  pause
  exit /b 1
)
call "%VENV%\Scripts\activate.bat"

set "DATEARG="
if not "%~1"=="" set "DATEARG=--date %~1"

python garmin.py sync %DATEARG%
if errorlevel 1 (
  echo.
  echo Sync failed. See the message above.
  pause
  exit /b 1
)

python garmin.py view %DATEARG%
echo.
pause

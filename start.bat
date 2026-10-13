@echo off
rem Double-click to start Playtime (server, plus a quick tunnel if you have no domain yet).
rem Close this window or press Ctrl+C to stop.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

if not exist ".env" (
  copy .env.example .env >nul
  echo Created .env - fill in your CHIP keys, save it, then start this again.
  notepad .env
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run.py %*
pause
exit /b 0

:error
echo.
echo Setup failed - see the messages above. Is Python 3.11+ installed?
pause
exit /b 1

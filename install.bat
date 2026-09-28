@echo off
REM One-shot installer for Windows: venv + dependencies + browser, then the setup wizard.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist .venv ( %PY% -m venv .venv || goto :err )
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\pip install -r requirements.txt -q || goto :err
.venv\Scripts\python -m playwright install chromium || goto :err
echo.
echo Installed. Launching setup...
.venv\Scripts\python run.py --setup
goto :eof
:err
echo Installation failed. Make sure Python 3.9+ is installed and on PATH.
exit /b 1

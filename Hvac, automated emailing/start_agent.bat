@echo off
setlocal

REM Always run from project root beside config and script files.
cd /d "%~dp0"

if not exist "config.json" (
  echo config.json not found in %cd%
  exit /b 1
)

REM Start continuous background polling mode.
echo Starting HVAC email agent in daemon mode...
where python >nul 2>nul
if %errorlevel%==0 (
  python hvac_email_agent.py --config config.json --daemon
) else (
  REM Fallback for Windows launcher environments where `py` is available instead.
  py -3 hvac_email_agent.py --config config.json --daemon
)

endlocal

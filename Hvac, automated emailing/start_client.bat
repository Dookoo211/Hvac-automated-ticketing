@echo off
setlocal
cd /d "%~dp0"

if exist hvac_email_agent.exe (
  hvac_email_agent.exe --setup-and-ui
) else (
  python hvac_email_agent.py --setup-and-ui
)

endlocal

@echo off
setlocal

REM Build a single-file executable for non-Python client machines.
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo Failed to install or update PyInstaller.
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --name hvac_email_agent --add-data "config.json;." --add-data "valid_licenses.json;." hvac_email_agent.py
if errorlevel 1 (
  echo CLI build failed.
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --windowed --onefile --name hvac_email_agent_ui --add-data "config.json;." --add-data "valid_licenses.json;." hvac_agent_ui.py
if errorlevel 1 (
  echo UI build failed.
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --windowed --onefile --name hvac_installer hvac_installer.py
if errorlevel 1 (
  echo Installer build failed.
  exit /b 1
)

if not exist release mkdir release
copy /Y README.md release\ >nul
copy /Y dist\hvac_installer.exe release\ >nul
copy /Y reset_install.bat release\ >nul

echo Build complete. Release folder:
echo   %cd%\release

endlocal

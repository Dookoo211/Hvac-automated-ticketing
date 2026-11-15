@echo off
setlocal

REM Build single-file executables for non-Python client machines.
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller -r requirements.txt
if errorlevel 1 (
  echo Failed to install PyInstaller / requirements.
  exit /b 1
)

REM WHY no --add-data config.json any more (AUDIT C5): it embedded the
REM developer's live mailbox settings (and, on old configs, the password)
REM inside every EXE. Each install creates its own config.json in setup.
REM keyring backends and tzdata are loaded dynamically, so collect them explicitly.
set COMMON=--noconfirm --clean --onefile --collect-submodules keyring.backends --collect-data tzdata

python -m PyInstaller %COMMON% --name hvac_email_agent hvac_email_agent.py
if errorlevel 1 (
  echo CLI build failed.
  exit /b 1
)

python -m PyInstaller %COMMON% --windowed --name hvac_email_agent_ui hvac_agent_ui.py
if errorlevel 1 (
  echo UI build failed.
  exit /b 1
)

python -m PyInstaller %COMMON% --windowed --name hvac_installer hvac_installer.py
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

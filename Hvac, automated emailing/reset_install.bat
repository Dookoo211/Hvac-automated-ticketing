@echo off
setlocal

set BASE=%~dp0
echo This will reset the HVAC agent install and remove local data.
echo.
choice /M "Are you sure you want to continue"
if errorlevel 2 (
  echo Cancelled.
  exit /b 0
)

powershell -Command "Remove-Item -Recurse -Force \"%BASE%data\",\"%BASE%logs\" -ErrorAction SilentlyContinue"
del /f /q "%BASE%config.json" >nul 2>&1

echo Reset complete. Launch the app to run setup again.
endlocal

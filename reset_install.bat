@echo off
setlocal

REM Resets the install (data, logs, config). WHY the backup step (AUDIT H7):
REM this used to delete everything permanently, including the dedup and
REM cooldown history that prevents duplicate customer emails.
set BASE=%~dp0
echo This will reset the HVAC agent install and remove local data.
echo A backup copy is saved to the backups folder first.
echo Stop the agent (UI Stop button / background task) before continuing.
echo.
choice /M "Are you sure you want to continue"
if errorlevel 2 (
  echo Cancelled.
  exit /b 0
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set TS=%%i
set DEST=%BASE%backups\reset-%TS%
mkdir "%DEST%" 2>nul
if not exist "%DEST%" (
  echo Could not create the backup folder. Nothing was deleted.
  exit /b 1
)

if exist "%BASE%data" (
  robocopy "%BASE%data" "%DEST%\data" /E /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 (
    echo Backup of data failed. Nothing was deleted.
    exit /b 1
  )
)
if exist "%BASE%logs" (
  robocopy "%BASE%logs" "%DEST%\logs" /E /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 (
    echo Backup of logs failed. Nothing was deleted.
    exit /b 1
  )
)
if exist "%BASE%config.json" (
  copy /Y "%BASE%config.json" "%DEST%\config.json" >nul
  if errorlevel 1 (
    echo Backup of config.json failed. Nothing was deleted.
    exit /b 1
  )
)

powershell -NoProfile -Command "Remove-Item -Recurse -Force \"%BASE%data\",\"%BASE%logs\" -ErrorAction SilentlyContinue"
del /f /q "%BASE%config.json" >nul 2>&1

echo Reset complete. Backup saved to:
echo   %DEST%
echo The mailbox password stays in Windows Credential Manager (entry "HVACEmailAgent")
echo and is replaced the next time setup is run.
endlocal

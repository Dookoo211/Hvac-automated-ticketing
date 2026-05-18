@echo off
setlocal
cd /d "%~dp0"

if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist
if exist release rmdir /S /Q release
if exist __pycache__ rmdir /S /Q __pycache__

echo Clean complete.
endlocal

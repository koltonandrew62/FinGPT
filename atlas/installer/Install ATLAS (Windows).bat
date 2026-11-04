@echo off
rem Double-click this file to install ATLAS with a Desktop shortcut.
rem It runs install-windows.ps1 from this folder (no admin rights needed).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
if errorlevel 1 (
  echo.
  echo Installation did not finish. Read the message above, fix it, and run this file again.
)
echo.
pause

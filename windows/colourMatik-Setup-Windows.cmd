@echo off
rem =====================================================================
rem  colourMatik - Setup for Windows 10/11 (x64). Double-click me.
rem  Fetches and launches the real colourMatik installer (a proper GUI
rem  Setup window: icon, progress bar, one click). Kept at THIS exact
rem  filename so the website download link never has to change.
rem  by Sevki Bugra Ozbek - catheadai.com
rem =====================================================================
title colourMatik Setup
echo.
echo   +----------------------------------------------+
echo   ^|   colourMatik  -  Setup ^(Windows^)            ^|
echo   ^|   getting the installer...                   ^|
echo   +----------------------------------------------+
echo.

set "DEST=%TEMP%\colourMatik-Setup.exe"
set "URL=https://github.com/iyiailerobotu-sudo/colourmatik-releases/releases/download/windows-latest/colourMatik-windows-setup.exe"

rem Download the NSIS installer and launch it. Setup.exe carries a
rem requireAdministrator manifest, so Windows shows its own UAC prompt and the
rem GUI installer takes over from there (installs Python/ffmpeg/engine/AI/panel/
rem effect and sets autostart). TLS 1.2 is forced for older Windows PowerShell.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "  Write-Host '  Downloading the installer...';" ^
  "  Invoke-WebRequest '%URL%' -OutFile '%DEST%' -UseBasicParsing;" ^
  "  Write-Host '  Opening the installer...';" ^
  "  Start-Process '%DEST%';" ^
  "  exit 0" ^
  "} catch { Write-Host ('  ERROR: ' + $_.Exception.Message); exit 1 }"

if errorlevel 1 (
  echo.
  echo   Couldn't fetch the installer. Check your internet connection and try
  echo   again, or download it directly from:
  echo     github.com/iyiailerobotu-sudo/colourmatik-releases/releases
  echo   ^(file: colourMatik-windows-setup.exe^)
  echo.
  pause
  exit /b 1
)

echo.
echo   The colourMatik installer is opening in its own window.
echo   Approve the Windows prompt, then follow it. You can close THIS window.
echo.
timeout /t 5 >nul

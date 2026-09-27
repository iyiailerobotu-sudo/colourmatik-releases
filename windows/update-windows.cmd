@echo off
rem colourMatik - update to the latest version (Windows). Double-click me.
rem /silent: the panel's Update button - no window, no pause, output to update.log.
rem Mind the size of everything above the download line: see "Landing strip".
rem Self-elevates: the panel agent and the effect (Program Files) need admin.
net session >nul 2>&1
if errorlevel 1 if not "%~2"=="/elevated" (
  echo ==^> Requesting administrator rights...
  if "%~1"=="/silent" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '/silent','/elevated' -Verb RunAs -WindowStyle Hidden"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  )
  if errorlevel 1 goto :no_admin
  exit /b
)
rem Re-exec from a copy: the download overwrites this very file, and cmd.exe
rem keeps reading the script by BYTE OFFSET. Decided by the name (see Notes).
if /i not "%~nx0"=="cmk-update-run.cmd" (
  copy /y "%~f0" "%TEMP%\cmk-update-run.cmd" >nul 2>&1
  set "CMK_HOME=%~dp0"
  call "%TEMP%\cmk-update-run.cmd" %*
  exit /b
)
cd /d "%CMK_HOME%.."
set "CMKPROG=%APPDATA%\colourMatik\update_progress"
set "CMKLOG=%APPDATA%\colourMatik\update.log"
if not exist "%APPDATA%\colourMatik" mkdir "%APPDATA%\colourMatik" >nul 2>&1
rem The elevated copy appends to update.log itself (see Notes).
if "%~2"=="/elevated" if not defined CMK_LOGGING (
  set "CMK_LOGGING=1"
  set "CMK_RAN="
  for /l %%i in (1,1,20) do 2>nul (>>"%CMKLOG%" (call )) || >nul ping -n 2 127.0.0.1
  call "%~f0" %* >> "%CMKLOG%" 2>&1
  if not defined CMK_RAN call "%~f0" %*
  exit /b
)
set "CMK_RAN=1"
<nul set /p "=5|Downloading the newest colourMatik" > "%CMKPROG%" 2>nul
echo ==^> Updating colourMatik...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CMK_HOME%fetch-latest.ps1" -Dest "%CD%"
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
rem Landing strip - keep it directly under the download line. An updater from
rem 1.8.2 or older can be running THIS file in place (its engine inherited
rem CMK_RELAUNCHED=1, so it skipped the copy): once the download replaces the
rem file, cmd resumes reading at its OLD byte offset. Every such offset (1.8.1
rem or 1.8.2, LF or CRLF) falls in the colons above - a label, skipped - so it
rem carries on right here with the download's errorlevel intact.
rem tests/run_tests.py checks the offsets whenever the lines above change.
rem
rem Notes:
rem - Self-elevation: without admin the panel and effect steps quietly fell
rem   over and the machine looked like it "never got the update". A declined
rem   admin prompt ends at :no_admin, at the bottom of this file.
rem - update_progress (CMKPROG) is what the panel's bar polls, through the
rem   engine: "pct|message" moves the bar, "FAIL|reason" stops it with that
rem   reason.
rem - The TEMP copy is chosen by file name, never by a variable: variables
rem   leak into the engine this script restarts, and from there into that
rem   engine's next update.
rem - Output: the engine starts this script with update.log as its output, in
rem   a console without a window, so every step logs there - PowerShell too.
rem   The elevated copy cannot inherit that, so /elevated makes it append to
rem   update.log itself. cmd's ">>" wants the file to itself, and the window
rem   that asked for admin rights holds it a moment longer: wait up to ~20 s,
rem   then run unlogged rather than not at all. /elevated never asks again: a
rem   PC that cannot elevate updates what it can instead of relaunching forever.
rem - fetch-latest.ps1 downloads the newest Windows Setup (one file, through
rem   the GitHub API) and unpacks its program over this folder. Never a git
rem   pull or a source zip, so every install updates the same way. It lives in
rem   its own .ps1 so no PowerShell parentheses are ever echoed inside a batch
rem   block. On failure stop HERE: running the steps below over the old code
rem   filled the bar while the version never moved.
if errorlevel 1 (
  <nul set /p "=FAIL|Could not download the update - check the internet connection and try again" > "%CMKPROG%" 2>nul
  echo ==^> Download failed.
  if not "%~1"=="/silent" pause
  exit /b 1
)

<nul set /p "=25|Refreshing the engine" > "%CMKPROG%" 2>nul
echo ==^> Refreshing engine + AI...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CMK_HOME%setup.ps1"
<nul set /p "=75|Reinstalling the panel" > "%CMKPROG%" 2>nul
echo ==^> Reinstalling panel + effect...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CMK_HOME%install-panel.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%CMK_HOME%install-effect.ps1"
rem The AE panel is per-user and needs no admin. install-effect.ps1 can die on a
rem locked colourMatik.aex while Premiere is open and never reach its CEP step,
rem so refresh it here directly — same guarantee the macOS updater added.
if exist "colourmatik-cep" (
  if not exist "%APPDATA%\Adobe\CEP\extensions\com.catheadai.colourmatik" mkdir "%APPDATA%\Adobe\CEP\extensions\com.catheadai.colourmatik" >nul 2>&1
  xcopy /e /y /i /q "colourmatik-cep\*" "%APPDATA%\Adobe\CEP\extensions\com.catheadai.colourmatik\" >nul
)
<nul set /p "=92|Restarting the engine" > "%CMKPROG%" 2>nul
echo ==^> Restarting the engine...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'colourmatik.webapp' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
rem The new engine inherits this environment: hand it none of our CMK*
rem variables, or its next update takes them for its own (a leaked
rem CMK_RELAUNCHED=1 is what made updates run this file in place).
setlocal
(for /f "delims==" %%v in ('set CMK 2^>nul') do set "%%v=") & start "" wscript "%CMK_HOME%engine-hidden.vbs"
endlocal
<nul set /p "=100|Done" > "%CMKPROG%" 2>nul
echo ==^> Updated. Restart Premiere Pro.
if not "%~1"=="/silent" pause
exit /b

:no_admin
rem The admin prompt was declined (or could not be shown), so nothing ran. Tell
rem the panel at once: its bar used to wait 15 minutes for an update that never
rem started, then say "timed out". After a FAIL the engine lets the panel's
rem retry start a new update. CMKPROG is not set this early, hence the path.
if "%~1"=="/silent" <nul set /p "=FAIL|Windows admin approval was not given, so nothing was updated. Click retry and choose Yes." > "%APPDATA%\colourMatik\update_progress" 2>nul
echo ==^> Windows admin approval was not given, so nothing was updated.
if not "%~1"=="/silent" pause
exit /b 1

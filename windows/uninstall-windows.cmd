@echo off
rem colourMatik - uninstaller (Windows). Removes the panel, effect, autostart and engine.
echo ==^> Stopping the engine...
rem keeper.stop: the keeper (engine-hidden.vbs) must not start it again.
if exist "%APPDATA%\colourMatik" type nul > "%APPDATA%\colourMatik\keeper.stop" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'colourmatik.webapp' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" 2>nul
rem ...and let it leave (it holds a file in the support folder removed below)
powershell -NoProfile -Command "for ($i = 0; $i -lt 20; $i++) { if (-not (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'wscript.exe' -and $_.CommandLine -match 'engine-hidden' })) { break }; Start-Sleep 1 }" 2>nul
echo ==^> Removing autostart...
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\colourMatik Engine.lnk" 2>nul
echo ==^> Removing the Premiere panel...
rem Remove via Adobe's plugin agent first (that is how it was installed), then
rem clear any developer-mode copy left in Plugins\External.
for %%R in ("%ProgramFiles%" "%ProgramFiles(x86)%") do (
  if exist "%%~R\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" (
    "%%~R\Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe" /remove com.colourmatik.panel >nul 2>&1
  )
)
for /d %%D in ("%APPDATA%\Adobe\UXP\Plugins\External\com.colourmatik.panel_*") do rmdir /s /q "%%D" 2>nul
powershell -NoProfile -Command "$r=Join-Path $env:APPDATA 'Adobe\UXP\PluginsInfo\v1\premierepro.json'; if(Test-Path $r){$j=Get-Content $r -Raw|ConvertFrom-Json; $j.plugins=@($j.plugins|Where-Object{$_.pluginId -ne 'com.colourmatik.panel'}); ($j|ConvertTo-Json -Depth 8 -Compress)|Set-Content -Encoding UTF8 $r}"
echo ==^> Removing the native effect + After Effects copies (needs admin)...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0uninstall-effect-admin.ps1'"
echo ==^> Removing the After Effects CEP panel...
rmdir /s /q "%APPDATA%\Adobe\CEP\extensions\com.catheadai.colourmatik" 2>nul
echo ==^> Removing the support folder...
rmdir /s /q "%APPDATA%\colourMatik" 2>nul
echo Done. Restart Premiere Pro to finish. (The colourMatik app folder was left in place.)
pause

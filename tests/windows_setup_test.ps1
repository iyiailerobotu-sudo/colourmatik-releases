# Install the built Windows Setup silently and check the offline engine runtime:
# the engine starts on the bundled Python with nothing downloaded, an update with
# the same runtime leaves it alone, and a changed runtime is staged while the
# engine runs and put in place after the updater stops it.
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows_setup_test.ps1 -Setup <colourMatik-windows-setup.exe>
# Installs into the real user profile and uses port 8765: a clean machine only
# (.github/workflows/windows-setup.yml runs it on a fresh runner, by hand).
param([Parameter(Mandatory = $true)][string]$Setup)
$ErrorActionPreference = "Stop"
$script:fails = 0
function Check($name, $ok, $detail = "") {
    if ($ok) { Write-Host "  [PASS] $name   $detail" } else { Write-Host "  [FAIL] $name   $detail"; $script:fails++ }
}
function Answers { try { (Invoke-RestMethod "http://127.0.0.1:8765/version" -TimeoutSec 2).version } catch { $null } }
function WaitAnswer($sec) { for ($i = 0; $i -lt $sec; $i++) { $v = Answers; if ($v) { return $v }; Start-Sleep 1 }; return $null }
function Keepers { @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "wscript.exe" -and $_.CommandLine -match "engine-hidden" }) }
function StopEngine {
    $d = Join-Path $env:APPDATA "colourMatik"; New-Item -ItemType Directory -Force -Path $d | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $d "keeper.stop") | Out-Null
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "colourmatik.webapp" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
}

Write-Host "=== Windows Setup with the bundled engine runtime ==="
$Setup = (Resolve-Path $Setup).Path
Write-Host ("Setup: {0:N1} MB" -f ((Get-Item $Setup).Length / 1MB))
if (Answers) { throw "an engine already answers on 127.0.0.1:8765 - run this on a clean machine" }
$t0 = Get-Date
# Not -Wait: that waits for every process Setup starts, and Setup starts the
# engine's keeper, which runs for as long as the user is logged in.
$p = Start-Process -FilePath $Setup -ArgumentList "/S" -PassThru
$p.WaitForExit()
Write-Host ("Setup exit code {0}, {1:N0} s" -f $p.ExitCode, ((Get-Date) - $t0).TotalSeconds)
$inst = Join-Path $env:USERPROFILE "colourMatik"
$want = ((Get-Content (Join-Path $inst "version.json") -Raw | ConvertFrom-Json).version)
$rtId = ((Get-Content (Join-Path $inst "runtime\RUNTIME_ID") -Raw -ErrorAction SilentlyContinue) + "").Trim()
$venvId = ((Get-Content (Join-Path $inst ".venv\RUNTIME_ID") -Raw -ErrorAction SilentlyContinue) + "").Trim()
Check "the engine's Python is installed from Setup" (Test-Path (Join-Path $inst "runtime\python.exe")) "($rtId)"
Check ".venv sits on the bundled runtime" ($rtId -and $venvId -eq $rtId) "(venv $venvId)"
$cfg = Get-Content (Join-Path $inst ".venv\pyvenv.cfg") -Raw -ErrorAction SilentlyContinue
Check "pyvenv.cfg points at the runtime" ($cfg -match [regex]::Escape((Join-Path $inst "runtime"))) ""
$v = WaitAnswer 150
Check "the engine starts after a silent install" ($v -eq $want) "(version $v, expected $want)"
Check "one keeper runs it" (@(Keepers).Count -eq 1) "($(@(Keepers).Count) keepers)"

# an update with the same runtime: the program is refreshed, the runtime left alone
$marker = Join-Path $inst "runtime\keep.marker"; Set-Content $marker "x"
& (Join-Path $inst "windows\fetch-latest.ps1") -Dest $inst -SetupExe $Setup
Check "an update with an unchanged runtime does not touch it" ((Test-Path $marker) -and -not (Test-Path (Join-Path $inst "runtime.new")))
Check "the running engine survived the update's copy" ([bool](Answers))

# an update with a changed runtime: staged while the engine runs, switched after it stops
Set-Content (Join-Path $inst "runtime\RUNTIME_ID") "older-runtime" -Encoding ascii -NoNewline
& (Join-Path $inst "windows\fetch-latest.ps1") -Dest $inst -SetupExe $Setup
Check "a changed runtime is staged as runtime.new while the engine runs" (Test-Path (Join-Path $inst "runtime.new\python.exe"))
StopEngine
& (Join-Path $inst "windows\switch-runtime.ps1")
$rtId2 = ((Get-Content (Join-Path $inst "runtime\RUNTIME_ID") -Raw) + "").Trim()
$venvId2 = ((Get-Content (Join-Path $inst ".venv\RUNTIME_ID") -Raw -ErrorAction SilentlyContinue) + "").Trim()
Check "after the engine stops, the new runtime is in place and .venv moved onto it" (
    $rtId2 -eq $rtId -and $venvId2 -eq $rtId -and -not (Test-Path (Join-Path $inst "runtime.new")) -and -not (Test-Path $marker)) "(runtime $rtId2, venv $venvId2)"
Start-Process wscript -ArgumentList ('"' + (Join-Path $inst "windows\engine-hidden.vbs") + '"') -WindowStyle Hidden
$v = WaitAnswer 150
Check "the engine starts again on the switched runtime" ($v -eq $want) "(version $v)"

StopEngine
if ($script:fails) { Write-Host "FAILED: $($script:fails)"; exit 1 }
Write-Host "ALL SETUP CHECKS PASSED"

# Behaviour test of the Windows engine keeper (windows\engine-hidden.vbs) on a
# real Windows machine: it starts the engine, only one keeper runs per user, a
# killed engine comes back by itself, keeper.stop makes the keeper leave, and
# the updater's hand-over (stop, kill, start a new keeper at once) ends with one
# keeper and a serving engine. Uses the real port 8765 - not on a PC whose own
# engine is running. Run from the repo root, after creating .venv:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\windows_keeper_test.ps1
# (.github/workflows/windows-keeper-test.yml runs it, by hand only.)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$vbs = Join-Path $root "windows\engine-hidden.vbs"
$data = Join-Path $env:APPDATA "colourMatik"
$stop = Join-Path $data "keeper.stop"
$script:fails = 0

function Check($name, $ok, $detail = "") {
    if ($ok) { Write-Host "  [PASS] $name   $detail" } else { Write-Host "  [FAIL] $name   $detail"; $script:fails++ }
}
function Answers { try { (Invoke-RestMethod "http://127.0.0.1:8765/version" -TimeoutSec 2).version } catch { $null } }
function WaitAnswer($sec) { for ($i = 0; $i -lt $sec; $i++) { $v = Answers; if ($v) { return $v }; Start-Sleep 1 }; return $null }
function WaitSilent($sec) { for ($i = 0; $i -lt $sec; $i++) { if (-not (Answers)) { return $true }; Start-Sleep 1 }; return $false }
function Keepers { @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "wscript.exe" -and $_.CommandLine -match "engine-hidden" }) }
function Engines { @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match "python" -and $_.CommandLine -match "colourmatik.webapp" }) }
function StartKeeper { Start-Process wscript -ArgumentList ('"' + $vbs + '"') -WindowStyle Hidden }
function KillEngines { Engines | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
function StopFile { New-Item -ItemType Directory -Force -Path $data | Out-Null; New-Item -ItemType File -Force -Path $stop | Out-Null }

Write-Host "=== Windows engine keeper ==="
if (Answers) { throw "an engine already answers on 127.0.0.1:8765 - run this on a machine without one" }

StartKeeper
$v = WaitAnswer 150
Check "the keeper starts the engine" ([bool]$v) "(version $v)"

StartKeeper
Start-Sleep 40
$n = @(Keepers).Count
Check "a second keeper leaves: one keeper per user" ($n -eq 1) "($n keepers)"

$before = (Engines | Select-Object -First 1).ProcessId
KillEngines
$down = WaitSilent 20
$v = WaitAnswer 150
$after = (Engines | Select-Object -First 1).ProcessId
Check "a killed engine comes back by itself" ($down -and [bool]$v -and $after -and $after -ne $before) "(down $down, pid $before -> $after, version $v)"

StopFile
KillEngines
Start-Sleep 20
$n = @(Keepers).Count; $v = Answers
Check "keeper.stop: the keeper leaves instead of restarting the engine" ($n -eq 0 -and -not $v) "($n keepers, engine $v)"

StartKeeper
$v = WaitAnswer 150
Check "a new keeper clears keeper.stop and starts the engine" ([bool]$v -and -not (Test-Path $stop)) "(version $v, stop file left: $(Test-Path $stop))"

# the updater's way: keeper.stop, kill the engine, start a new keeper at once
StopFile
KillEngines
StartKeeper
$v = WaitAnswer 150
Start-Sleep 5
$n = @(Keepers).Count
Check "update hand-over: one keeper, engine serving" ($n -eq 1 -and [bool]$v -and -not (Test-Path $stop)) "($n keepers, version $v)"

StopFile
KillEngines
Start-Sleep 5
if ($script:fails) { Write-Host "FAILED: $($script:fails)"; exit 1 }
Write-Host "ALL KEEPER CHECKS PASSED"

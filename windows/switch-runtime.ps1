# colourMatik - run by the updater after it has stopped the engine: put a new
# bundled runtime in place (fetch-latest.ps1 stages it as runtime.new, because
# Windows cannot replace files a running engine holds) and move .venv onto it.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$new = Join-Path $Root "runtime.new"
if (Test-Path (Join-Path $new "python.exe")) {
    # the engine's files can stay locked for a moment after it is killed
    for ($i = 0; $i -lt 15; $i++) {
        try { if (Test-Path "runtime") { Remove-Item "runtime" -Recurse -Force }; break } catch { Start-Sleep 1 }
    }
    if (Test-Path "runtime") { Write-Host "==> The old engine runtime is still in use - keeping it this time."; exit 0 }
    Rename-Item $new "runtime"
    Write-Host "==> Engine runtime updated"
}
if (Test-Path "runtime\python.exe") {
    & (Join-Path $Root "windows\setup.ps1")
}

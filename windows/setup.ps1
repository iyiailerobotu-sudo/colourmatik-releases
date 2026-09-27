# colourMatik — Windows engine setup. Creates the Python venv, installs deps,
# Fast classical engine only; the heavy local-AI stack is opt-in (see below).
#   powershell -ExecutionPolicy Bypass -File windows\setup.ps1
param([switch]$NoAI)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Find-Python311 {
    foreach ($cand in @("py -3.11", "python3.11", "python")) {
        try {
            $v = & cmd /c "$cand -c ""import sys;print(sys.version_info[:2])""" 2>$null
            if ($v -match "\(3, 1[1-9]\)") { return $cand }
        } catch {}
    }
    return $null
}

Write-Host "==> Locating Python 3.11+"
$py = Find-Python311
if (-not $py) {
    Write-Host "==> Installing Python 3.11 via winget..."
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Find-Python311
    if (-not $py) { throw "Python 3.11 not found after install - open a new terminal and re-run." }
}
Write-Host "    using: $py"

if (-not (Test-Path ".venv")) {
    Write-Host "==> Creating virtualenv (.venv)"
    & cmd /c "$py -m venv .venv"
}
# Always "python -m pip", never pip.exe: on Windows pip.exe cannot replace itself,
# so "pip.exe install --upgrade pip" logged an ERROR on every update and pip never moved.
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install --quiet --upgrade pip

Write-Host "==> Installing base engine deps"
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing the engine's dependencies failed - see the messages above." }

# -NoAI is obsolete: the default IS the classical engine now.

# AI extras are OPT-IN now (kept for the future):
#   .venv\Scripts\pip install -r requirements-ai.txt
# (CUDA torch install moved behind the AI opt-in as well.)

# The heavy local-AI stack is OPT-IN now (kept for the future):
#   .venv\Scripts\pip install -r requirements-ai.txt
#   git clone https://github.com/Jinwon-Ko/CanonCGT vendor\CanonCGT  (+ weights)
# The default engine is the fast classical core; nothing here downloads models.

Write-Host "==> Done. Start the engine with:  windows\colourmatik-app.cmd"
Write-Host "    (First AI run also auto-downloads the SegFormer scene model, ~15MB.)"

# Prove the environment can actually start before declaring success — a silent
# pip failure used to leave a machine the installer called "complete" and the
# panel could never connect to.
& $python -c "import fastapi, numpy, cv2" 2>$null
if ($LASTEXITCODE -ne 0) { throw "The engine environment is incomplete (a dependency is missing)." }
Write-Host "==> Engine environment verified."

# Build the engine's own Python for the Windows Setup: a relocatable CPython
# (python-build-standalone) with the engine's packages already in it, so an
# install needs no winget, no Python download and no pip - it works offline.
#   powershell -NoProfile -ExecutionPolicy Bypass -File windows\setup\build-runtime.ps1 -Out <folder>
# Writes <folder>\runtime (with RUNTIME_ID). build-setup.ps1 calls it.
param([Parameter(Mandatory = $true)][string]$Out)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = (Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")).Path

# Pinned: a release at least two weeks old when it was chosen, and its checksum.
$Tag = "20261110"
$File = "cpython-3.11.18+$Tag-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$Sha = "f31751f4a8483f5ac7cd77ad83f8dfb80d9f2a7245bff1585e27516c777fb46f"
$Url = "https://github.com/astral-sh/python-build-standalone/releases/download/$Tag/cpython-3.11.18%2B$Tag-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"

$work = Join-Path ([IO.Path]::GetTempPath()) ("cmk-rt-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    $tgz = Join-Path $work $File
    Write-Host "==> Downloading $File"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $Url -OutFile $tgz -UseBasicParsing
    $got = (Get-FileHash $tgz -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $Sha) { throw "checksum mismatch for $File ($got)" }
    & tar -xzf $tgz -C $work
    if ($LASTEXITCODE -ne 0) { throw "could not unpack $File" }
    $rt = Join-Path $Out "runtime"
    if (Test-Path $rt) { Remove-Item $rt -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Out | Out-Null
    Move-Item (Join-Path $work "python") $rt
} finally {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
$py = Join-Path $rt "python.exe"

Write-Host "==> Installing the engine's packages into it"
& $py -m pip install --quiet --disable-pip-version-check --no-warn-script-location --only-binary=:all: -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip could not install the engine's packages" }
& $py -c "import fastapi, uvicorn, numpy, scipy, cv2, colour, imageio, imageio_ffmpeg, PIL, certifi, multipart"
if ($LASTEXITCODE -ne 0) { throw "the runtime cannot import the engine's packages" }

Write-Host "==> Leaving out what the engine never runs"
$lib = Join-Path $rt "Lib"; $sp = Join-Path $lib "site-packages"
Get-ChildItem $sp -Recurse -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "tests" -or $_.Name -eq "test" } |
    Sort-Object { $_.FullName.Length } |
    ForEach-Object { if (Test-Path $_.FullName) { Remove-Item $_.FullName -Recurse -Force } }
foreach ($x in @("$lib\test", "$lib\idlelib", "$lib\tkinter", "$lib\turtledemo", "$lib\lib2to3", "$rt\tcl",
                 "$sp\colour\htmlcov")) {             # colour-science ships a 75 MB coverage report
    if (Test-Path $x) { Remove-Item $x -Recurse -Force }
}
Get-ChildItem (Join-Path $rt "DLLs") -Filter "*tk*" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem (Join-Path $rt "DLLs") -Filter "*tcl*" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem $rt -Recurse -Directory -Filter "__pycache__" | ForEach-Object { if (Test-Path $_.FullName) { Remove-Item $_.FullName -Recurse -Force } }
& $py -m compileall -q -j 0 $lib | Out-Null

$req = (Get-FileHash (Join-Path $Root "requirements.txt") -Algorithm SHA256).Hash.ToLower().Substring(0, 12)
$id = "cpython-3.11.18+$Tag-x86_64-pc-windows-msvc|req-$req|1"
Set-Content -Path (Join-Path $rt "RUNTIME_ID") -Value $id -Encoding ascii -NoNewline
& $py -c "import fastapi, numpy, scipy, cv2, colour, imageio_ffmpeg"
if ($LASTEXITCODE -ne 0) { throw "the stripped runtime does not start" }
$size = (Get-ChildItem $rt -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("    runtime: {0:N0} MB ({1})" -f ($size / 1MB), $id)

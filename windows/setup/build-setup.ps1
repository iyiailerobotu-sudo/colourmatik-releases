# Build colourMatik-windows-setup.exe - the self-contained Windows Setup.
#   powershell -NoProfile -ExecutionPolicy Bypass -File windows\setup\build-setup.ps1
#
# The Setup carries the whole program, so installing (and updating) never
# downloads source from GitHub. It packs the COMMITTED tree - `git archive HEAD`,
# so uncommitted edits are not shipped - minus the build-time paths that
# .gitattributes marks export-ignore (artwork, tests, CI). The version stamp
# comes from that tree's version.json.
param([string]$MakeNsis = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$Payload = Join-Path $Here "payload"

# (not named "git": PowerShell names are case-insensitive, and a function called
# Git would call itself instead of git.exe)
function Invoke-Git {
    $out = & git -C $Root @args
    if ($LASTEXITCODE -ne 0) { throw "git $args failed (code $LASTEXITCODE)" }
    $out
}

$head = Invoke-Git rev-parse --short HEAD
$dirty = Invoke-Git status --porcelain --untracked-files=no
if ($dirty) {
    Write-Warning "Uncommitted changes are NOT in this Setup - it packs HEAD ($head):"
    $dirty | ForEach-Object { Write-Warning "  $_" }
}

Write-Host "==> Staging the program from $head"
if (Test-Path $Payload) { Remove-Item $Payload -Recurse -Force }
$zip = Join-Path ([IO.Path]::GetTempPath()) ("cmk-payload-" + [guid]::NewGuid().ToString("N") + ".zip")
# CRLF text files whatever the building machine's git settings: this is a
# Windows install, and batch files are only reliable with CRLF.
Invoke-Git -c core.autocrlf=true archive --format=zip -o $zip HEAD | Out-Null
try { Expand-Archive $zip $Payload } finally { Remove-Item $zip -Force -ErrorAction SilentlyContinue }

foreach ($need in @("version.json", "windows\install-windows.ps1", "windows\update-windows.cmd",
                    "windows\fetch-latest.ps1", "colourmatik\webapp.py",
                    "colourmatik-uxp\colourMatik.ccx", "colourmatik-cep\CSXS\manifest.xml",
                    "colourmatik-fx\colourMatik.aex", "colourmatik-fx\colourMatik-ae.aex")) {
    if (-not (Test-Path (Join-Path $Payload $need))) { throw "The program is missing $need - nothing was built." }
}
$ver = (Get-Content (Join-Path $Payload "version.json") -Raw | ConvertFrom-Json).version
if ($ver -notmatch '^\d+\.\d+\.\d+$') { throw "version.json has no x.y.z version (got '$ver')." }

# The .ccx is what Adobe's plugin agent installs: a stale one would put an old
# panel on every new machine while everything else says $ver.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$ccx = [IO.Compression.ZipFile]::OpenRead((Join-Path $Payload "colourmatik-uxp\colourMatik.ccx"))
try {
    $sr = New-Object IO.StreamReader($ccx.GetEntry("manifest.json").Open())
    try { $ccxVer = ($sr.ReadToEnd() | ConvertFrom-Json).version } finally { $sr.Close() }
} finally { $ccx.Dispose() }
if ($ccxVer -ne $ver) { throw "colourMatik.ccx carries $ccxVer but version.json says $ver - rebuild the .ccx first." }

$files = Get-ChildItem $Payload -Recurse -File | Measure-Object Length -Sum
Write-Host ("    {0} files, {1:N1} MB, version {2}" -f $files.Count, ($files.Sum / 1MB), $ver)

if (-not $MakeNsis) {
    $MakeNsis = @("${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$env:ProgramFiles\NSIS\makensis.exe") |
                Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $MakeNsis) { $c = Get-Command makensis -ErrorAction SilentlyContinue; if ($c) { $MakeNsis = $c.Source } }
if (-not $MakeNsis) { throw "makensis not found - install NSIS 3 (nsis.sourceforge.io)." }

Write-Host "==> makensis $ver"
& $MakeNsis /V2 "/DVERSION=$ver" (Join-Path $Here "colourMatik.nsi")
if ($LASTEXITCODE -ne 0) { throw "makensis failed (code $LASTEXITCODE)." }
Remove-Item $Payload -Recurse -Force

$exe = Get-Item (Join-Path $Here "colourMatik-windows-setup.exe")
$sha = (Get-FileHash $exe.FullName -Algorithm SHA256).Hash.ToLower()
Write-Host ("==> {0}`n    {1:N0} bytes, version {2}, from {3}, sha256 {4}" -f $exe.FullName, $exe.Length, $ver, $head, $sha)

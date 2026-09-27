# colourMatik - refresh the install directory from the newest Windows Setup.
#
# The Setup carries the whole program, so an update needs exactly one file: the
# same colourMatik-windows-setup.exe the website links to (release tag
# windows-latest). It is fetched the official way - the GitHub REST API, asset
# download with "Accept: application/octet-stream", and a User-Agent that says
# what we are (never a browser's) - and then asked to unpack itself
# (/EXTRACT=<folder>) instead of installing. No source zip, no raw files.
#
# Kept as its own file on purpose: building this inline inside update-windows.cmd
# meant echoing PowerShell (which is full of parentheses) into a parenthesised
# batch block, and batch closes such a block at the first unescaped ")" - the
# script would have been written out truncated and the update would fail in a
# way that looks like "nothing happened".
#   -SetupExe <file>  use an already-downloaded Setup (tests; offline repair)
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$SetupExe = ""
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # the progress UI slows downloads 10x
$Api = 'https://api.github.com/repos/iyiailerobotu-sudo/colourmatik-releases'
$AssetName = 'colourMatik-windows-setup.exe'

# A machine with TEMP unset (or redirected to nothing) would otherwise fail on
# a null path before downloading anything.
$tmpRoot = $env:TEMP
if (-not $tmpRoot) { $tmpRoot = $env:TMP }
if (-not $tmpRoot) { $tmpRoot = [IO.Path]::GetTempPath() }
$tmp = Join-Path $tmpRoot ('cmk-upd-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $installed = 'unknown'
    try { $installed = (Get-Content (Join-Path $Dest 'version.json') -Raw | ConvertFrom-Json).version } catch {}
    if (-not $SetupExe) {
        # Windows PowerShell's default User-Agent starts with "Mozilla/5.0";
        # always send our own.
        $ua = "colourMatik-updater/$installed (Windows)"
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Write-Host "==> Looking up the newest colourMatik release..."
        $rel = Invoke-RestMethod "$Api/releases/tags/windows-latest" -UserAgent $ua -UseBasicParsing `
                   -Headers @{ 'Accept' = 'application/vnd.github+json'; 'X-GitHub-Api-Version' = '2022-11-28' }
        $asset = @($rel.assets) | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
        if (-not $asset) { throw "The windows-latest release has no $AssetName." }
        $SetupExe = Join-Path $tmp $AssetName
        Write-Host ("==> Downloading {0} ({1:N1} MB)..." -f $AssetName, ($asset.size / 1MB))
        Invoke-WebRequest $asset.url -OutFile $SetupExe -UserAgent $ua -UseBasicParsing `
            -Headers @{ 'Accept' = 'application/octet-stream' }
        $got = (Get-Item $SetupExe).Length
        if ($got -ne [int64]$asset.size) { throw "The download is incomplete ($got of $($asset.size) bytes)." }
    }
    $v = (Get-Item $SetupExe).VersionInfo.ProductVersion
    Write-Host "==> Setup $v (installed: $installed) - unpacking..."
    $out = Join-Path $tmp 'program'
    # Setup asks Windows for admin because it installs machine-wide; unpacking
    # into a temp folder needs none, so never let it raise a UAC prompt here.
    $env:__COMPAT_LAYER = 'RunAsInvoker'
    try {
        $p = Start-Process -FilePath $SetupExe -ArgumentList @('/S', "/EXTRACT=`"$out`"") -Wait -PassThru
    } finally {
        Remove-Item Env:__COMPAT_LAYER -ErrorAction SilentlyContinue
    }
    if ($p.ExitCode -ne 0) { throw "Setup could not unpack the update (code $($p.ExitCode))." }
    if (-not (Test-Path (Join-Path $out 'version.json'))) { throw 'Setup unpacked no program.' }
    # Overwrite the program in place; .venv, vendored models and slot files are
    # not part of it and are left alone.
    Copy-Item (Join-Path $out '*') $Dest -Recurse -Force
    Write-Host "==> Program refreshed into $Dest"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

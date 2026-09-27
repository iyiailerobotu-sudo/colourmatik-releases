# colourMatik - refresh the install directory from the newest Windows Setup.
#
# The Setup carries the whole program, so an update needs exactly one file: the
# same colourMatik-windows-setup.exe the website links to, on our own download
# server (releases.catheadai.com - Cloudflare R2, not GitHub). Its latest.json
# names the file with its size and SHA-256; the download must match both, and
# then the Setup is asked to unpack itself (/EXTRACT=<folder>) instead of
# installing. The User-Agent says what we are, never a browser.
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
$Latest = 'https://releases.catheadai.com/colourmatik/latest.json'
$Downloads = 'https://releases.catheadai.com/colourmatik/'

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
        $w = (Invoke-RestMethod $Latest -UserAgent $ua -UseBasicParsing).windows
        if (-not $w -or -not ([string]$w.url).StartsWith($Downloads) -or -not $w.sha256) {
            throw "latest.json names no Windows Setup on $Downloads."
        }
        $SetupExe = Join-Path $tmp 'colourMatik-windows-setup.exe'
        Write-Host ("==> Downloading colourMatik {0} ({1:N1} MB)..." -f $w.version, ($w.size / 1MB))
        # "?v=": the server's cache can hold the previous file under the fixed
        # name for a while; an address per version is always fetched fresh.
        Invoke-WebRequest ($w.url + '?v=' + $w.version) -OutFile $SetupExe -UserAgent $ua -UseBasicParsing
        $got = (Get-Item $SetupExe).Length
        if ($got -ne [int64]$w.size) { throw "The download is incomplete ($got of $($w.size) bytes)." }
        $sha = (Get-FileHash $SetupExe -Algorithm SHA256).Hash
        if ($sha -ne [string]$w.sha256) { throw "The download does not match latest.json (SHA-256 $sha)." }
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

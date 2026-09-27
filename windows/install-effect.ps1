# colourMatik — install the native effect (.aex) into Premiere Pro (Windows, x64).
# Uses a local build if present (windows\colourMatik.aex or colourmatik-fx\colourMatik.aex),
# otherwise downloads it from the project's latest GitHub release. Needs admin for
# the shared MediaCore folder (the caller elevates).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DestDir = "C:\Program Files\Adobe\Common\Plug-ins\7.0\MediaCore"
$Dest = Join-Path $DestDir "colourMatik.aex"
$ReleaseAsset = "https://github.com/iyiailerobotu-sudo/colourmatik-releases/releases/download/windows-latest/colourMatik-effect-windows.zip"

$local = @("$Root\windows\colourMatik.aex", "$Root\colourmatik-fx\colourMatik.aex") |
         Where-Object { Test-Path $_ } | Select-Object -First 1

# Resolve the .aex once into $Src (local build, else the release zip).
if ($local) {
    Write-Host "==> Using local build: $local"
    $Src = $local
} else {
    Write-Host "==> Downloading the effect from the latest release..."
    $tmp = Join-Path $env:TEMP "colourMatik-effect-windows.zip"
    Invoke-WebRequest -Uri $ReleaseAsset -OutFile $tmp -UseBasicParsing
    $ext = Join-Path $env:TEMP "colourMatik-effect-win"
    if (Test-Path $ext) { Remove-Item -Recurse -Force $ext }
    Expand-Archive $tmp $ext
    # The zip carries BOTH builds now, so pick by exact name — a bare *.aex
    # match could hand Premiere the AE variant (wrong match name).
    $aex = Get-ChildItem $ext -Recurse -Filter "colourMatik.aex" | Select-Object -First 1
    if (-not $aex) { throw "colourMatik.aex not found in the release zip." }
    $Src = $aex.FullName
    $aeFromZip = Get-ChildItem $ext -Recurse -Filter "colourMatik-ae.aex" |
                 Select-Object -First 1
    if ($aeFromZip) { $AeSrcFromZip = $aeFromZip.FullName }
}

# 1) Premiere Pro / Media Encoder: the shared MediaCore folder.
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Copy-Item $Src $Dest -Force
Unblock-File $Dest -ErrorAction SilentlyContinue
Write-Host "Effect installed (Premiere) -> $Dest"

# 2) After Effects does NOT load effects from MediaCore — only from its OWN
#    Plug-ins folder. Install a copy for every AE version present, or the effect
#    shows in Premiere but is invisible in After Effects.
$aeRoots = @("C:\Program Files\Adobe", "C:\Program Files (x86)\Adobe")
foreach ($aeRoot in $aeRoots) {
    if (-not (Test-Path $aeRoot)) { continue }
    Get-ChildItem $aeRoot -Directory -Filter "Adobe After Effects *" -ErrorAction SilentlyContinue | ForEach-Object {
        $aePlug = Join-Path $_.FullName "Support Files\Plug-ins"
        if (Test-Path $aePlug) {
            $aeDestDir = Join-Path $aePlug "colourMatik"
            New-Item -ItemType Directory -Force -Path $aeDestDir | Out-Null
            $aeDest = Join-Path $aeDestDir "colourMatik.aex"
            # AE gets the distinct-match-name variant (avoids AE's "duplicated
            # effect plugin" warning); falls back to the main build if absent.
            $aeSrc = @("$Root\colourmatik-fx\colourMatik-ae.aex", "$Root\windows\colourMatik-ae.aex") | Where-Object { Test-Path $_ } | Select-Object -First 1
            if (-not $aeSrc -and $AeSrcFromZip) { $aeSrc = $AeSrcFromZip }   # downloaded install
            if (-not $aeSrc) { $aeSrc = $Src }
            Copy-Item $aeSrc $aeDest -Force
            Unblock-File $aeDest -ErrorAction SilentlyContinue
            Write-Host "Effect installed (After Effects) -> $aeDest"
            # remove the deprecated ScriptUI panel from older installs
            $oldJsx = Join-Path $_.FullName "Support Files\Scripts\ScriptUI Panels\colourMatik.jsx"
            if (Test-Path $oldJsx) { Remove-Item -Force $oldJsx -ErrorAction SilentlyContinue }
        }
    }
}

# AE Match & Apply panel (CEP — full HTML, identical to the Premiere panel).
# Per-user (%APPDATA%), no admin: Window > Extensions > colourMatik.
$cepSrc = Join-Path $Root "colourmatik-cep"
if (Test-Path $cepSrc) {
    # resolve the LOGGED-IN user's profile even when elevated
    $userProfile = $env:USERPROFILE
    try {
        $ex = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Select-Object -First 1
        if ($ex) { $owner = Invoke-CimMethod -InputObject $ex -MethodName GetOwner
                   if ($owner.User) {
                       # Never fabricate C:\Users\<account>: renamed accounts,
                       # rebuilt profiles (john.DESKTOP-8KQ2), relocated profiles
                       # and non-C: installs all break that assumption, and the
                       # panel would be copied where After Effects never looks.
                       $cand = Join-Path (Join-Path $env:SystemDrive "Users") $owner.User
                       if (Test-Path $cand) { $userProfile = $cand }
                     } }
    } catch {}
    $cepDest = Join-Path $userProfile "AppData\Roaming\Adobe\CEP\extensions\com.catheadai.colourmatik"
    if (Test-Path $cepDest) { Remove-Item -Recurse -Force $cepDest }
    New-Item -ItemType Directory -Force -Path $cepDest | Out-Null
    Copy-Item "$cepSrc\*" $cepDest -Recurse -Force
    # allow the (unsigned) extension to load (PlayerDebugMode is a STRING "1")
    foreach ($v in 9..12) {
        try { reg add "HKCU\Software\Adobe\CSXS.$v" /v PlayerDebugMode /t REG_SZ /d 1 /f | Out-Null } catch {}
    }
    Write-Host "AE panel (CEP) installed -> Window > Extensions > colourMatik"
}

# The AE panel's curl needs "Allow Scripts to Write Files and Access Network".
# A running script can't set it, so write it into each AE version's prefs (same as
# the checkbox). Prefs live in the LOGGED-IN user's profile — resolve it even when
# this installer is elevated (APPDATA would otherwise point at the admin profile).
try {
    $userProfile = $env:USERPROFILE
    try {
        $ex = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Select-Object -First 1
        if ($ex) { $owner = Invoke-CimMethod -InputObject $ex -MethodName GetOwner
                   if ($owner.User) {
                       # Never fabricate C:\Users\<account>: renamed accounts,
                       # rebuilt profiles (john.DESKTOP-8KQ2), relocated profiles
                       # and non-C: installs all break that assumption, and the
                       # panel would be copied where After Effects never looks.
                       $cand = Join-Path (Join-Path $env:SystemDrive "Users") $owner.User
                       if (Test-Path $cand) { $userProfile = $cand }
                     } }
    } catch {}
    $aePrefRoot = Join-Path $userProfile "AppData\Roaming\Adobe\After Effects"
    if (Test-Path $aePrefRoot) {
        Get-ChildItem $aePrefRoot -Directory | ForEach-Object {
            Get-ChildItem $_.FullName -Filter "Adobe After Effects * Prefs.txt" -ErrorAction SilentlyContinue | ForEach-Object {
                $c = Get-Content $_.FullName -Raw
                if ($c -match '"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "0"') {
                    ($c -replace '"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "0"', '"Pref_SCRIPTING_FILE_NETWORK_SECURITY" = "1"') |
                        Set-Content $_.FullName -NoNewline -Encoding UTF8
                    Write-Host "Enabled AE scripting/network -> $($_.Directory.Name)"
                }
            }
        }
    }
} catch { Write-Warning "Couldn't set the AE scripting preference automatically: $_" }

Write-Host "Restart Premiere Pro / After Effects, then find it under Effects > colourMatik > colourMatik."

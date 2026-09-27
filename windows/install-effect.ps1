# colourMatik — install the native effect (.aex) into Premiere Pro (Windows, x64).
# The effect ships with the program (colourmatik-fx\colourMatik.aex, built by the
# windows-effect workflow; windows\colourMatik.aex wins if present) - it is never
# downloaded. Needs admin for the shared MediaCore folder (the caller elevates).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DestDir = "C:\Program Files\Adobe\Common\Plug-ins\7.0\MediaCore"
$Dest = Join-Path $DestDir "colourMatik.aex"

$Src = @("$Root\windows\colourMatik.aex", "$Root\colourmatik-fx\colourMatik.aex") |
       Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Src) { throw "colourMatik.aex is not in $Root\colourmatik-fx - run Setup again to restore the program." }
Write-Host "==> Using the effect build: $Src"

# A running Premiere / After Effects holds its loaded .aex open, and overwriting
# it fails - which stopped this script before the AE panel step on every update
# started from the panel. An identical copy needs no write at all.
function Install-Aex([string]$from, [string]$to) {
    if ((Test-Path $to) -and ((Get-FileHash $from).Hash -eq (Get-FileHash $to).Hash)) {
        Write-Host "Effect already current -> $to"
        return
    }
    Copy-Item $from $to -Force
    Unblock-File $to -ErrorAction SilentlyContinue
    Write-Host "Effect installed -> $to"
}

# 1) Premiere Pro / Media Encoder: the shared MediaCore folder.
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Install-Aex $Src $Dest

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
            if (-not $aeSrc) { $aeSrc = $Src }
            Install-Aex $aeSrc $aeDest
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

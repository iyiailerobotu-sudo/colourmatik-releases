# colourMatik - one-paste diagnostic + repair (Windows).
#   Win+R -> powershell -> paste:
#   irm https://raw.githubusercontent.com/iyiailerobotu-sudo/colourmatik-releases/main/windows/diag.ps1 | iex
# Prints where every copy of the panel lives and what version it is, removes
# stale ones, installs the current panel, and leaves a report on the Desktop.
# ASCII-only on purpose: PowerShell 5.1 mangles non-ASCII piped through iex.

$ErrorActionPreference = "Continue"
$lines = New-Object System.Collections.Generic.List[string]
function Say([string]$s) { Write-Host $s; $lines.Add($s) | Out-Null }

Say "=== colourMatik DIAG $(Get-Date -Format s) ==="

# -- engine ---------------------------------------------------------------
try {
    $v = (Invoke-RestMethod "http://127.0.0.1:8765/version" -TimeoutSec 3).version
    Say ("engine        : " + $v)
} catch { Say "engine        : NOT RUNNING" }

# -- install dir ----------------------------------------------------------
$inst = Join-Path $env:USERPROFILE "colourMatik"
if (Test-Path (Join-Path $inst "version.json")) {
    $iv = (Get-Content (Join-Path $inst "version.json") -Raw | ConvertFrom-Json).version
    Say ("install dir   : " + $inst + "  version " + $iv)
} else { Say ("install dir   : " + $inst + "  (no version.json)") }

# -- every panel copy on this machine --------------------------------------
$uxp = Join-Path $env:APPDATA "Adobe\UXP"
# Machine-wide UXP roots: when the agent runs ELEVATED it installs here, and no
# per-user cleanup ever touches them - the last hiding place a 1.2.0 can live.
$machineRoots = @()
foreach ($pfBase in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData)) {
    if (-not $pfBase) { continue }
    foreach ($sub in @("Common Files\Adobe\UXP\PluginsStorage", "Common Files\Adobe\UXP\Plugins\External", "Adobe\UXP\PluginsStorage", "Adobe\UXP\Plugins\External")) {
        $mr = Join-Path $pfBase $sub
        if (Test-Path $mr) { $machineRoots += $mr }
    }
}
Say "--- panel copies ---"
$found = @()
foreach ($root in (@((Join-Path $uxp "Plugins\External"), (Join-Path $uxp "PluginsStorage")) + $machineRoots)) {
    if (-not (Test-Path $root)) { continue }
    Get-ChildItem $root -Recurse -Directory -Filter "com.colourmatik*" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $mj = Join-Path $_.FullName "main.js"
        $ver = "?"
        if (Test-Path $mj) {
            $m = Select-String -Path $mj -Pattern 'LOCAL_VERSION = "([^"]+)"' | Select-Object -First 1
            if ($m) { $ver = $m.Matches[0].Groups[1].Value }
        }
        Say ("  " + $_.FullName.Replace($env:APPDATA, "%APPDATA%") + "  -> v" + $ver)
        $found += $_
    }
}
if (-not $found) { Say "  (none found)" }

# -- manifest sanity ---------------------------------------------------------
# Informational only. (An earlier theory blamed ".exe" in launchProcess for
# the invisible panel; Premiere's own UXP logs on the field machine showed no
# manifest rejection at all - the real blocker was a stale SYSTEM registration,
# see below. The panel does not need ".exe": it never launches processes.)
Say "--- manifest check ---"
$mfRoots = @((Join-Path $uxp "Plugins\External"))
foreach ($mr0 in $machineRoots) { $mfRoots += $mr0 }
foreach ($mfRoot in $mfRoots) {
    foreach ($mfp in (Get-ChildItem $mfRoot -Recurse -Filter "manifest.json" -ErrorAction SilentlyContinue)) {
        try {
            $mm = Get-Content $mfp.FullName -Raw | ConvertFrom-Json
            if ($mm.id -notlike "com.colourmatik*") { continue }
            $ext = @()
            try { $ext = @($mm.requiredPermissions.launchProcess.extensions) } catch {}
            $flag = ""
            if ($ext -contains ".exe") { $flag = "  <-- .exe present (not needed; current builds ship without it)" }
            Say ("  v" + $mm.version + " launchProcess.extensions = [" + ($ext -join ", ") + "]" + $flag)
        } catch { Say ("  unreadable manifest: " + $mfp.FullName) }
    }
}

Say "--- UXP registries (per-user) ---"
$regDir = Join-Path $uxp "PluginsInfo\v1"
if (Test-Path $regDir) {
    Get-ChildItem $regDir -Filter "*.json" | ForEach-Object {
        try {
            $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
            $ours = @($j.plugins | Where-Object { $_.pluginId -like "com.colourmatik*" })
            foreach ($p in $ours) { Say ("  " + $_.Name + " : " + $p.pluginId + " v" + $p.versionString + " path=" + $p.path) }
            if (-not $ours) { Say ("  " + $_.Name + " : no colourMatik entry") }
        } catch { Say ("  " + $_.Name + " : UNREADABLE (" + $_.Exception.Message + ")") }
    }
} else { Say "  (no PluginsInfo dir)" }

# The SYSTEM PluginsInfo registry is the one Premiere loads FIRST (written by
# Adobe's agent when a .ccx installs elevated; token $systemPlugins resolves to
# C:\Program Files\Common Files\Adobe\UXP\Plugins). An entry here claims the
# plugin id even when its folder is gone - Premiere then logs "failed to
# create/initialize plugin" and never loads ANY other copy of the same id.
# THIS was the weeks-long invisible-panel bug on the field machine.
Say "--- UXP registries (SYSTEM - Premiere trusts these first) ---"
$sysRegDirs = @()
foreach ($pfBase in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData)) {
    if (-not $pfBase) { continue }
    foreach ($sub in @("Common Files\Adobe\UXP\PluginsInfo\v1", "Adobe\UXP\PluginsInfo\v1")) {
        $r = Join-Path $pfBase $sub
        if (Test-Path $r) { $sysRegDirs += $r }
    }
}
if ($sysRegDirs) {
    foreach ($sr in $sysRegDirs) {
        Get-ChildItem $sr -Filter "*.json" -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
                $ours = @($j.plugins | Where-Object { $_.pluginId -like "com.colourmatik*" })
                foreach ($p in $ours) {
                    $mark = ""
                    $resolved = $p.path -replace '\$systemPlugins', (Join-Path (Split-Path (Split-Path $sr -Parent) -Parent) "Plugins") -replace '/', '\'
                    if (-not (Test-Path $resolved)) { $mark = "  <-- STALE: folder is GONE, this entry BLOCKS the panel" }
                    Say ("  " + $_.FullName + " : " + $p.pluginId + " v" + $p.versionString + " path=" + $p.path + $mark)
                }
                if (-not $ours) { Say ("  " + $_.FullName + " : no colourMatik entry") }
            } catch { Say ("  " + $_.FullName + " : UNREADABLE (" + $_.Exception.Message + ")") }
        }
    }
} else { Say "  (none found)" }

# Wrong ownership of these two folders makes Adobe's agent refuse EVERY
# install with status -198 (it validates ownership before touching them).
# An elevated cleanup session is exactly what leaves them owned by
# BUILTIN\Administrators instead of the user.
Say "--- folder ownership (must belong to the user, not Administrators) ---"
foreach ($ownPath in @((Join-Path $env:APPDATA "Adobe\UXP"), (Join-Path $env:APPDATA "Adobe\UPI"))) {
    if (Test-Path $ownPath) {
        try { Say ("  " + $ownPath + "  owner: " + (Get-Acl $ownPath).Owner) } catch {}
    }
}

# -- Adobe plugin agent -------------------------------------------------------
$upia = $null
foreach ($r in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if (-not $r) { continue }
    $p = Join-Path $r "Common Files\Adobe\Adobe Desktop Common\RemoteComponents\UPI\UnifiedPluginInstallerAgent\UnifiedPluginInstallerAgent.exe"
    if (Test-Path $p) { $upia = $p; break }
}
Say "--- plugin agent ---"
if ($upia) {
    (& $upia /list all 2>&1 | Select-String "colourMatik") | ForEach-Object { Say ("  " + $_) }
} else { Say "  (agent not installed)" }

# ============================ REPAIR =========================================
Say ""
Say "=== REPAIR ==="
$curVer = $null
$srcPanel = Join-Path $inst "colourmatik-uxp"
if (Test-Path (Join-Path $srcPanel "manifest.json")) {
    $curVer = ((Get-Content (Join-Path $srcPanel "manifest.json") -Raw | ConvertFrom-Json).version)
    Say ("current panel available: v" + $curVer)

    # 1) agent forgets us (its database outlives file deletion).
    # /remove takes the extension NAME from /list ("colourMatik"), NOT the
    # plugin id - the id form fails with -406 and the phantom row survives.
    # And ALWAYS call the agent from the system drive: launched from another
    # drive (a D:\Downloads folder) it mis-resolves the user profile and every
    # install fails with -198. Both field-diagnosed.
    if ($upia) {
        Push-Location ($env:SystemDrive + "\")
        try {
            foreach ($rmArg in @("colourMatik", "com.colourmatik.panel")) {
                $rmOut = (& $upia /remove $rmArg 2>&1 | Out-String).Trim()
                Say ("  agent /remove " + $rmArg + ": " + (($rmOut -split "`r?`n") | Select-Object -Last 1))
                if ($rmOut -match "(?i)successful") { break }
            }
        } finally { Pop-Location }
    }

    # 2) delete every stale copy (per-user AND machine-wide, all hosts/versions)
    $needElevation = @()
    foreach ($root in (@((Join-Path $uxp "Plugins\External"), (Join-Path $uxp "PluginsStorage")) + $machineRoots)) {
        if (-not (Test-Path $root)) { continue }
        Get-ChildItem $root -Recurse -Directory -Filter "com.colourmatik*" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne ("com.colourmatik.panel_" + $curVer) } |
        ForEach-Object {
            Say ("  removing " + $_.FullName.Replace($env:APPDATA, "%APPDATA%"))
            Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue
            if (Test-Path $_.FullName) { $needElevation += $_.FullName }
        }
    }
    if ($needElevation.Count -gt 0) {
        Say ""
        Say "!! Some copies are in protected folders and need ADMIN to remove:"
        foreach ($ne in $needElevation) { Say ("     " + $ne) }
        $cmd = ($needElevation | ForEach-Object { "Remove-Item -Recurse -Force '" + $_ + "'" }) -join "; "
        Say "   Removing them now via an elevated window (approve the prompt)..."
        try {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile", "-Command", $cmd
            $left = @($needElevation | Where-Object { Test-Path $_ })
            if ($left.Count -eq 0) { Say "   removed with admin rights - done" }
            else { Say "   STILL PRESENT (prompt declined?) - run PowerShell as Administrator and paste:"; Say ("     " + $cmd) }
        } catch { Say ("   elevation failed - run PowerShell as Administrator and paste:"); Say ("     " + $cmd) }
    }

    # 3) clean EVERY registry json (premierepro.json, the Beta's file, all of them)
    if (Test-Path $regDir) {
        Get-ChildItem $regDir -Filter "*.json" | ForEach-Object {
            try {
                $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
                if ($j.plugins) {
                    $keep = @($j.plugins | Where-Object { -not ($_.pluginId -eq "com.colourmatik.panel" -and $_.versionString -ne $curVer) })
                    if ($keep.Count -ne @($j.plugins).Count) {
                        $j.plugins = $keep
                        [IO.File]::WriteAllText($_.FullName, ($j | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
                        Say ("  cleaned " + $_.Name)
                    }
                }
            } catch {}
        }
    }

    # 3b) SYSTEM registries + folder ownership. The SYSTEM PluginsInfo file is
    # what Premiere trusts first: a stale colourMatik row there (folder gone)
    # claims the plugin id and blocks every other copy - THE weeks-long
    # invisible-panel bug. And wrong ownership of %APPDATA%\Adobe\UXP / UPI
    # (left behind by elevated cleanups) makes the agent refuse installs
    # with -198. Both need admin, so do them inline when we are admin and
    # through one elevated helper script otherwise.
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $helper = @'
param([string]$Ver, [string]$UserName, [string]$UserAppData)
foreach ($d in @((Join-Path $env:ProgramFiles "Common Files\Adobe\UXP\PluginsInfo\v1"),
                 (Join-Path $env:ProgramData "Adobe\UXP\PluginsInfo\v1"))) {
    if (-not (Test-Path $d)) { continue }
    Get-ChildItem $d -Filter "*.json" | ForEach-Object {
        try {
            $j = Get-Content $_.FullName -Raw | ConvertFrom-Json
            if ($j.plugins) {
                $k = @($j.plugins | Where-Object { -not ($_.pluginId -eq "com.colourmatik.panel" -and $_.versionString -ne $Ver) })
                if ($k.Count -ne @($j.plugins).Count) {
                    $j.plugins = $k
                    [IO.File]::WriteAllText($_.FullName, ($j | ConvertTo-Json -Depth 10 -Compress), (New-Object Text.UTF8Encoding $false))
                }
            }
        } catch {}
    }
}
foreach ($p in @((Join-Path $UserAppData "Adobe\UXP"), (Join-Path $UserAppData "Adobe\UPI"))) {
    if (Test-Path $p) { icacls $p /setowner $UserName /T /C /Q | Out-Null }
}
'@
    $helperPath = Join-Path $env:TEMP "colourmatik-sysfix.ps1"
    Set-Content -Path $helperPath -Value $helper -Encoding ASCII
    if ($isAdmin) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $helperPath -Ver $curVer -UserName $env:USERNAME -UserAppData $env:APPDATA
        Say "  SYSTEM registries cleaned + folder ownership returned to the user"
    } else {
        Say "  cleaning SYSTEM registries + folder ownership via an elevated window (approve the prompt)..."
        try {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",$helperPath,"-Ver",$curVer,"-UserName",$env:USERNAME,"-UserAppData",$env:APPDATA
            Say "  done"
        } catch { Say "  elevation declined - a stale SYSTEM entry can keep blocking the panel; run PowerShell as Administrator and re-run this diag." }
    }

    # 4) put the current panel in place (developer-mode path)
    $PluginFolder = "com.colourmatik.panel_" + $curVer
    $dest = Join-Path $uxp ("Plugins\External\" + $PluginFolder)
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item (Join-Path $srcPanel "*") $dest -Force -Exclude "*.ccx"
    Say ("  installed -> " + $dest.Replace($env:APPDATA, "%APPDATA%"))
    # 5) REGISTER it through Adobe's agent so Premiere shows it WITHOUT any
    # developer-mode toggle. Removing the old registration above without doing
    # this left one field machine with files on disk and an empty UXP window.
    # 5a) WRITE THE REGISTRY ENTRY OURSELVES - this is what macOS has always
    # done, and it is why the Mac panel never goes missing. Windows relied
    # entirely on Adobe's agent, so when the agent's row was gone the panel
    # vanished from the UXP window with the files sitting right there.
    if (-not (Test-Path $regDir)) { New-Item -ItemType Directory -Force -Path $regDir | Out-Null }
    $mf = Get-Content (Join-Path $srcPanel "manifest.json") -Raw | ConvertFrom-Json
    $hostMin = "26.0"
    try { if ($mf.host -and $mf.host.minVersion) { $hostMin = $mf.host.minVersion } } catch {}
    $entry = [ordered]@{
        hostMinVersion = $hostMin
        name           = $(if ($mf.name) { $mf.name } else { "colourMatik" })
        path           = '$localPlugins/External/' + $PluginFolder
        pluginId       = "com.colourmatik.panel"
        status         = "enabled"
        type           = "uxp"
        versionString  = $curVer
    }
    foreach ($regName in @("premierepro.json", "PremierePro.json")) {
        $regFile = Join-Path $regDir $regName
        if (-not (Test-Path $regFile)) {
            if ($regName -ne "premierepro.json") { continue }
            [IO.File]::WriteAllText($regFile, '{"plugins":[]}', (New-Object Text.UTF8Encoding $false))
        }
        try {
            $j = Get-Content $regFile -Raw | ConvertFrom-Json
            $keep = @()
            if ($j.plugins) { $keep = @($j.plugins | Where-Object { $_.pluginId -ne "com.colourmatik.panel" }) }
            $keep += [pscustomobject]$entry
            $j.plugins = $keep
            # NO BOM - Premiere refuses to parse this file with one.
            [IO.File]::WriteAllText($regFile, ($j | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
            Say ("  registered in " + $regName + " -> " + $entry.path)
        } catch { Say ("  could not write " + $regName + " (" + $_.Exception.Message + ")") }
    }

    $agentDone = $false
    if ($upia) {
        $zip = Join-Path $env:TEMP "colourMatik-diag.zip"
        $ccx = Join-Path $env:TEMP "colourMatik-diag.ccx"
        foreach ($f in @($zip, $ccx)) { if (Test-Path $f) { Remove-Item -Force $f } }
        $files = Get-ChildItem $srcPanel -File | Where-Object { $_.Extension -in ".json",".html",".js",".png" }
        Compress-Archive -Path ($files | ForEach-Object { $_.FullName }) -DestinationPath $zip -Force
        Move-Item $zip $ccx -Force
        # From the system drive, ALWAYS (see the /remove note above: -198 otherwise).
        Push-Location ($env:SystemDrive + "\")
        try { $out = (& $upia /install $ccx 2>&1 | Out-String).Trim() } finally { Pop-Location }
        if ($out) { Say ("  agent install: " + (($out -split "`r?`n") | Select-Object -Last 1)) }
        if ($out -match "(?i)success") { Say "  panel REGISTERED through Adobe's agent - no developer mode needed"; $agentDone = $true }
        else { Say "  agent did not confirm" }
    }
    # 5b) agent absent or refused, but we are admin: write the SYSTEM
    # registration ourselves - files in the machine-wide Plugins\External plus
    # a row in the SYSTEM premierepro.json, byte-for-byte the shape the agent
    # writes ($systemPlugins token, backslashes, compact JSON, no BOM). This
    # is the registration Premiere provably loads.
    if (-not $agentDone -and $isAdmin) {
        try {
            $sysPlugRoot = Join-Path $env:ProgramFiles "Common Files\Adobe\UXP"
            $sysDest = Join-Path $sysPlugRoot ("Plugins\External\" + $PluginFolder)
            New-Item -ItemType Directory -Force -Path $sysDest | Out-Null
            Copy-Item (Join-Path $srcPanel "*") $sysDest -Force -Exclude "*.ccx"
            $sysRegDir = Join-Path $sysPlugRoot "PluginsInfo\v1"
            if (-not (Test-Path $sysRegDir)) { New-Item -ItemType Directory -Force -Path $sysRegDir | Out-Null }
            $sysRegFile = Join-Path $sysRegDir "premierepro.json"
            if (-not (Test-Path $sysRegFile)) {
                [IO.File]::WriteAllText($sysRegFile, '{"plugins":[]}', (New-Object Text.UTF8Encoding $false))
            }
            $sj = Get-Content $sysRegFile -Raw | ConvertFrom-Json
            $skeep = @()
            if ($sj.plugins) { $skeep = @($sj.plugins | Where-Object { $_.pluginId -ne "com.colourmatik.panel" }) }
            $skeep += [pscustomobject][ordered]@{
                hostMinVersion = $hostMin
                name           = "colourMatik"
                path           = ('$systemPlugins\External\' + $PluginFolder)
                pluginId       = "com.colourmatik.panel"
                status         = "enabled"
                type           = "uxp"
                versionString  = $curVer
            }
            $sj.plugins = $skeep
            [IO.File]::WriteAllText($sysRegFile, ($sj | ConvertTo-Json -Depth 10 -Compress), (New-Object Text.UTF8Encoding $false))
            Say ("  registered SYSTEM-level -> " + '$systemPlugins\External\' + $PluginFolder + " - no developer mode needed")
        } catch { Say ("  could not write the SYSTEM registration (" + $_.Exception.Message + ")") }
    }
    Say ""
    Say "NOW: quit Premiere completely and open it again."
    Say "If the panel is missing under Window > UXP Plugins, turn on"
    Say "Settings > Plugins > 'Enable developer mode' once, then restart Premiere."
} else {
    Say "colourMatik install dir has no panel source - run colourMatik-Setup.exe once first."
}

# -- engine repair ------------------------------------------------------------
# The smoking gun on the reported machine: install dir at 1.6.6 but the RUNNING
# engine answering 0.2.0 - a leftover process from the very first install held
# port 8765, so every newer engine died at bind and the ancient one kept
# serving (no /update_now endpoint -> the panel could only download files; and
# ancient matching code -> "everything is slow"). Kill anything matching, start
# the current engine, and wait until the version it ANSWERS matches the disk.
Say ""
Say "=== ENGINE REPAIR ==="
$diskVer = $null
if (Test-Path (Join-Path $inst "version.json")) {
    $diskVer = (Get-Content (Join-Path $inst "version.json") -Raw | ConvertFrom-Json).version
}
$liveVer = $null
try { $liveVer = (Invoke-RestMethod "http://127.0.0.1:8765/version" -TimeoutSec 3).version } catch {}
if ($diskVer -and $liveVer -ne $diskVer) {
    Say ("running engine v" + $liveVer + " != installed v" + $diskVer + " - restarting it")
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "colourmatik" -and $_.Name -match "python|pythonw" } |
        ForEach-Object { Say ("  killing pid " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 2
    $vbs = Join-Path $inst "windows\engine-hidden.vbs"
    if (Test-Path $vbs) { Start-Process wscript -ArgumentList ('"' + $vbs + '"') -WindowStyle Hidden; Say "  engine start requested" }
    else { Say ("  MISSING " + $vbs + " - run colourMatik-Setup.exe once") }
    $ok = $null
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep 2
        try {
            $nv = (Invoke-RestMethod "http://127.0.0.1:8765/version" -TimeoutSec 2).version
            if ($nv -eq $diskVer) { $ok = $nv; break }
        } catch {}
    }
    if ($ok) { Say ("  ENGINE NOW v" + $ok + " - all good") }
    else { Say "  engine did not come up in 90s - it can need a few minutes on first run (AI libraries); check again with: irm http://127.0.0.1:8765/version" }
} elseif ($diskVer) {
    Say ("engine already matches the install (v" + $diskVer + ")")
}

$rep = Join-Path ([Environment]::GetFolderPath("Desktop")) "colourmatik-diag.txt"
$lines | Set-Content -Path $rep -Encoding UTF8
Say ""
Say ("Report saved to " + $rep + " - send a photo of THIS window if anything still looks wrong.")

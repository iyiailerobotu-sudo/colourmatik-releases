; colourMatik - Windows Setup.exe (NSIS). One file, double-click, installs.
; Self-contained: the whole program is packed INSIDE this file (build-setup.ps1
; stages it into windows\setup\payload), so installing never downloads anything
; from GitHub. The installer then runs PHASE BY PHASE (prereqs / code / panel /
; effect / engine / autostart) so the progress bar advances between stages and
; the log streams live.
;
;   colourMatik-windows-setup.exe /S /EXTRACT=<folder>
; only unpacks the program into <folder> and exits (0 = done, 2 = failed): the
; in-app updater downloads this same file and takes the new version out of it,
; so a release is one file.
;
; Build: powershell -NoProfile -ExecutionPolicy Bypass -File windows\setup\build-setup.ps1
; by Sevki Bugra Ozbek - catheadai.com

Unicode true
!ifndef VERSION
  !error "Build with windows\setup\build-setup.ps1: it stages the payload and passes /DVERSION=x.y.z"
!endif
Name "colourMatik"
; Stamp the real version on the installer itself. Without it Windows showed the
; NSIS default (1.0.0) and macOS showed a frozen 1.2.0, so a user who checked
; the DOWNLOADED FILE concluded the site was still serving an ancient build —
; even though it installs the current one.
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "colourMatik"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "catheadai"
VIAddVersionKey "FileDescription" "colourMatik Setup"
VIAddVersionKey "LegalCopyright" "Sevki Bugra Ozbek"
; Same name as the fixed download (release tag windows-latest), so what the
; build produces is exactly what gets uploaded - no renaming step to forget.
OutFile "colourMatik-windows-setup.exe"
InstallDir "$LOCALAPPDATA\colourMatik"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
BrandingText "colourMatik  -  catheadai.com"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!define MUI_ICON   "colourMatik.ico"
!define MUI_UNICON "colourMatik.ico"
!define MUI_WELCOMEPAGE_TITLE "Install colourMatik ${VERSION} for Premiere Pro"
!define MUI_WELCOMEPAGE_TEXT  "This sets up colourMatik on your PC: the local engine, the Premiere panel, the After Effects panel and the native effect.$\r$\n$\r$\nIt installs Python and ffmpeg if they are missing and downloads the engine's Python packages, so it takes a few minutes. The progress bar shows each stage.$\r$\n$\r$\nClick Install to begin."
!define MUI_FINISHPAGE_TITLE  "colourMatik is installed"
!define MUI_FINISHPAGE_TEXT   "Restart Premiere Pro, then open  Window > UXP Plugins > colourMatik.$\r$\n$\r$\nPick a reference clip and a target clip, then Match and Apply."
!define MUI_FINISHPAGE_LINK   "catheadai.com"
!define MUI_FINISHPAGE_LINK_LOCATION "https://catheadai.com"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

; 64-bit PowerShell for every phase. This installer is a 32-bit program, and a
; plain "powershell" started from it is the 32-bit one (WOW64 redirects System32
; to SysWOW64). There $env:ProgramFiles is "C:\Program Files (x86)": the panel
; step never found Adobe's plugin agent and wrote its machine-wide registration
; under Program Files (x86)\Common Files\Adobe\UXP, which 64-bit Premiere never
; reads - so a Setup install only ever had the per-user fallback. Sysnative is
; the 32-bit process's window onto the real 64-bit System32.
Var PSEXE
Var SRC          ; where the program is unpacked; the phases run from here

; The program itself. The ONE place the payload is added, so this file holds it
; once however many places unpack it.
Function UnpackPayload
  SetOutPath "$SRC"
  File /r "payload\*.*"
FunctionEnd

Function .onInit
  StrCpy $PSEXE "powershell"
  IfFileExists "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" 0 +2
    StrCpy $PSEXE "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"

  ; /EXTRACT=<folder>: unpack only - the updater's way in. No pages and no
  ; install steps (so no admin work either).
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/EXTRACT=" $SRC
  IfErrors extract_no
  SetSilent silent
  ; a quoted path may arrive with its quotes still on
  StrCpy $1 $SRC 1
  StrCmp $1 '"' 0 +2
    StrCpy $SRC $SRC "" 1
  StrCpy $1 $SRC 1 -1
  StrCmp $1 '"' 0 +2
    StrCpy $SRC $SRC -1
  StrCmp $SRC "" extract_fail
  ClearErrors
  Call UnpackPayload
  IfErrors extract_fail
  IfFileExists "$SRC\version.json" 0 extract_fail
  SetErrorLevel 0
  Quit
  extract_fail:
  SetErrorLevel 2
  Quit
  extract_no:
FunctionEnd

; Run one installer phase; $1 = exit code afterwards.
!macro RunPhase PHASE
  nsExec::ExecToLog '"$PSEXE" -NoProfile -ExecutionPolicy Bypass -File "$SRC\windows\install-windows.ps1" -Phase ${PHASE}'
  Pop $1
!macroend

Section "colourMatik"
  SetDetailsPrint both

  ; --- stage 1: unpack the program (it travels inside this Setup) ------------
  DetailPrint "[ 5%] Unpacking colourMatik ${VERSION}..."
  StrCpy $SRC "$TEMP\colourmatik-src\src"
  RMDir /r "$TEMP\colourmatik-src"
  SetDetailsPrint none
  ClearErrors
  Call UnpackPayload
  SetDetailsPrint both
  IfErrors unpack_fail
  IfFileExists "$SRC\windows\install-windows.ps1" unpack_ok
  unpack_fail:
    MessageBox MB_OK|MB_ICONEXCLAMATION "Could not unpack colourMatik into $SRC. Free up some disk space, then run Setup again." /SD IDOK
    Abort
  unpack_ok:
  ; Run the phases from a neutral folder. Unpacking made $SRC this process's
  ; working directory, every phase inherits it, and the engine launcher the
  ; last phase starts was still sitting in it when the copy was removed.
  SetOutPath "$TEMP"

  ; --- stage 2: prerequisites ------------------------------------------------
  DetailPrint "[15%] Installing prerequisites (Python 3.11, ffmpeg)..."
  !insertmacro RunPhase "prereqs"
  StrCmp $1 "0" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Prerequisite install failed (code $1). Install 'App Installer' from the Microsoft Store, then run Setup again." /SD IDOK
    Abort

  ; --- stage 3: place the code ----------------------------------------------
  DetailPrint "[22%] Placing colourMatik in your user folder..."
  !insertmacro RunPhase "code"
  StrCmp $1 "0" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Could not place colourMatik in your user folder (code $1). Run Setup again." /SD IDOK
    Abort

  ; --- stage 4: Premiere panel (quick + reliable — BEFORE the engine's package
  ;     download, so a flaky connection there never leaves the user with no panel)
  DetailPrint "[30%] Installing the Premiere panel..."
  !insertmacro RunPhase "panel"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: panel install returned code $1 (you can re-run windows\install-panel.ps1 later)."

  ; --- stage 5: native effect ---------------------------------------------------
  DetailPrint "[38%] Installing the colourMatik effect..."
  !insertmacro RunPhase "effect"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: effect install returned code $1 (you can re-run windows\install-effect.ps1 later)."

  ; --- stage 6: engine (the long one) — a failure here does NOT abort; the panel
  ;     + effect are already installed, so the user can finish later ---------------
  DetailPrint "[45%] Setting up the engine. This is the long stage: it downloads"
  DetailPrint "      the engine's Python packages. The log below keeps streaming."
  !insertmacro RunPhase "engine"
  StrCmp $1 "0" +2 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "The engine setup didn't finish (code $1). The Premiere panel and effect ARE installed. Run Setup again on a stable connection to finish the engine.$\r$\nHelp: catheadai.com" /SD IDOK

  ; --- stage 7: engine autostart -------------------------------------------------
  DetailPrint "[97%] Starting the engine + enabling autostart..."
  !insertmacro RunPhase "autostart"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: autostart setup returned code $1."

  ; the program now lives in the user folder; drop the unpacked copy
  RMDir /r "$TEMP\colourmatik-src"
  DetailPrint "[100%] Done. Restart Premiere Pro."
SectionEnd

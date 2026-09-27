; colourMatik - Windows Setup.exe (NSIS). One file, double-click, installs.
; Bootstrapper: downloads the latest colourMatik and runs the installer PHASE BY
; PHASE (prereqs / code / engine+AI / panel / effect / autostart) so the progress
; bar advances between stages and the log streams live percent markers.
; by Sevki Bugra Ozbek - catheadai.com

Unicode true
Name "colourMatik"
; Stamp the real version on the installer itself. Without it Windows showed the
; NSIS default (1.0.0) and macOS showed a frozen 1.2.0, so a user who checked
; the DOWNLOADED FILE concluded the site was still serving an ancient build —
; even though it installs the current one.
VIProductVersion "1.8.1.0"
VIAddVersionKey "ProductName" "colourMatik"
VIAddVersionKey "ProductVersion" "1.8.1"
VIAddVersionKey "FileVersion" "1.8.1"
VIAddVersionKey "CompanyName" "catheadai"
VIAddVersionKey "FileDescription" "colourMatik Setup"
VIAddVersionKey "LegalCopyright" "Sevki Bugra Ozbek"
OutFile "colourMatik-Setup.exe"
InstallDir "$LOCALAPPDATA\colourMatik"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
BrandingText "colourMatik  -  catheadai.com"

!include "MUI2.nsh"
!define MUI_ICON   "colourMatik.ico"
!define MUI_UNICON "colourMatik.ico"
!define MUI_WELCOMEPAGE_TITLE "Install colourMatik for Premiere Pro"
!define MUI_WELCOMEPAGE_TEXT  "This sets up colourMatik on your PC: the local engine + AI, the Premiere panel, and the native effect.$\r$\n$\r$\nThe AI download is a few GB, so it takes 10-20 minutes. The progress bar shows each stage.$\r$\n$\r$\nClick Install to begin."
!define MUI_FINISHPAGE_TITLE  "colourMatik is installed"
!define MUI_FINISHPAGE_TEXT   "Restart Premiere Pro, then open  Window > UXP Plugins > colourMatik.$\r$\n$\r$\nPick a reference clip and a target clip, then Match and Apply."
!define MUI_FINISHPAGE_LINK   "catheadai.com"
!define MUI_FINISHPAGE_LINK_LOCATION "https://catheadai.com"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

; Run one installer phase; $1 = exit code afterwards.
!macro RunPhase PHASE
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$TEMP\colourmatik-src\src\windows\install-windows.ps1" -Phase ${PHASE}'
  Pop $1
!macroend

Section "colourMatik"
  SetDetailsPrint both
  SetOutPath "$TEMP\colourMatik-setup"

  ; --- stage 1: download the latest code -----------------------------------
  DetailPrint "[ 5%] Downloading colourMatik..."
  FileOpen $0 "$TEMP\colourMatik-setup\dl.ps1" w
  FileWrite $0 "$$ErrorActionPreference='Stop'$\r$\n"
  FileWrite $0 "$$z=Join-Path $$env:TEMP 'colourmatik-src.zip'$\r$\n"
  FileWrite $0 "Invoke-WebRequest 'https://github.com/iyiailerobotu-sudo/colourmatik-releases/archive/refs/heads/main.zip' -OutFile $$z -UseBasicParsing$\r$\n"
  FileWrite $0 "$$d=Join-Path $$env:TEMP 'colourmatik-src'$\r$\n"
  FileWrite $0 "if(Test-Path $$d){Remove-Item -Recurse -Force $$d}$\r$\n"
  FileWrite $0 "Expand-Archive $$z $$d$\r$\n"
  FileWrite $0 "$$inner=Get-ChildItem $$d -Directory | Select-Object -First 1$\r$\n"
  FileWrite $0 "Rename-Item $$inner.FullName 'src'$\r$\n"
  FileClose $0
  nsExec::ExecToLog 'powershell -NoProfile -ExecutionPolicy Bypass -File "$TEMP\colourMatik-setup\dl.ps1"'
  Pop $1
  StrCmp $1 "0" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Could not download colourMatik (code $1). Check your internet connection and run Setup again."
    Abort

  ; --- stage 2: prerequisites ------------------------------------------------
  DetailPrint "[15%] Installing prerequisites (Python 3.11, git, ffmpeg)..."
  !insertmacro RunPhase "prereqs"
  StrCmp $1 "0" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Prerequisite install failed (code $1). Install 'App Installer' from the Microsoft Store, then run Setup again."
    Abort

  ; --- stage 3: place the code ----------------------------------------------
  DetailPrint "[22%] Placing colourMatik in your user folder..."
  !insertmacro RunPhase "code"
  StrCmp $1 "0" +3 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "Could not fetch the colourMatik code (code $1). Check your internet connection and run Setup again."
    Abort

  ; --- stage 4: Premiere panel (quick + reliable — BEFORE the big download, so a
  ;     flaky connection on the engine stage never leaves the user with no panel) -
  DetailPrint "[30%] Installing the Premiere panel..."
  !insertmacro RunPhase "panel"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: panel install returned code $1 (you can re-run windows\install-panel.ps1 later)."

  ; --- stage 5: native effect ---------------------------------------------------
  DetailPrint "[38%] Installing the colourMatik effect..."
  !insertmacro RunPhase "effect"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: effect install returned code $1 (you can re-run windows\install-effect.ps1 later)."

  ; --- stage 6: engine + AI (the long one) — a failure here NO LONGER aborts;
  ;     the panel + effect are already installed, so the user can finish later ----
  DetailPrint "[45%] Setting up the engine + AI. This is the long stage:"
  DetailPrint "      the AI download is a few GB (10-20 minutes). The log below keeps streaming."
  !insertmacro RunPhase "engine"
  StrCmp $1 "0" +2 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "The engine/AI download didn't finish (code $1). The Premiere panel and effect ARE installed. Re-run Setup on a stable connection to finish the engine.$\r$\nManual: github.com/iyiailerobotu-sudo/colourmatik-releases"

  ; --- stage 7: engine autostart -------------------------------------------------
  DetailPrint "[97%] Starting the engine + enabling autostart..."
  !insertmacro RunPhase "autostart"
  StrCmp $1 "0" +2 0
    DetailPrint "      WARNING: autostart setup returned code $1."

  DetailPrint "[100%] Done. Restart Premiere Pro."
SectionEnd

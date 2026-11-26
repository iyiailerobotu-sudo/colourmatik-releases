' colourMatik - runs the engine with no console window, and keeps it running.
' Started by the Startup shortcut at login, by Setup and by the updater.
' Engine output goes to %LOCALAPPDATA%\colourMatik\engine.log.
'
' If the engine stops (a crash, a killed process) it is started again within
' seconds - what launchd's KeepAlive does on the Mac. Before 1.8.5 this script
' started the engine once and left, so an engine that died stayed dead until
' the next login and the panel said "Can't reach the engine" all day.
'
' One keeper per user: keeper.lock (in %APPDATA%\colourMatik) stays open for
' writing while it runs and no other keeper can open it. A new keeper waits up
' to 30 s for it - the keeper it replaces may still be leaving - then gives up.
' Whoever stops the engine on purpose (the updater, the uninstaller, Setup,
' diag) first writes keeper.stop: the keeper then leaves instead of starting the
' engine again, and the next keeper to take the lock removes the file.
Option Explicit
Dim sh, fso, root, logDir, dataDir, lockPath, stopPath, py, cmd, lock, i, t0, ranFor, pause

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
logDir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\colourMatik"
dataDir = sh.ExpandEnvironmentStrings("%APPDATA%") & "\colourMatik"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
If Not fso.FolderExists(dataDir) Then fso.CreateFolder(dataDir)
lockPath = dataDir & "\keeper.lock"
stopPath = dataDir & "\keeper.stop"
py = root & "\.venv\Scripts\python.exe"
cmd = "cmd /c cd /d """ & root & """ && "".venv\Scripts\python.exe"" -u -m colourmatik.webapp >> """ & logDir & "\engine.log"" 2>&1"

' The engine knows it is kept (GET /version says "supervised"), so its
' "Restart engine" can simply exit.
sh.Environment("Process")("COLOURMATIK_KEEPER") = "1"

Set lock = Nothing
For i = 1 To 30
  On Error Resume Next
  Set lock = fso.OpenTextFile(lockPath, 2, True)
  If Err.Number <> 0 Then Set lock = Nothing
  Err.Clear
  On Error GoTo 0
  If Not lock Is Nothing Then Exit For
  WScript.Sleep 1000
Next
If lock Is Nothing Then WScript.Quit 0
On Error Resume Next
If fso.FileExists(stopPath) Then fso.DeleteFile stopPath, True
Err.Clear
On Error GoTo 0

pause = 3
Do
  If fso.FileExists(stopPath) Then Exit Do
  If Not fso.FileExists(py) Then Exit Do              ' uninstalled or moved
  If EngineAnswers() Then
    ' Another engine is serving (one started by hand in a console, or by an
    ' updater older than 1.8.5): watch it, and take over when it is gone.
    WScript.Sleep 15000
  Else
    t0 = Timer
    sh.Run cmd, 0, True                               ' returns when the engine stops
    ranFor = Timer - t0
    If ranFor < 0 Then ranFor = ranFor + 86400        ' Timer restarts at midnight
    If fso.FileExists(stopPath) Then Exit Do
    ' An engine that stops at once (port taken, broken environment) is
    ' retried less and less often, up to every 5 minutes.
    If ranFor > 120 Then
      pause = 3
    Else
      pause = pause * 2
      If pause > 300 Then pause = 300
    End If
    WScript.Sleep pause * 1000
  End If
Loop
lock.Close
WScript.Quit 0

Function EngineAnswers()
  Dim x
  EngineAnswers = False
  On Error Resume Next
  Set x = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  x.setTimeouts 1000, 1000, 2000, 2000
  x.open "GET", "http://127.0.0.1:8765/version", False
  x.send
  If Err.Number = 0 Then EngineAnswers = (x.status = 200)
  Err.Clear
  On Error GoTo 0
End Function

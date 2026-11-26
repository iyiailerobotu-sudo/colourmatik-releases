-- colourMatik Engine.app - starts the local colourMatik engine.
-- The panels open it from "Start engine" when the engine does not answer:
-- Premiere's panel cannot run commands itself. build-app.sh compiles it into
-- the program (mac/colourMatik Engine.app), signed with the installer.
--
-- The engine is a launchd job (com.colourmatik.engine, KeepAlive). When launchd
-- holds it, a kickstart is all it needs; when it does not (an install older
-- than 1.8.5, or a login item switched off), the job is registered again.

set label to "com.colourmatik.engine"
set uid to do shell script "id -u"
set plist to (POSIX path of (path to home folder)) & "Library/LaunchAgents/" & label & ".plist"
set svc to "gui/" & uid & "/" & label

if (do shell script "test -f " & quoted form of plist & " && echo yes || echo no") is "no" then
	display dialog "colourMatik's engine is not installed on this Mac. Run the colourMatik installer again." buttons {"OK"} default button "OK" with title "colourMatik" with icon caution
	return
end if

if (do shell script "launchctl print " & svc & " >/dev/null 2>&1 && echo yes || echo no") is "yes" then
	do shell script "launchctl kickstart " & svc & " >/dev/null 2>&1 || true"
else
	do shell script "launchctl bootstrap gui/" & uid & " " & quoted form of plist & " >/dev/null 2>&1 || true"
	if (do shell script "launchctl print " & svc & " >/dev/null 2>&1 && echo yes || echo no") is "no" then
		display dialog "macOS did not let colourMatik's engine start. Open System Settings > General > Login Items & Extensions, allow colourMatik (it may be listed as python) to run in the background, then click Start engine again." buttons {"OK"} default button "OK" with title "colourMatik" with icon caution
	end if
end if

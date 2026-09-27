-- colourMatik Installer.app — double-click to install colourMatik for Premiere Pro + After Effects.
-- Signed with Developer ID Application and notarized, so it opens with no warning.
-- Asks for the admin password once, then shows a LIVE progress bar (percent + stage)
-- while the install runs, by reading /tmp/colourMatik-progress ("PCT|CAP|message").
--
-- NOTE: the installer is launched with a plain "&" + stdin closed — NOT nohup.
-- nohup breaks inside "with administrator privileges" ("can't detach from console")
-- and the install never starts. The plain-& detach is verified to survive this app.

set myPath to path to me
set scriptPOSIX to POSIX path of ((myPath as text) & "Contents:Resources:install-mac.sh")
set progressFile to "/tmp/colourMatik-progress"

set welcome to "Install colourMatik for Premiere Pro and After Effects?

This sets up the local engine, the Premiere and After Effects panels, and the native effect. Everything runs on your Mac — nothing is uploaded.

It takes a few minutes. A progress bar will show exactly where it is."

try
	display dialog welcome buttons {"Cancel", "Install"} default button "Install" cancel button "Cancel" with title "colourMatik" with icon note
on error number -128
	return
end try

-- Note the launch moment so we ignore a stale progress file from an earlier run.
set launchStamp to (do shell script "date +%s") as integer

-- One admin prompt; the installer then runs detached (as root) so this app is free
-- to show the progress window.
do shell script "/bin/bash " & quoted form of scriptPOSIX & " </dev/null >>/tmp/colourMatik-install.log 2>&1 &" with administrator privileges

-- Live progress window ---------------------------------------------------------
set progress total steps to 100
set progress completed steps to 1
set progress description to "Installing colourMatik…"
set progress additional description to "Starting…"

set shownPct to 1
set stagePct to 1
set stageCap to 4
set stageMsg to "Starting…"
set failMsg to ""
set doneOK to false
set sawProgress to false
set idleTicks to 0
set maxTicks to 3600 -- 2 s per tick = 2 hours hard stop

try
	repeat with tick from 1 to maxTicks
		delay 2
		-- read the progress file, but only once it's newer than our launch
		set pline to ""
		try
			set fresh to (do shell script "/usr/bin/stat -f %m " & quoted form of progressFile & " 2>/dev/null || echo 0") as integer
			if fresh ≥ launchStamp then set pline to (do shell script "/bin/cat " & quoted form of progressFile & " 2>/dev/null || true")
		end try
		if pline is not "" then
			set AppleScript's text item delimiters to "|"
			set parts to text items of pline
			set AppleScript's text item delimiters to ""
			if (count of parts) ≥ 3 then
				set p1 to item 1 of parts
				if p1 is "FAIL" then
					set failMsg to item 3 of parts
					exit repeat
				end if
				try
					set newPct to p1 as integer
					set newCap to (item 2 of parts) as integer
					set newMsg to item 3 of parts
					set sawProgress to true
					if newPct ≥ 100 then
						set doneOK to true
						exit repeat
					end if
					if newMsg is not stageMsg or newPct > stagePct then
						-- new stage: jump the bar and reset the creep
						set stagePct to newPct
						set stageCap to newCap
						set stageMsg to newMsg
						if newPct > shownPct then set shownPct to newPct
						set idleTicks to 0
					end if
				end try
			end if
		end if
		-- watchdog: if the installer hasn't reported ANYTHING within 90 s, it never
		-- started — say so loudly instead of sitting on a fake bar forever.
		if (not sawProgress) and tick ≥ 45 then exit repeat
		-- creep: keep the bar visibly alive inside a long stage (1 % / ~14 s, capped)
		set idleTicks to idleTicks + 1
		if idleTicks ≥ 7 and shownPct < (stageCap - 1) then
			set shownPct to shownPct + 1
			set idleTicks to 0
		end if
		set progress completed steps to shownPct
		set progress additional description to stageMsg & "  (" & shownPct & "%)"
	end repeat
on error number -128
	-- user clicked Stop
	if sawProgress then
		display dialog "The installer window was closed, but the install keeps running in the background.

You'll get a notification when it's ready (a few minutes). Then restart Premiere Pro and After Effects." buttons {"OK"} default button 1 with title "colourMatik" with icon note
	end if
	return
end try

if doneOK then
	set progress completed steps to 100
	set progress additional description to "Done  (100%)"
	display dialog "colourMatik is installed. 🦎

Restart Premiere Pro and After Effects, then open:
   Premiere Pro:  Window ▸ UXP Plugins ▸ colourMatik
   After Effects:  Window ▸ Extensions ▸ colourMatik

Pick a reference clip and a target clip, then Match & Apply." buttons {"Great"} default button 1 with title "colourMatik" with icon note
else if failMsg is not "" then
	display dialog "colourMatik couldn't finish installing.

Problem: " & failMsg & "

Check your internet connection and run the installer again. Details: /tmp/colourMatik-install.log" buttons {"OK"} default button 1 with title "colourMatik" with icon stop
else if not sawProgress then
	display dialog "The installer couldn't start.

Please run the installer again. If it happens twice, send me the file /tmp/colourMatik-install.log" buttons {"OK"} default button 1 with title "colourMatik" with icon stop
else
	display dialog "The install is taking unusually long, but may still be running in the background.

You'll get a notification if it finishes. Details: /tmp/colourMatik-install.log" buttons {"OK"} default button 1 with title "colourMatik" with icon caution
end if

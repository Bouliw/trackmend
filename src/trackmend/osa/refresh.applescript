-- Make Music read the tags of the given tracks again from their files.
-- usage: osascript refresh.applescript PID [PID ...]
-- JavaScript for Automation crashes on "refresh", so this one stays in AppleScript.
on run argv
	set okCount to 0
	set failed to {}
	tell application "Music"
		set lib to library playlist 1
		repeat with pid in argv
			try
				refresh (first file track of lib whose persistent ID is (pid as text))
				set okCount to okCount + 1
			on error
				set end of failed to (pid as text)
			end try
		end repeat
	end tell
	set AppleScript's text item delimiters to ","
	return (okCount as text) & "|" & (failed as text)
end run

-- Add audio files to the library and print the persistent ID of each new track.
-- usage: osascript add.applescript PATH [PATH ...]
on run argv
	set out to {}
	tell application "Music"
		repeat with p in argv
			try
				set t to add (POSIX file (p as text)) to library playlist 1
				set end of out to (persistent ID of t)
			on error errMsg
				set end of out to ("ERROR " & errMsg)
			end try
		end repeat
	end tell
	set AppleScript's text item delimiters to linefeed
	return out as text
end run

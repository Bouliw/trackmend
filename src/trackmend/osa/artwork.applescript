-- Set or remove the artwork of a track through Music itself. Writing the image into the
-- file and asking Music to refresh is not enough: Music keeps showing its cached image.
-- usage: osascript artwork.applescript set PID IMAGE_PATH
--        osascript artwork.applescript delete PID
on run argv
	set cmd to item 1 of argv
	set pid to item 2 of argv
	if cmd is "set" then
		-- the image is always a JPEG file (trackmend converts PNG first)
		set img to read (POSIX file (item 3 of argv)) as JPEG picture
	end if
	tell application "Music"
		set t to first file track of library playlist 1 whose persistent ID is pid
		if cmd is "set" then
			set data of artwork 1 of t to img
		else
			delete artworks of t
		end if
	end tell
	return "ok"
end run

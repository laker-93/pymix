# pymix

depends on navidrome
must enable 'report real path' option in navidrome
settings -> players -> click on name of player -> toggle on option for 'report real path'
this is so that pymix can get the real path of a track from what's in the navidrome app and map it to the track's full path on disk so that it can be properly loaded in to the rekordbox xml

## create navidrome structure from rekordbox
create the navidrome collection and playlist structure from a rekordbox collection
1. backup rekordbox collection
   2. file -> Library -> Backup Library -> when asked if you want to backup music files as well, click yes.
   3. this creates a 'rekordbox_bak' directory with your music files in
4. create rekordbox xml with the meta data of your playlist structure in rekordbox
   5. file -> export collection in xml format
6. Now need to provide both of these to subbox.
7. subbox will take the folder containing the music files and tag it and import it to the data directory navidrome uses
   8. This uses beets for the import
9. The tracks now appear in navidrome UI.
10. subbox then takes the rekordbox xml and creates playlists in navidrome. I
11. It matches the tracks from the XML against the library imported by beets and moves and tracks in to the matched playlists

## todo
1. during beets import stage, user needs to provide input to resolve imports
2. what to do if subbox cannot find a song in navidrome that matches a track in the rekordbox xml? Need a way for user to manually specify
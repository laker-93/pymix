# pymix

depends on navidrome
must enable 'report real path' option in navidrome
settings -> players -> click on name of player -> toggle on option for 'report real path'
this is so that pymix can get the real path of a track from what's in the navidrome app and map it to the track's full path on disk so that it can be properly loaded in to the rekordbox xml

at its core, this is an ETL application that transforms between subsonic (as supported by navidrome) and rekordbox.

Phase 1:

each user made their own beets and navidrome instance in a user directory. This way do not have to deal with handling
tracks across users.

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

## When importing sub box produced xml in to rb

set the xml path in rb to the xml output file produced by sub box
then follow the steps in this video

https://www.youtube.com/watch?v=xzW0jHWSNPk
## todo
0. when doing first import of a rekord box export, skip over any tracks that do not have an artist set. These have caused issues with rekordbox import with following failures (Appendix A)
1. first beets import is done in quiet mode with 'asis' fallback. API added to navidrome to allow users to add the music brainz tag which then triggers beets import on the updated music.
2. what to do if subbox cannot find a song in navidrome that matches a track in the rekordbox xml? Need a way for user to manually specify
3. add mbsync to keep library up to date (can be configured to run as a job) https://beets.readthedocs.io/en/stable/plugins/mbsync.html
4. single entry point for uploading/downloading music files/rekordbox xml through the filebrowser docker


### Appendix A
Error log from a beets import containing numerous [Unknown Artist]/[Unknown Album] due to not having artist or album set in rb.

```
The content of stderr is 'Traceback (most recent call last):
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 574, in substitute
    res = self.compiled(values, functions)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 598, in wrapper_func
    args[VARIABLE_PREFIX + varname] = values[varname]
                                      ~~~~~~^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 428, in __getitem__
    value = self._get(key)
            ^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 415, in _get
    if self.for_path and key in self.album_keys:
                                ^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/__init__.py", line 1081, in wrapper
    value = func(self)
            ^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 396, in album_keys
    if self.album:
       ^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 409, in album
    return self.item._cached_album
           ^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 581, in _cached_album
    self.__album.load()
  File "/lsiopy/lib/python3.11/site-packages/beets/dbcore/db.py", line 562, in load
    assert stored_obj is not None, f"object {self.id} not in DB"
           ^^^^^^^^^^^^^^^^^^^^^^
AssertionError: object 8 not in DB

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/lsiopy/bin/beet", line 8, in <module>
    sys.exit(main())
             ^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/ui/__init__.py", line 1285, in main
    _raw_main(args)
  File "/lsiopy/lib/python3.11/site-packages/beets/ui/__init__.py", line 1272, in _raw_main
    subcommand.func(lib, suboptions, subargs)
  File "/lsiopy/lib/python3.11/site-packages/beets/ui/commands.py", line 973, in import_func
    import_files(lib, paths, query)
  File "/lsiopy/lib/python3.11/site-packages/beets/ui/commands.py", line 943, in import_files
    session.run()
  File "/lsiopy/lib/python3.11/site-packages/beets/importer.py", line 340, in run
    pl.run_parallel(QUEUE_SIZE)
  File "/lsiopy/lib/python3.11/site-packages/beets/util/pipeline.py", line 446, in run_parallel
    raise exc_info[1].with_traceback(exc_info[2])
  File "/lsiopy/lib/python3.11/site-packages/beets/util/pipeline.py", line 358, in run
    self.coro.send(msg)
  File "/lsiopy/lib/python3.11/site-packages/beets/util/pipeline.py", line 170, in coro
    task = func(*(args + (task,)))
           ^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/importer.py", line 1566, in manipulate_files
    task.manipulate_files(
  File "/lsiopy/lib/python3.11/site-packages/beets/importer.py", line 757, in manipulate_files
    item.move(operation)
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 915, in move
    dest = self.destination(basedir=basedir)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 979, in destination
    subpath = self.evaluate_template(subpath_tmpl, True)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/dbcore/db.py", line 625, in evaluate_template
    return template.substitute(self.formatted(for_path=for_path),
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 576, in substitute
    res = self.interpret(values, functions)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 568, in interpret
    return self.expr.evaluate(Environment(values, functions))
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 256, in evaluate
    out.append(part.evaluate(env))
               ^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/functemplate.py", line 163, in evaluate
    if self.ident in env.values:
       ^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen _collections_abc>", line 780, in __contains__
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 428, in __getitem__
    value = self._get(key)
            ^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 415, in _get
    if self.for_path and key in self.album_keys:
                                ^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/util/__init__.py", line 1081, in wrapper
    value = func(self)
            ^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 396, in album_keys
    if self.album:
       ^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 409, in album
    return self.item._cached_album
           ^^^^^^^^^^^^^^^^^^^^^^^
  File "/lsiopy/lib/python3.11/site-packages/beets/library.py", line 581, in _cached_album
    self.__album.load()
  File "/lsiopy/lib/python3.11/site-packages/beets/dbcore/db.py", line 562, in load
    assert stored_obj is not None, f"object {self.id} not in DB"
           ^^^^^^^^^^^^^^^^^^^^^^
AssertionError: object 8 not in DB
```
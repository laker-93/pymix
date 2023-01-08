from pyrekordbox import RekordboxXml


def main():
    xml = RekordboxXml("/Users/lajp/rekordbox/rekordbox_original.xml")
    pl = xml.add_playlist('bar4')
    track = xml.add_track("/Users/lajp/music/Roza Terenzi & D. Tiffany/Edge of Innocence/03 Liquorice Skritch.mp3", TrackID=902, Name='Enna')
    pl.add_track(track.TrackID)
    xml.save("/Users/lajp/Downloads/rekordbox_collection.xml")


if __name__ == '__main__':
    main()

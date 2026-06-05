from pyrekordbox import RekordboxXml


def read():
    #xml = RekordboxXml("/Users/lukepurnell/Documents/rekordbox-test-250526.xml")
    xml = RekordboxXml("/Users/lukepurnell/Downloads/rekordbox_xml.xml")
    tracks = xml.get_tracks()
    print(len(tracks))
    #for track in tracks:
    #    print(track.Name, track.Artist, track.Album, track.Location, track.AverageBpm)

def write():
    xml = RekordboxXml("/Users/lajp/rekordbox/rekordbox_original.xml")
    pl = xml.add_playlist('bar4')
    track = xml.add_track("/Users/lajp/music/Roza Terenzi & D. Tiffany/Edge of Innocence/03 Liquorice Skritch.mp3", TrackID=902, Name='Enna')
    pl.add_track(track.TrackID)
    xml.save("/Users/lajp/Downloads/rekordbox_collection.xml")


if __name__ == '__main__':
    read()

from pymix.utils.navidrome_scan import scan_targets


def test_one_target_per_folder_sorted():
    assert scan_targets(['B/Album/02.mp3', 'A/EP/01.mp3', 'B/Album/01.mp3']) == ['1:A/EP', '1:B/Album']


def test_a_file_at_the_library_root_means_a_full_scan():
    # No folder to name: the caller scans the whole library.
    assert scan_targets(['A/EP/01.mp3', 'loose.mp3']) is None


def test_no_files_no_targets():
    assert scan_targets([]) is None


def test_a_folder_name_with_a_colon_is_kept_whole():
    # Navidrome splits a target on its first colon only (strings.Cut in
    # model.ParseTargets, 0.60.3): library id, then the path as given.
    assert scan_targets(['Artist: Live/Set/01.mp3']) == ['1:Artist: Live/Set']

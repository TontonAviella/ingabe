"""Upload keys (built and checked in one place) and safe ZIP unpacking."""

import os
import stat
import zipfile

import pytest

from src.upload.archive import ArchiveTooLarge, extract_zip, member_names
from src.upload.keys import NotYourUpload, check_upload_key, upload_key


def test_a_key_built_for_an_upload_passes_its_own_check():
    key = upload_key("user_01H", "Pabc", "Lxyz", ".tif")
    assert key == "uploads/user_01H/Pabc/Lxyz.tif"
    check_upload_key(key, "user_01H", project_id="Pabc", layer_id="Lxyz")
    check_upload_key("uploads/user_01H/Pabc/Lxyz.tif.gz", "user_01H")


@pytest.mark.parametrize("key", [
    "uploads/someone/Pabc/Lxyz.tif",            # another user
    "uploads/user_01Hx/Pabc/Lxyz.tif",          # a user id that only starts like ours
    "uploads/user_01H/Pabc/Lxyz",               # no extension
    "uploads/user_01H/Pabc/sub/Lxyz.tif",       # an extra folder
    "uploads/user_01H/../other/Lxyz.tif",       # walking out of the folder
    "uploads/user_01H//Lxyz.tif",               # an empty segment
    "drone_vision/v2/user_01H/Pabc/Lxyz.tif",   # another prefix
    "/uploads/user_01H/Pabc/Lxyz.tif",
])
def test_a_key_that_is_not_the_users_own_upload_is_refused(key):
    with pytest.raises(NotYourUpload):
        check_upload_key(key, "user_01H")


def test_project_and_layer_must_match_when_given():
    key = upload_key("u1", "Pabc", "Lxyz", ".laz")
    with pytest.raises(NotYourUpload):
        check_upload_key(key, "u1", project_id="Pother")
    with pytest.raises(NotYourUpload):
        check_upload_key(key, "u1", layer_id="Lother")


def test_a_user_id_with_regex_characters_is_matched_literally():
    with pytest.raises(NotYourUpload):
        check_upload_key("uploads/aXb/Pabc/Lxyz.tif", "a.b")


def _zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def test_a_normal_archive_unpacks_and_skips_macos_metadata(tmp_path):
    archive = _zip(tmp_path / "shp.zip", [("roads.shp", b"shp"), ("roads.dbf", b"dbf"), ("__MACOSX/._roads.shp", b"x")])
    out = tmp_path / "out"
    written = extract_zip(str(archive), str(out))
    assert sorted(os.path.basename(p) for p in written) == ["roads.dbf", "roads.shp"]


def test_a_zip_bomb_is_stopped_by_the_bytes_written_not_the_declared_size(tmp_path):
    archive = _zip(tmp_path / "bomb.zip", [("zeros.bin", b"\0" * (5 * 1024 * 1024))])  # ~5 KB compressed
    with pytest.raises(ArchiveTooLarge):
        extract_zip(str(archive), str(tmp_path / "out"), max_total_bytes=1024 * 1024)


def test_too_many_entries_are_refused_before_anything_is_written(tmp_path):
    archive = _zip(tmp_path / "many.zip", [(f"f{i}.txt", b"x") for i in range(30)])
    out = tmp_path / "out"
    with pytest.raises(ArchiveTooLarge):
        extract_zip(str(archive), str(out), max_entries=20)
    assert not out.exists() or not any(out.iterdir())


def test_paths_that_leave_the_folder_and_symbolic_links_are_not_written(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../../escape.txt", b"out")
        link = zipfile.ZipInfo("link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/etc/passwd")
        z.writestr("ok.txt", b"in")
    out = tmp_path / "out"
    written = extract_zip(str(archive), str(out))
    assert [os.path.basename(p) for p in written] == ["ok.txt"]
    assert not (tmp_path / "escape.txt").exists() and not (out / "link").exists()


def test_a_file_that_is_not_a_zip_raises_bad_zip(tmp_path):
    bad = tmp_path / "not.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        extract_zip(str(bad), str(tmp_path / "out"))


def test_only_unpacks_just_the_accepted_entries_and_counts_only_them(tmp_path):
    archive = _zip(tmp_path / "photos.zip", [("a.jpg", b"1"), ("b.jpg", b"2"), ("clip.mp4", b"\0" * 4096),
                                              ("log.mrk", b"x"), ("__MACOSX/._a.jpg", b"x")])
    assert sorted(member_names(str(archive))) == ["a.jpg", "b.jpg", "clip.mp4", "log.mrk"]
    only_jpg = lambda name: name.endswith(".jpg")  # noqa: E731
    written = extract_zip(str(archive), str(tmp_path / "out"), max_entries=2, max_total_bytes=10, only=only_jpg)
    assert sorted(os.path.basename(p) for p in written) == ["a.jpg", "b.jpg"]
    with pytest.raises(ArchiveTooLarge):
        extract_zip(str(archive), str(tmp_path / "out2"), max_entries=1, only=only_jpg)

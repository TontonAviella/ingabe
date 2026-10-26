import sqlite3

from src.services import disk_memo


def test_a_kept_value_comes_back_and_a_missing_one_is_a_miss():
    assert disk_memo.get("ns", "k") == (False, None)
    disk_memo.put("ns", "k", {"z": -0.4, "ndvi": None})
    assert disk_memo.get("ns", "k") == (True, {"z": -0.4, "ndvi": None})
    assert disk_memo.get("other", "k") == (False, None)


def test_none_is_a_kept_value_not_a_miss():
    disk_memo.put("ns", "nodata", None)
    assert disk_memo.get("ns", "nodata") == (True, None)


def test_it_outlives_the_process_memory(tmp_path, monkeypatch):
    path = tmp_path / "shared.sqlite3"
    monkeypatch.setenv("DISK_MEMO_PATH", str(path))
    disk_memo.put("ns", "k", 7)
    rows = sqlite3.connect(path).execute("SELECT namespace, key, value FROM memo").fetchall()
    assert rows == [("ns", "k", "7")]


def test_a_disk_that_cannot_be_used_is_a_miss_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DISK_MEMO_PATH", str(tmp_path / "no-such-dir" / "memo.sqlite3"))
    disk_memo.put("ns", "k", 1)  # logged, not raised
    assert disk_memo.get("ns", "k") == (False, None)

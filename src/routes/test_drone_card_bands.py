"""The band-naming routes' own rules: what counts as a rewrite still running, and which photo is never written over."""

from datetime import datetime, timedelta, timezone

from src.routes import drone_card_routes as routes


def test_a_rewrite_counts_as_running_for_two_hours():
    now = datetime.now(timezone.utc)
    assert routes._naming_now({"bands_naming": {"at": now.isoformat()}})
    assert not routes._naming_now({"bands_naming": {"at": (now - timedelta(hours=3)).isoformat()}})  # it stopped
    assert not routes._naming_now({}) and not routes._naming_now({"bands_naming": {"at": "not a time"}})


def test_the_photo_as_uploaded_is_what_a_second_naming_starts_from():
    assert routes._unnamed_key({"cog_key": "cog/a.tif"}) == "cog/a.tif"
    named = {"cog_key": "cog/a.tif.named-1234.tif", "cog_key_before_names": "cog/a.tif"}
    assert routes._unnamed_key(named) == "cog/a.tif"

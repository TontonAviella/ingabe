"""The suite never runs on the live local database, whatever else the environment says."""

import pytest

from conftest import _refuse_the_live_database


@pytest.mark.parametrize("database", ["mundidb", "", None])
def test_live_or_unset_database_is_refused(monkeypatch, database):
    if database is None:
        monkeypatch.delenv("POSTGRES_DB", raising=False)
    else:
        monkeypatch.setenv("POSTGRES_DB", database)

    with pytest.raises(pytest.exit.Exception):
        _refuse_the_live_database()


def test_the_old_disposable_flag_no_longer_lets_the_live_database_through(monkeypatch):
    # The CI command used to pass MUNDI_TEST_DB_IS_DISPOSABLE=1; copied to a
    # laptop, the same command ran the whole suite on live data.
    monkeypatch.setenv("POSTGRES_DB", "mundidb")
    monkeypatch.setenv("MUNDI_TEST_DB_IS_DISPOSABLE", "1")

    with pytest.raises(pytest.exit.Exception):
        _refuse_the_live_database()


@pytest.mark.parametrize("database", ["mundidb_ci", "mundidb_pytest_wos"])
def test_a_copy_is_allowed(monkeypatch, database):
    monkeypatch.setenv("POSTGRES_DB", database)

    _refuse_the_live_database()

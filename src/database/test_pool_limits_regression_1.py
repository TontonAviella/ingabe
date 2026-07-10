from src.database.pool import _configured_pool_size


def test_pool_default_leaves_room_for_other_services(monkeypatch):
    monkeypatch.delenv("DB_POOL_MAX_SIZE", raising=False)

    assert _configured_pool_size("DB_POOL_MAX_SIZE") == 10


def test_pool_size_never_drops_below_asyncpg_minimum(monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "1")

    assert _configured_pool_size("DB_POOL_MAX_SIZE") == 2

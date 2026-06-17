-- Corrective rewrite for the 5 PostGIS district layers.
--
-- v1 was wrong: it used `_inner.*` which exposed BOTH the new ROW_NUMBER id
-- and the original `district AS id`, producing two `id` columns. When
-- postgis_tiles.py wraps the query as `(query) t` and references `t.id`,
-- PostgreSQL errors with "column reference 'id' is ambiguous".
--
-- v2: enumerate columns explicitly. Since all 5 layers select from
-- rwanda_district_boundaries with the same shape, we can write per-layer
-- SQL with no wildcard.
--
-- Final shape per layer:
--   id     : bigint (ROW_NUMBER OVER) — required by ST_AsMVT
--   district: text (the human-readable label)
--   geom   : geometry
--
-- Attribute column list becomes {district}.

\echo '=== v2 corrective rewrite ==='

BEGIN;

-- LEgev72kquKe — Kigali City Districts
UPDATE map_layers
SET
    postgis_query = 'SELECT ROW_NUMBER() OVER (ORDER BY district, ST_AsEWKT(geom))::bigint AS id, district, geom FROM rwanda_district_boundaries WHERE district IN (''Gasabo'', ''Kicukiro'', ''Nyarugenge'')',
    postgis_attribute_column_list = ARRAY['district']::text[],
    last_edited = CURRENT_TIMESTAMP
WHERE layer_id = 'LEgev72kquKe';

-- L5rejDfzRBjx — Nyanza District
UPDATE map_layers
SET
    postgis_query = 'SELECT ROW_NUMBER() OVER (ORDER BY district, ST_AsEWKT(geom))::bigint AS id, district, geom FROM rwanda_district_boundaries WHERE district = ''Nyanza''',
    postgis_attribute_column_list = ARRAY['district']::text[],
    last_edited = CURRENT_TIMESTAMP
WHERE layer_id = 'L5rejDfzRBjx';

-- LyDbUnFRcgsW — Rusizi District
UPDATE map_layers
SET
    postgis_query = 'SELECT ROW_NUMBER() OVER (ORDER BY district, ST_AsEWKT(geom))::bigint AS id, district, geom FROM rwanda_district_boundaries WHERE district = ''Rusizi''',
    postgis_attribute_column_list = ARRAY['district']::text[],
    last_edited = CURRENT_TIMESTAMP
WHERE layer_id = 'LyDbUnFRcgsW';

-- Lbx16Ugzw8Ez — Nyagatare District
UPDATE map_layers
SET
    postgis_query = 'SELECT ROW_NUMBER() OVER (ORDER BY district, ST_AsEWKT(geom))::bigint AS id, district, geom FROM rwanda_district_boundaries WHERE district = ''Nyagatare''',
    postgis_attribute_column_list = ARRAY['district']::text[],
    last_edited = CURRENT_TIMESTAMP
WHERE layer_id = 'Lbx16Ugzw8Ez';

-- Ldp4xokKtC1h — Rwanda Districts NDVI (all 30 districts)
UPDATE map_layers
SET
    postgis_query = 'SELECT ROW_NUMBER() OVER (ORDER BY district, ST_AsEWKT(geom))::bigint AS id, district, geom FROM rwanda_district_boundaries',
    postgis_attribute_column_list = ARRAY['district']::text[],
    last_edited = CURRENT_TIMESTAMP
WHERE layer_id = 'Ldp4xokKtC1h';

-- Verify each rewritten query: must be runnable and t.id must NOT be ambiguous.
DO $$
DECLARE
    r RECORD;
    _sql TEXT;
BEGIN
    FOR r IN
        SELECT layer_id, name, postgis_query
        FROM map_layers
        WHERE layer_id IN (
            'LEgev72kquKe', 'L5rejDfzRBjx', 'LyDbUnFRcgsW',
            'Lbx16Ugzw8Ez', 'Ldp4xokKtC1h'
        )
    LOOP
        -- Mimic the postgis_tiles wrap: select t.id from (query) t
        _sql := format(
            'SELECT t.id, t.district FROM (%s) t ORDER BY t.id LIMIT 1',
            r.postgis_query
        );
        BEGIN
            EXECUTE _sql;
            RAISE NOTICE 'OK: % (%)', r.layer_id, r.name;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'VERIFY FAILED for % (%): %', r.layer_id, r.name, SQLERRM;
        END;
    END LOOP;
END$$;

COMMIT;

\echo '=== v2 done ==='

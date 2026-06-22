-- Deprecated compatibility wrapper.
--
-- The original v1 script dynamically wrapped existing map_layers.postgis_query
-- values with `_inner.*`, which could produce duplicate `id` columns and
-- ambiguous MVT queries. It is intentionally disabled so it cannot mutate
-- production data by accident.
--
-- Use scripts/fix_nonint_id_layers_v2.sql instead. v2 enumerates the expected
-- columns explicitly, creates deterministic integer ids, and verifies the same
-- query shape that the MVT renderer uses.

\echo 'fix_nonint_id_layers.sql is deprecated and made no database changes.'
\echo 'Run scripts/fix_nonint_id_layers_v2.sql instead.'

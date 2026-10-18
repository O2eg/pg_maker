CREATE FUNCTION pg_maker_probe() RETURNS integer
AS 'MODULE_PATHNAME', 'pg_maker_probe' LANGUAGE C STRICT;

CREATE EXTENSION pg_maker_probe;
SELECT pg_maker_probe() = 42 AS ok;

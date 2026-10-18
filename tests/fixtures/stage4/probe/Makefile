EXTENSION = pg_maker_probe
MODULES = pg_maker_probe
DATA = pg_maker_probe--1.0.sql
REGRESS = probe
PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

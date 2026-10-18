#include "postgres.h"
#include "fmgr.h"
PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(pg_maker_probe);
Datum pg_maker_probe(PG_FUNCTION_ARGS)
{
    PG_RETURN_INT32(42);
}

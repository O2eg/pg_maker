/* Expanded input for the pg_maker text insertion parser. */
struct config_bool ConfigureNamesBool[] =
{
    {
        "maker_probe_enabled",
        0
    },
    /* End-of-list marker */
    {0, 0}
};

struct config_int ConfigureNamesInt[] =
{
    {
        "maker_probe_limit",
        11
    },
    /* End-of-list marker */
    {0, 0}
};

struct config_int64 ConfigureNamesInt64[] = {
    {"maker_probe_capacity", 4294967296LL},
    /* End-of-list marker */
    {0, 0}
};

struct config_real ConfigureNamesReal[] = {
    {"maker_probe_ratio", 0.25},
    /* End-of-list marker */
    {0, 0}
};

struct config_string ConfigureNamesString[] = {
    {"maker_probe_label", "expanded"},
    /* End-of-list marker */
    {0, 0}
};

struct config_enum ConfigureNamesEnum[] = {
    {"maker_probe_mode", "sample"},
    /* End-of-list marker */
    {0, 0}
};

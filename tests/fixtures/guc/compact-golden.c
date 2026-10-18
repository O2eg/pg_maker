/* Compact input for the pg_maker text insertion parser. */
struct config_bool ConfigureNamesBool[] = {
    {"maker_probe_enabled", 1},
	/* maker bool insertion */
    /* End-of-list marker */
    {0, 0}
};

struct config_int ConfigureNamesInt[] = {
    {"maker_probe_limit", 7},
	/* maker int insertion */
    /* End-of-list marker */
    {0, 0}
};

struct config_string ConfigureNamesString[] = {
    {"maker_probe_label", "compact"},
    /* End-of-list marker */
    {0, 0}
};

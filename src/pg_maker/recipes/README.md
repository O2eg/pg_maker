# Recipes

The built-in platform is platforms/debian-13.yaml: PostgreSQL 14–19, x86_64/x86-64,
Autotools, DEB, build/test dependencies and naming policy. Custom platform/component
recipes are explicit YAML files; overriding a built-in requires an explicit path or
replace_builtin: true. A recipe is not evidence of completed build/installation
qualification.

Major-specific behavior lives in postgresql.yaml: tool minimums, C99/C11, options,
backends and DocBook XML 4.5 targets. The selected entry enters BuildLock; tool versions
are checked during environment preparation. Majors below 14 are unsupported and
unknown majors do not inherit behavior. Catalog coverage does not prove all minor/
option combinations. The source distribution includes docs/interfaces/postgresql.md.

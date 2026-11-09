# pg-maker documentation

Start with the [main README](../README.md) for installation, profile selection,
the build/export sequence and test modes. These guides describe the current source
tree; a pip release contains the documentation shipped with that version.

## Task guides

| Task | Guide |
| --- | --- |
| Build YC DEBs, upload to Nexus and install on Debian 13 | [Package walkthrough](yc-clone-packages.md) |
| Select PostgreSQL 15.18/15.19 / ICU 70.1 and extensions | [PG15 profiles](managed-15-icu70.md) |
| Understand YC clone physical compatibility requirements and gaps | [Compatibility contract](yc-clone-compatibility.md) |
| Select PostgreSQL 16.15 / ICU 70.1 and extensions | [PG16 profile](managed-16-icu70.md) |
| Select PostgreSQL 17.11 / ICU 70.1 and extensions | [PG17 profile](managed-17-icu70.md) |
| Select PostgreSQL 18.6 / ICU 70.1 and extensions | [PG18 profile](managed-18-icu70.md) |
| Edit core/extensions, maintain patches and rebuild selected components | [Development sessions](development.md) |
| Export results and reclaim build/cache space | [Disk management](development-storage.md) |
| Build or translate PostgreSQL documentation | [Documentation pipeline](interfaces/documentation.md) |
| Retry a failed operation or reuse compilation | [Recovery and cache](interfaces/recovery.md) |
| Automate builds or prepare a runtime image | [API](interfaces/api.md), [machine output](interfaces/machine.md), [runtime handoff](interfaces/runtime.md) |
| Modify and validate the Python utility | [Contributing](../CONTRIBUTING.md) |
| Release the Python package | [Release procedure](releasing.md) |

## Reference and implementation limits

[Interface index](interfaces/README.md) covers configuration, sources, environments,
plans, changes, tests, packages, documentation, lifecycle and cleanup.
[Architecture](architecture.md) describes modules and extension rules;
[reuse decisions](reuse.md) records design influences. The [roadmap](roadmap.md)
separates implemented behavior from remaining work. [Changelog](../CHANGELOG.md)
records release and unreleased changes.

Bundled targets are Debian 13 amd64. The catalog includes PostgreSQL 14–19;
database-test adapters support 15/16/17/18. ARM64 needs additional recipes/helper changes.
External curl uploads to Nexus are documented, while integrated repository publication
is unimplemented. A successful skipped-test build remains unverified.

## Qualification and historical reports

Qualification belongs to exact package bytes, locked inputs and test evidence.
It does not transfer automatically to changed paths, recipes, controller versions
or architectures. Current YC paths are `/opt/postgresql-yc-clone/<major>` and
`/var/lib/postgresql-yc-clone/<major>/data`; historical `/opt/test/...` paths below
describe earlier artifacts and are not current installation instructions.

- [PG16 qualification, 2026-11-05](managed-16-icu70-qualification-20261105.md).
- [PG17 qualification, 2026-11-06](managed-17-icu70.md#native-qualification--2026-11-06).
- [Core/source checks](validation/stage4.md), [DEB packaging](validation/stage56.md)
  and [documentation packaging](validation/stage7.md), 2026-10-17.
- [Integration](validation/integration.md), [review findings](validation/review-fixes.md),
  [major-version builds](validation/postgresql-majors.md) and
  [documentation theme](validation/documentation-theme.md), 2026-10-17.
- [Default-component build](validation/default-components.md) and
  [development/release checks](validation/development-release.md), 2026-10-18.

Reports under `validation/` retain the versions, defaults, paths and limitations
observed at their recorded date. Use current guides/contracts for new work; do not
sum overlapping suite inventories or treat omitted tests as passing evidence.

## Installed manuals

Pip installs this index and linked manuals under
`<installation-prefix>/share/doc/pg-maker`. In a virtual environment, open
`.venv/share/doc/pg-maker/README.md`. GitHub code/catalog links require network access;
profile inputs and patches are bundled in the Python package and copied by `init`.
Editable installs use the checkout for code; reinstall after documentation changes
to refresh installed copies of the manuals.

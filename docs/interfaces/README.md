# Interface specifications

These contracts describe implemented public behavior:

- [API and CLI](api.md)
- [Models and documents](documents.md)
- [Runner and subprocesses](execution.md)
- [State, events and cancellation](state.md)
- [Errors, logs and secrets](observability.md)
- [Files, hashes and package resources](storage.md)
- [Machine contract](machine.md)
- [Configuration and identity](configuration.md)
- [PostgreSQL major-version catalog](postgresql.md)
- [Sources and InputStore](sources.md)
- [Environment preparation](environments.md)
- [BuildLock and BuildPlan](planning.md)
- [ChangeSets and agent workspaces](changes.md)
- [Git sessions, patches and development iterations](development.md)
- [Default components and their documentation](components.md)
- [Documentation pipeline](documentation.md)
- [Build backend, components and test evidence](build.md)
- [InstallTree, PackageSpec, DEB and installation checks](packaging.md)
- [Resume, cache, resource guards and export](recovery.md)
- [Runtime image handoff](runtime.md)
- [Scratch cleanup](cleanup.md)

[Distribution publication](publication.md) is a design boundary with no executable
adapter yet. Python package publication has a separate [release procedure](../releasing.md).

Public behavior changes start with the corresponding MD contract and JSON Schema.
Document compatibility explicitly. Private helpers need types, docstrings and
validation at their public caller, rather than a separate specification per function.

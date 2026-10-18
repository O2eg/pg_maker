# InstallTree, PackageSpec, DEB and installation checks

The DEB adapter is implemented. Documents carry API versions; semantic changes
require contract updates. Functions currently take BuildBranch; an abstract Packager
is unnecessary until another implementation needs it.

| Boundary | Input | Output |
| --- | --- | --- |
| tree.collect | Private prefix, core inventory, selected components | InstallTree, exclusions, checked pg_config layout |
| tree.seal_tree / verify_tree | Tree and file roles | Typed inventory and tree hash |
| tree.split | Verified tree and package roles | Per-role staging trees |
| deb.dependencies | ELF files and platform recipe | SONAME/NEEDED/RPATH and role dependencies |
| spec.package_spec | Identity, vendor, dependencies, inventory | Format-neutral PackageSpec |
| spec.deb_control | PackageSpec | Shared-template control text and expected fields |
| deb.package | Staging and PackageSpec | Verified DEBs and relative artifact references |
| packages.validate_packages | DEBs, test image and runtime repository | Isolated installation/upgrade evidence |

InstallTree entries record path, type, mode, intended owner/group, size/hash or link
target. Every file has one role; common directories use shared-directory. Intended
ownership is root/root, never the host build UID. Inventory is checked before/after
packaging and compared with finished DEB payload bytes/modes/links. Escaping links,
links to excluded files, setuid/setgid and special files are rejected.

Core inventory is captured immediately after make install. Payload includes core and
explicit runtime_files of selected components; install-world-bin does not ship every
contrib automatically. Components cannot overwrite core runtime files. Headers,
PGXS, pkgconfig, ecpg/libecpg/libpgtypes, static libraries and the development libpq.so
link are excluded with reasons. The private prefix still supports PGXS compilation.
Upstream COPYRIGHT and discovered selected-component licenses are included.
Configuration and file logs belong in PGDATA.

## Universal package description

Templates receive vendor, feature and major from build parameters, without fixed
product/version strings. PackageSpec contains identity (DEB/RPM versions/releases and
native architecture), metadata, dependencies, paths, service, tree hash and exact
inventory. Naming policy computes identities; platform recipes name OS dependencies;
InstallTree alone determines contents. Changing vendor/version/path does not require
a copied template or Python packager. Metadata rejects NUL/CR/LF and substitution
never executes shell. Per-role descriptions are saved under package-specs for review.

A future RPM adapter will consume the same description/inventory with a shared spec
and native Requires/scriptlets. It must translate native version/dependency semantics;
Debian maintainer scripts will not be copied into RPM. RPM execution is not implemented.
There is no feature-specific template tree, universal DSL or plugin framework.

## DEB dependencies and lifecycle

Readelf and dpkg-shlibdeps run inside the build image. shlibs.local binds the private
libpq. ELF files require a runtime search path to their own libraries; /work or /tmp
RPATH entries fail. Server depends on exact-version client/libpq; client depends on
exact-version libpq. Discovered system dependencies must fit recipe/option/component
declarations. Missing declarations fail; unused declarations warn. Service dependencies
add passwd/coreutils/util-linux without unversioned duplicates. libpq SONAME must match
the ABI suffix. A unit file alone does not require systemd; actual --with-systemd
linkage does require libsystemd0.

Documentation Suggests is absent when docs are disabled. With docs, server suggests
the same-version docs package. HTML/PDF belong to docs; manpages belong to their command
packages. Docs can install without server. Archives use root-owner-group, are verified
independently of dpkg's exit code and are atomically renamed. See
[dpkg-deb](https://manpages.debian.org/trixie/dpkg/dpkg-deb.1.en.html) and
[dpkg-shlibdeps](https://manpages.debian.org/trixie/dpkg-dev/dpkg-shlibdeps.1.en.html).

Server postinst creates a missing account, empty PGDATA and socket directory. It enables
the unit only for initialized PGDATA with systemd available; installation never runs
initdb/start. Existing UID/GID/home remain unchanged. ExecStartPre checks PG_VERSION,
pg_control, base and ownership. The unit uses pg_ctl start/stop/reload, readiness and
explicit timeouts. Upgrade stops an active service and restarts it after configure.
Remove/purge preserve data, config, logs and account. Shared sockets use mode 2775
and tmpfiles type=d, are never RuntimeDirectory and are not removed. /tmp permissions
are untouched; tmpfiles uses canonical /run. Client/libpq have no server scripts.

## Validation and qualification

Fresh root containers test libpq-only, libpq+client and complete installations with
minimal capabilities and no network, sources, objects or build prefix. APT uses only
the hash-verified local runtime repository. Installation/runtime image builders
explicitly include /usr/share/doc/package files to preserve licenses on Debian slim.

Checks cover installation/removal, payload/loader inventory, absence of implicit
initdb, negative startup conditions and database lifecycle: socket/TCP, transactions,
components, restart, dump/restore, crash recovery, checksums, invalid permissions/
config/port/socket, reinstall, remove/install/purge and fresh acceptance clusters.
The strict systemctl harness verifies PID/UID/executable for the generated unit and
reports service_manager=emulated, native_systemd=not-tested.

Previous-release inputs declare variant, same-major version and three role/path/hash
packages. Resolve freezes bytes. A clean upgrade environment installs the predecessor,
creates sentinel data/config, upgrades and checks version/data/ownership. Missing or
changed packages fail rather than skip. Without a predecessor, first-release is
not-applicable. Major upgrades are unsupported.

Quick package remains unverified. Verified requires source verification and every
required installed check, acceptance and declared upgrade. Only candidate can become
publication-eligible, after the entire matrix including baseline succeeds. Baseline
failure blocks promotion before candidate manifests change. The internal run_database=False
fixture mode never qualifies PostgreSQL. See [executed evidence](../validation/stage56.md).

Every packaging goal preserves the pinned runtime repository and manifest, even without
docs. Exact file sets/hashes are checked; symlinks/extra files fail. trusted=yes is
limited to this verified offline local set and does not mean APT publication.

The Debian recipe declares optional lz4/zstd, XML/XSLT, PAM, systemd, UUID, PL/Perl,
PL/Python, PL/Tcl, LDAP and GSSAPI dependencies. Build options select them explicitly;
LLVM/OpenSSL/ICU defaults remain unchanged. Full LDAP/GSSAPI qualification needs
additional fixtures and remains unsupported.

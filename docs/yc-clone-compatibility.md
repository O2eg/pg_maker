# YC clone compatibility contract

Every yc-clone profile targets physical data/WAL and extension compatibility with
its recorded managed source. The required set includes the complete standard
PostgreSQL contrib and all observed non-contrib extensions. PLV8 is the explicitly
authorized exclusion. An unavailable source or dependency is a blocker, not an
implicit exclusion.

**Current status: full managed binary compatibility is not verified for any
bundled YC profile or previously produced package set.** Public upstream sources
and matching SQL versions do not identify the vendor's patches, ABI or stored
data formats. The user has no access to the exact vendor sources, patches or DEBs.
Builds remain useful candidates for qualification; their names do not certify
managed equivalence. No control/SQL version may be fabricated to conceal a gap.

## Evidence required before making a compatibility claim

1. Identify the exact source server build, architecture, major/minor, catalog and
   control/WAL format parameters. Record any vendor core changes or obtain their
   compatibility contract; do not equate compiler version with format identity.
2. Audit every available and installed extension, preload/output-plugin module,
   dependency, SQL version and data/index format. Resolve blocked catalog entries
   and managed contrib backports. A public project with the same name is not
   enough. Older maintenance profiles need their own evidence.
3. Match ICU data and every relevant locale's stored/actual collation version;
   check representative ordering/index lookups. Record libc locale differences
   separately. A matching root collation alone is not sufficient.
4. On an authorized isolated qualification stand, restore a physical backup and
   replay representative source WAL, including extension objects and indexes,
   recovery/restart, concurrent changes and query result comparisons. Confirm
   applicable data/index checks and the source/replica comparison.
5. Retain immutable source/package hashes, the test workload and results, known
   limitations and the source build against which compatibility was tested.

`source-verified` and `package-verified` are pg-maker build qualifications. They
must not be presented as proof of managed physical compatibility. Catalog
`binary_compatibility.status: not-verified` remains independent of those results.
Even a successful finite replay test establishes evidence for its documented
scope, not a universal guarantee for unknown vendor behavior.

## Recorded source catalogs

- [PostgreSQL 15](managed-15-icu70.md): 15.19 observed; 15.18/15.19 candidates.
- [PostgreSQL 16](managed-16-icu70.md): 16.15 observed; 16.14/16.15 candidates.
- [PostgreSQL 17](managed-17-icu70.md): 17.11 observed; 17.10/17.11 candidates.
- [PostgreSQL 18](managed-18-icu70.md): 18.6 observed; 18.4/18.6 candidates.

For PG15, exact anon 1.3.2.1, Oracle client dependencies, the pgvector alias and
managed amcheck/pageinspect backports remain unresolved. Full upstream contrib
stays enabled while these differences are reported explicitly.

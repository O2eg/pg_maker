# Sources and InputStore

`SourceResolver(store, directory, offline=False, cancelled=..., on_process=...)`
provides `resolve(SourceSpec, base) -> dict`. SourceSpec selects Git, snapshot or
archive. Retrieval paths and URLs do not enter the immutable result.

Git resolution uses a separate bare fetch, resolves a full commit ID, archives that
commit and recursively resolves gitlinks from `.gitmodules` up to ten levels. It
does not read dirty/untracked files or modify the original checkout. Results record
all submodule commit IDs and the combined tree's source hash. Git runs through the
shared process runner with timeout, logging and cancellation.

Snapshots use explicit include/exclude lists with `fnmatchcase-posix/v1`. Patterns
match the entire relative POSIX path; `*` can match `/`. Excluding a directory excludes
its subtree; exclusions win. Gitignore is not applied automatically: use `.git/**`
to exclude Git metadata. Selected uncommitted files are included. A snapshot cannot
be stored inside its source directory.

Archives accept a local path or HTTP(S), require SHA256 and use explicit
`strip_components` (default 0). Bytes stream into a blob, undergo checksum
verification, then extract. Supported formats are tar and gzip/bzip2/xz tar through
Python tarfile. Absolute/parent paths, duplicate members, special files and writes
through symlinks are rejected. A hardlink can reference only an already extracted
regular file. Symlinks and composed links cannot escape the tree. ZIP, arbitrary
protocols and URLs containing passwords/query credentials are unsupported.

`InputStore(root)` stores `blobs/<hash>`, `trees/<hash>` and
`resolutions/<request-hash>`. `put_blob`/`put_tree` copy and verify stable inputs,
then publish with rename/fsync under a lock. Reading a blob/tree rechecks content.
Inventory `pg_maker/tree/v1` records relative path, type, mode, file SHA256 or link
target, sorted by UTF-8 path bytes. Timestamps/uid/gid are excluded from identity;
executable bits and symlink targets are included. Copies must preserve modes/links.

Offline mode uses previously resolved SourceSpec entries. A new mutable ref or
uncached snapshot is a missing input; changed local files do not replace a cached
offline snapshot. Corrupt cache data is an error. Resolve aggregates missing inputs.
Acquiring new inputs requires online resolution; planning never invokes the resolver.

`materialize(source, destination)` makes a private build tree with preserved modes
and links and identical `mtime=1` for regular files. Timestamps do not affect source
identity. This avoids PostgreSQL 14's VPATH rule generating an empty `lwlocknames.c`
when the extracted header is newer. Normalization happens before ChangeSets;
changed generator inputs receive new mtimes and rebuild normally. CAS and the
original checkout remain unchanged.

[Development sessions](development.md) snapshot Git-tracked and nonignored files
without `.git`, then build from immutable copies. Their mutable checkouts never
replace the exact inputs of an existing operation.

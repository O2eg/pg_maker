# Files, hashes and package resources

`storage.canonical_json` uses UTF-8, sorted keys and compact separators, rejecting
NaN/Infinity. `parse_json` also rejects duplicate keys. `content_hash` hashes canonical
JSON; `file_hash` streams file bytes. Container-command input identity excludes the
local operation directory, timestamps and owner. Preparation request idempotency
includes its output location; final lock/plan semantic hashes exclude transport paths.

`write_text`/`write_json` use a same-directory temporary file with 0600 permissions,
flush/fsync, replace and directory fsync. `ensure_directory` rejects symlink traversal.
`exclusive_lock` uses Linux flock; nonblocking contention produces ownership_error.
An owner descriptor can transfer to a worker; closing the parent copy does not
release the worker's lock.

`assets.initialize_project(directory)` reads notes, build/config YAML, patches and
current schemas through importlib.resources. Identical files remain unchanged;
conflicting files are not overwritten. Every path is checked before writes. The
result lists created/unchanged files; forced overwrite is not offered. Wheel/sdist
must include the license, schemas, recipes, templates and bundled assets. Release
checks also verify every project Markdown document byte-for-byte in both archives.

Tree identity comes from `sources.store.inventory`, including paths, types, modes,
contents and exact symlink targets. A file hash is not a substitute for tree inventory.
YAML loading uses yamlio; implementation identity lives in planning.documents.

"""Keep PostgreSQL's private subdirectories independent of the vendor prefix."""

import difflib
import hashlib
from pathlib import Path

from pg_maker.errors import PreconditionError


def normalize_install_layout(source: Path, receipt: Path) -> dict:
    # PostgreSQL normally searches the whole absolute path for "postgres"/"pgsql".
    # A vendor such as postgresql-yc-clone then suppresses the lib/postgresql and
    # share/postgresql suffixes expected by the component recipes and packager.
    # Inspect only the final directory name, retaining the normal suffix rules.
    path = source / "src/Makefile.global.in"
    if not path.is_file() or path.is_symlink():
        raise PreconditionError("missing regular PostgreSQL src/Makefile.global.in")
    original = path.read_text()
    result = original
    for directory in ("datadir", "sysconfdir", "pkglibdir", "pkgincludedir", "docdir"):
        for marker in ("pgsql", "postgres"):
            old = f"$(findstring {marker}, $({directory}))"
            new = f"$(findstring {marker}, $(notdir $({directory})))"
            if result.count(old) == 1 and new not in result:
                result = result.replace(old, new)
            elif old in result or result.count(new) != 1:
                raise PreconditionError(
                    "unsupported PostgreSQL installation directory rules",
                    details={"directory": directory, "marker": marker},
                )
    if result != original:
        path.write_text(result)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            "".join(
                difflib.unified_diff(
                    original.splitlines(True),
                    result.splitlines(True),
                    fromfile="a/src/Makefile.global.in",
                    tofile="b/src/Makefile.global.in",
                )
            )
        )
    return {
        "policy": "postgresql-subdirectories/v1",
        "source_file": "src/Makefile.global.in",
        "sha256": "sha256:" + hashlib.sha256(result.encode()).hexdigest(),
    }

"""Install a wheel in a temporary venv and exercise public entry points outside checkout."""

import argparse
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="pg-maker-wheel-") as temporary:
        directory = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(directory / "venv")
        binaries = directory / "venv/bin"

        def run(argv):
            return subprocess.check_output(
                [str(arg) for arg in argv], cwd=directory, env=environment, text=True
            )

        run([binaries / "python", "-m", "pip", "install", "--disable-pip-version-check", wheel])
        run([binaries / "python", "-m", "pip", "check"])
        print(run([binaries / "pg-maker", "--version"]).strip())
        response = json.loads(run([binaries / "pg-maker", "--machine", "--component-capabilities"]))
        assert response["status"] == "succeeded", response
        run([binaries / "pg-maker", "init", directory / "project", "--with-doc"])
        run([binaries / "pg-maker", "validate", directory / "project/build.yaml"])
        run([binaries / "pg-maker", "dev", "--help"])
        run([binaries / "sgml-writer", "--help"])
        run([binaries / "sgml-translate", "--help"])
        manuals = directory / "venv/share/doc/pg-maker"
        assert (manuals / "README.md").is_file()
        assert (manuals / "docs/interfaces/development.md").is_file()
    print("OK: isolated wheel install, dependencies, CLI, init/validate and installed manuals")


if __name__ == "__main__":
    main()

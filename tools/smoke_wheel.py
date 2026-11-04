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
        managed = directory / "yc16"
        run([binaries / "pg-maker", "init", managed, "--profile", "yc-16.15-icu70"])
        run([binaries / "pg-maker", "validate", managed / "build.yaml"])
        profile_check = """
import sys
from pathlib import Path
from pg_maker.config import load_config
from pg_maker.yamlio import load_yaml
root = Path(sys.argv[1])
config = load_config(root / 'build.yaml').data
assert config['postgresql']['version'] == '16.15'
variant = config['variants'][0]
assert variant['identity']['product_version'] == '16.15'
assert all(p['deb_version'] == '16.15-1.deb13' for p in variant['identity']['packages'])
assert {p['role']: p['name'] for p in variant['identity']['packages']} == {
    'server': 'postgresql-16-yc-clone',
    'client': 'postgresql-client-16-yc-clone',
    'libpq': 'libpq5-yc-clone',
}
target = load_yaml(root / 'build.yaml')['spec']['targets'][0]
assert (target['platform'], target['architecture']) == ('debian-13', 'x86_64')
components = {c['metadata']['id']: c['spec'] for c in config['features'][0]['spec']['components']}
assert components['icu70']['build_phase'] == 'before-postgresql'
assert components['icu70']['pkg_config_versions'] == {'icu-i18n': '70.1', 'icu-uc': '70.1'}
assert config['defaults']['validation']['run_db_tests'] is True
assert config['defaults']['packaging']['server_dev_files'] is True
assert config['postgresql_recipe']['db_test_adapter'] == 'pg16'
assert all(c['test_targets'] or c.get('test_commands') for c in components.values())
from pg_maker.config import resource_path
assert resource_path('validation', 'upstream', 'partman.py').is_file()
assert resource_path('validation', 'upstream', 'logerrors.py').is_file()
assert resource_path('schemas', 'diagnostics.schema.json').is_file()
from pg_maker.postgresql import recipe
assert recipe('17.5')['db_test_adapter'] == 'pg17'
declared = {
    n: v for c in components.values() for n, v in c.get('sql_extension_versions', {}).items()
}
entries = load_yaml(root / 'catalogs/yc-16.15-extensions.yaml')['extensions']
assert len(entries) == 38 and len(declared) == 33
for entry in entries:
    if entry['status'] in {'blocked', 'disabled'}:
        assert entry['reason'] and entry['name'] not in declared
    else:
        assert declared[entry['name']] == entry['managed_default_version']
        source = components[entry['component_id']]['source']
        if source['kind'] == 'git':
            assert entry['source_ref'] == source['ref']
assert components['yc-jsquery']['source']['ref'] == '40b1db58b3f3f4b315a6f606ba88752c0f8b6aaf'
assert components['yc-pg-cron']['source']['ref'] == '0cb8118e8dfda30af58a65e14e2b7fe73ec6c931'
assert 'yc-plv8' not in components
assert next(e for e in entries if e['name'] == 'plv8')['status'] == 'disabled'
print('OK: installed managed profile: PostgreSQL 16.15, Debian 13 amd64, '
      'ICU 70.1, 33 selected extensions; PLV8 disabled')
"""
        print(run([binaries / "python", "-c", profile_check, managed]).strip())
        run([binaries / "pg-maker", "dev", "--help"])
        run([binaries / "pg-maker", "test-full", "--help"])
        run([binaries / "pg-maker", "logs", "verify", "--help"])
        run([binaries / "sgml-writer", "--help"])
        run([binaries / "sgml-translate", "--help"])
        manuals = directory / "venv/share/doc/pg-maker"
        assert (manuals / "README.md").is_file()
        assert (manuals / "docs/interfaces/development.md").is_file()
        assert (manuals / "docs/managed-16-icu70.md").is_file()
    print("OK: isolated wheel install, dependencies, CLI, init/validate and installed manuals")


if __name__ == "__main__":
    main()

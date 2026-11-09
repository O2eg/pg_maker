"""Install a wheel in a temporary venv and exercise public entry points outside checkout."""

import argparse
import json
import os
import re
import subprocess
import tempfile
import venv
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    environment = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="pg-maker-wheel-") as temporary:
        directory = Path(temporary)
        # A copied relocatable Python executable may lose its stdlib prefix.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(directory / "venv")
        binaries = directory / "venv/bin"

        def run(argv):
            return subprocess.check_output(
                [str(arg) for arg in argv], cwd=directory, env=environment, text=True
            )

        run([binaries / "python", "-m", "pip", "install", "--disable-pip-version-check", wheel])
        run([binaries / "python", "-m", "pip", "check"])
        # Import the runtime TOML reader without dev extras, including on Python 3.10.
        run([binaries / "python", "-c", "from pg_maker.validation.upstream import vectorscale"])
        print(run([binaries / "pg-maker", "--version"]).strip())
        response = json.loads(run([binaries / "pg-maker", "--machine", "--component-capabilities"]))
        assert response["status"] == "succeeded", response
        run([binaries / "pg-maker", "init", directory / "project", "--with-doc"])
        run([binaries / "pg-maker", "validate", directory / "project/build.yaml"])
        profiles = json.loads(
            run(
                [
                    binaries / "python",
                    "-c",
                    "import json; from pg_maker.assets import BUILD_PROFILES; "
                    "print(json.dumps(BUILD_PROFILES))",
                ]
            )
        )
        versions = [profile.removeprefix("yc-").removesuffix("-icu70") for profile in profiles]
        profile_check = """
import sys
from pathlib import Path
from pg_maker.config import load_config
from pg_maker.yamlio import load_yaml
root = Path(sys.argv[1])
config = load_config(root / 'build.yaml').data
version = sys.argv[2]
assert config['postgresql']['version'] == version
variant = config['variants'][0]
assert variant['identity']['paths']['runtime_prefix'] == '/opt/postgresql-yc-clone/16'
assert variant['identity']['paths']['data_directory'] == '/var/lib/postgresql-yc-clone/16/data'
assert variant['identity']['service'] == 'postgresql-yc-clone-16'
assert variant['identity']['product_version'] == version
assert all(p['deb_version'] == version + '-1.deb13' for p in variant['identity']['packages'])
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
assert config['defaults']['packaging']['standard_contrib'] is True
from pg_maker.packaging.contrib import REQUIRED_OPTIONS
assert all(variant['build']['options'][name] for name in REQUIRED_OPTIONS)
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
print('OK: installed managed profile: PostgreSQL ' + version + ', Debian 13 amd64, '
      'ICU 70.1, 33 selected extensions; PLV8 disabled')
"""
        for version in (v for v in versions if v.startswith("16.")):
            managed = directory / ("yc" + version)
            run([binaries / "pg-maker", "init", managed, "--profile", "yc-" + version + "-icu70"])
            run([binaries / "pg-maker", "validate", managed / "build.yaml"])
            print(run([binaries / "python", "-c", profile_check, managed, version]).strip())
        for version in (v for v in versions if v.startswith("15.")):
            managed = directory / ("yc" + version)
            run([binaries / "pg-maker", "init", managed, "--profile", "yc-" + version + "-icu70"])
            run([binaries / "pg-maker", "validate", managed / "build.yaml"])
            print(
                run(
                    [
                        binaries / "python",
                        "-c",
                        """
import sys
from pathlib import Path
from pg_maker.config import load_config, resource_path
from pg_maker.builders.autotools import validate_build
from pg_maker.yamlio import load_yaml
root = Path(sys.argv[1])
c = load_config(root / 'build.yaml').data
validate_build(c, 'verified')
assert c['postgresql']['version'] == sys.argv[2]
assert c['postgresql_recipe']['db_test_adapter'] == 'pg15'
assert c['defaults']['packaging']['standard_contrib'] is True
assert c['defaults']['validation']['run_db_tests'] is True
components = {x['metadata']['id']: x['spec'] for x in c['features'][0]['spec']['components']}
assert 'yc15-plv8' not in components
assert sum(len(x.get('sql_extension_versions', {})) for x in components.values()) == 32
catalog = load_yaml(root / 'catalogs/yc-15.19-extensions.yaml')
assert len(catalog['extensions']) == 36
assert catalog['binary_compatibility']['status'] == 'not-verified'
assert resource_path('validation', 'upstream', 'pglogical.py').is_file()
assert resource_path('bundled', 'patches', 'pgrouting-3.4.1-gcc14.patch').is_file()
assert c['variants'][0]['identity']['paths']['runtime_prefix'] == '/opt/postgresql-yc-clone/15'
print('OK: installed PG' + sys.argv[2] + ' candidate profile, full contrib, ICU70, PLV8 disabled')
""",
                        managed,
                        version,
                    ]
                ).strip()
            )
        for version in (v for v in versions if v.startswith(("17.", "18."))):
            managed = directory / ("yc" + version)
            run([binaries / "pg-maker", "init", managed, "--profile", "yc-" + version + "-icu70"])
            run([binaries / "pg-maker", "validate", managed / "build.yaml"])
            print(
                run(
                    [
                        binaries / "python",
                        "-c",
                        """
import sys
from pathlib import Path
from pg_maker.config import load_config, resource_path
from pg_maker.yamlio import load_yaml
root = Path(sys.argv[1])
c = load_config(root / 'build.yaml').data
version = sys.argv[2]
major = version.split('.')[0]
assert c['postgresql']['version'] == version
assert c['defaults']['packaging']['standard_contrib'] is True
from pg_maker.packaging.contrib import REQUIRED_OPTIONS
assert all(c['variants'][0]['build']['options'][name] for name in REQUIRED_OPTIONS)
assert c['postgresql_recipe']['db_test_adapter'] == 'pg' + major
identity = c['variants'][0]['identity']
assert identity['product_version'] == version
assert all(p['deb_version'] == version + '-1.deb13' for p in identity['packages'])
assert identity['paths']['runtime_prefix'] == '/opt/postgresql-yc-clone/' + major
assert identity['paths']['data_directory'] == '/var/lib/postgresql-yc-clone/' + major + '/data'
assert identity['service'] == 'postgresql-yc-clone-' + major
components = {x['metadata']['id']: x['spec'] for x in c['features'][0]['spec']['components']}
assert 'yc' + major + '-plv8' not in components
assert ('yc' + major + '-repl-mon' in components) == (major == '18')
rust_inputs = components['yc' + major + '-vectorscale']['extra_sources']
assert len(rust_inputs) == 626 and 'rustfmt' in rust_inputs
assert len([name for name in rust_inputs if name.startswith('crate-')]) == 621
assert sum(len(x.get('sql_extension_versions', {})) for x in components.values()) == 39
catalog_version = {'17': '17.11', '18': '18.6'}[major]
catalog = load_yaml(root / ('catalogs/yc-' + catalog_version + '-extensions.yaml'))
assert len(catalog['extensions']) == 44
for helper in ('vectorscale', 'partman5', 'lwaldump'):
    assert resource_path('validation', 'upstream', helper + '.py').is_file()
print('OK: installed PG' + version + ' profile, ICU70.1, frozen Cargo inputs, PLV8 disabled')
    """,
                        managed,
                        version,
                    ]
                ).strip()
            )
        run([binaries / "pg-maker", "dev", "--help"])
        run([binaries / "pg-maker", "test-full", "--help"])
        run([binaries / "pg-maker", "logs", "verify", "--help"])
        run([binaries / "sgml-writer", "--help"])
        run([binaries / "sgml-translate", "--help"])
        manuals = directory / "venv/share/doc/pg-maker"
        assert (manuals / "README.md").is_file()
        assert (manuals / "docs/interfaces/development.md").is_file()
        assert (manuals / "docs/managed-16-icu70.md").is_file()
        assert (manuals / "docs/managed-15-icu70.md").is_file()
        assert (manuals / "docs/yc-clone-compatibility.md").is_file()
        assert (manuals / "docs/managed-17-icu70.md").is_file()
        assert (manuals / "docs/managed-18-icu70.md").is_file()
        assert (manuals / "docs/yc-clone-packages.md").is_file()
        for document in manuals.rglob("*.md"):
            prose = re.sub(r"```.*?```", "", document.read_text(), flags=re.DOTALL)
            for target in re.findall(r"!?\[[^\]\n]*\]\(([^\s)]+)", prose):
                link = urlsplit(target.strip("<>"))
                if not link.scheme and not link.netloc and link.path:
                    assert (document.parent / unquote(link.path)).exists(), (document, target)
    print("OK: isolated wheel install, dependencies, CLI, init/validate and installed manuals")


if __name__ == "__main__":
    main()

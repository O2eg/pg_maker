"""Build pinned Rust/pgrx inputs offline, then run the upstream extension suites."""

import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def crate_key(name, version):
    return (
        ("crate-" + name + "-" + version)
        .lower()
        .replace("_", "-")
        .replace(".", "-")
        .replace("+", "-")
    )


def run(argv, *, env, cwd, log=None):
    if log is None:
        subprocess.run(argv, env=env, cwd=cwd, check=True)
        return ""
    with Path(log).open("w") as stream:
        result = subprocess.run(argv, env=env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    text = Path(log).read_text(errors="replace")
    print(text, end="", flush=True)
    result.check_returncode()
    return text


def prepare(root, extra, toolchain):
    for name in ("rustc", "rust-std", "cargo", "rustfmt"):
        run(
            ["sh", "install.sh", "--prefix=" + str(toolchain), "--disable-ldconfig"],
            cwd=extra / name,
            env=os.environ,
        )
    vendor = root / "pg-maker-vendor"
    vendor.mkdir(exist_ok=True)
    packages = {}
    for path in (root / "Cargo.lock", extra / "cargo-pgrx/Cargo.lock"):
        for item in tomllib.loads(path.read_text())["package"]:
            if "source" in item:
                assert item["source"] == "registry+https://github.com/rust-lang/crates.io-index"
                key = item["name"] + "-" + item["version"]
                if key in packages:
                    assert packages[key]["checksum"] == item["checksum"]
                packages[key] = item
    for key, item in packages.items():
        target = vendor / key
        if not target.exists():
            shutil.copytree(extra / crate_key(item["name"], item["version"]), target)
        files = {
            p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in target.rglob("*")
            if p.is_file() and p.name != ".cargo-checksum.json"
        }
        (target / ".cargo-checksum.json").write_text(
            json.dumps({"package": item["checksum"], "files": files})
        )
    home = root / "pg-maker-cargo"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(
        '[source.crates-io]\nreplace-with = "frozen"\n[source.frozen]\ndirectory = '
        + json.dumps(str(vendor))
        + "\n[net]\noffline = true\n"
    )


def postgresql_feature(prefix):
    version = subprocess.check_output([prefix + "/bin/pg_config", "--version"], text=True)
    match = re.fullmatch(r"PostgreSQL (17|18)\.\d+[^\n]*\n?", version)
    if not match:
        raise ValueError("pinned vectorscale recipe requires PostgreSQL 17 or 18")
    return "pg" + match[1]


def upgrade_test_report(feature, ignored):
    expected = {
        "access_method::upgrade_test::tests::test_upgrade_from_" + version
        for version in (
            "0_0_2",
            "0_2_0",
            "0_3_0",
            "0_4_0",
            "0_5_0",
            "0_5_1",
            "0_6_0",
            "0_7_0",
            "0_7_1",
            "0_8_0",
        )
    }
    if set(ignored) != expected or len(ignored) != len(expected):
        raise RuntimeError("unexpected pgrx ignored-test inventory: " + repr(ignored))
    if feature == "pg18":
        status = "not-applicable; not counted as passed"
        reason = (
            "Pinned upstream upgrade_test.rs returns before testing on pg18: "
            "the historical releases have no PostgreSQL 18 build"
        )
    elif feature == "pg17":
        status = "upstream-disabled; not executed or counted as passed"
        reason = (
            "Pinned upstream upgrade_test.rs ignores these tests and its pg17 "
            "guard returns before exercising upgrades. Historical PG17 upgrade "
            "coverage is not established by this suite."
        )
    else:
        raise ValueError("unsupported PostgreSQL feature")
    return {"status": status, "reason": reason, "tests": ignored}


def main():
    action, prefix, jobs, pgrx_source = sys.argv[1:]
    feature = postgresql_feature(prefix)
    root = Path.cwd()
    extra = Path(pgrx_source).parent
    toolchain = root / "pg-maker-toolchain"
    if action == "build":
        prepare(root, extra, toolchain)
    env = {
        **os.environ,
        "PATH": f"{toolchain}/bin:{prefix}/bin:/usr/local/bin:/usr/bin:/bin",
        "CARGO_HOME": str(root / "pg-maker-cargo"),
        # pgrx connects as the OS role created by its own initdb.
        "USER": pwd.getpwuid(os.getuid()).pw_name,
        "CARGO_NET_OFFLINE": "true",
        "CARGO_BUILD_JOBS": jobs,
        "CARGO_PROFILE_DEV_DEBUG": "0",
        "PGRX_HOME": str(root / "pg-maker-pgrx"),
        "PGRX_PG_CONFIG_PATH": prefix + "/bin/pg_config",
        # Keep build tools on the baseline; the extension requires AVX2/FMA.
        "RUSTFLAGS": "-Ctarget-cpu=x86-64",
    }
    crate = root / "pgvectorscale"

    def command(argv, log=None, cwd=crate, *, extension=False):
        command_env = env
        if extension:
            command_env = {**env, "RUSTFLAGS": env["RUSTFLAGS"] + " -Ctarget-feature=+avx2,+fma"}
        return run(argv, env=command_env, cwd=cwd, log=root / log if log else None)

    install = [
        "cargo",
        "pgrx",
        "install",
        "--release",
        "--no-default-features",
        "--features",
        feature + ",build_parallel",
        "--pg-config",
        prefix + "/bin/pg_config",
    ]
    if action == "build":
        command(
            [
                "cargo",
                "install",
                "--path",
                pgrx_source,
                "--locked",
                "--offline",
                "--root",
                str(toolchain),
            ],
            cwd=root,
        )
        command(["cargo", "pgrx", "init", "--" + feature + "=" + prefix + "/bin/pg_config"])
        command(install, extension=True)
        return
    if action != "test":
        raise ValueError("unknown action")
    text = command(
        [
            "cargo",
            "pgrx",
            "test",
            feature,
            "--release",
            "--no-default-features",
            "--features",
            feature + ",build_parallel",
        ],
        "pg-maker-pgrx-tests.log",
        extension=True,
    )
    ignored = re.findall(r"^test (\S+) \.\.\. ignored$", text, re.M)
    (root / "pg-maker-inapplicable-tests.log").write_text(
        json.dumps(upgrade_test_report(feature, ignored), indent=2)
    )
    counts = [int(n) for n in re.findall(r"test result: ok\. (\d+) passed; 0 failed", text)]
    if not counts or sum(counts) == 0:
        raise RuntimeError("pgrx did not report nonempty passing tests")
    # pgrx test installs its pg_test binary. Restore the production release.
    command(install, extension=True)
    data = root / "pg-maker-python-data"
    command(
        ["initdb", "-D", str(data), "-U", "builder", "-A", "trust", "--no-locale", "-E", "UTF8"]
    )
    with (data / "postgresql.conf").open("a") as f:
        f.write("\nport=55438\nlisten_addresses='127.0.0.1'\nunix_socket_directories='/tmp'\n")
    command(["pg_ctl", "-D", str(data), "-l", str(data / "server.log"), "-w", "start"])
    try:
        env.update(DB_HOST="127.0.0.1", DB_PORT="55438", DB_USER="builder", DB_NAME="postgres")
        text = command(
            ["python3", "-m", "pytest", "-v", "tests"], "pg-maker-python-tests.log", cwd=root
        )
        passed = re.search(r"(\d+) passed", text)
        if not passed or int(passed[1]) == 0:
            raise RuntimeError("Python suite did not report passing tests")
        counts.append(int(passed[1]))
    finally:
        command(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])
    print("PG_MAKER_VECTORSCALE_TESTS_PASSED=" + str(sum(counts)), flush=True)


if __name__ == "__main__":
    main()

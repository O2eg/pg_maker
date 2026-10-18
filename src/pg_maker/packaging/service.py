"""Service units and maintainer scripts; installation never initializes a database."""

import re
import shlex

from pg_maker.errors import ValidationError
from pg_maker.postgresql import version_info


def settings(config, variant):
    identity = variant["identity"]
    service = config["defaults"]["service"]
    result = {
        "prefix": identity["paths"]["runtime_prefix"],
        "data": identity["paths"]["data_directory"],
        "name": identity["service"],
        "socket": variant["socket_directory"],
        "user": service["user"],
        "group": service["group"],
        "home": service["home"],
        "port": str(service["port"]),
        "major": str(version_info(config["postgresql"]["version"])["major"]),
    }
    for key in ("prefix", "data", "socket", "home"):
        if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", result[key]) or ".." in result[key].split("/"):
            raise ValidationError(
                "packaging requires simple absolute service paths", details={"field": key}
            )
    for key in ("user", "group"):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", result[key]):
            raise ValidationError("invalid service account name")
    if not result["prefix"].startswith("/opt/"):
        raise ValidationError("initial DEB adapter requires a private prefix below /opt")
    return result


def service_files(config, variant):
    s = settings(config, variant)
    helper = s["prefix"] + "/libexec/pg-maker/" + s["name"]
    start = (
        f"{s['prefix']}/bin/pg_ctl start -D {s['data']} -w -t 60 "
        f'-l {s["data"]}/server.log -o "-p {s["port"]}"'
    )
    unit = f"""[Unit]
Description={s["name"]} PostgreSQL
After=network.target

[Service]
Type=forking
User={s["user"]}
Group={s["group"]}
WorkingDirectory={s["data"]}
Environment=PGDATA={s["data"]}
PIDFile={s["data"]}/postmaster.pid
ExecStartPre=+{helper} prepare
ExecStart={start}
ExecStop={s["prefix"]}/bin/pg_ctl stop -D {s["data"]} -w -t 60 -m fast
ExecReload={s["prefix"]}/bin/pg_ctl reload -D {s["data"]}
TimeoutStartSec=90
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
"""
    check = f"""#!/bin/sh
set -eu
test "$1" = prepare
data={shlex.quote(s["data"])}
socket={shlex.quote(s["socket"])}
if [ ! -f "$data/PG_VERSION" ] || [ ! -f "$data/global/pg_control" ] || [ ! -d "$data/base" ]; then
    echo 'PGDATA is not an initialized PostgreSQL cluster; run initdb explicitly' >&2
    exit 1
fi
test "$(cat "$data/PG_VERSION")" = {shlex.quote(s["major"])} || {{
    echo 'PGDATA major version differs from this service' >&2; exit 1;
}}
test "$(stat -c %U "$data")" = {shlex.quote(s["user"])} || {{
    echo 'PGDATA owner differs from service user' >&2; exit 1;
}}
if [ ! -d "$socket" ]; then
    install -d -m 2775 -o {s["user"]} -g {s["group"]} "$socket"
fi
"""
    runtime_socket = (
        "/run/" + s["socket"][9:] if s["socket"].startswith("/var/run/") else s["socket"]
    )
    tmpfiles = f"d {runtime_socket} 2775 {s['user']} {s['group']} -\n"
    files = {
        "usr/lib/systemd/system/" + s["name"] + ".service": (unit, 0o644),
        helper.lstrip("/"): (check, 0o755),
    }
    # /tmp is an existing shared system directory, never change its ownership or mode.
    if s["socket"] != "/tmp":
        files["usr/lib/tmpfiles.d/" + s["name"] + ".conf"] = (tmpfiles, 0o644)
    return files


def maintainer_scripts(config, variant):
    s = settings(config, variant)
    name, user, group, home, data = (
        shlex.quote(s[key]) for key in ("name", "user", "group", "home", "data")
    )
    flag = shlex.quote("/run/" + s["name"] + ".pg-maker-restart")
    socket = shlex.quote(s["socket"])
    prepare_socket = (
        ""
        if s["socket"] == "/tmp"
        else (f"if [ ! -e {socket} ]; then install -d -m 2775 -o {user} -g {group} {socket}; fi")
    )
    postinst = f"""#!/bin/sh
set -eu
case "$1" in
configure)
    if ! getent group {group} >/dev/null; then groupadd --system {group}; fi
    if ! getent passwd {user} >/dev/null; then
        useradd --system --gid {group} --home-dir {home} \\
            --no-create-home --shell /usr/sbin/nologin {user}
    fi
    if [ ! -e {data} ]; then install -d -m 0700 -o {user} -g {group} {data}; fi
    {prepare_socket}
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload
        if [ -f {data}/PG_VERSION ]; then systemctl enable {name}.service; fi
    fi
    ;;
abort-upgrade|abort-remove|abort-deconfigure) ;;
*) echo 'unsupported postinst action' >&2; exit 1 ;;
esac
if [ -f {flag} ] && [ -d /run/systemd/system ]; then
    systemctl start {name}.service
    rm -f {flag}
fi
"""
    prerm = f"""#!/bin/sh
set -eu
case "$1" in
remove|upgrade|deconfigure)
    if [ -d /run/systemd/system ]; then
        if systemctl is-active --quiet {name}.service; then
            if [ "$1" = upgrade ]; then : > {flag}; fi
        else
            status=$?
            case "$status" in 3|4) ;; *) exit "$status" ;; esac
        fi
        systemctl stop {name}.service
        if [ "$1" = remove ]; then systemctl disable {name}.service; fi
    fi
    ;;
failed-upgrade) ;;
*) echo 'unsupported prerm action' >&2; exit 1 ;;
esac
"""
    postrm = f"""#!/bin/sh
set -eu
case "$1" in
remove|purge)
    if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
    rm -f {flag}
    ;;
upgrade|failed-upgrade|abort-install|abort-upgrade|disappear) ;;
*) echo 'unsupported postrm action' >&2; exit 1 ;;
esac
# PGDATA, configuration, logs and the service account are deliberately preserved.
"""
    return {"postinst": postinst, "prerm": prerm, "postrm": postrm}

"""Run pg_clickhouse's native regressions against an isolated pinned ClickHouse."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape


def main():
    binary = Path(sys.argv[1]) / "usr/bin/clickhouse"
    root = (Path.cwd() / "pg-maker-clickhouse").resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    directory = escape(str(root))
    config = root / "config.xml"
    config.write_text(f"""<clickhouse>
<logger><level>information</level><log>{directory}/server.log</log>
<errorlog>{directory}/error.log</errorlog></logger>
<path>{directory}/data/</path><tmp_path>{directory}/tmp/</tmp_path>
<user_files_path>{directory}/user_files/</user_files_path>
<format_schema_path>{directory}/format_schemas/</format_schema_path>
<listen_host>127.0.0.1</listen_host><http_port>8123</http_port><tcp_port>9000</tcp_port>
<timezone>UTC</timezone><max_connections>256</max_connections>
<background_schedule_pool_size>16</background_schedule_pool_size>
<background_pool_size>16</background_pool_size>
<max_server_memory_usage>4294967296</max_server_memory_usage>
<profiles><default><max_memory_usage>2147483648</max_memory_usage>
<max_threads>8</max_threads></default></profiles>
<users><default><password></password><networks><ip>127.0.0.1</ip></networks>
<profile>default</profile><quota>default</quota><access_management>1</access_management>
</default></users><quotas><default></default></quotas>
</clickhouse>""")
    with (root / "console.log").open("w") as log:
        process = subprocess.Popen(
            [str(binary), "server", "--config-file=" + str(config)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError("ClickHouse exited; see pg-maker-clickhouse/console.log")
                result = subprocess.run(
                    [str(binary), "client", "--host", "127.0.0.1", "--query", "SELECT 1"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip() == "1":
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("ClickHouse did not become ready")
            command = (
                ["cmake", "--build", sys.argv[4], "--target", "installcheck", "--parallel", "1"]
                if len(sys.argv) > 4
                else [
                    "make",
                    "--output-sync=target",
                    "-j",
                    sys.argv[3],
                    "USE_PGXS=1",
                    "PG_CONFIG=" + sys.argv[2] + "/bin/pg_config",
                    "installcheck",
                ]
            )
            subprocess.run(command, env=os.environ, check=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()

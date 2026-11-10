"""Receiver registry. Upload and verification are separate, resumable actions."""

import gzip
import hashlib
import io
import os
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urlencode

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError
from pg_maker.publishing.artifacts import plain
from pg_maker.publishing.transport import Http, NetworkError, pause
from pg_maker.storage import file_hash, sync_directory


def conflict():
    raise PreconditionError(
        "receiver already contains different bytes for this package; use a new revision"
    )


def checked_chunks(stream, package, check):
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        check()
        digest.update(block)
        size += len(block)
        yield block
    if size != package["size_bytes"] or "sha256:" + digest.hexdigest() != package["sha256"]:
        raise PreconditionError("package changed during publication")


class Directory:
    def __init__(self, receiver, config, check):
        self.receiver, self.check = receiver, check
        self.root = plain(Path(receiver["directory"])) / receiver.get("path_prefix", "")
        plain(self.root)

    def path(self, package):
        return plain(self.root / package["filename"])

    def preflight(self, packages):
        for package in packages:
            self.existing(package)

    def existing(self, package):
        path = self.path(package)
        if path.exists():
            if not path.is_file() or file_hash(path) != package["sha256"]:
                conflict()
            return str(path)
        return None

    def publish(self, package):
        self.check()
        if self.existing(package):
            return {
                "destination": str(self.path(package)),
                "action": "reused",
                "index_verified": False,
            }
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(package)
        with tempfile.TemporaryDirectory(dir=self.root, prefix=".pg-maker-") as temporary:
            staged = Path(temporary) / "package"
            with Path(package["path"]).open("rb") as source, staged.open("wb") as out:
                for block in checked_chunks(source, package, self.check):
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            staged.chmod(0o644)
            try:
                os.link(staged, path)
            except FileExistsError:
                self.existing(package)
            sync_directory(self.root)
        self.existing(package)
        return {"destination": str(path), "action": "uploaded", "index_verified": False}


class Nexus:
    def __init__(self, receiver, config, check):
        self.receiver, self.config, self.check = receiver, config, check
        self.http = Http(receiver, config, check)
        self.base = receiver["url"]
        self.repository = receiver["repository"]
        self.format = receiver["adapter"].removeprefix("nexus-")
        self.repo = self.base + "/repository/" + quote(self.repository, safe="") + "/"
        self.api = self.base + "/service/rest/v1/"

    def metadata_url(self, path):
        if not relative_output(path) or "%" in path or "\\" in path:
            raise PreconditionError("invalid repository metadata path")
        return self.repo + quote(path, safe="/+~")

    def assets(self):
        token = None
        seen = set()
        items = []
        for _ in range(10000):
            query = {"repository": self.repository}
            if token:
                query["continuationToken"] = token
            page = self.http.json(self.api + "assets?" + urlencode(query))
            items.extend(page["items"])
            token = page.get("continuationToken")
            if not token:
                return items
            if token in seen:
                raise PreconditionError("receiver pagination did not advance")
            seen.add(token)
        raise PreconditionError("receiver asset listing exceeded page limit")

    def match(self, package, assets):
        expected = "/".join(filter(None, [self.receiver.get("path_prefix"), package["filename"]]))
        matches = []
        for asset in assets:
            path = asset["path"]
            matches_name = (
                Path(path).name == package["filename"] if self.format == "apt" else path == expected
            )
            if matches_name:
                if asset.get("checksum", {}).get("sha256") != package["sha256"][7:]:
                    conflict()
                self.metadata_url(path)
                # Construct URLs from the configured origin; never forward credentials to API URLs.
                matches.append(path)
        if len(matches) > 1:
            raise PreconditionError("ambiguous remote package identity")
        return matches[0] if matches else None

    def preflight(self, packages):
        repository = self.http.json(self.api + "repositories/" + quote(self.repository, safe=""))
        if (repository.get("format"), repository.get("type")) != (self.format, "hosted"):
            raise PreconditionError(
                "receiver must be the configured Nexus hosted repository format"
            )
        settings = self.http.json(
            self.api + f"repositories/{self.format}/hosted/" + quote(self.repository, safe="")
        )
        if settings.get("storage", {}).get("writePolicy") != "ALLOW_ONCE":
            raise PreconditionError(
                "Nexus hosted repository must use Disable redeploy (ALLOW_ONCE) "
                "to prevent concurrent overwrite"
            )
        if (
            self.format == "apt"
            and settings.get("apt", {}).get("distribution") != self.receiver["suite"]
        ):
            raise PreconditionError("APT suite differs from hosted repository distribution")
        assets = self.assets()
        for package in packages:
            self.match(package, assets)

    def upload(self, package):
        path = Path(package["path"])
        if file_hash(path) != package["sha256"]:
            raise PreconditionError("package changed before upload")
        boundary = "pg-maker-" + uuid.uuid4().hex
        if self.format == "apt":
            header = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="apt.asset"; '
                f'filename="{package["filename"]}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            footer = f"\r\n--{boundary}--\r\n".encode()
            url = self.api + "components?" + urlencode({"repository": self.repository})
            method = "POST"
            headers = {
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(header) + package["size_bytes"] + len(footer)),
            }
        else:
            header = footer = b""
            method = "PUT"
            url = self.metadata_url(
                "/".join(filter(None, [self.receiver.get("path_prefix"), package["filename"]]))
            )
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(package["size_bytes"]),
            }
        # Freeze one verified package before sending any byte; build outputs may change.
        with tempfile.TemporaryFile() as stream:
            with path.open("rb") as incoming:
                for block in checked_chunks(incoming, package, self.check):
                    stream.write(block)
            stream.seek(0)

            def body():
                if header:
                    yield header
                yield from checked_chunks(stream, package, self.check)
                if footer:
                    yield footer

            with self.http.open(url, method=method, data=body(), headers=headers):
                pass

    def index_contains(self, package, path):
        if self.format == "raw":
            return True
        if self.format == "apt":
            prefix = (
                f"dists/{self.receiver['suite']}/{self.receiver['component']}/"
                f"binary-{package['architecture']}/"
            )
            # Nexus serves compressed Packages; tolerate repositories exposing only plain text.
            try:
                data = self.http.bytes(self.metadata_url(prefix + "Packages.gz"))
            except NetworkError as exc:
                if exc.status != 404:
                    raise
                data = self.http.bytes(self.metadata_url(prefix + "Packages"))
            else:
                data = bounded_gzip(data)
            for paragraph in data.decode("utf-8").split("\n\n"):
                fields = dict(
                    line.split(": ", 1)
                    for line in paragraph.splitlines()
                    if ": " in line and not line.startswith(" ")
                )
                if (fields.get("Package"), fields.get("Version"), fields.get("Architecture")) == (
                    package["name"],
                    package["version"],
                    package["architecture"],
                ):
                    if fields.get("SHA256") != package["sha256"][7:]:
                        conflict()
                    return fields.get("Filename") == path and fields.get("Size") == str(
                        package["size_bytes"]
                    )
            return False
        prefix = self.receiver.get("path_prefix", "")
        # YUM metadata belongs to the repository root configured by path_prefix.
        repomd = "/".join(filter(None, [prefix, "repodata/repomd.xml"]))
        document = ET.fromstring(self.http.bytes(self.metadata_url(repomd)))
        ns = {"r": "http://linux.duke.edu/metadata/repo"}
        node = document.find("r:data[@type='primary']", ns)
        if node is None:
            return False
        location = node.find("r:location", ns).attrib["href"]
        checksum = node.find("r:checksum", ns)
        raw = self.http.bytes(self.metadata_url("/".join(filter(None, [prefix, location]))))
        if (
            checksum.attrib["type"] not in ("sha256", "sha512")
            or hashlib.new(checksum.attrib["type"], raw).hexdigest() != checksum.text
        ):
            raise PreconditionError("YUM primary metadata checksum mismatch")
        primary = ET.fromstring(bounded_gzip(raw) if location.endswith(".gz") else raw)
        ns = {"c": "http://linux.duke.edu/metadata/common"}
        for item in primary.findall("c:package", ns):
            href = item.find("c:location", ns).attrib["href"]
            if "/".join(filter(None, [prefix, href])) != path:
                continue
            digest = item.find("c:checksum", ns)
            if digest.attrib["type"] != "sha256" or digest.text != package["sha256"][7:]:
                conflict()
            return True
        return False

    def publish(self, package):
        existing = self.match(package, self.assets())
        action = "reused" if existing else "uploaded"
        upload_error = None
        if not existing:
            try:
                self.upload(package)
            except NetworkError as exc:
                # A timeout may follow a committed write; verify before any later attempt.
                if exc.status is not None and exc.status not in (
                    400,
                    408,
                    409,
                    429,
                    500,
                    502,
                    503,
                    504,
                ):
                    raise
                upload_error = exc
        deadline = time.monotonic() + self.config["verification_timeout_seconds"]
        while True:
            self.check()
            try:
                path = self.match(package, self.assets())
                if path and self.index_contains(package, path):
                    self.http.verify_file(self.metadata_url(path), package)
                    return {
                        "destination": self.metadata_url(path),
                        "action": action,
                        "index_verified": self.format != "raw",
                    }
            except NetworkError as exc:
                if exc.status is not None and exc.status not in (404, 408, 429, 500, 502, 503, 504):
                    raise
            if time.monotonic() >= deadline:
                raise PreconditionError(
                    "publication not verified before deadline; resume rechecks remote state",
                    details={"upload_http_status": upload_error.status if upload_error else None},
                )
            pause(self.config["poll_interval_seconds"], self.check)


def bounded_gzip(data):
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        result = stream.read(64 * 1024 * 1024 + 1)
    if len(result) > 64 * 1024 * 1024:
        raise PreconditionError("expanded repository metadata exceeds size limit")
    return result


ADAPTERS = {"directory": Directory, "nexus-apt": Nexus, "nexus-yum": Nexus, "nexus-raw": Nexus}

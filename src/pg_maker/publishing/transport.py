"""Bounded, streaming HTTP with runtime-only credentials and no redirects."""

import base64
import hashlib
import json
import netrc
import os
import ssl
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from pg_maker.errors import MakerError, PreconditionError


class NetworkError(MakerError):
    def __init__(self, status=None):
        super().__init__(
            "receiver HTTP request failed" if status else "receiver connection failed",
            details={"http_status": status},
        )
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Http:
    def __init__(self, receiver, config, check):
        self.receiver, self.config, self.check = receiver, config, check
        tls = receiver.get("tls", {})
        context = ssl.create_default_context(cafile=tls.get("ca_file"))
        if not tls.get("verify", True):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.authorization = None
        auth = receiver.get("auth", {})
        try:
            if "bearer_env" in auth:
                token = os.environ[auth["bearer_env"]]
                if not token or any(c in token for c in "\r\n"):
                    raise ValueError()
                self.authorization = "Bearer " + token
            elif auth:
                if "netrc_file" in auth:
                    path = Path(auth["netrc_file"])
                    if path.stat().st_mode & 0o077:
                        raise ValueError()
                    entry = netrc.netrc(str(path)).hosts.get(urlsplit(receiver["url"]).hostname)
                    if not entry:
                        raise ValueError()
                    username, _, password = entry
                else:
                    username, password = (
                        os.environ[auth[k]] for k in ("username_env", "password_env")
                    )
                if not username or not password or ":" in username:
                    raise ValueError()
                self.authorization = (
                    "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
                )
        except (OSError, KeyError, ValueError, netrc.NetrcParseError):
            raise PreconditionError(
                "receiver credentials unavailable or invalid; netrc must be private"
            ) from None

    def open(self, url, *, method="GET", data=None, headers=None):
        self.check()
        base = urlsplit(self.receiver["url"])
        target = urlsplit(url)
        if (
            (target.scheme, target.netloc) != (base.scheme, base.netloc)
            or target.username
            or target.password
        ):
            raise PreconditionError("receiver supplied a foreign download URL")
        merged = {"Accept-Encoding": "identity", **(headers or {})}
        if self.authorization:
            merged["Authorization"] = self.authorization
        request = Request(url, method=method, data=data, headers=merged)
        try:
            return self.opener.open(request, timeout=self.config["request_timeout_seconds"])
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise NetworkError(code) from None
        except (URLError, OSError, HTTPException):
            raise NetworkError() from None

    def blocks(self, response, chunk_size):
        while True:
            self.check()
            try:
                block = response.read(chunk_size)
            except (URLError, OSError, HTTPException):
                raise NetworkError() from None
            if not block:
                # read(n) can silently return EOF before Content-Length is exhausted.
                if getattr(response, "length", None) not in (None, 0):
                    raise NetworkError()
                return
            yield block

    def bytes(self, url, limit=64 * 1024 * 1024):
        with self.open(url) as response:
            chunks = []
            size = 0
            for block in self.blocks(response, 65536):
                size += len(block)
                if size > limit:
                    raise PreconditionError("receiver metadata exceeds size limit")
                chunks.append(block)
        return b"".join(chunks)

    def json(self, url):
        try:
            return json.loads(self.bytes(url, 16 * 1024 * 1024))
        except (ValueError, UnicodeError):
            raise PreconditionError("invalid receiver JSON") from None

    def verify_file(self, url, package):
        digest = hashlib.sha256()
        size = 0
        with self.open(url) as response:
            for block in self.blocks(response, 1024 * 1024):
                size += len(block)
                if size > package["size_bytes"]:
                    raise PreconditionError("remote package size differs")
                digest.update(block)
        if size != package["size_bytes"] or "sha256:" + digest.hexdigest() != package["sha256"]:
            raise PreconditionError("remote package bytes differ from tested artifact")


def pause(seconds, check):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))

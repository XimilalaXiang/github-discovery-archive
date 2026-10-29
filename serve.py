#!/usr/bin/env python3
"""Static file server for the GitHub Discovery Archive site.

Pure stdlib (http.server). Serves the ``web/`` directory as the site root, plus
``/data/projects.json`` from ``data/``, with sensible security headers, correct
MIME types, a ``/healthz`` endpoint, and no directory listings.

Usage:
    python3 serve.py --host 127.0.0.1 --port 8793
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(PROJECT_ROOT, "web")
DATA_ROOT = os.path.join(PROJECT_ROOT, "data")

# Extra MIME mappings (mimetypes is inconsistent across platforms for these).
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: https://avatars.githubusercontent.com; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "GithubDiscoveryArchive/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------- #

    def _send_headers(self, status: int, ctype: str, length: int, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def _maybe_gzip(self, body: bytes, ctype: str) -> tuple[bytes, dict]:
        """Gzip text payloads over 1 KB when the client advertises support."""
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        compressible = ctype.startswith("text/") or "json" in ctype or "javascript" in ctype
        if compressible and "gzip" in accept and len(body) > 1024:
            import gzip as _gzip

            compressed = _gzip.compress(body, compresslevel=6)
            if len(compressed) < len(body):
                return compressed, {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        return body, {"Vary": "Accept-Encoding"}

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _not_found(self):
        body = b'{"error":"not found"}'
        self._send_headers(404, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _resolve(self, url_path: str):
        """Resolve a URL path to a filesystem path, or None if disallowed.

        Returns ``(fs_path, is_data)``.
        """
        path = posixpath.normpath(unquote(url_path))
        if path == "/" or path == "":
            path = "/index.html"
        # Prevent path traversal.
        if path.startswith("/..") or "/../" in path or "\x00" in path:
            return None
        if path == "/data/projects.json":
            return os.path.join(DATA_ROOT, "projects.json"), True
        # Everything else is served from web/.
        rel = path.lstrip("/")
        fs_path = os.path.join(WEB_ROOT, rel)
        # Ensure resolution stays within WEB_ROOT.
        real = os.path.realpath(fs_path)
        if not (real == WEB_ROOT or real.startswith(WEB_ROOT + os.sep)):
            return None
        return real, False

    # -- verbs ------------------------------------------------------------- #

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            data_ok = os.path.exists(os.path.join(DATA_ROOT, "projects.json"))
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "github-discovery-archive",
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dataset_present": data_ok,
                },
            )
            return

        resolved = self._resolve(path)
        if resolved is None:
            self._not_found()
            return
        fs_path, is_data = resolved

        if not os.path.isfile(fs_path):
            self._not_found()
            return

        if is_data and not os.access(fs_path, os.R_OK):
            self._not_found()
            return

        ctype, _ = mimetypes.guess_type(fs_path)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/json",
            "text/javascript",
            "image/svg+xml",
        ):
            if "charset" not in ctype:
                ctype += "; charset=utf-8"

        try:
            with open(fs_path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._not_found()
            return

        body, extra = self._maybe_gzip(body, ctype)
        self._send_headers(200, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):  # keep default-ish but tidy
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Serving github-discovery-archive at http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

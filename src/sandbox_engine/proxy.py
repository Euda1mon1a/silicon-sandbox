"""Domain allowlist HTTP proxy for sandboxed network access.

Runs on port 8098 (configurable). Only allows connections to explicitly
whitelisted domains. Used by Tier B MicroVMs when networking is enabled
to prevent arbitrary internet access.

Supports:
  - HTTP CONNECT (tunneling for HTTPS)
  - HTTP GET/POST/etc. (forwarding for plain HTTP)
  - Wildcard domain matching (e.g., *.pypi.org)
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Default allowlist — overridden by config
DEFAULT_ALLOWED_DOMAINS = [
    "pypi.org",
    "*.pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "github.com",
    "*.github.com",
    "raw.githubusercontent.com",
    "api.anthropic.com",
]


class AllowlistProxy:
    """HTTP proxy server that only allows connections to whitelisted domains."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8098,
        allowed_domains: list[str] | None = None,
        deny_all: bool = True,
    ):
        self.host = host
        self.port = port
        self.allowed_domains = allowed_domains or DEFAULT_ALLOWED_DOMAINS
        self.deny_all = deny_all
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._sandbox_domains: dict[str, list[str]] = {}  # sandbox_id -> extra domains
        self._lock = threading.Lock()

    def add_sandbox_domains(self, sandbox_id: str, domains: list[str]) -> None:
        """Add per-sandbox temporary domain allowlist entries."""
        with self._lock:
            self._sandbox_domains[sandbox_id] = domains
            logger.info("Added %d domains for sandbox %s", len(domains), sandbox_id)

    def remove_sandbox_domains(self, sandbox_id: str) -> None:
        """Remove per-sandbox temporary domain allowlist entries."""
        with self._lock:
            removed = self._sandbox_domains.pop(sandbox_id, None)
            if removed:
                logger.info("Removed %d domains for sandbox %s", len(removed), sandbox_id)

    def _get_all_allowed(self) -> list[str]:
        """Get combined global + per-sandbox allowed domains."""
        with self._lock:
            extra = []
            for domains in self._sandbox_domains.values():
                extra.extend(domains)
        return self.allowed_domains + extra

    def is_domain_allowed(self, domain: str) -> bool:
        """Check if a domain is in the allowlist (supports wildcards)."""
        if not self.deny_all:
            return True

        domain = domain.lower().strip()
        # Strip port if present
        if ":" in domain:
            domain = domain.split(":")[0]

        for pattern in self._get_all_allowed():
            pattern = pattern.lower().strip()
            if pattern == domain:
                return True
            # Wildcard matching: *.example.com matches sub.example.com
            if pattern.startswith("*."):
                # Match the domain itself and any subdomain
                base = pattern[2:]
                if domain == base or domain.endswith("." + base):
                    return True
            # fnmatch for more complex patterns
            if fnmatch.fnmatch(domain, pattern):
                return True

        return False

    def start(self) -> None:
        """Start the proxy server in a background thread."""
        proxy = self

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                """Handle HTTPS tunneling."""
                host = self.path.split(":")[0]
                port = int(self.path.split(":")[1]) if ":" in self.path else 443

                if not proxy.is_domain_allowed(host):
                    self.send_error(403, f"Domain not in allowlist: {host}")
                    logger.warning("BLOCKED CONNECT to %s", host)
                    return

                logger.info("CONNECT tunnel to %s:%d", host, port)
                try:
                    remote = socket.create_connection((host, port), timeout=10)
                except (socket.error, OSError) as e:
                    self.send_error(502, f"Cannot connect to {host}:{port}: {e}")
                    return

                self.send_response(200, "Connection Established")
                self.end_headers()

                # Bidirectional tunnel
                self.connection.setblocking(False)
                remote.setblocking(False)

                bufsize = 8192
                try:
                    while True:
                        # Client -> Remote
                        try:
                            data = self.connection.recv(bufsize)
                            if data:
                                remote.sendall(data)
                            else:
                                break
                        except BlockingIOError:
                            pass

                        # Remote -> Client
                        try:
                            data = remote.recv(bufsize)
                            if data:
                                self.connection.sendall(data)
                            else:
                                break
                        except BlockingIOError:
                            pass

                        import select
                        select.select(
                            [self.connection, remote], [], [], 1.0
                        )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    remote.close()

            def do_GET(self):
                self._forward_request()

            def do_POST(self):
                self._forward_request()

            def do_PUT(self):
                self._forward_request()

            def do_DELETE(self):
                self._forward_request()

            def _forward_request(self):
                """Forward HTTP request to the target, checking allowlist."""
                parsed = urlparse(self.path)
                host = parsed.hostname or ""

                if not proxy.is_domain_allowed(host):
                    self.send_error(403, f"Domain not in allowlist: {host}")
                    logger.warning("BLOCKED %s to %s", self.command, host)
                    return

                logger.info("%s %s", self.command, self.path)
                try:
                    # Read request body if present
                    content_length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(content_length) if content_length else None

                    # Forward headers (skip hop-by-hop)
                    headers = {}
                    skip_headers = {
                        "host", "proxy-connection", "proxy-authorization",
                        "te", "trailers", "transfer-encoding", "upgrade",
                    }
                    for key, value in self.headers.items():
                        if key.lower() not in skip_headers:
                            headers[key] = value

                    with httpx.Client(timeout=30.0) as client:
                        resp = client.request(
                            method=self.command,
                            url=self.path,
                            headers=headers,
                            content=body,
                        )

                    self.send_response(resp.status_code)
                    for key, value in resp.headers.items():
                        if key.lower() not in ("transfer-encoding", "connection"):
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(resp.content)

                except Exception as e:
                    self.send_error(502, f"Proxy error: {e}")

            def log_message(self, format, *args):
                """Redirect to Python logger instead of stderr."""
                logger.debug("proxy: %s", format % args)

        self._server = HTTPServer((self.host, self.port), ProxyHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info(
            "Allowlist proxy started on %s:%d (%d domains allowed)",
            self.host, self.port, len(self.allowed_domains),
        )

    def stop(self) -> None:
        """Stop the proxy server."""
        if self._server:
            self._server.shutdown()
            self._server = None
            self._thread = None
            logger.info("Allowlist proxy stopped")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

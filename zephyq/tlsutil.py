"""TLS context helpers (Python's built-in ssl module, backed by OpenSSL)."""
from __future__ import annotations

import ssl
from typing import Optional


def server_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


def client_context(cafile: str) -> ssl.SSLContext:
    """Builds a TLS client that trusts only our lab CA and checks the server's IP."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile)
    return ctx


def describe(sock) -> str:
    """Human-readable TLS status, e.g. 'TLSv1.3 TLS_AES_256_GCM_SHA384'."""
    if not isinstance(sock, ssl.SSLSocket):
        return "plain TCP (NOT encrypted)"
    cipher: Optional[tuple] = sock.cipher()
    return "%s %s" % (sock.version(), cipher[0] if cipher else "?")

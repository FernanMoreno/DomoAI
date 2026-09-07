"""Local mTLS and certificate-rotation probe for the disposable lab."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import threading
from pathlib import Path


def _serve(
    directory: Path,
    ready: threading.Event,
    port: list[int],
    handshake: list[bool],
) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "server.pem", directory / "server.key")
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(directory / "ca.pem")
    with socket.socket() as raw:
        raw.bind(("127.0.0.1", 0))
        raw.listen(4)
        raw.settimeout(5)
        port.append(raw.getsockname()[1])
        ready.set()
        try:
            connection, _ = raw.accept()
            with context.wrap_socket(connection, server_side=True) as secured:
                secured.recv(1)
            handshake.append(True)
        except (OSError, ssl.SSLError):
            handshake.append(False)


def _connect(directory: Path, port: int, certificate: str, key: str) -> bool:
    context = ssl.create_default_context(cafile=directory / "ca.pem")
    context.load_cert_chain(directory / certificate, directory / key)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
            with context.wrap_socket(raw, server_hostname="domoai-lab-v2-server") as secured:
                secured.sendall(b"1")
        return True
    except (OSError, ssl.SSLError):
        return False


def _probe(directory: Path, certificate: str, key: str) -> bool:
    ready = threading.Event()
    port: list[int] = []
    handshake: list[bool] = []
    thread = threading.Thread(
        target=_serve,
        args=(directory, ready, port, handshake),
        daemon=True,
    )
    thread.start()
    if not ready.wait(5):
        return False
    accepted = _connect(directory, port[0], certificate, key)
    thread.join(timeout=5)
    return accepted and handshake == [True]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory
    valid = _probe(directory, "client.pem", "client.key")
    rotated = _probe(directory, "rotated-client.pem", "rotated-client.key")
    invalid = not _probe(directory, "untrusted-client.pem", "untrusted-client.key")
    expired_rejected = not _probe(directory, "expired-client.pem", "expired-client.key")
    passed = valid and rotated and invalid and expired_rejected
    print(json.dumps({"status": "passed" if passed else "failed"}, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

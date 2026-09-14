"""Minimal QEMU Machine Protocol client over a UNIX socket."""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class QMPError(RuntimeError):
    pass


class QMP:
    def __init__(self, sock_path: Path, timeout: float = 30.0):
        self.sock_path = Path(sock_path)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> "QMP":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(str(self.sock_path))
        self.sock = s
        greeting = self._read()
        if "QMP" not in greeting:
            raise QMPError(f"unexpected greeting: {greeting}")
        self.execute("qmp_capabilities")
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _read(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise QMPError("QMP socket closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def execute(self, cmd: str, **args: Any) -> Any:
        msg = {"execute": cmd}
        if args:
            msg["arguments"] = args
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            resp = self._read()
            if "event" in resp:       # skip async events
                continue
            if "error" in resp:
                raise QMPError(f"{cmd}: {resp['error'].get('class')}: {resp['error'].get('desc')}")
            return resp.get("return")

    def hmp(self, command_line: str) -> str:
        """Run a human-monitor (HMP) command such as `savevm`/`loadvm`/`info snapshots`."""
        return self.execute("human-monitor-command", **{"command-line": command_line}) or ""

    def send_key(self, *keys: str, hold_ms: int = 100) -> None:
        self.execute("send-key", keys=[{"type": "qcode", "data": k} for k in keys], **{"hold-time": hold_ms})

    def screendump(self, path: Path, fmt: str = "png") -> None:
        try:
            self.execute("screendump", filename=str(path), format=fmt)
        except QMPError:
            self.execute("screendump", filename=str(path))

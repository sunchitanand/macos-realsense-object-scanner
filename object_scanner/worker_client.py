"""Unprivileged client for the macOS RealSense camera worker."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

from .capture import CaptureStateError

_MAX_MESSAGE_BYTES = 1_048_576


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("The privileged camera worker stopped responding.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class PrivilegedCaptureClient:
    """Expose the capture-service API over a private Unix-domain socket."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = str(socket_path)

    def start(self) -> dict[str, Any]:
        return self._request("start", timeout=25.0)[0]

    def stop(self) -> dict[str, Any]:
        return self._request("stop", timeout=10.0)[0]

    def start_recording(
        self,
        name: str,
        *,
        save_fps: float = 5.0,
        max_depth_m: float = 1.5,
    ) -> dict[str, Any]:
        return self._request(
            "start_recording",
            timeout=10.0,
            name=name,
            save_fps=save_fps,
            max_depth_m=max_depth_m,
        )[0]

    def stop_recording(self) -> dict[str, Any]:
        return self._request("stop_recording", timeout=10.0)[0]

    def latest_preview(self) -> bytes | None:
        _result, payload = self._request("latest_preview", timeout=5.0)
        return payload or None

    def status(self) -> dict[str, Any]:
        return self._request("status", timeout=5.0)[0]

    def _request(
        self,
        command: str,
        *,
        timeout: float,
        **parameters: Any,
    ) -> tuple[dict[str, Any], bytes]:
        request = json.dumps(
            {"command": command, **parameters},
            separators=(",", ":"),
        ).encode("utf-8")
        if len(request) > _MAX_MESSAGE_BYTES:
            raise ValueError("Camera worker request is too large.")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self.socket_path)
            connection.sendall(struct.pack("!I", len(request)))
            connection.sendall(request)
            header_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if header_size > _MAX_MESSAGE_BYTES:
                raise RuntimeError("Camera worker response is too large.")
            header = json.loads(_recv_exact(connection, header_size).decode("utf-8"))
            payload_size = int(header.get("payload_bytes", 0))
            payload = _recv_exact(connection, payload_size) if payload_size else b""

        if not header.get("ok"):
            message = str(header.get("error", "Unknown camera worker failure."))
            if header.get("error_type") == "capture_state":
                raise CaptureStateError(message)
            raise RuntimeError(message)
        result = header.get("result")
        return (result if isinstance(result, dict) else {}), payload

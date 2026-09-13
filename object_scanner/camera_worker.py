"""Minimal privileged RealSense worker for macOS hardware access."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import stat
import struct
import tempfile
import threading
from pathlib import Path
from typing import Any

from .capture import CaptureStateError, RealSenseCaptureService

_MAX_MESSAGE_BYTES = 1_048_576


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("The camera worker connection closed unexpectedly.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_message(connection: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if size > _MAX_MESSAGE_BYTES:
        raise ValueError("Camera worker request is too large.")
    value = json.loads(_recv_exact(connection, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Camera worker request must be a JSON object.")
    return value


def _send_message(connection: socket.socket, value: dict[str, Any], payload: bytes = b"") -> None:
    header = dict(value, payload_bytes=len(payload))
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    connection.sendall(struct.pack("!I", len(encoded)))
    connection.sendall(encoded)
    if payload:
        connection.sendall(payload)


class CameraWorker:
    """Dispatch a deliberately small command surface to the physical camera."""

    def __init__(
        self,
        scan_root: Path,
        invoking_uid: int,
        invoking_gid: int,
        *,
        serial: str = "",
    ) -> None:
        self.scan_root = Path(scan_root).resolve()
        self.invoking_uid = invoking_uid
        self.invoking_gid = invoking_gid
        self.scan_root_fd = self._open_scan_root()
        self.staging_root = Path(tempfile.mkdtemp(prefix="realsense-capture-", dir="/var/tmp"))
        os.chmod(self.staging_root, 0o700)
        self.staging_fd = os.open(
            self.staging_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        self.capture = RealSenseCaptureService(self.staging_root, serial=serial)
        self._lock = threading.RLock()

    def close(self) -> None:
        try:
            state = self.capture.status()
            pending_scan_id = state.get("scan_id") if state.get("recording") else None
            self.capture.stop()
            if pending_scan_id:
                self._publish_scan(str(pending_scan_id))
        finally:
            os.close(self.staging_fd)
            os.close(self.scan_root_fd)
            try:
                self.staging_root.rmdir()
            except OSError:
                pass

    def dispatch(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        command = str(request.get("command", ""))
        with self._lock:
            if command == "status":
                return {"ok": True, "result": self.capture.status()}, b""
            if command == "start":
                return {"ok": True, "result": self.capture.start()}, b""
            if command == "stop":
                state_before = self.capture.status()
                result = self.capture.stop()
                if state_before.get("recording") and state_before.get("scan_id"):
                    self._publish_scan(str(state_before["scan_id"]))
                return {"ok": True, "result": result}, b""
            if command == "start_recording":
                result = self.capture.start_recording(
                    str(request.get("name", "")),
                    save_fps=float(request.get("save_fps", 5.0)),
                    max_depth_m=float(request.get("max_depth_m", 1.5)),
                )
                return {"ok": True, "result": result}, b""
            if command == "stop_recording":
                result = self.capture.stop_recording()
                scan_id = result.get("scan_id")
                if scan_id:
                    self._publish_scan(str(scan_id))
                return {"ok": True, "result": result}, b""
            if command == "latest_preview":
                return {"ok": True}, self.capture.latest_preview() or b""
        raise ValueError(f"Unsupported camera worker command: {command}")

    def _open_scan_root(self) -> int:
        metadata = self.scan_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or self.scan_root.is_symlink():
            raise RuntimeError("The configured scan root must be a real directory.")
        if metadata.st_uid != self.invoking_uid:
            raise RuntimeError("The configured scan root must be owned by the invoking user.")
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("The configured scan root must not be group- or world-writable.")
        return os.open(
            self.scan_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )

    def _publish_scan(self, scan_id: str) -> None:
        if Path(scan_id).name != scan_id:
            raise RuntimeError("The camera worker produced an invalid scan identifier.")
        source = self.staging_root / scan_id
        if not source.is_dir() or source.is_symlink():
            raise RuntimeError("The completed scan is missing from the privileged staging area.")
        self._chown_tree(source)
        os.rename(
            scan_id,
            scan_id,
            src_dir_fd=self.staging_fd,
            dst_dir_fd=self.scan_root_fd,
        )

    def _chown_tree(self, root: Path) -> None:
        for directory, subdirectories, files in os.walk(root, followlinks=False):
            os.chown(directory, self.invoking_uid, self.invoking_gid, follow_symlinks=False)
            for name in subdirectories:
                os.chown(
                    Path(directory) / name,
                    self.invoking_uid,
                    self.invoking_gid,
                    follow_symlinks=False,
                )
            for name in files:
                os.chown(
                    Path(directory) / name,
                    self.invoking_uid,
                    self.invoking_gid,
                    follow_symlinks=False,
                )


class _WorkerRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        worker: CameraWorker = self.server.worker  # type: ignore[attr-defined]
        try:
            request = _recv_message(self.request)
            response, payload = worker.dispatch(request)
            _send_message(self.request, response, payload)
        except Exception as exc:
            error_type = "capture_state" if isinstance(exc, CaptureStateError) else "error"
            _send_message(
                self.request,
                {"ok": False, "error": str(exc), "error_type": error_type},
            )


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--scan-root", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--serial", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        import cv2
        import numpy as np
        import pyrealsense2 as rs

        print(
            json.dumps(
                {
                    "status": "ok",
                    "opencv": cv2.__version__,
                    "numpy": np.__version__,
                    "realsense": rs.__version__,
                }
            )
        )
        return

    if os.geteuid() != 0:
        raise RuntimeError("The macOS camera worker must be launched through sudo.")

    os.umask(0o077)
    socket_path = Path(args.socket)
    if socket_path.exists() or socket_path.is_symlink():
        raise RuntimeError("Refusing to replace an existing camera worker socket.")

    worker = CameraWorker(Path(args.scan_root), args.uid, args.gid, serial=args.serial)
    server = _ThreadingUnixServer(str(socket_path), _WorkerRequestHandler)
    server.worker = worker  # type: ignore[attr-defined]
    os.chown(socket_path, args.uid, args.gid, follow_symlinks=False)
    os.chmod(socket_path, 0o600)

    def stop_server(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop_server)
    signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        worker.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()

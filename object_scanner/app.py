"""Flask application for local RealSense object scanning."""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, render_template, request, send_file

from .capture import CaptureStateError, RealSenseCaptureService
from .reconstruction import reconstruct_scan
from .worker_client import PrivilegedCaptureClient


def create_app(
    *,
    scan_root: Path | None = None,
    capture_service: Any | None = None,
    reconstruction_function: Any | None = None,
) -> Flask:
    package_root = Path(__file__).resolve().parent
    project_root = package_root.parent
    scans = Path(scan_root or os.environ.get("OBJECT_SCANNER_SCAN_ROOT", project_root / "scans"))
    scans.mkdir(parents=True, exist_ok=True)
    scans = scans.resolve()

    app = Flask(__name__)
    app.config.update(SCAN_ROOT=scans)
    worker_socket = os.environ.get("OBJECT_SCANNER_WORKER_SOCKET", "").strip()
    capture = capture_service or (
        PrivilegedCaptureClient(Path(worker_socket))
        if worker_socket
        else RealSenseCaptureService(
            scans,
            serial=os.environ.get("OBJECT_SCANNER_SERIAL", "").strip(),
        )
    )
    reconstruct = reconstruction_function or reconstruct_scan
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.RLock()

    @app.before_request
    def enforce_local_request_boundary() -> tuple[Response, int] | None:
        if not _is_loopback_hostname(_hostname_from_netloc(request.host)):
            return _error("This scanner only accepts loopback requests.", 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and not _is_loopback_hostname(_hostname_from_url(origin)):
                return _error("Cross-origin scanner controls are not allowed.", 403)
        return None

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/help")
    def help_page() -> str:
        return render_template("help.html")

    @app.get("/api/status")
    def api_status() -> Response:
        with jobs_lock:
            active_job = next(
                (dict(value, scan_id=key) for key, value in jobs.items() if value["state"] == "processing"),
                None,
            )
            if active_job is None and jobs:
                scan_id = next(reversed(jobs))
                active_job = dict(jobs[scan_id], scan_id=scan_id)
        return jsonify({"camera": capture.status(), "processing": active_job, "scans": _list_scans(scans)})

    @app.post("/api/camera/start")
    def start_camera() -> tuple[Response, int] | Response:
        try:
            return jsonify({"camera": capture.start()})
        except Exception as exc:
            return _error(str(exc), 503)

    @app.post("/api/camera/stop")
    def stop_camera() -> tuple[Response, int] | Response:
        try:
            return jsonify({"camera": capture.stop()})
        except Exception as exc:
            return _error(str(exc), 500)

    @app.get("/api/preview.jpg")
    def preview() -> tuple[Response, int] | Response:
        image = capture.latest_preview()
        if image is None:
            return Response(status=204)
        return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/scan/start")
    def start_scan() -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True) or {}
        try:
            state = capture.start_recording(
                str(payload.get("name", "")),
                save_fps=float(payload.get("save_fps", 5)),
                max_depth_m=float(payload.get("max_depth_m", 1.5)),
            )
            return jsonify({"camera": state})
        except (CaptureStateError, ValueError) as exc:
            return _error(str(exc), 400)
        except FileExistsError:
            return _error("A scan with that generated identifier already exists. Try again.", 409)

    @app.post("/api/scan/stop")
    def stop_scan() -> tuple[Response, int] | Response:
        try:
            return jsonify({"camera": capture.stop_recording()})
        except CaptureStateError as exc:
            return _error(str(exc), 400)

    @app.post("/api/scans/<scan_id>/process")
    def process_scan(scan_id: str) -> tuple[Response, int] | Response:
        scan_dir = _resolve_scan_dir(scans, scan_id, require_trusted_owner=True)
        if scan_dir is None:
            return _error("Scan not found.", 404)
        if _scan_file_exists(scans, scan_id, "result.json"):
            return _error("This scan has already been reconstructed.", 409)
        if capture.status().get("recording"):
            return _error("Stop recording before reconstruction.", 409)

        with jobs_lock:
            if any(job["state"] == "processing" for job in jobs.values()):
                return _error("Another reconstruction is already running.", 409)
            jobs[scan_id] = {"state": "processing", "progress": 0, "message": "Starting reconstruction"}

        if capture.status().get("running"):
            capture.stop()

        def update_progress(percent: int, message: str) -> None:
            with jobs_lock:
                jobs[scan_id] = {
                    "state": "processing",
                    "progress": max(0, min(100, int(percent))),
                    "message": message,
                }

        def worker() -> None:
            try:
                result = reconstruct(scan_dir, update_progress)
                with jobs_lock:
                    jobs[scan_id] = {
                        "state": "complete",
                        "progress": 100,
                        "message": "Object model ready",
                        "result": result,
                    }
            except Exception as exc:
                with jobs_lock:
                    jobs[scan_id] = {
                        "state": "error",
                        "progress": 0,
                        "message": str(exc),
                    }

        threading.Thread(target=worker, name=f"reconstruct-{scan_id}", daemon=True).start()
        return jsonify({"scan_id": scan_id, "job": jobs[scan_id]}), 202

    @app.get("/api/scans/<scan_id>/result")
    def scan_result(scan_id: str) -> tuple[Response, int] | Response:
        scan_dir = _resolve_scan_dir(scans, scan_id)
        if scan_dir is None:
            return _error("Scan not found.", 404)
        with jobs_lock:
            job = jobs.get(scan_id)
        result = _read_scan_json(scans, scan_id, "result.json")
        if result is not None:
            return jsonify(result)
        if job:
            return jsonify({"scan_id": scan_id, "job": job}), 202
        return _error("This scan has not been reconstructed.", 404)

    @app.get("/api/scans/<scan_id>/preview-data")
    def preview_data(scan_id: str) -> tuple[Response, int] | Response:
        scan_dir = _resolve_scan_dir(scans, scan_id)
        if scan_dir is None:
            return _error("Scan not found.", 404)
        file_handle = _open_scan_file(scans, scan_id, "preview_points.json")
        if file_handle is None:
            return _error("Preview data is not available.", 404)
        response = send_file(file_handle, mimetype="application/json", download_name="preview_points.json")
        response.call_on_close(file_handle.close)
        return response

    @app.get("/api/scans/<scan_id>/download/<artifact>")
    def download(scan_id: str, artifact: str) -> tuple[Response, int] | Response:
        scan_dir = _resolve_scan_dir(scans, scan_id)
        if scan_dir is None:
            return _error("Scan not found.", 404)
        allowed = {
            "raw-mesh": "model_raw.ply",
            "object-mesh": "object_mesh.ply",
            "printable-stl": "object_printable.stl",
            "object-points": "object_points.ply",
            "object-bounds": "object_bounds.ply",
            "trajectory": "trajectory.json",
            "measurements": "result.json",
        }
        filename = allowed.get(artifact)
        if filename is None:
            return _error("Requested artifact is not available.", 404)
        file_handle = _open_scan_file(scans, scan_id, filename)
        if file_handle is None:
            return _error("Requested artifact is not available.", 404)
        response = send_file(file_handle, as_attachment=True, download_name=filename)
        response.call_on_close(file_handle.close)
        return response

    @app.errorhandler(404)
    def not_found(_error_value: Any) -> tuple[Response, int]:
        if request.path.startswith("/api/"):
            return _error("Endpoint not found.", 404)
        return render_template("index.html"), 404

    app.extensions["capture_service"] = capture
    app.extensions["reconstruction_jobs"] = jobs
    atexit.register(capture.stop)
    return app


def _resolve_scan_dir(
    scan_root: Path,
    scan_id: str,
    *,
    require_trusted_owner: bool = False,
) -> Path | None:
    if not scan_id or Path(scan_id).name != scan_id:
        return None
    candidate = scan_root / scan_id
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or candidate.is_symlink():
        return None
    if require_trusted_owner and os.geteuid() == 0:
        if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
    if candidate.parent != scan_root:
        return None
    return candidate


def _list_scans(scan_root: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for directory in sorted(scan_root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        try:
            manifest = _read_scan_json(scan_root, directory.name, "manifest.json")
            if manifest is None:
                continue
            values.append(
                {
                    "scan_id": directory.name,
                    "name": manifest.get("name", directory.name),
                    "status": (
                        "complete"
                        if _scan_file_exists(scan_root, directory.name, "result.json")
                        else manifest.get("status", "captured")
                    ),
                    "frame_count": manifest.get("frame_count", 0),
                    "created_at": manifest.get("created_at"),
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return values[:20]


def _open_scan_file(scan_root: Path, scan_id: str, filename: str) -> Any | None:
    if Path(scan_id).name != scan_id or Path(filename).name != filename:
        return None
    if os.name != "posix":
        candidate = (scan_root / scan_id / filename).resolve()
        expected_parent = (scan_root / scan_id).resolve()
        if candidate.parent != expected_parent or candidate.is_symlink() or not candidate.is_file():
            return None
        return candidate.open("rb")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = scan_fd = file_fd = None
    try:
        root_fd = os.open(scan_root, directory_flags)
        scan_fd = os.open(scan_id, directory_flags, dir_fd=root_fd)
        file_fd = os.open(filename, file_flags, dir_fd=scan_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_fd)
            file_fd = None
            return None
        return os.fdopen(file_fd, "rb")
    except OSError:
        if file_fd is not None:
            os.close(file_fd)
        return None
    finally:
        if scan_fd is not None:
            os.close(scan_fd)
        if root_fd is not None:
            os.close(root_fd)


def _read_scan_json(scan_root: Path, scan_id: str, filename: str) -> dict[str, Any] | None:
    file_handle = _open_scan_file(scan_root, scan_id, filename)
    if file_handle is None:
        return None
    try:
        value = json.load(file_handle)
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        file_handle.close()


def _scan_file_exists(scan_root: Path, scan_id: str, filename: str) -> bool:
    file_handle = _open_scan_file(scan_root, scan_id, filename)
    if file_handle is None:
        return False
    file_handle.close()
    return True


def _error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _hostname_from_netloc(netloc: str) -> str:
    return _hostname_from_url(f"//{netloc}")


def _hostname_from_url(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def main() -> None:
    os.umask(0o077)
    host = os.environ.get("OBJECT_SCANNER_HOST", "127.0.0.1")
    port = int(os.environ.get("OBJECT_SCANNER_PORT", "5055"))
    if not _is_loopback_hostname(host):
        raise RuntimeError("Refusing to bind the privileged scanner outside loopback.")
    app = create_app()
    print(f"macOS RealSense Object Scanner: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

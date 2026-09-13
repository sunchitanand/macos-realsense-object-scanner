"""Hardware-free demo server for exercising the complete scanner UI."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import open3d as o3d

from .app import create_app
from .capture import CaptureStateError
from .measurement import dimensions_from_extents, quality_summary, slugify_scan_name
from .reconstruction import write_printable_stl_mm


class DemoCaptureService:
    """Generates synthetic RGB-D previews and fake frame counts."""

    def __init__(self, scan_root: Path) -> None:
        self.scan_root = Path(scan_root)
        self._lock = threading.RLock()
        self._running = False
        self._recording = False
        self._frame_count = 0
        self._scan_id: str | None = None
        self._latest_preview: bytes | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="demo-camera")
            self._thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._recording = False
            self._running = False
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        return self.status()

    def start_recording(self, name: str, *, save_fps: float, max_depth_m: float) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                raise CaptureStateError("Start the camera before starting a scan.")
            if self._recording:
                raise CaptureStateError("A scan is already being recorded.")
            slug = slugify_scan_name(name)
            stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
            self._scan_id = f"{stamp}-{slug}"
            scan_dir = self.scan_root / self._scan_id
            scan_dir.mkdir(parents=True, exist_ok=False)
            (scan_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "scan_id": self._scan_id,
                        "name": name,
                        "status": "recording",
                        "created_at": datetime.now(tz=UTC).isoformat(),
                        "capture": {
                            "saved_fps": save_fps,
                            "max_depth_m": max_depth_m,
                            "depth_scale_m_per_unit": 0.001,
                        },
                    },
                    indent=2,
                )
            )
            self._frame_count = 0
            self._recording = True
            return self.status()

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            if not self._recording:
                raise CaptureStateError("No scan is currently being recorded.")
            self._recording = False
            manifest_path = self.scan_root / self._scan_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.update(status="captured", frame_count=self._frame_count)
            manifest_path.write_text(json.dumps(manifest, indent=2))
            return self.status()

    def latest_preview(self) -> bytes | None:
        with self._lock:
            return self._latest_preview

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "recording": self._recording,
                "error": None,
                "device": {
                    "name": "Simulated RealSense D435i",
                    "serial": "DEMO",
                    "firmware": "demo",
                    "usb_type": "3.2",
                },
                "frame_count": self._frame_count,
                "scan_id": self._scan_id,
                "latest_depth_mm": 620,
                "resolution": "640x480",
                "camera_fps": 15,
                "saved_fps": 5,
                "max_depth_m": 1.5,
            }

    def _loop(self) -> None:
        phase = 0.0
        while not self._stop_event.is_set():
            rgb = np.full((480, 640, 3), 24, dtype=np.uint8)
            depth = np.zeros((480, 640), dtype=np.uint8)
            center = (320 + int(22 * np.sin(phase)), 245)
            axes = (112, 78)
            cv2.ellipse(rgb, center, axes, -14, 0, 360, (168, 126, 78), -1, cv2.LINE_AA)
            cv2.ellipse(rgb, center, axes, -14, 0, 360, (235, 235, 235), 2, cv2.LINE_AA)
            cv2.circle(rgb, (center[0] - 34, center[1] - 18), 13, (72, 72, 72), -1, cv2.LINE_AA)
            cv2.ellipse(depth, center, axes, -14, 0, 360, 155, -1, cv2.LINE_AA)
            depth_view = cv2.applyColorMap(depth, cv2.COLORMAP_CIVIDIS)
            depth_view[depth == 0] = 0

            cv2.putText(rgb, "SIMULATED RGB", (14, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (245, 245, 245), 1)
            cv2.putText(
                depth_view,
                "SIMULATED DEPTH  620 mm",
                (14, 27),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                (245, 245, 245),
                1,
            )
            if self._recording:
                cv2.putText(
                    rgb,
                    f"RECORDING  {self._frame_count} frames",
                    (14, 460),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.58,
                    (245, 245, 245),
                    1,
                )

            ok, encoded = cv2.imencode(".jpg", np.hstack((rgb, depth_view)))
            with self._lock:
                if ok:
                    self._latest_preview = encoded.tobytes()
                if self._recording:
                    self._frame_count += 1
            phase += 0.08
            time.sleep(0.2)


def demo_reconstruct(scan_dir: Path, progress: Any) -> dict[str, Any]:
    """Create a synthetic ellipsoid with metric dimensions and STL output."""
    for percent, message in (
        (10, "Loading simulated RGB-D frames"),
        (35, "Tracking the simulated camera orbit"),
        (60, "Fusing a synthetic TSDF surface"),
        (80, "Removing the simulated support plane"),
    ):
        progress(percent, message)
        time.sleep(0.18)

    extents_m = np.array([0.12, 0.08, 0.055])
    mesh = o3d.geometry.TriangleMesh.create_sphere(radius=0.5, resolution=44)
    vertices = np.asarray(mesh.vertices)
    vertices[:] = vertices * extents_m
    mesh.compute_vertex_normals()
    mesh.paint_uniform_color([0.66, 0.49, 0.30])

    points = mesh.sample_points_uniformly(number_of_points=18_000)
    colors = np.asarray(points.colors)
    positions = np.asarray(points.points)
    color_range = np.maximum(np.ptp(positions, axis=0), 1e-6)
    colors[:] = np.clip(
        0.45 + (positions - positions.min(0)) / color_range * 0.4,
        0,
        1,
    )

    object_mesh_path = scan_dir / "object_mesh.ply"
    object_points_path = scan_dir / "object_points.ply"
    raw_mesh_path = scan_dir / "model_raw.ply"
    stl_path = scan_dir / "object_printable.stl"
    o3d.io.write_triangle_mesh(str(object_mesh_path), mesh)
    o3d.io.write_triangle_mesh(str(raw_mesh_path), mesh)
    o3d.io.write_point_cloud(str(object_points_path), points)
    readiness = write_printable_stl_mm(o3d, mesh, stl_path)

    bounding_box = points.get_oriented_bounding_box()
    lines = o3d.geometry.LineSet.create_from_oriented_bounding_box(bounding_box)
    o3d.io.write_line_set(str(scan_dir / "object_bounds.ply"), lines)
    preview = {
        "points": np.round(positions[::2], 5).tolist(),
        "colors": np.round(colors[::2], 3).tolist(),
    }
    (scan_dir / "preview_points.json").write_text(json.dumps(preview, separators=(",", ":")))
    (scan_dir / "trajectory.json").write_text(json.dumps({"camera_to_world": []}))

    manifest_path = scan_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "complete"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    result = {
        "scan_id": scan_dir.name,
        "status": "complete",
        "dimensions": dimensions_from_extents(extents_m),
        "dimension_basis": "oriented principal-axis bounding box",
        "quality": quality_summary(180, 200, len(points.points)),
        "tracking": {"accepted_frames": 180, "rejected_frames": 20, "total_frames": 200},
        "print_readiness": readiness,
        "files": {
            "raw_mesh": raw_mesh_path.name,
            "object_mesh": object_mesh_path.name,
            "object_points": object_points_path.name,
            "object_bounds": "object_bounds.ply",
            "printable_stl": stl_path.name,
            "preview": "preview_points.json",
            "trajectory": "trajectory.json",
        },
        "warnings": [],
    }
    (scan_dir / "result.json").write_text(json.dumps(result, indent=2))
    progress(100, "Simulated object model ready")
    return result


def main() -> None:
    os.umask(0o077)
    project_root = Path(__file__).resolve().parent.parent
    demo_scan_root = project_root / "demo_scans"
    demo_scan_root.mkdir(exist_ok=True)
    host = os.environ.get("OBJECT_SCANNER_HOST", "127.0.0.1")
    port = int(os.environ.get("OBJECT_SCANNER_PORT", "5055"))
    capture = DemoCaptureService(demo_scan_root)
    app = create_app(
        scan_root=demo_scan_root,
        capture_service=capture,
        reconstruction_function=demo_reconstruct,
    )
    print(f"macOS RealSense Object Scanner demo: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()

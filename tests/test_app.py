from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from object_scanner.app import create_app
from object_scanner.capture import CaptureStateError


class FakeCapture:
    def __init__(self, scan_root: Path) -> None:
        self.scan_root = scan_root
        self.state = {
            "running": False,
            "recording": False,
            "error": None,
            "device": {"name": "Fake D435i", "usb_type": "3.2"},
            "frame_count": 0,
            "scan_id": None,
        }

    def start(self):
        self.state["running"] = True
        return self.status()

    def stop(self):
        self.state["running"] = False
        self.state["recording"] = False
        return self.status()

    def start_recording(self, name, *, save_fps, max_depth_m):
        if not self.state["running"]:
            raise CaptureStateError("Start the camera before starting a scan.")
        scan_id = "20260913-120000-test-object"
        scan_dir = self.scan_root / scan_id
        scan_dir.mkdir()
        (scan_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "scan_id": scan_id,
                    "name": name,
                    "status": "recording",
                    "frame_count": 0,
                    "capture": {"saved_fps": save_fps, "max_depth_m": max_depth_m},
                }
            )
        )
        self.state.update(recording=True, scan_id=scan_id)
        return self.status()

    def stop_recording(self):
        if not self.state["recording"]:
            raise CaptureStateError("No scan is currently being recorded.")
        self.state.update(recording=False, frame_count=42)
        manifest_path = self.scan_root / self.state["scan_id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(status="captured", frame_count=42)
        manifest_path.write_text(json.dumps(manifest))
        return self.status()

    def latest_preview(self):
        return b"fake-jpeg"

    def status(self):
        return dict(self.state)


def fake_reconstruct(scan_dir: Path, progress):
    progress(50, "Halfway")
    result = {
        "scan_id": scan_dir.name,
        "status": "complete",
        "dimensions": {"length_mm": 120.0, "width_mm": 80.0, "height_mm": 40.0},
        "quality": {"label": "GOOD"},
        "files": {},
    }
    (scan_dir / "result.json").write_text(json.dumps(result))
    manifest_path = scan_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "complete"
    manifest_path.write_text(json.dumps(manifest))
    progress(100, "Ready")
    return result


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.scan_root = Path(self.temp_dir.name)
        self.capture = FakeCapture(self.scan_root)
        app = create_app(
            scan_root=self.scan_root,
            capture_service=self.capture,
            reconstruction_function=fake_reconstruct,
        )
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_camera_and_scan_state_flow(self) -> None:
        self.assertEqual(self.client.post("/api/camera/start").status_code, 200)
        response = self.client.post(
            "/api/scan/start",
            json={"name": "Test object", "save_fps": 5, "max_depth_m": 1.5},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["camera"]["recording"])

        response = self.client.post("/api/scan/stop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["camera"]["frame_count"], 42)

    def test_scan_requires_running_camera(self) -> None:
        response = self.client.post(
            "/api/scan/start",
            json={"name": "Test object", "save_fps": 5, "max_depth_m": 1.5},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Start the camera", response.get_json()["error"])

    def test_path_traversal_scan_id_is_rejected(self) -> None:
        response = self.client.get("/api/scans/..%2Fsecret/result")
        self.assertEqual(response.status_code, 404)

    def test_hostile_origin_cannot_trigger_camera_controls(self) -> None:
        response = self.client.post(
            "/api/camera/start",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.capture.status()["running"])

    def test_non_loopback_host_is_rejected(self) -> None:
        response = self.client.get("/api/status", headers={"Host": "scanner.example:5055"})
        self.assertEqual(response.status_code, 403)

    def test_scan_artifact_symlink_is_rejected(self) -> None:
        scan_id = "20260913-120000-symlink"
        scan_dir = self.scan_root / scan_id
        scan_dir.mkdir()
        (scan_dir / "manifest.json").write_text(
            json.dumps({"scan_id": scan_id, "name": "Symlink", "status": "captured"})
        )
        outside = self.scan_root / "outside.json"
        outside.write_text(json.dumps({"secret": "must-not-be-served"}))
        (scan_dir / "result.json").symlink_to(outside)

        self.assertEqual(self.client.get(f"/api/scans/{scan_id}/result").status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/scans/{scan_id}/download/measurements").status_code,
            404,
        )

    def test_processing_completes_and_result_is_available(self) -> None:
        self.client.post("/api/camera/start")
        start = self.client.post(
            "/api/scan/start",
            json={"name": "Test object", "save_fps": 5, "max_depth_m": 1.5},
        )
        scan_id = start.get_json()["camera"]["scan_id"]
        self.client.post("/api/scan/stop")

        response = self.client.post(f"/api/scans/{scan_id}/process")
        self.assertEqual(response.status_code, 202)
        for _ in range(30):
            result = self.client.get(f"/api/scans/{scan_id}/result")
            if result.status_code == 200:
                break
            time.sleep(0.01)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["dimensions"]["length_mm"], 120.0)
        self.assertEqual(self.client.post(f"/api/scans/{scan_id}/process").status_code, 409)


if __name__ == "__main__":
    unittest.main()

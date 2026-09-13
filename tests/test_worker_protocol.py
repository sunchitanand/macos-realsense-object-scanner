from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from object_scanner.camera_worker import (
    CameraWorker,
    _ThreadingUnixServer,
    _WorkerRequestHandler,
)
from object_scanner.capture import CaptureStateError
from object_scanner.worker_client import PrivilegedCaptureClient


class FakeWorker:
    def dispatch(self, request):
        command = request["command"]
        if command == "status":
            return {"ok": True, "result": {"running": True}}, b""
        if command == "latest_preview":
            return {"ok": True}, b"jpeg"
        if command == "stop_recording":
            raise CaptureStateError("Nothing is recording.")
        raise RuntimeError("Unsupported test command.")


class WorkerProtocolTests(unittest.TestCase):
    def test_launcher_elevates_only_the_root_owned_worker_bundle(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        launcher = (project_root / "run_object_scanner.sh").read_text()
        self.assertIn('sudo -n "$secure_worker"', launcher)
        self.assertNotIn('sudo -n env PYTHONPATH="$runtime_pythonpath"', launcher)

    def test_client_round_trip_and_capture_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "camera.sock"
            server = _ThreadingUnixServer(str(socket_path), _WorkerRequestHandler)
            server.worker = FakeWorker()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = PrivilegedCaptureClient(socket_path)
                self.assertTrue(client.status()["running"])
                self.assertEqual(client.latest_preview(), b"jpeg")
                with self.assertRaisesRegex(CaptureStateError, "Nothing is recording"):
                    client.stop_recording()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                socket_path.unlink(missing_ok=True)

    def test_completed_scan_moves_from_private_staging_to_user_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scan_root = Path(directory)
            os.chmod(scan_root, 0o700)
            worker = CameraWorker(scan_root, os.getuid(), os.getgid())
            scan_id = "20260913-120000-object"
            staged_scan = worker.staging_root / scan_id
            staged_scan.mkdir(mode=0o700)
            (staged_scan / "manifest.json").write_text('{"status":"captured"}')
            try:
                worker._publish_scan(scan_id)
                self.assertTrue((scan_root / scan_id / "manifest.json").is_file())
                self.assertFalse(staged_scan.exists())
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()

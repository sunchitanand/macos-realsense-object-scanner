"""Threaded RealSense RGB-D capture with browser-friendly preview frames."""

from __future__ import annotations

import csv
import json
import platform
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .measurement import slugify_scan_name


class CaptureStateError(RuntimeError):
    """Raised when a capture action is invalid for the current state."""


def validate_realsense_runtime(version: str, *, system: str | None = None) -> None:
    """Reject macOS SDK builds that contain the D435i HID/libusb crash."""
    current_system = system or platform.system()
    numbers = tuple(int(part) for part in re.findall(r"\d+", version)[:3])
    if current_system == "Darwin" and numbers < (2, 58, 4):
        raise RuntimeError(
            f"RealSense SDK {version} is unsafe on macOS with a D435i. "
            "Run scripts/install_realsense_macos.sh to install SDK 2.58.4."
        )


def choose_stream_profile(usb_type: str) -> dict[str, int]:
    """Use a lower-bandwidth native profile when the camera negotiates USB 2."""
    if str(usb_type).startswith("3"):
        return {
            "depth_width": 640,
            "depth_height": 480,
            "color_width": 640,
            "color_height": 480,
            "fps": 15,
        }
    return {
        "depth_width": 480,
        "depth_height": 270,
        "color_width": 424,
        "color_height": 240,
        "fps": 15,
    }


def reset_macos_camera_helpers() -> None:
    """Release UVC interfaces that macOS camera services claim automatically."""
    if platform.system() != "Darwin":
        return
    subprocess.run(
        ["killall", "VDCAssistant", "UVCAssistant", "cameracaptured"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class RealSenseCaptureService:
    """Owns the RealSense pipeline and records aligned RGB-D datasets."""

    def __init__(
        self,
        scan_root: Path,
        *,
        serial: str = "",
        width: int = 640,
        height: int = 480,
        fps: int = 15,
    ) -> None:
        self.scan_root = Path(scan_root)
        self.serial = serial
        self.width = width
        self.height = height
        self.depth_width = width
        self.depth_height = height
        self.fps = fps

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pipeline: Any = None
        self._align: Any = None
        self._cv2: Any = None
        self._np: Any = None
        self._rs: Any = None

        self._running = False
        self._recording = False
        self._error: str | None = None
        self._latest_preview: bytes | None = None
        self._latest_depth_mm: int | None = None
        self._device_info: dict[str, Any] = {}
        self._intrinsics: dict[str, Any] = {}
        self._depth_scale_m = 0.001

        self._scan_id: str | None = None
        self._scan_dir: Path | None = None
        self._frame_count = 0
        self._save_fps = 5.0
        self._max_depth_m = 1.5
        self._next_save_at = 0.0
        self._record_started_at = 0.0
        self._timestamp_file: Any = None
        self._timestamp_writer: csv.writer | None = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()

            try:
                import cv2
                import numpy as np
                import pyrealsense2 as rs
            except ImportError as exc:
                raise RuntimeError(
                    "Camera dependencies are missing. Run the application launcher to install them."
                ) from exc
            validate_realsense_runtime(getattr(rs, "__version__", "0"))

            attempts = 2 if platform.system() == "Darwin" else 1
            last_error: Exception | None = None
            pipeline = None
            profile = None
            align = None
            color_frame = None
            depth_frame = None
            for _attempt in range(attempts):
                try:
                    usb_type = self._prepare_device(rs)
                    stream_profile = choose_stream_profile(usb_type)
                    self.depth_width = stream_profile["depth_width"]
                    self.depth_height = stream_profile["depth_height"]
                    self.width = stream_profile["color_width"]
                    self.height = stream_profile["color_height"]
                    self.fps = stream_profile["fps"]

                    pipeline = rs.pipeline()
                    config = rs.config()
                    if self.serial:
                        config.enable_device(self.serial)
                    config.enable_stream(
                        rs.stream.depth,
                        self.depth_width,
                        self.depth_height,
                        rs.format.z16,
                        self.fps,
                    )
                    config.enable_stream(
                        rs.stream.color,
                        self.width,
                        self.height,
                        rs.format.bgr8,
                        self.fps,
                    )
                    self._disable_global_time(rs, pipeline, config)
                    profile = pipeline.start(config)
                    align = rs.align(rs.stream.color)
                    frames = pipeline.wait_for_frames(timeout_ms=6000)
                    aligned = align.process(frames)
                    color_frame = aligned.get_color_frame()
                    depth_frame = aligned.get_depth_frame()
                    if not color_frame or not depth_frame:
                        raise RuntimeError("The D435i opened but did not deliver an aligned RGB-D frame.")
                    break
                except Exception as exc:
                    last_error = exc
                    if pipeline is not None:
                        try:
                            pipeline.stop()
                        except Exception:
                            pass
                    pipeline = None
                    profile = None
                    time.sleep(0.5)
            if profile is None or pipeline is None or color_frame is None or depth_frame is None:
                message = str(last_error) if last_error else "unknown camera startup failure"
                self._error = message
                raise RuntimeError(
                    "Could not recover the D435i RGB-D streams automatically. "
                    f"Native driver response: {message}"
                ) from last_error

            device = profile.get_device()
            color_profile = profile.get_stream(rs.stream.color).as_video_stream_profile()
            intrinsics = color_profile.get_intrinsics()
            depth_sensor = device.first_depth_sensor()

            def device_value(key: Any) -> str:
                try:
                    return device.get_info(key)
                except Exception:
                    return "unknown"

            self._device_info = {
                "name": device_value(rs.camera_info.name),
                "serial": device_value(rs.camera_info.serial_number),
                "firmware": device_value(rs.camera_info.firmware_version),
                "usb_type": device_value(rs.camera_info.usb_type_descriptor),
            }
            self._intrinsics = {
                "width": intrinsics.width,
                "height": intrinsics.height,
                "intrinsic_matrix": [
                    intrinsics.fx,
                    0,
                    0,
                    0,
                    intrinsics.fy,
                    0,
                    intrinsics.ppx,
                    intrinsics.ppy,
                    1,
                ],
            }
            self._depth_scale_m = float(depth_sensor.get_depth_scale())

            self._cv2 = cv2
            self._np = np
            self._rs = rs
            self._pipeline = pipeline
            self._align = align
            self._stop_event.clear()
            self._error = None
            self._running = True
            color = np.asanyarray(color_frame.get_data())
            depth = np.asanyarray(depth_frame.get_data())
            self._update_preview(color, depth)
            self._thread = threading.Thread(target=self._capture_loop, name="realsense-capture", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._recording:
                self._stop_recording_locked()
            if not self._running:
                return self.status()
            self._stop_event.set()
            thread = self._thread

        if thread is not None:
            thread.join(timeout=6)

        with self._lock:
            if self._pipeline is not None:
                try:
                    self._pipeline.stop()
                except Exception:
                    pass
            self._running = False
            self._pipeline = None
            self._thread = None
            return self.status()

    def start_recording(
        self,
        name: str,
        *,
        save_fps: float = 5.0,
        max_depth_m: float = 1.5,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                raise CaptureStateError("Start the camera before starting a scan.")
            if self._recording:
                raise CaptureStateError("A scan is already being recorded.")
            if not 1.0 <= save_fps <= self.fps:
                raise ValueError(f"Save rate must be between 1 and {self.fps} frames per second.")
            if not 0.4 <= max_depth_m <= 4.0:
                raise ValueError("Maximum depth must be between 0.4 and 4.0 metres.")

            slug = slugify_scan_name(name)
            stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
            scan_id = f"{stamp}-{slug}"
            scan_dir = self.scan_root / scan_id
            scan_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            (scan_dir / "color").mkdir(mode=0o700)
            (scan_dir / "depth").mkdir(mode=0o700)

            (scan_dir / "camera_intrinsic.json").write_text(
                json.dumps(self._intrinsics, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest = {
                "scan_id": scan_id,
                "name": name.strip(),
                "status": "recording",
                "created_at": datetime.now(tz=UTC).isoformat(),
                "camera": {
                    key: value for key, value in self._device_info.items() if key != "serial"
                },
                "capture": {
                    "width": self.width,
                    "height": self.height,
                    "camera_fps": self.fps,
                    "saved_fps": save_fps,
                    "max_depth_m": max_depth_m,
                    "depth_scale_m_per_unit": self._depth_scale_m,
                    "depth_aligned_to_color": True,
                },
            }
            self._write_json(scan_dir / "manifest.json", manifest)

            timestamp_file = (scan_dir / "timestamps.csv").open("w", newline="", encoding="utf-8")
            timestamp_writer = csv.writer(timestamp_file)
            timestamp_writer.writerow(["frame", "device_timestamp_ms", "elapsed_s"])

            self._scan_id = scan_id
            self._scan_dir = scan_dir
            self._frame_count = 0
            self._save_fps = float(save_fps)
            self._max_depth_m = float(max_depth_m)
            self._record_started_at = time.monotonic()
            self._next_save_at = self._record_started_at
            self._timestamp_file = timestamp_file
            self._timestamp_writer = timestamp_writer
            self._recording = True
            return self.status()

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            if not self._recording:
                raise CaptureStateError("No scan is currently being recorded.")
            self._stop_recording_locked()
            return self.status()

    def latest_preview(self) -> bytes | None:
        with self._lock:
            return self._latest_preview

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "recording": self._recording,
                "error": self._error,
                "device": dict(self._device_info),
                "frame_count": self._frame_count,
                "scan_id": self._scan_id,
                "latest_depth_mm": self._latest_depth_mm,
                "resolution": f"{self.width}x{self.height}",
                "depth_resolution": f"{self.depth_width}x{self.depth_height}",
                "camera_fps": self.fps,
                "saved_fps": self._save_fps,
                "max_depth_m": self._max_depth_m,
            }

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                frames = self._pipeline.wait_for_frames(timeout_ms=5000)
                aligned = self._align.process(frames)
                color_frame = aligned.get_color_frame()
                depth_frame = aligned.get_depth_frame()
                if not color_frame or not depth_frame:
                    continue

                color = self._np.asanyarray(color_frame.get_data())
                depth = self._np.asanyarray(depth_frame.get_data())
                self._update_preview(color, depth)

                with self._lock:
                    should_save = self._recording and time.monotonic() >= self._next_save_at
                    if should_save:
                        self._save_frame_locked(color, depth, float(color_frame.get_timestamp()))
            except Exception as exc:
                with self._lock:
                    if not self._stop_event.is_set():
                        self._error = str(exc)
                time.sleep(0.1)

    def _prepare_device(self, rs: Any) -> str:
        if platform.system() == "Darwin":
            reset_macos_camera_helpers()

        context = rs.context()
        devices = context.query_devices()
        device = None
        for candidate in devices:
            try:
                serial = candidate.get_info(rs.camera_info.serial_number)
            except Exception:
                serial = ""
            if not self.serial or serial == self.serial:
                device = candidate
                break
        if device is None:
            raise RuntimeError("No matching RealSense D435i was detected.")

        try:
            usb_type = device.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb_type = "unknown"

        if platform.system() != "Darwin":
            return usb_type

        device.hardware_reset()
        del device
        del devices
        del context
        time.sleep(4)
        reset_macos_camera_helpers()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            reconnect_context = rs.context()
            reconnect_devices = reconnect_context.query_devices()
            for candidate in reconnect_devices:
                try:
                    serial = candidate.get_info(rs.camera_info.serial_number)
                except Exception:
                    serial = ""
                if self.serial and serial != self.serial:
                    continue
                try:
                    return candidate.get_info(rs.camera_info.usb_type_descriptor)
                except Exception:
                    return "unknown"
            del reconnect_devices
            del reconnect_context
            time.sleep(0.5)
        raise RuntimeError("The D435i did not reconnect after its macOS hardware reset.")

    @staticmethod
    def _disable_global_time(rs: Any, pipeline: Any, config: Any) -> None:
        if platform.system() != "Darwin":
            return
        resolved = config.resolve(rs.pipeline_wrapper(pipeline))
        for sensor in resolved.get_device().query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 0)

    def _update_preview(self, color: Any, depth: Any) -> None:
        color_view = color.copy()
        max_units = max(1.0, self._max_depth_m / self._depth_scale_m)
        clipped = self._np.clip(depth, 0, max_units)
        intensity = self._np.zeros_like(depth, dtype=self._np.uint8)
        valid = depth > 0
        intensity[valid] = self._np.round(255.0 * (1.0 - clipped[valid] / max_units)).astype(self._np.uint8)
        depth_view = self._cv2.applyColorMap(intensity, self._cv2.COLORMAP_CIVIDIS)
        depth_view[~valid] = 0

        center_y = depth.shape[0] // 2
        center_x = depth.shape[1] // 2
        center_mm = int(round(float(depth[center_y, center_x]) * self._depth_scale_m * 1000.0))
        label = "NO DEPTH" if center_mm <= 0 else f"CENTER {center_mm} mm"

        self._draw_label(color_view, "RGB", (12, 24))
        self._draw_label(depth_view, f"DEPTH  {label}", (12, 24))
        if self._recording:
            self._draw_label(
                color_view,
                f"RECORDING  {self._frame_count} frames",
                (12, color_view.shape[0] - 16),
            )

        composite = self._np.hstack((color_view, depth_view))
        ok, encoded = self._cv2.imencode(".jpg", composite, [self._cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            with self._lock:
                self._latest_preview = encoded.tobytes()
                self._latest_depth_mm = center_mm if center_mm > 0 else None

    def _save_frame_locked(self, color: Any, depth: Any, timestamp_ms: float) -> None:
        if self._scan_dir is None or self._timestamp_writer is None:
            return

        frame_name = f"{self._frame_count:06d}"
        color_path = self._scan_dir / "color" / f"{frame_name}.jpg"
        depth_path = self._scan_dir / "depth" / f"{frame_name}.png"
        if not self._cv2.imwrite(str(color_path), color, [self._cv2.IMWRITE_JPEG_QUALITY, 94]):
            raise RuntimeError(f"Could not write {color_path}")
        if not self._cv2.imwrite(str(depth_path), depth):
            raise RuntimeError(f"Could not write {depth_path}")

        elapsed = time.monotonic() - self._record_started_at
        self._timestamp_writer.writerow([self._frame_count, f"{timestamp_ms:.3f}", f"{elapsed:.3f}"])
        self._timestamp_file.flush()
        self._frame_count += 1
        self._next_save_at += 1.0 / self._save_fps

    def _stop_recording_locked(self) -> None:
        scan_dir = self._scan_dir
        duration = max(0.0, time.monotonic() - self._record_started_at)
        if self._timestamp_file is not None:
            self._timestamp_file.close()

        if scan_dir is not None:
            manifest_path = scan_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(
                {
                    "status": "captured",
                    "completed_at": datetime.now(tz=UTC).isoformat(),
                    "duration_s": round(duration, 3),
                    "frame_count": self._frame_count,
                }
            )
            self._write_json(manifest_path, manifest)

        self._recording = False
        self._timestamp_file = None
        self._timestamp_writer = None
        self._scan_dir = None

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _draw_label(self, image: Any, text: str, origin: tuple[int, int]) -> None:
        self._cv2.putText(
            image,
            text,
            origin,
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            4,
            self._cv2.LINE_AA,
        )
        self._cv2.putText(
            image,
            text,
            origin,
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (245, 245, 245),
            1,
            self._cv2.LINE_AA,
        )

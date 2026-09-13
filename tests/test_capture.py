from __future__ import annotations

import unittest
from unittest.mock import patch

from object_scanner.capture import RealSenseCaptureService, choose_stream_profile, validate_realsense_runtime


class RealSenseRuntimeTests(unittest.TestCase):
    def test_rejects_crashing_macos_sdk(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsafe on macOS"):
            validate_realsense_runtime("2.56.5", system="Darwin")

    def test_accepts_fixed_macos_sdk(self) -> None:
        validate_realsense_runtime("2.58.4", system="Darwin")

    def test_old_sdk_is_not_blocked_on_other_platforms(self) -> None:
        validate_realsense_runtime("2.56.5", system="Linux")

    def test_usb2_uses_lower_bandwidth_native_profiles(self) -> None:
        profile = choose_stream_profile("2.1")
        self.assertEqual((profile["depth_width"], profile["depth_height"]), (480, 270))
        self.assertEqual((profile["color_width"], profile["color_height"]), (424, 240))

    def test_usb3_uses_full_vga_profiles(self) -> None:
        profile = choose_stream_profile("3.2")
        self.assertEqual((profile["depth_width"], profile["depth_height"]), (640, 480))
        self.assertEqual((profile["color_width"], profile["color_height"]), (640, 480))

    def test_capture_does_not_pin_a_device_serial_by_default(self) -> None:
        capture = RealSenseCaptureService(scan_root="unused")
        self.assertEqual(capture.serial, "")

    def test_non_macos_capture_reads_the_negotiated_usb_link(self) -> None:
        class FakeDevice:
            def get_info(self, _key):
                return "2.1"

        class FakeContext:
            def query_devices(self):
                return [FakeDevice()]

        class FakeCameraInfo:
            serial_number = object()
            usb_type_descriptor = object()

        class FakeRealSense:
            camera_info = FakeCameraInfo()

            @staticmethod
            def context():
                return FakeContext()

        capture = RealSenseCaptureService(scan_root="unused")
        with patch("object_scanner.capture.platform.system", return_value="Linux"):
            usb_type = capture._prepare_device(FakeRealSense())
        self.assertEqual(usb_type, "2.1")


if __name__ == "__main__":
    unittest.main()

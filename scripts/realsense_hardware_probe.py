"""Open the physical RealSense camera and prove RGB-D frames are arriving."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time


def device_value(device: object, key: object) -> str:
    try:
        return device.get_info(key)  # type: ignore[attr-defined, no-any-return]
    except Exception:
        return "unknown"


def reset_macos_camera_helpers() -> None:
    if platform.system() != "Darwin":
        return
    subprocess.run(
        ["killall", "VDCAssistant", "UVCAssistant", "cameracaptured"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    import pyrealsense2 as rs

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("both", "depth", "color"), default="both")
    parser.add_argument("--disable-global-time", action="store_true")
    parser.add_argument("--depth-width", type=int, default=640)
    parser.add_argument("--depth-height", type=int, default=480)
    parser.add_argument("--color-width", type=int, default=640)
    parser.add_argument("--color-height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--color-format", choices=("bgr8", "mjpeg", "yuyv"), default="bgr8")
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--hardware-reset", action="store_true")
    args = parser.parse_args()

    log_fd, log_path = tempfile.mkstemp(prefix="realsense-sdk-probe-", suffix=".log")
    os.close(log_fd)
    os.chmod(log_path, 0o600)
    rs.log_to_console(rs.log_severity.debug)
    rs.log_to_file(rs.log_severity.debug, log_path)

    if args.list_profiles:
        context = rs.context()
        devices = context.query_devices()
        if not devices:
            raise RuntimeError("No RealSense camera detected.")
        profiles = []
        for sensor in devices[0].query_sensors():
            sensor_name = device_value(sensor, rs.camera_info.name)
            for profile in sensor.get_stream_profiles():
                video = profile.as_video_stream_profile()
                profiles.append(
                    {
                        "sensor": sensor_name,
                        "stream": str(profile.stream_type()),
                        "format": str(profile.format()),
                        "width": video.width(),
                        "height": video.height(),
                        "fps": profile.fps(),
                    }
                )
        print(json.dumps(profiles, indent=2), flush=True)
        return 0

    if args.hardware_reset:
        reset_context = rs.context()
        devices = reset_context.query_devices()
        if not devices:
            raise RuntimeError("No RealSense camera detected for hardware reset.")
        devices[0].hardware_reset()
        del devices
        del reset_context
        time.sleep(4)
        reset_macos_camera_helpers()
        time.sleep(1)

    pipeline = rs.pipeline()
    config = rs.config()
    if args.mode in {"both", "depth"}:
        config.enable_stream(
            rs.stream.depth,
            args.depth_width,
            args.depth_height,
            rs.format.z16,
            args.fps,
        )
    if args.mode in {"both", "color"}:
        color_format = getattr(rs.format, args.color_format)
        config.enable_stream(
            rs.stream.color,
            args.color_width,
            args.color_height,
            color_format,
            args.fps,
        )

    profile = None
    try:
        if args.disable_global_time:
            resolved = config.resolve(rs.pipeline_wrapper(pipeline))
            device = resolved.get_device()
            for sensor in device.query_sensors():
                if sensor.supports(rs.option.global_time_enabled):
                    sensor.set_option(rs.option.global_time_enabled, 0)
        profile = pipeline.start(config)
        deadline = time.monotonic() + 12
        frames = None
        while time.monotonic() < deadline:
            candidate = pipeline.wait_for_frames(3000)
            has_depth = args.mode == "color" or bool(candidate.get_depth_frame())
            has_color = args.mode == "depth" or bool(candidate.get_color_frame())
            if has_depth and has_color:
                frames = candidate
                break
        if frames is None:
            raise RuntimeError(f"Camera opened, but no {args.mode} frame arrived.")

        depth = frames.get_depth_frame() if args.mode in {"both", "depth"} else None
        color = frames.get_color_frame() if args.mode in {"both", "color"} else None
        device = profile.get_device()
        result = {
            "status": "ok",
            "mode": args.mode,
            "sdk_version": rs.__version__,
            "device": {
                "name": device_value(device, rs.camera_info.name),
                "serial": device_value(device, rs.camera_info.serial_number),
                "firmware": device_value(device, rs.camera_info.firmware_version),
                "usb_type": device_value(device, rs.camera_info.usb_type_descriptor),
            },
        }
        if depth:
            center_distance_m = depth.get_distance(depth.get_width() // 2, depth.get_height() // 2)
            result["depth"] = {
                "width": depth.get_width(),
                "height": depth.get_height(),
                "frame_number": depth.get_frame_number(),
                "center_distance_m": round(center_distance_m, 4),
            }
        if color:
            result["color"] = {
                "width": color.get_width(),
                "height": color.get_height(),
                "frame_number": color.get_frame_number(),
            }
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps({"status": "error", "error": str(exc), "sdk_log": log_path}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        if profile is not None:
            pipeline.stop()


if __name__ == "__main__":
    raise SystemExit(main())

# macOS RealSense Object Scanner

[![CI](https://github.com/sunchitanand/macos-realsense-object-scanner/actions/workflows/ci.yml/badge.svg)](https://github.com/sunchitanand/macos-realsense-object-scanner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-white.svg)](LICENSE)

A local, browser-based RGB-D scanning application for the Intel RealSense D435i. It records
aligned color and metric depth frames, estimates free-form camera motion, fuses the observations
into a surface, measures the isolated object, and exports PLY and STL files.

The project is macOS-first and includes a pinned Apple Silicon build of librealsense that avoids
the D435i HID/libusb crash present in older unofficial macOS Python packages.

> [!IMPORTANT]
> This is an experimental scanner, not a metrology tool. It is appropriate for reference models,
> approximate dimensions, and larger objects. Validate critical dimensions with calipers.

> [!WARNING]
> Version 0.1.0 builds the privileged macOS camera worker locally from the checked-out source.
> No publisher-signed or notarized helper binary is distributed. Only launch hardware mode from
> a repository and local account you trust.

## What it does

- Streams aligned RGB and metric depth from a RealSense D435i.
- Adapts capture profiles for USB 2 and USB 3 links.
- Recovers the camera automatically from common macOS UVC interface failures.
- Records synchronized color JPEGs and 16-bit depth PNGs.
- Tracks camera motion with Open3D RGB-D odometry.
- Fuses frames into a TSDF volume and extracts the dominant object.
- Reports oriented bounding-box dimensions in millimetres.
- Exports point clouds, colored PLY meshes, and a millimetre-scaled printable STL.
- Includes a complete hardware-free demo mode.

## Current platform support

| Platform | Hardware capture | Demo mode |
| --- | --- | --- |
| Apple Silicon macOS | Supported with the bundled installer | Supported |
| Linux x86-64 | Uses the official `pyrealsense2` wheel | Supported |
| Intel macOS | Not currently supported by the native installer | Supported |
| Windows | Untested | Expected to work with compatible dependencies |

Hardware mode on macOS currently targets Python 3.12 and librealsense 2.58.4.

## Requirements

### Hardware

- Intel RealSense D435i
- A direct USB 3 data cable is strongly recommended
- A static object with a matte or textured surface

USB charging cables frequently negotiate at USB 2 speed. The interface reports the detected link.
USB 2 works at reduced capture resolution; USB 3 enables the full 640×480 profile used by this app.

### macOS software

- Apple Silicon Mac
- macOS 15 or later
- Homebrew
- Python 3.12
- Xcode Command Line Tools

Install the prerequisites:

```bash
xcode-select --install
brew install python@3.12 cmake libusb
```

## Quick start

```bash
git clone https://github.com/sunchitanand/macos-realsense-object-scanner.git
cd macos-realsense-object-scanner
./run_object_scanner.sh
```

Then open <http://127.0.0.1:5055>.

The first hardware launch:

1. Creates a Python 3.12 virtual environment.
2. Installs the Python application.
3. Builds the pinned official librealsense source for Apple Silicon.
4. Freezes the camera worker into a self-contained bundle.
5. Installs that bundle in a root-owned, non-writable system directory.
6. Keeps the Flask server, reconstruction pipeline, and file downloads unprivileged.

The native build is cached under `.native/` and is not repeated on later launches.

To select a specific camera when multiple RealSense devices are connected:

```bash
OBJECT_SCANNER_SERIAL=your-camera-serial ./run_object_scanner.sh
```

## Demo mode

Demo mode exercises camera controls, capture state, reconstruction progress, the 3D result viewer,
measurements, and STL downloads without a physical RealSense camera.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
./run_object_scanner_demo.sh
```

## Scanning workflow

1. Place one object on a clear, flat surface.
2. Start the camera and verify that both RGB and depth are updating.
3. Keep the object fixed.
4. Start capture and move the camera slowly around the object.
5. Maintain heavy overlap and include high and low viewing angles.
6. Finish near the starting view and stop capture.
7. Run **Stitch and measure**.
8. Review tracking quality before trusting dimensions or exporting the STL.

For best results, keep the camera roughly 0.4–0.8 metres from medium-sized objects. Glossy,
transparent, very dark, thin, or textureless surfaces can produce missing depth or tracking loss.

## Reconstruction pipeline

```text
Aligned RGB-D frames
        │
        ▼
Hybrid RGB-D odometry
        │
        ▼
Metric TSDF integration
        │
        ▼
Support-plane removal + clustering
        │
        ▼
Object point cloud + oriented bounds
        │
        ▼
Mesh cleanup + millimetre STL export
```

Scans are stored locally under `scans/<scan-id>/`:

```text
color/                  aligned RGB frames
depth/                  aligned 16-bit depth frames
camera_intrinsic.json   color-camera intrinsics
timestamps.csv          frame timing
manifest.json           device and capture metadata
model_raw.ply           fused scene mesh
object_points.ply       isolated object point cloud
object_mesh.ply         isolated object mesh
object_printable.stl    millimetre-scaled STL
result.json             dimensions and quality report
trajectory.json         estimated camera poses
```

`scans/`, `demo_scans/`, compiled native libraries, and virtual environments are ignored by Git.

## Accuracy and limitations

The output voxel size is not the same as measurement accuracy. Results are affected by:

- RealSense depth noise and calibration
- Camera-to-object distance
- USB link speed and selected profile
- Motion blur and odometry drift
- Missing depth around occlusion boundaries
- Surface reflectivity and infrared texture
- Support-plane removal and object segmentation
- Incomplete viewing coverage

This project should not be used for safety-critical inspection, tight mechanical fits, threads,
small holes, or sub-millimetre reverse engineering. Benchmark it against an object with known
dimensions before relying on a new setup.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/pytest
node --check object_scanner/static/app.js
zsh -n run_object_scanner.sh run_object_scanner_demo.sh scripts/install_realsense_macos.sh
zsh -n scripts/install_camera_worker_macos.sh
```

The hardware probe can test individual or combined interfaces:

```bash
PYTHONPATH=.native/realsense-2.58.4-cp312-arm64 \
  sudo .venv/bin/python scripts/realsense_hardware_probe.py --mode both
```

## Security

The hardware launcher uses `sudo` only for an isolated camera worker because the current macOS
RSUSB backend requires direct USB access. The Flask server, reconstruction process, and downloads
remain under your normal user account. The server binds to `127.0.0.1`, rejects non-loopback hosts
and cross-origin control requests, and refuses remote binding. The worker is frozen into a
self-contained bundle and executed only after it is copied into a root-owned, non-writable
directory under `/Library/Application Support`.

See [SECURITY.md](SECURITY.md) for reporting instructions.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © 2026 Sunchit Anand. See [LICENSE](LICENSE).

Intel RealSense and librealsense are separate projects and trademarks of their respective owners.
This repository is not affiliated with or endorsed by Intel or RealSense.

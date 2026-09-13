# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] - 2026-09-13

### Added

- Local Flask interface for guided RGB-D object capture.
- Aligned RealSense color and metric depth recording.
- Open3D pose-graph optimization, loop closure, TSDF fusion, fail-closed object extraction, and
  minimum-volume dimension estimation.
- PLY point-cloud and mesh artifacts plus millimetre-scaled STL export.
- Apple Silicon librealsense 2.58.4 installer with macOS camera recovery.
- Isolated privileged camera worker with a root-owned frozen runtime.
- USB 2 fallback and USB 3 capture profiles.
- Hardware-free synthetic demo mode.
- Automated tests and linting.

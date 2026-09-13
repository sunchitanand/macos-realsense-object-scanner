# Contributing

Thanks for helping improve macOS RealSense Object Scanner.

## Development setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Run the checks before opening a pull request:

```bash
.venv/bin/ruff check .
.venv/bin/pytest
node --check object_scanner/static/app.js
zsh -n run_object_scanner.sh run_object_scanner_demo.sh \
  scripts/install_realsense_macos.sh scripts/install_camera_worker_macos.sh
```

## Pull requests

- Keep changes focused and explain the user-visible outcome.
- Include tests for behavioral changes.
- Do not commit scans, camera serials, native binaries, virtual environments, or personal paths.
- Call out any behavior tested only with synthetic data.
- For hardware fixes, include the camera model, firmware, macOS version, USB link type, and a
  sanitized log excerpt.

## Bug reports

Include:

- Operating system and CPU architecture
- Python and librealsense versions
- RealSense model and firmware
- Whether the connection reports USB 2 or USB 3
- Exact reproduction steps
- The smallest relevant error excerpt

Remove device serials and other personal information before posting logs.

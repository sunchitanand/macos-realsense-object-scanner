# Security policy

## Supported versions

Security fixes are applied to the latest version on the `main` branch.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's security advisory feature instead of
opening a public issue. Include reproduction steps and the affected commit when possible.

## Local security model

Hardware mode launches a narrow camera worker with elevated privileges on macOS so the RSUSB
backend can access the D435i interfaces. The Flask server, reconstruction pipeline, and download
routes remain unprivileged. The web server binds to `127.0.0.1` and rejects non-loopback host
headers and cross-origin control requests.

Before elevation, the launcher freezes the worker and its Python/native runtime into a standalone
bundle. It then installs that bundle under `/Library/Application Support` with root ownership and
no group or world write permission. The editable checkout and development virtual environment are
never executed as root.

Version 0.1.0 is source-only: it does not ship a publisher-signed or notarized helper binary. The
installation trust boundary therefore assumes the checked-out source and the invoking macOS user
account are not already compromised while the local worker bundle is built. Do not run hardware
mode from an untrusted fork, archive, or modified checkout.

Do not:

- Expose the server on a public or untrusted network.
- Run a modified camera worker through `sudo` without reviewing it.
- Commit captured scans, device serials, or native build artifacts.
- Install third-party RealSense Python wheels on macOS without verifying their librealsense source.

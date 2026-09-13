#!/bin/zsh
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
venv_dir="$project_dir/.venv"
native_dir="$project_dir/.native/realsense-2.58.4-cp312-arm64"
secure_worker="/Library/Application Support/macos-realsense-object-scanner/current/realsense-camera-worker"
python_command="${OBJECT_SCANNER_PYTHON:-python3.12}"
scan_root="${OBJECT_SCANNER_SCAN_ROOT:-$project_dir/scans}"
scanner_host="${OBJECT_SCANNER_HOST:-127.0.0.1}"
scanner_port="${OBJECT_SCANNER_PORT:-5055}"
runtime_pythonpath=""

umask 077

if [[ ! -x "$venv_dir/bin/python" ]]; then
  if ! command -v "$python_command" >/dev/null 2>&1; then
    echo "Python 3.12 is required. Install it with: brew install python@3.12" >&2
    exit 1
  fi
  "$python_command" -m venv "$venv_dir"
  "$venv_dir/bin/python" -m pip install --upgrade pip
fi

if ! "$venv_dir/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12)' 2>/dev/null; then
  echo "The existing .venv is not using Python 3.12. Remove it and rerun the launcher." >&2
  exit 1
fi

if ! "$venv_dir/bin/python" -c 'import flask, cv2, open3d' 2>/dev/null; then
  "$venv_dir/bin/python" -m pip install -e "$project_dir"
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  "$project_dir/scripts/install_realsense_macos.sh"
  "$project_dir/scripts/install_camera_worker_macos.sh"
  runtime_pythonpath="$native_dir"
fi

if ! PYTHONPATH="$runtime_pythonpath" "$venv_dir/bin/python" -c \
  'import re; import pyrealsense2 as rs; assert tuple(map(int, re.findall(r"\d+", rs.__version__)[:3])) >= (2, 58, 4)' \
  2>/dev/null; then
  echo "RealSense 2.58.4 macOS runtime is not available." >&2
  exit 1
fi

mkdir -p "$scan_root"
chmod 700 "$scan_root"
echo "Starting macOS RealSense Object Scanner at http://$scanner_host:$scanner_port"
if [[ "$(uname -s)" != "Darwin" ]]; then
  exec env \
    PYTHONPATH="$runtime_pythonpath" \
    OBJECT_SCANNER_SERIAL="${OBJECT_SCANNER_SERIAL:-}" \
    OBJECT_SCANNER_SCAN_ROOT="$scan_root" \
    OBJECT_SCANNER_HOST="$scanner_host" \
    OBJECT_SCANNER_PORT="$scanner_port" \
    "$venv_dir/bin/python" -m object_scanner.app
fi
if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this launcher as your normal macOS user, not through sudo." >&2
  exit 1
fi

echo "macOS may ask for your administrator password to start the isolated camera worker."
sudo -v
worker_dir="$(mktemp -d /tmp/realsense-object-scanner.XXXXXX)"
worker_socket="$worker_dir/camera.sock"
worker_pid=""

cleanup_worker() {
  if [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null; then
    sudo -n kill "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
  rmdir "$worker_dir" 2>/dev/null || true
}
trap cleanup_worker EXIT INT TERM

sudo -n "$secure_worker" \
  --socket "$worker_socket" \
  --scan-root "$scan_root" \
  --uid "$(id -u)" \
  --gid "$(id -g)" \
  --serial "${OBJECT_SCANNER_SERIAL:-}" &
worker_pid="$!"

for _attempt in {1..100}; do
  if [[ -S "$worker_socket" ]]; then
    break
  fi
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    wait "$worker_pid" || true
    echo "The privileged camera worker failed to start." >&2
    exit 1
  fi
  sleep 0.1
done
if [[ ! -S "$worker_socket" ]]; then
  echo "Timed out waiting for the privileged camera worker." >&2
  exit 1
fi

env \
  OBJECT_SCANNER_WORKER_SOCKET="$worker_socket" \
  OBJECT_SCANNER_SCAN_ROOT="$scan_root" \
  OBJECT_SCANNER_HOST="$scanner_host" \
  OBJECT_SCANNER_PORT="$scanner_port" \
  "$venv_dir/bin/python" -m object_scanner.app

#!/bin/zsh
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
venv_dir="$project_dir/.venv"
native_dir="$project_dir/.native/realsense-2.58.4-cp312-arm64"
secure_root="/Library/Application Support/macos-realsense-object-scanner"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "The privileged worker bundle currently supports Apple Silicon macOS only." >&2
  exit 1
fi

if [[ ! -x "$venv_dir/bin/pyinstaller" ]]; then
  "$venv_dir/bin/python" -m pip install -e "${project_dir}[macos-worker]"
fi

fingerprint_files=(
  "$project_dir/object_scanner/__init__.py"
  "$project_dir/object_scanner/camera_worker.py"
  "$project_dir/object_scanner/capture.py"
  "$project_dir/object_scanner/measurement.py"
  "$project_dir/scripts/camera_worker_entry.py"
  "$native_dir/pyrealsense2.cpython-312-darwin.so"
  "$native_dir/pyrsutils.cpython-312-darwin.so"
  "$native_dir/librealsense2.2.58.dylib"
  "$native_dir/librealsense2.dylib"
)

source_fingerprint() {
  shasum -a 256 "${fingerprint_files[@]}" | shasum -a 256 | awk '{print $1}'
}

fingerprint="$(source_fingerprint)"
secure_bundle="$secure_root/worker-$fingerprint"
secure_executable="$secure_bundle/realsense-camera-worker"
if [[ -x "$secure_executable" && -f "$secure_bundle/.complete" && ! -w "$secure_executable" ]]; then
  sudo install -d -m 0755 "$secure_root"
  sudo ln -sfn "worker-$fingerprint" "$secure_root/current"
  exit 0
fi

build_root="$(mktemp -d /tmp/realsense-worker-build.XXXXXX)"
cleanup_build() {
  rm -rf -- "$build_root"
}
trap cleanup_build EXIT INT TERM

PYTHONPATH="$project_dir:$native_dir" "$venv_dir/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --onedir \
  --name realsense-camera-worker \
  --distpath "$build_root/dist" \
  --workpath "$build_root/work" \
  --specpath "$build_root/spec" \
  --paths "$project_dir" \
  --paths "$native_dir" \
  --hidden-import cv2 \
  --hidden-import numpy \
  --hidden-import pyrealsense2 \
  --hidden-import pyrsutils \
  --add-binary "$native_dir/pyrealsense2.cpython-312-darwin.so:." \
  --add-binary "$native_dir/pyrsutils.cpython-312-darwin.so:." \
  --add-binary "$native_dir/librealsense2.2.58.dylib:." \
  --add-binary "$native_dir/librealsense2.dylib:." \
  "$project_dir/scripts/camera_worker_entry.py"

if [[ "$(source_fingerprint)" != "$fingerprint" ]]; then
  echo "Worker sources changed while the privileged bundle was being built." >&2
  exit 1
fi

sudo install -d -m 0755 "$secure_root"
if sudo test -e "$secure_bundle"; then
  echo "An incomplete worker bundle already exists at $secure_bundle." >&2
  exit 1
fi
secure_staging="$secure_root/.worker-$fingerprint-$$"
sudo install -d -m 0700 "$secure_staging"
sudo ditto "$build_root/dist/realsense-camera-worker" "$secure_staging"
sudo chown -R root:wheel "$secure_staging"
sudo chmod -R go-w "$secure_staging"
sudo codesign --force --deep --sign - "$secure_staging/realsense-camera-worker" >/dev/null
sudo "$secure_staging/realsense-camera-worker" \
  --socket /dev/null \
  --scan-root /dev/null \
  --uid 0 \
  --gid 0 \
  --self-test >/dev/null
sudo touch "$secure_staging/.complete"
sudo chmod 0444 "$secure_staging/.complete"
sudo chmod 0755 "$secure_staging"
sudo mv "$secure_staging" "$secure_bundle"
sudo ln -sfn "worker-$fingerprint" "$secure_root/current"

if [[ ! -x "$secure_executable" ]]; then
  echo "The root-owned camera worker bundle was not installed correctly." >&2
  exit 1
fi

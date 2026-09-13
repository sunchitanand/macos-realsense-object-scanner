#!/bin/zsh
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
venv_python="$project_dir/.venv/bin/python"
sdk_version="2.58.4"
sdk_commit="34d6c778e1134d8505adcd56bb57acbe7598a459"
native_dir="$project_dir/.native/realsense-${sdk_version}-cp312-arm64"
source_dir="$project_dir/.native/source/librealsense-${sdk_version}"
build_dir="$project_dir/.native/build/librealsense-${sdk_version}"
release_dir="$build_dir/Release"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This installer currently supports Apple Silicon macOS only." >&2
  exit 1
fi

if PYTHONPATH="$native_dir" "$venv_python" -c \
  'import pyrealsense2 as rs; assert rs.__version__ == "2.58.4"' 2>/dev/null; then
  exit 0
fi

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required to install the native RealSense dependencies." >&2
  exit 1
fi

for command_name in git cmake install_name_tool codesign; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    if [[ "$command_name" == "cmake" ]]; then
      brew install cmake
    else
      echo "Missing required command: $command_name" >&2
      exit 1
    fi
  fi
done

if ! brew list --versions libusb >/dev/null 2>&1; then
  brew install libusb
fi

mkdir -p "${source_dir:h}" "${build_dir:h}" "$native_dir"

if [[ ! -d "$source_dir/.git" ]]; then
  git clone --filter=blob:none --no-checkout \
    https://github.com/realsenseai/librealsense.git "$source_dir"
fi
git -C "$source_dir" fetch --depth 1 origin "$sdk_commit"
git -C "$source_dir" checkout --detach "$sdk_commit"
if [[ "$(git -C "$source_dir" rev-parse HEAD)" != "$sdk_commit" ]]; then
  echo "The librealsense source did not match the pinned commit." >&2
  exit 1
fi

if [[ ! -f "$build_dir/CMakeCache.txt" ]]; then
  cmake -S "$source_dir" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=15.0 \
    -DBUILD_PYTHON_BINDINGS=ON \
    -DBUILD_SHARED_LIBS=ON \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_GRAPHICAL_EXAMPLES=OFF \
    -DBUILD_TOOLS=OFF \
    -DBUILD_UNIT_TESTS=OFF \
    -DBUILD_WITH_OPENMP=OFF \
    -DHWM_OVER_XU=OFF \
    -DFORCE_RSUSB_BACKEND=ON \
    -DPYTHON_EXECUTABLE="$venv_python" \
    -DLIBUSB_INC="$(brew --prefix libusb)/include/libusb-1.0" \
    -DLIBUSB_LIB="$(brew --prefix libusb)/lib/libusb-1.0.dylib"
fi

cmake --build "$build_dir" --config Release --parallel "$(sysctl -n hw.logicalcpu)"

cp -L "$release_dir/pyrealsense2.cpython-312-darwin.so" "$native_dir/"
cp -L "$release_dir/pyrsutils.cpython-312-darwin.so" "$native_dir/"
cp -L "$release_dir/librealsense2.$sdk_version.dylib" "$native_dir/librealsense2.2.58.dylib"
cp -L "$release_dir/librealsense2.$sdk_version.dylib" "$native_dir/librealsense2.dylib"

install_name_tool -delete_rpath "$release_dir" -add_rpath @loader_path \
  "$native_dir/pyrealsense2.cpython-312-darwin.so"
install_name_tool -delete_rpath "$release_dir" -add_rpath @loader_path \
  "$native_dir/pyrsutils.cpython-312-darwin.so" 2>/dev/null || true

codesign --force --sign - \
  "$native_dir/librealsense2.2.58.dylib" \
  "$native_dir/librealsense2.dylib" \
  "$native_dir/pyrealsense2.cpython-312-darwin.so" \
  "$native_dir/pyrsutils.cpython-312-darwin.so" >/dev/null

PYTHONPATH="$native_dir" "$venv_python" -c \
  'import pyrealsense2 as rs; assert rs.__version__ == "2.58.4"'

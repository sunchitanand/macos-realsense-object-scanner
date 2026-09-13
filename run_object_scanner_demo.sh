#!/bin/zsh
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
venv_dir="$project_dir/.venv"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  echo "Run ./run_object_scanner.sh once to install the environment."
  exit 1
fi

echo "Starting macOS RealSense Object Scanner demo at http://127.0.0.1:5055"
exec "$venv_dir/bin/python" -m object_scanner.demo

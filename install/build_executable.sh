#!/usr/bin/env bash
# Build the standalone Linux x86_64 executable dist/mridian-dump with PyInstaller.
# Built inside Debian bullseye (glibc 2.31) so it runs on most Linux machines from ~2020 on.
# Usage (from anywhere):  bash install/build_executable.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/dist"

docker run --rm -v "$ROOT":/src:ro -v "$ROOT/dist":/dist python:3.12-slim-bullseye bash -c '
  set -e
  apt-get update -qq && apt-get install -y -qq --no-install-recommends binutils > /dev/null
  pip install -q --no-cache-dir --root-user-action=ignore -r /src/install/requirements.txt pyinstaller==6.11.1
  mkdir -p /build && cp /src/decode_message_dump.py /src/cine_to_video.py /src/install/mridian_dump.py /build/
  cd /build
  pyinstaller --onefile --name mridian-dump --collect-binaries imageio_ffmpeg --collect-data imageio_ffmpeg \
    --exclude-module tkinter --log-level WARN mridian_dump.py
  cp dist/mridian-dump /dist/
'
ls -la "$ROOT/dist/mridian-dump"

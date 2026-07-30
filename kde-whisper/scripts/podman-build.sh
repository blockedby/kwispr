#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
image=${KWISPR_KDE_DEV_IMAGE:-kwispr-kde-dev}

podman build -t "$image" -f "$repo_root/kde-whisper/Containerfile" "$repo_root"

podman run --rm \
  -v "$repo_root":/work:Z \
  -w /work \
  "$image" \
  bash -lc 'cmake -S kde-whisper -B kde-whisper/build -G Ninja -DBUILD_TESTING=OFF && cmake --build kde-whisper/build'

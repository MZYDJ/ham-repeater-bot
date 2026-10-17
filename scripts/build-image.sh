#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Always query first; query failures stop the build before Docker is invoked.
python3 scripts/update_edge_tts_version.py

# Compose must read the newly written .env values, not stale exported shell values.
unset EDGE_TTS_VERSION BUILD_TIME GIT_COMMIT
if docker info >/dev/null 2>&1; then
    DOCKER_BUILDKIT=1 docker compose build announce
elif command -v sudo >/dev/null 2>&1; then
    DOCKER_BUILDKIT=1 sudo docker compose build announce
else
    echo "[ERROR] Docker daemon access failed and sudo is unavailable." >&2
    exit 1
fi

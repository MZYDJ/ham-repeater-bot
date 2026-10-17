#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Always query first; query failures stop the build before Docker is invoked.
python3 scripts/update_edge_tts_version.py

# Pass the freshly queried version directly; GUI Compose builds use Dockerfile's automatic fallback.
EDGE_TTS_VERSION="$(sed -n 's/^EDGE_TTS_VERSION=//p' .env | head -n 1)"
if [[ ! "$EDGE_TTS_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9.!+_-]*$ ]]; then
    echo "[ERROR] .env 中没有有效的 EDGE_TTS_VERSION。" >&2
    exit 1
fi
if docker info >/dev/null 2>&1; then
    DOCKER_BUILDKIT=1 docker compose build --build-arg "EDGE_TTS_VERSION=$EDGE_TTS_VERSION" announce
elif command -v sudo >/dev/null 2>&1; then
    DOCKER_BUILDKIT=1 sudo docker compose build --build-arg "EDGE_TTS_VERSION=$EDGE_TTS_VERSION" announce
else
    echo "[ERROR] Docker daemon access failed and sudo is unavailable." >&2
    exit 1
fi

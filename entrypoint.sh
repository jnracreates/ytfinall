#!/bin/bash
set -e

echo "[entrypoint] Updating yt-dlp..."
# If this fails because of a network issue, the container will still boot up safely
pip install --upgrade --pre "yt-dlp[default]" || echo "[entrypoint] Update failed, using cached version."

echo "[entrypoint] Starting ytfinall..."
exec "$@"


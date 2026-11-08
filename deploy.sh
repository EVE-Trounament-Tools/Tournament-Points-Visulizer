#!/usr/bin/env bash
# Copy the code to a self-hosting server. The server's data/ (the database) and
# site/ (the built pages) are left alone.
#
# Usage: ./deploy.sh [ssh-host] [dest]   (defaults: phone-root /opt/pointsRanker)
set -euo pipefail

HOST="${1:-phone-root}"
DEST="${2:-/opt/pointsRanker}"

cd "$(dirname "$0")"

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.wrangler/' \
  --exclude '__pycache__/' \
  --exclude '*.egg-info/' \
  --exclude '/data/' \
  --exclude '/site/' \
  --exclude '/site.old/' \
  --exclude '/.site-*/' \
  ./ "$HOST:$DEST/"

echo "Deployed to $HOST:$DEST"

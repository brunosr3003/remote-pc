#!/bin/sh
# Downloads noVNC next to server.py. It is not vendored in this repo.
set -eu
cd "$(dirname "$0")/.."
VERSION="${NOVNC_VERSION:-v1.7.0}"
if [ -d novnc ]; then
    echo "novnc/ already exists, nothing to do"
    exit 0
fi
git clone --depth 1 --branch "$VERSION" https://github.com/novnc/noVNC.git novnc
echo "noVNC $VERSION installed in novnc/"

#!/usr/bin/env bash
# Re-apply ACM Lab branding to the open-webui container.
#
# These changes live in the container's writable layer + env, so they survive
# `docker restart` but are LOST if the container is recreated (image upgrade,
# `docker rm`). Re-run this after any recreate.
#
# IMPORTANT: the app name comes from the WEBUI_NAME env var, which can only be
# set at container-create time. When (re)creating the container, add:
#     -e "WEBUI_NAME=ACM Lab"
# Then run this script to apply the theme, icons, title and drop the
# "(Open WebUI)" attribution suffix.
set -euo pipefail
C="${1:-open-webui}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/5] theme + base assets -> $C"
docker cp "$HERE/custom.css"   "$C:/app/build/static/custom.css"
docker cp "$HERE/favicon.svg"  "$C:/app/build/static/favicon.svg"
docker cp "$HERE/favicon.ico"  "$C:/app/build/static/favicon.ico"
docker cp "$HERE/logo.png"     "$C:/tmp/acm_logo.png"

echo "[2/5] generate favicon/splash/icon set from logo"
docker cp "$HERE/gen_icons.py" "$C:/tmp/gen_icons.py"
docker exec "$C" python3 /tmp/gen_icons.py

echo "[3/5] page title + manifest"
docker exec "$C" sed -i 's/Open WebUI/ACM Lab/g' /app/build/index.html /app/build/manifest.json || true

echo "[4/5] drop ' (Open WebUI)' attribution suffix in backend"
docker exec "$C" sed -i "s|    WEBUI_NAME += ' (Open WebUI)'|    WEBUI_NAME += ''  # ACM Lab branding|" \
    /app/backend/open_webui/env.py || true

echo "[5/5] restart to load backend change"
docker restart "$C" >/dev/null
echo "done. Verify: curl -s localhost:3000/api/config | grep -o '\"name\":\"[^\"]*\"'"

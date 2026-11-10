#!/usr/bin/env bash
# Builds dist/srvmon-aapanel-plugin.zip: the aaPanel plugin with the core
# bundled, importable from App Store > Third-party Plug-ins > Import plugin.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$ROOT/aapanel-plugin/srvmon" "$STAGE/srvmon"
mkdir -p "$STAGE/srvmon/core/srvmon"
cp "$ROOT/install.sh" "$ROOT/uninstall.sh" "$STAGE/srvmon/core/"
cp "$ROOT/srvmon/srvmon.py" "$ROOT/srvmon/dashboard.html" "$STAGE/srvmon/core/srvmon/"
mkdir -p "$ROOT/dist"
rm -f "$ROOT/dist/srvmon-aapanel-plugin.zip"
(cd "$STAGE" && zip -qr "$ROOT/dist/srvmon-aapanel-plugin.zip" srvmon)
echo "built dist/srvmon-aapanel-plugin.zip"

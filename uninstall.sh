#!/usr/bin/env bash
# Removes srvmon. Keeps /etc/srvmon.conf and the database unless --purge is given.
set -euo pipefail

PURGE=no
case "${1:-}" in
  --purge) PURGE=yes ;;
  "") ;;
  -h|--help) echo "Usage: sudo $0 [--purge]   (--purge also deletes config and collected data)"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 1 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "error: run as root" >&2; exit 1; }

DB_PATH="$(sed -n 's/^DB_PATH=//p' /etc/srvmon.conf 2>/dev/null | tail -1)"
DB_PATH="${DB_PATH:-/var/lib/srvmon/srvmon.db}"

if [ -f /etc/systemd/system/srvmon-collect.timer ]; then
  systemctl disable --now srvmon-collect.timer >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/srvmon-collect.timer /etc/systemd/system/srvmon-collect.service
  systemctl daemon-reload
fi
rm -f /etc/cron.d/srvmon /usr/local/bin/srvmon
rm -rf /www/server/panel/plugin/srvmon
rm -rf /opt/srvmon

if [ "$PURGE" = yes ]; then
  rm -f /etc/srvmon.conf "$DB_PATH" "$DB_PATH-journal"
  rmdir "$(dirname "$DB_PATH")" 2>/dev/null || true
  echo "srvmon removed, including config and data."
else
  echo "srvmon removed. Kept /etc/srvmon.conf and $DB_PATH (use --purge to delete them)."
fi

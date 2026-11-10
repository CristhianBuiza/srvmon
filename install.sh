#!/usr/bin/env bash
# srvmon installer. Safe to re-run: upgrades the code and keeps config and data.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/srvmon
BIN=/usr/local/bin/srvmon
CONF=/etc/srvmon.conf
PANEL=/www/server/panel
UNIT_DIR=/etc/systemd/system

RETENTION=""
TOP_N=""
LABEL=""
PLUGIN=auto          # auto | yes | no
PANEL_MONITOR=no     # also switch on aaPanel's built-in monitor with the same retention
SYSSTAT=yes
SCHEDULER=auto       # auto | systemd | cron

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

  --retention DAYS     days of history to keep (default 7)
  --top N              processes recorded per sample, by CPU and by RAM (default 5)
  --label NAME         name shown on the dashboard (default: hostname)
  --scheduler TYPE     systemd | cron (default: systemd when available)
  --plugin             install the aaPanel plugin (default: when aaPanel is detected)
  --no-plugin          never install the aaPanel plugin
  --panel-monitor      also enable aaPanel's built-in monitor with the same retention
  --no-sysstat-import  do not backfill history from sysstat (sar) files
  -h, --help           show this help
EOF
}

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --retention) RETENTION="${2:-}"; shift 2 ;;
    --top) TOP_N="${2:-}"; shift 2 ;;
    --label) LABEL="${2:-}"; shift 2 ;;
    --scheduler) SCHEDULER="${2:-}"; shift 2 ;;
    --plugin) PLUGIN=yes; shift ;;
    --no-plugin) PLUGIN=no; shift ;;
    --panel-monitor) PANEL_MONITOR=yes; shift ;;
    --no-sysstat-import) SYSSTAT=no; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo ./install.sh)"
[ -r /proc/stat ] || die "Linux /proc is required"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys, sqlite3; sys.exit(0 if sys.version_info >= (3, 6) else 1)' \
  || die "python3 >= 3.6 with the sqlite3 module is required"
[ -f "$SRC_DIR/srvmon/srvmon.py" ] || die "run this script from the srvmon source directory"
if [ -n "$RETENTION" ]; then [[ "$RETENTION" =~ ^[0-9]+$ ]] && [ "$RETENTION" -ge 1 ] || die "--retention must be a positive integer"; fi
if [ -n "$TOP_N" ]; then [[ "$TOP_N" =~ ^[0-9]+$ ]] && [ "$TOP_N" -ge 1 ] || die "--top must be a positive integer"; fi
if [ -n "$LABEL" ]; then [[ "$LABEL" =~ ^[A-Za-z0-9._\ -]+$ ]] || die "--label may only contain letters, digits, space, dot, dash and underscore"; fi

info "Installing srvmon to $PREFIX"
install -d -m 755 "$PREFIX"
install -m 755 "$SRC_DIR/srvmon/srvmon.py" "$PREFIX/srvmon.py"
install -m 644 "$SRC_DIR/srvmon/dashboard.html" "$PREFIX/dashboard.html"
install -m 755 "$SRC_DIR/uninstall.sh" "$PREFIX/uninstall.sh"
ln -sf "$PREFIX/srvmon.py" "$BIN"

set_key() {
  if grep -q "^$1=" "$CONF"; then sed -i "s|^$1=.*|$1=$2|" "$CONF"; else echo "$1=$2" >> "$CONF"; fi
}
if [ ! -f "$CONF" ]; then
  info "Writing $CONF"
  cat > "$CONF" <<'EOF'
# srvmon configuration. Changes apply on the next sample; no restart needed.
# Days of history to keep. Older samples are deleted on every collection.
RETENTION_DAYS=7
# Process groups stored per sample: the top N by CPU plus the top N by RAM.
TOP_N=5
# Thresholds highlighted on the dashboard and counted as "time above".
CPU_ALERT=80
RAM_ALERT=90
# Name shown on the dashboard. Empty = hostname.
LABEL=
DB_PATH=/var/lib/srvmon/srvmon.db
# Where to look for sysstat (sar) files for the initial backfill.
SYSSTAT_DIRS=/var/log/sysstat:/var/log/sa
EOF
  chmod 644 "$CONF"
fi
[ -n "$RETENTION" ] && set_key RETENTION_DAYS "$RETENTION"
[ -n "$TOP_N" ] && set_key TOP_N "$TOP_N"
[ -n "$LABEL" ] && set_key LABEL "$LABEL"
RETENTION_EFFECTIVE="$(sed -n 's/^RETENTION_DAYS=//p' "$CONF" | tail -1)"
DB_PATH="$(sed -n 's/^DB_PATH=//p' "$CONF" | tail -1)"
install -d -m 755 "$(dirname "${DB_PATH:-/var/lib/srvmon/srvmon.db}")"

if [ "$SCHEDULER" = auto ]; then
  if [ -d /run/systemd/system ] && command -v systemctl >/dev/null; then SCHEDULER=systemd; else SCHEDULER=cron; fi
fi
case "$SCHEDULER" in
  systemd)
    info "Scheduling collection every minute with a systemd timer"
    cat > "$UNIT_DIR/srvmon-collect.service" <<EOF
[Unit]
Description=srvmon: record CPU, load, RAM and top processes

[Service]
Type=oneshot
ExecStart=$BIN collect
Nice=10
IOSchedulingClass=idle
StandardOutput=null
EOF
    cat > "$UNIT_DIR/srvmon-collect.timer" <<'EOF'
[Unit]
Description=srvmon: sample every minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF
    rm -f /etc/cron.d/srvmon
    systemctl daemon-reload
    systemctl enable --now srvmon-collect.timer >/dev/null
    ;;
  cron)
    info "Scheduling collection every minute with cron (/etc/cron.d/srvmon)"
    [ -d /etc/cron.d ] || die "/etc/cron.d not found; install cron or use systemd"
    echo "* * * * * root $BIN collect >/dev/null 2>&1" > /etc/cron.d/srvmon
    chmod 644 /etc/cron.d/srvmon
    if [ -f "$UNIT_DIR/srvmon-collect.timer" ]; then
      systemctl disable --now srvmon-collect.timer >/dev/null 2>&1 || true
      rm -f "$UNIT_DIR/srvmon-collect.timer" "$UNIT_DIR/srvmon-collect.service"
    fi
    ;;
  *) die "--scheduler must be systemd or cron" ;;
esac

info "Taking the first sample"
"$BIN" collect

if [ "$SYSSTAT" = yes ] && command -v sadf >/dev/null; then
  info "Backfilling history from sysstat (last $RETENTION_EFFECTIVE days)"
  "$BIN" import-sysstat || echo "warning: sysstat import failed; continuing" >&2
fi

if [ "$PLUGIN" = auto ]; then [ -d "$PANEL/plugin" ] && PLUGIN=yes || PLUGIN=no; fi
if [ "$PLUGIN" = yes ]; then
  [ -d "$PANEL/plugin" ] || die "aaPanel not found at $PANEL"
  info "Installing the aaPanel plugin to $PANEL/plugin/srvmon"
  install -d -m 755 "$PANEL/plugin/srvmon"
  for f in info.json srvmon_main.py index.html icon.png install.sh; do
    install -m 644 "$SRC_DIR/aapanel-plugin/srvmon/$f" "$PANEL/plugin/srvmon/$f"
  done
fi
if [ "$PANEL_MONITOR" = yes ]; then
  [ -d "$PANEL/data" ] || die "aaPanel not found at $PANEL"
  info "Enabling aaPanel's built-in monitor with $RETENTION_EFFECTIVE-day retention"
  echo -n "$RETENTION_EFFECTIVE" > "$PANEL/data/control.conf"
fi

echo
"$BIN" status
cat <<EOF

srvmon is collecting every minute and keeps $RETENTION_EFFECTIVE days.
  Daily report:    srvmon report
  One day:         srvmon report --day $(date +%F)
  HTML dashboard:  srvmon html --out /tmp/srvmon.html
  Live dashboard:  srvmon serve   (http://127.0.0.1:8765, use an SSH tunnel)
EOF
[ "$PLUGIN" = yes ] && echo "  aaPanel:         App Store > Third-party Plug-ins > Server Monitor (srvmon)"
exit 0

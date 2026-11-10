#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srvmon: CPU, load, RAM and top-process history for Linux servers.

One `srvmon collect` run stores one sample. Schedule it every minute and srvmon
keeps a rolling window (7 days by default) in SQLite, then renders text reports,
JSON and a self-contained HTML dashboard from it.

Standard library only. Python 3.6+. Linux (/proc) only.
"""

import argparse
import bisect
import glob
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time

VERSION = "1.0.1"

CONFIG_PATH = "/etc/srvmon.conf"
DEFAULTS = {
    "DB_PATH": "/var/lib/srvmon/srvmon.db",
    "RETENTION_DAYS": "7",
    "TOP_N": "5",
    "CPU_ALERT": "80",
    "RAM_ALERT": "90",
    "LABEL": "",
    "SYSSTAT_DIRS": "/var/log/sysstat:/var/log/sa",
}

PROC = "/proc"
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024

# Consecutive samples further apart than this are a gap (collector stopped,
# server down), not one long interval.
MAX_GAP = 900
# Resolution of the chart series.
BUCKET = 600

INTERPRETERS = {
    "python", "python2", "python3", "node", "nodejs", "php", "java", "perl",
    "ruby", "bash", "sh", "dotnet", "bun", "deno",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts            INTEGER PRIMARY KEY,
    interval      INTEGER NOT NULL,
    source        TEXT    NOT NULL,
    cpu           REAL,
    cpu_user      REAL,
    cpu_nice      REAL,
    cpu_system    REAL,
    cpu_iowait    REAL,
    cpu_steal     REAL,
    load1         REAL,
    load5         REAL,
    load15        REAL,
    mem_total_kb  INTEGER,
    mem_used_kb   INTEGER,
    mem_pct       REAL,
    swap_total_kb INTEGER,
    swap_used_kb  INTEGER
);
CREATE TABLE IF NOT EXISTS proc_samples (
    ts     INTEGER NOT NULL,
    name   TEXT    NOT NULL,
    cpu    REAL    NOT NULL,
    mem_kb INTEGER NOT NULL,
    nproc  INTEGER NOT NULL,
    PRIMARY KEY (ts, name)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

class Config(object):
    """KEY=VALUE file (default /etc/srvmon.conf), overridable by SRVMON_<KEY>."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("SRVMON_CONFIG", CONFIG_PATH)
        values = dict(DEFAULTS)
        if os.path.isfile(self.path):
            with open(self.path) as fh:
                values.update(parse_config(fh.read()))
        for key in DEFAULTS:
            env = os.environ.get("SRVMON_" + key)
            if env is not None:
                values[key] = env
        self.db_path = values["DB_PATH"]
        self.retention_days = max(1, int(float(values["RETENTION_DAYS"])))
        self.top_n = max(1, int(values["TOP_N"]))
        self.cpu_alert = float(values["CPU_ALERT"])
        self.ram_alert = float(values["RAM_ALERT"])
        self.label = values["LABEL"] or socket.gethostname()
        self.sysstat_dirs = [d for d in values["SYSSTAT_DIRS"].split(":") if d]


def parse_config(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def set_config_value(path, key, value):
    """Update or append KEY=VALUE in the config file, keeping everything else."""
    lines = []
    if os.path.isfile(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = "%s=%s" % (key, value)
            break
    else:
        lines.append("%s=%s" % (key, value))
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def connect(cfg):
    directory = os.path.dirname(cfg.db_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, 0o755)
    is_new = not os.path.exists(cfg.db_path)
    conn = sqlite3.connect(cfg.db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    if is_new:
        # Must be set before the first table exists; lets prune() return space.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.executescript(SCHEMA)
    return conn


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps(value, separators=(",", ":"))))


def prune(conn, retention_days, now=None):
    cutoff = int((now or time.time()) - retention_days * 86400)
    with conn:
        removed = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        removed += conn.execute("DELETE FROM proc_samples WHERE ts < ?", (cutoff,)).rowcount
    if removed:
        conn.execute("PRAGMA incremental_vacuum").fetchall()
    return removed


# --------------------------------------------------------------------------
# /proc readers
# --------------------------------------------------------------------------

def read_cpu_times():
    """Aggregate jiffies: user nice system idle iowait irq softirq steal."""
    with open(os.path.join(PROC, "stat")) as fh:
        for line in fh:
            if line.startswith("cpu "):
                values = [int(x) for x in line.split()[1:9]]
                return (values + [0] * 8)[:8]
    raise RuntimeError("no aggregate cpu line in %s/stat" % PROC)


def read_uptime():
    with open(os.path.join(PROC, "uptime")) as fh:
        return float(fh.read().split()[0])


def read_loadavg():
    with open(os.path.join(PROC, "loadavg")) as fh:
        parts = fh.read().split()
    return float(parts[0]), float(parts[1]), float(parts[2])


def read_meminfo():
    info = {}
    with open(os.path.join(PROC, "meminfo")) as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                info[key] = int(fields[0])
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable")
    if available is None:  # kernels older than 3.14
        available = (info.get("MemFree", 0) + info.get("Buffers", 0)
                     + info.get("Cached", 0) + info.get("SReclaimable", 0))
    used = max(0, total - available)
    swap_total = info.get("SwapTotal", 0)
    return {
        "total": total,
        "used": used,
        "pct": round(100.0 * used / total, 2) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - info.get("SwapFree", 0)),
    }


def parse_stat_line(data):
    """Parse /proc/<pid>/stat. Returns (comm, ppid, ticks, starttime, rss_kb)."""
    lpar, rpar = data.find("("), data.rfind(")")
    if lpar < 0 or rpar < 0:
        return None
    comm = data[lpar + 1:rpar]
    fields = data[rpar + 2:].split()
    try:
        return (comm, int(fields[1]), int(fields[11]) + int(fields[12]),
                int(fields[19]), int(fields[21]) * PAGE_KB)
    except (IndexError, ValueError):
        return None


def process_name(pid, comm, ppid):
    """Group key for a process: its comm, refined for kernel threads and interpreters."""
    if pid == 2 or ppid == 2:
        return comm.split("/", 1)[0]  # kworker/u8:1 -> kworker
    if re.sub(r"[\d.]+$", "", comm) in INTERPRETERS:
        try:
            with open(os.path.join(PROC, str(pid), "cmdline"), "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").split("\0")
        except (IOError, OSError):
            return comm
        for arg in argv[1:]:
            if arg and not arg.startswith("-"):
                return "%s %s" % (comm, os.path.basename(arg.rstrip("/"))[:40])
    return comm


def read_pss(pid):
    try:
        with open(os.path.join(PROC, pid, "smaps_rollup")) as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (IOError, OSError, ValueError):
        pass
    return None


def proc_mem_metric():
    return "PSS" if read_pss("self") is not None else "RSS"


def read_processes():
    """{pid: (name, starttime, ticks, mem_kb)} for every visible process.

    mem_kb is PSS (shared pages split between the processes using them) when
    smaps_rollup is readable, otherwise RSS.
    """
    procs = {}
    for entry in os.listdir(PROC):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(PROC, entry, "stat")) as fh:
                parsed = parse_stat_line(fh.read())
        except (IOError, OSError):
            continue  # exited while we were looking
        if parsed is None:
            continue
        comm, ppid, ticks, start, rss = parsed
        mem = read_pss(entry) if rss else None
        procs[entry] = (process_name(int(entry), comm, ppid), start, ticks,
                        rss if mem is None else mem)
    return procs


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def take_snapshot():
    return {
        "ts": time.time(),
        "uptime": read_uptime(),
        "cpu": read_cpu_times(),
        "procs": read_processes(),
    }


def persistable(snapshot):
    return {
        "ts": snapshot["ts"],
        "uptime": snapshot["uptime"],
        "cpu": snapshot["cpu"],
        "procs": {pid: [p[1], p[2]] for pid, p in snapshot["procs"].items()},
    }


def snapshot_usable(prev, cur):
    if not prev:
        return False
    elapsed = cur["ts"] - prev["ts"]
    if elapsed <= 0 or elapsed > MAX_GAP or cur["uptime"] < prev["uptime"]:
        return False  # first run, long gap or reboot
    return all(c >= p for c, p in zip(cur["cpu"], prev["cpu"]))


def cpu_breakdown(prev, cur):
    user, nice, system, idle, iowait, irq, softirq, steal = [
        max(0, c - p) for c, p in zip(cur, prev)]
    total = user + nice + system + idle + iowait + irq + softirq + steal
    if total <= 0:
        return {"cpu": 0.0, "user": 0.0, "nice": 0.0, "system": 0.0, "iowait": 0.0, "steal": 0.0}

    def pct(value):
        return round(100.0 * value / total, 2)

    return {
        "cpu": pct(total - idle - iowait),
        "user": pct(user),
        "nice": pct(nice),
        "system": pct(system + irq + softirq),
        "iowait": pct(iowait),
        "steal": pct(steal),
    }


def process_usage(prev, cur, cores):
    """Per process-name CPU % (of total machine capacity), RSS and count."""
    elapsed = cur["ts"] - prev["ts"]
    prev_procs = prev["procs"]
    prev_uptime_ticks = prev["uptime"] * CLK_TCK
    groups = {}
    for pid, (name, start, ticks, mem) in cur["procs"].items():
        group = groups.setdefault(name, [0, 0, 0])
        old = prev_procs.get(pid)
        if old is not None and old[0] == start:
            group[0] += max(0, ticks - old[1])
        elif start >= prev_uptime_ticks:
            group[0] += ticks  # started during the interval
        group[1] += mem
        group[2] += 1
    usage = []
    for name, (dticks, mem, count) in groups.items():
        cpu = 100.0 * dticks / CLK_TCK / elapsed / cores if elapsed > 0 else 0.0
        usage.append((name, round(min(cpu, 100.0), 2), mem, count))
    return usage


def select_top(usage, n):
    by_cpu = sorted((u for u in usage if u[1] > 0), key=lambda u: -u[1])[:n]
    by_ram = sorted((u for u in usage if u[2] > 0), key=lambda u: -u[2])[:n]
    chosen = {}
    for item in by_cpu + by_ram:
        chosen[item[0]] = item
    return list(chosen.values())


def collect(cfg):
    conn = connect(cfg)
    prev = meta_get(conn, "snapshot")
    cur = take_snapshot()
    if not snapshot_usable(prev, cur):
        prev = persistable(cur)
        time.sleep(1.0)
        cur = take_snapshot()
    cores = os.cpu_count() or 1
    cpu = cpu_breakdown(prev["cpu"], cur["cpu"])
    top = select_top(process_usage(prev, cur, cores), cfg.top_n)
    load1, load5, load15 = read_loadavg()
    mem = read_meminfo()
    ts = int(round(cur["ts"]))
    interval = max(1, int(round(cur["ts"] - prev["ts"])))
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, interval, "native", cpu["cpu"], cpu["user"], cpu["nice"], cpu["system"],
             cpu["iowait"], cpu["steal"], load1, load5, load15, mem["total"], mem["used"],
             mem["pct"], mem["swap_total"], mem["swap_used"]))
        conn.executemany("INSERT OR REPLACE INTO proc_samples VALUES (?,?,?,?,?)",
                         [(ts, name, c, mem, n) for name, c, mem, n in top])
        meta_set(conn, "snapshot", persistable(cur))
        meta_set(conn, "cores", cores)
        meta_set(conn, "mem_total_kb", mem["total"])
        meta_set(conn, "proc_mem_metric", proc_mem_metric())
    prune(conn, cfg.retention_days)
    conn.close()
    return {"ts": ts, "interval": interval, "cpu": cpu["cpu"], "load1": load1,
            "mem_pct": mem["pct"], "top": top}


# --------------------------------------------------------------------------
# sysstat backfill
# --------------------------------------------------------------------------

def sadf_rows(path, activity):
    try:
        out = subprocess.check_output(["sadf", "-U", "-d", path, "--", activity],
                                      stderr=subprocess.DEVNULL, universal_newlines=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    header, rows = None, []
    for line in out.splitlines():
        if line.startswith("#"):
            header = [h.strip() for h in line.lstrip("# ").split(";")]
            continue
        values = line.split(";")
        if header and len(values) == len(header):
            rows.append(dict(zip(header, values)))
    return rows


def _num(row, key):
    try:
        return float(row[key].replace(",", "."))
    except (KeyError, ValueError, AttributeError):
        return None


def import_sysstat(cfg, conn, dirs=None, now=None):
    """Backfill CPU, load and RAM from sysstat (sar) files within the retention window.

    Only fills time before the first native sample, so the two sources never
    overlap. Process data cannot be backfilled (sysstat does not keep it).
    """
    now = now or time.time()
    cutoff = now - cfg.retention_days * 86400
    first_native = conn.execute(
        "SELECT MIN(ts) FROM samples WHERE source = 'native'").fetchone()[0]
    limit = first_native if first_native is not None else now + 1
    mem_total_now = read_meminfo()["total"]
    files = []
    for directory in (dirs or cfg.sysstat_dirs):
        files.extend(glob.glob(os.path.join(directory, "sa[0-9]*")))
    merged = {}
    for path in sorted(files):
        if os.path.getmtime(path) < cutoff:
            continue
        for row in sadf_rows(path, "-u"):
            idle, iowait = _num(row, "%idle"), _num(row, "%iowait")
            ts = _num(row, "timestamp")
            if ts is None or idle is None:
                continue
            rec = merged.setdefault(int(ts), {})
            rec["interval"] = int(_num(row, "interval") or 600)
            rec["cpu"] = round(max(0.0, 100.0 - idle - (iowait or 0.0)), 2)
            rec["cpu_user"] = _num(row, "%user")
            rec["cpu_nice"] = _num(row, "%nice")
            rec["cpu_system"] = _num(row, "%system")
            rec["cpu_iowait"] = iowait
            rec["cpu_steal"] = _num(row, "%steal")
        for row in sadf_rows(path, "-q"):
            ts = _num(row, "timestamp")
            if ts is None:
                continue
            rec = merged.setdefault(int(ts), {})
            rec["load1"], rec["load5"], rec["load15"] = (
                _num(row, "ldavg-1"), _num(row, "ldavg-5"), _num(row, "ldavg-15"))
        for row in sadf_rows(path, "-r"):
            ts, avail = _num(row, "timestamp"), _num(row, "kbavail")
            used, pct = _num(row, "kbmemused"), _num(row, "%memused")
            if ts is None or avail is None:
                continue
            total = int(round(used * 100.0 / pct)) if used and pct else mem_total_now
            rec = merged.setdefault(int(ts), {})
            rec["mem_total_kb"] = total
            rec["mem_used_kb"] = max(0, int(total - avail))
            rec["mem_pct"] = round(100.0 * rec["mem_used_kb"] / total, 2) if total else None
        for row in sadf_rows(path, "-S"):
            ts, free, used = _num(row, "timestamp"), _num(row, "kbswpfree"), _num(row, "kbswpused")
            if ts is None or used is None:
                continue
            rec = merged.setdefault(int(ts), {})
            rec["swap_used_kb"] = int(used)
            rec["swap_total_kb"] = int(used + (free or 0))
    columns = ["cpu", "cpu_user", "cpu_nice", "cpu_system", "cpu_iowait", "cpu_steal",
               "load1", "load5", "load15", "mem_total_kb", "mem_used_kb", "mem_pct",
               "swap_total_kb", "swap_used_kb"]
    rows = []
    for ts, rec in sorted(merged.items()):
        if ts < cutoff or ts >= limit or "cpu" not in rec:
            continue
        rows.append([ts, rec["interval"], "sysstat"] + [rec.get(c) for c in columns])
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO samples (ts, interval, source, %s) VALUES (%s)"
            % (", ".join(columns), ", ".join("?" * (len(columns) + 3))), rows)
        inserted = conn.total_changes - before
    return {"files": len(files), "candidates": len(rows), "inserted": inserted}


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def local_midnight(ts):
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def next_midnight(midnight):
    return local_midnight(midnight + 93600)  # +26h lands inside the next day, DST-safe


def percentile(values, p):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _round(value, digits=2):
    return None if value is None else round(value, digits)


def period_stats(rows, span, cfg):
    covered = sum(r["w"] for r in rows)
    out = {
        "samples": len(rows),
        "coverage": _round(min(100.0, 100.0 * covered / span) if span > 0 else 0.0, 1),
    }
    for key, prefix in (("cpu", "cpu"), ("load1", "load1"), ("load5", "load5"),
                        ("load15", "load15"), ("mem_pct", "mem"), ("mem_used_kb", "mem_kb")):
        valid = [r for r in rows if r[key] is not None]
        if not valid:
            out[prefix + "_avg"] = out[prefix + "_max"] = out[prefix + "_max_at"] = None
            continue
        weight = sum(r["w"] for r in valid)
        peak = max(valid, key=lambda r: r[key])
        out[prefix + "_avg"] = _round(sum(r[key] * r["w"] for r in valid) / weight)
        out[prefix + "_max"] = _round(peak[key])
        out[prefix + "_max_at"] = peak["ts"]
    out["cpu_p95"] = _round(percentile([r["cpu"] for r in rows], 95))
    out["mem_p95"] = _round(percentile([r["mem_pct"] for r in rows], 95))
    out["cpu_alert_min"] = int(round(sum(
        r["w"] for r in rows if r["cpu"] is not None and r["cpu"] >= cfg.cpu_alert) / 60.0))
    out["mem_alert_min"] = int(round(sum(
        r["w"] for r in rows if r["mem_pct"] is not None and r["mem_pct"] >= cfg.ram_alert) / 60.0))
    return out


def hourly(rows):
    buckets = [[] for _ in range(24)]
    for r in rows:
        buckets[time.localtime(r["ts"]).tm_hour].append(r)
    out = {"cpu": [], "mem": [], "load1": []}
    for bucket in buckets:
        for key, name in (("cpu", "cpu"), ("mem_pct", "mem"), ("load1", "load1")):
            valid = [r for r in bucket if r[key] is not None]
            weight = sum(r["w"] for r in valid)
            out[name].append(_round(sum(r[key] * r["w"] for r in valid) / weight) if weight else None)
    return out


def top_processes(proc_rows, weights, native_seconds, n):
    """Rank process groups by average CPU and by average RSS.

    CPU average counts the group as 0% in samples where it was not in the top
    list; RSS average is over the samples where it was present.
    """
    agg = {}
    for ts, name, cpu, mem, nproc in proc_rows:
        w = weights.get(ts)
        if w is None:
            continue
        a = agg.get(name)
        if a is None:
            a = agg[name] = {"name": name, "cpu_sum": 0.0, "cpu_max": -1.0, "cpu_max_at": ts,
                             "mem_sum": 0.0, "mem_w": 0, "mem_max": -1, "mem_max_at": ts,
                             "nproc_max": 0}
        a["cpu_sum"] += cpu * w
        if cpu > a["cpu_max"]:
            a["cpu_max"], a["cpu_max_at"] = cpu, ts
        a["mem_sum"] += mem * w
        a["mem_w"] += w
        if mem > a["mem_max"]:
            a["mem_max"], a["mem_max_at"] = mem, ts
        a["nproc_max"] = max(a["nproc_max"], nproc)
    items = []
    for a in agg.values():
        items.append({
            "name": a["name"],
            "cpu_avg": _round(a["cpu_sum"] / native_seconds) if native_seconds else None,
            "cpu_max": _round(a["cpu_max"]),
            "cpu_max_at": a["cpu_max_at"],
            "mem_avg_kb": int(a["mem_sum"] / a["mem_w"]) if a["mem_w"] else None,
            "mem_max_kb": a["mem_max"],
            "mem_max_at": a["mem_max_at"],
            "seen_pct": _round(100.0 * a["mem_w"] / native_seconds, 1) if native_seconds else None,
            "nproc_max": a["nproc_max"],
        })
    by_cpu = sorted((i for i in items if i["cpu_avg"]), key=lambda i: -i["cpu_avg"])[:n]
    by_ram = sorted((i for i in items if i["mem_avg_kb"]), key=lambda i: -i["mem_avg_kb"])[:n]
    return by_cpu, by_ram


def bucketize(rows, size=BUCKET):
    """[bucket_ts, cpu_avg, cpu_max, mem_pct, mem_used_kb, load1, load5, load15]"""
    buckets = {}
    for r in rows:
        buckets.setdefault(r["ts"] // size * size, []).append(r)
    series = []
    for ts in sorted(buckets):
        group = buckets[ts]
        point = [ts]
        for key in ("cpu", None, "mem_pct", "mem_used_kb", "load1", "load5", "load15"):
            if key is None:
                cpus = [r["cpu"] for r in group if r["cpu"] is not None]
                point.append(_round(max(cpus)) if cpus else None)
                continue
            valid = [r for r in group if r[key] is not None]
            weight = sum(r["w"] for r in valid)
            value = sum(r[key] * r["w"] for r in valid) / weight if weight else None
            point.append(int(value) if key == "mem_used_kb" and value is not None else _round(value))
        series.append(point)
    return series


def summarize(cfg, conn, days=None, now=None):
    now = int(now or time.time())
    days = int(days or cfg.retention_days)
    start = now - days * 86400
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM samples WHERE ts > ? AND ts <= ? ORDER BY ts", (start, now))]
    for r in rows:
        r["w"] = min(r["interval"], MAX_GAP)
    proc_rows = conn.execute(
        "SELECT ts, name, cpu, mem_kb, nproc FROM proc_samples WHERE ts > ? AND ts <= ?",
        (start, now)).fetchall()
    weights = dict((r["ts"], r["w"]) for r in rows if r["source"] == "native")

    day_starts = []
    midnight = local_midnight(start)
    while midnight <= now:
        day_starts.append(midnight)
        midnight = next_midnight(midnight)
    day_rows = [[] for _ in day_starts]
    for r in rows:
        day_rows[bisect.bisect_right(day_starts, r["ts"]) - 1].append(r)
    day_procs = [[] for _ in day_starts]
    for p in proc_rows:
        day_procs[bisect.bisect_right(day_starts, p[0]) - 1].append(tuple(p))

    day_list = []
    for i, day_start in enumerate(day_starts):
        day_end = next_midnight(day_start)
        span = min(day_end, now) - max(day_start, start)
        stats = period_stats(day_rows[i], span, cfg)
        native = sum(r["w"] for r in day_rows[i] if r["source"] == "native")
        stats["top_cpu"], stats["top_mem"] = top_processes(day_procs[i], weights, native, cfg.top_n)
        stats["hourly"] = hourly(day_rows[i])
        stats["date"] = time.strftime("%Y-%m-%d", time.localtime(day_start))
        stats["start"], stats["end"] = day_start, day_end
        day_list.append(stats)

    overall = period_stats(rows, now - start, cfg)
    native_total = sum(weights.values())
    top_cpu, top_mem = top_processes([tuple(p) for p in proc_rows], weights, native_total,
                                     max(10, cfg.top_n))
    counts = dict((src, n) for src, n in conn.execute(
        "SELECT source, COUNT(*) FROM samples GROUP BY source"))
    first_last = conn.execute("SELECT MIN(ts), MAX(ts) FROM samples").fetchone()
    mem_total = rows[-1]["mem_total_kb"] if rows and rows[-1]["mem_total_kb"] else meta_get(
        conn, "mem_total_kb")
    return {
        "version": VERSION,
        "label": cfg.label,
        "hostname": socket.gethostname(),
        "generated_at": now,
        "tz_offset": time.localtime(now).tm_gmtoff,
        "tz_name": time.strftime("%Z", time.localtime(now)),
        "retention_days": cfg.retention_days,
        "window_days": days,
        "start": start,
        "end": now,
        "cores": meta_get(conn, "cores") or os.cpu_count(),
        "mem_total_kb": mem_total,
        "cpu_alert": cfg.cpu_alert,
        "ram_alert": cfg.ram_alert,
        "top_n": cfg.top_n,
        "proc_mem_metric": meta_get(conn, "proc_mem_metric") or "RSS",
        "bucket": BUCKET,
        "counts": {"native": counts.get("native", 0), "sysstat": counts.get("sysstat", 0)},
        "first_ts": first_last[0],
        "last_ts": first_last[1],
        "overall": overall,
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "days": day_list,
        "series": bucketize(rows),
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def fmt_kb(kb):
    if kb is None:
        return "-"
    if kb >= 1048576:
        return "%.2f GiB" % (kb / 1048576.0)
    return "%d MiB" % round(kb / 1024.0)


def fmt_pct(value):
    return "-" if value is None else "%.1f%%" % value


def fmt_load(value):
    return "-" if value is None else "%.2f" % value


def fmt_at(ts, with_day=False):
    if ts is None:
        return ""
    return time.strftime("%a %H:%M" if with_day else "%H:%M", time.localtime(ts))


def bar(value, scale, width=24):
    if value is None:
        return ""
    filled = int(round(min(1.0, value / scale) * width)) if scale else 0
    return "#" * filled + "." * (width - filled)


def text_report(data, day=None):
    out = []
    o = data["overall"]
    out.append("srvmon report: %s, last %d days (%s to %s %s)" % (
        data["label"], data["window_days"],
        time.strftime("%b %d %H:%M", time.localtime(data["start"])),
        time.strftime("%b %d %H:%M", time.localtime(data["end"])), data["tz_name"]))
    out.append("%s cores, %s RAM, retention %d days, %d samples (%d collector, %d sysstat)" % (
        data["cores"], fmt_kb(data["mem_total_kb"]), data["retention_days"],
        o["samples"], data["counts"]["native"], data["counts"]["sysstat"]))
    out.append("")
    if day:
        days = [d for d in data["days"] if d["date"] == day]
        if not days:
            return "No data for %s (available: %s)" % (
                day, ", ".join(d["date"] for d in data["days"]))
        return "\n".join(out + day_report(data, days[0]))

    header = "%-10s %5s %7s %7s %13s %11s %7s %13s  %s" % (
        "Day", "Cover", "CPU avg", "CPU p95", "CPU peak", "Load1 peak", "RAM avg",
        "RAM peak", "Top CPU / Top RAM")
    out.append(header)
    out.append("-" * len(header))
    for d in data["days"]:
        tc = d["top_cpu"][0]["name"] if d["top_cpu"] else "-"
        tm = d["top_mem"][0]["name"] if d["top_mem"] else "-"
        out.append("%-10s %4.0f%% %7s %7s %7s %5s %5s %5s %7s %7s %5s  %s / %s" % (
            time.strftime("%a %m-%d", time.localtime(d["start"])), d["coverage"],
            fmt_pct(d["cpu_avg"]), fmt_pct(d["cpu_p95"]), fmt_pct(d["cpu_max"]),
            fmt_at(d["cpu_max_at"]), fmt_load(d["load1_max"]), fmt_at(d["load1_max_at"]),
            fmt_pct(d["mem_avg"]), fmt_pct(d["mem_max"]), fmt_at(d["mem_max_at"]), tc, tm))
    out.append("-" * len(header))
    out.append("%-10s %4.0f%% %7s %7s %7s %-11s %5s %-5s %7s %7s %s" % (
        "Window", o["coverage"], fmt_pct(o["cpu_avg"]), fmt_pct(o["cpu_p95"]),
        fmt_pct(o["cpu_max"]), fmt_at(o["cpu_max_at"], True), fmt_load(o["load1_max"]),
        "", fmt_pct(o["mem_avg"]), fmt_pct(o["mem_max"]), fmt_at(o["mem_max_at"], True)))
    out.append("")
    out.extend(process_tables(data["top_cpu"], data["top_mem"], True))
    return "\n".join(out)


def day_report(data, d):
    out = ["Day %s: coverage %.0f%%, CPU avg %s (peak %s at %s), load1 peak %s at %s, "
           "RAM avg %s (peak %s at %s)" % (
               d["date"], d["coverage"], fmt_pct(d["cpu_avg"]), fmt_pct(d["cpu_max"]),
               fmt_at(d["cpu_max_at"]), fmt_load(d["load1_max"]), fmt_at(d["load1_max_at"]),
               fmt_pct(d["mem_avg"]), fmt_pct(d["mem_max"]), fmt_at(d["mem_max_at"])), ""]
    out.append("Hour  CPU avg                          RAM avg   Load1")
    load_scale = max([v for v in d["hourly"]["load1"] if v is not None] + [data["cores"] or 1])
    for h in range(24):
        cpu, mem, load = d["hourly"]["cpu"][h], d["hourly"]["mem"][h], d["hourly"]["load1"][h]
        if cpu is None and mem is None:
            out.append("%02d:00 -" % h)
            continue
        out.append("%02d:00 %6s %s %7s  %5s %s" % (
            h, fmt_pct(cpu), bar(cpu, 100), fmt_pct(mem), fmt_load(load), bar(load, load_scale, 10)))
    out.append("")
    out.extend(process_tables(d["top_cpu"], d["top_mem"], False))
    return out


def process_tables(top_cpu, top_mem, with_day):
    out = ["Top processes by CPU (average share of total CPU capacity)"]
    if not top_cpu:
        out.append("  no process data yet (recorded by the srvmon collector only)")
    for i, p in enumerate(top_cpu, 1):
        out.append("  %2d. %-32s avg %6s  peak %6s at %s" % (
            i, p["name"][:32], fmt_pct(p["cpu_avg"]), fmt_pct(p["cpu_max"]),
            fmt_at(p["cpu_max_at"], with_day)))
    out.append("")
    out.append("Top processes by RAM (PSS or RSS, summed over processes with the same name)")
    if not top_mem:
        out.append("  no process data yet (recorded by the srvmon collector only)")
    for i, p in enumerate(top_mem, 1):
        out.append("  %2d. %-32s avg %9s  peak %9s at %s  (up to %d procs)" % (
            i, p["name"][:32], fmt_kb(p["mem_avg_kb"]), fmt_kb(p["mem_max_kb"]),
            fmt_at(p["mem_max_at"], with_day), p["nproc_max"]))
    return out


def render_html(data):
    template_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "dashboard.html")
    with open(template_path) as fh:
        template = fh.read()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    title = data["label"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return template.replace("/*__DATA__*/null", payload).replace("__TITLE__", title)


def status_info(cfg, conn):
    first, last, count = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM samples").fetchone()
    last_native = conn.execute(
        "SELECT MAX(ts) FROM samples WHERE source = 'native'").fetchone()[0]
    procs = conn.execute("SELECT COUNT(*) FROM proc_samples").fetchone()[0]
    scheduler = "unknown"
    if os.path.exists("/etc/systemd/system/srvmon-collect.timer"):
        try:
            state = subprocess.check_output(
                ["systemctl", "is-active", "srvmon-collect.timer"],
                stderr=subprocess.DEVNULL, universal_newlines=True).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            state = (getattr(exc, "output", "") or "inactive").strip()
        scheduler = "systemd timer (%s)" % state
    elif os.path.exists("/etc/cron.d/srvmon"):
        scheduler = "cron (/etc/cron.d/srvmon)"
    return {
        "version": VERSION,
        "config": cfg.path,
        "db_path": cfg.db_path,
        "db_size_kb": int(os.path.getsize(cfg.db_path) / 1024) if os.path.exists(cfg.db_path) else 0,
        "retention_days": cfg.retention_days,
        "top_n": cfg.top_n,
        "samples": count,
        "process_rows": procs,
        "first_ts": first,
        "last_ts": last,
        "last_native_ts": last_native,
        "last_native_age_s": int(time.time() - last_native) if last_native else None,
        "scheduler": scheduler,
    }


def serve(cfg, bind, port, token=None):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if token and query.get("token", [""])[0] != token:
                return self.reply(403, "text/plain", b"forbidden\n")
            try:
                days = int(query.get("days", [cfg.retention_days])[0])
            except ValueError:
                days = cfg.retention_days
            conn = connect(cfg)
            try:
                data = summarize(cfg, conn, days=days)
            finally:
                conn.close()
            if url.path in ("/", "/index.html"):
                return self.reply(200, "text/html; charset=utf-8", render_html(data).encode("utf-8"))
            if url.path == "/api/summary":
                return self.reply(200, "application/json", json.dumps(data).encode("utf-8"))
            return self.reply(404, "text/plain", b"not found\n")

        def reply(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    server = HTTPServer((bind, port), Handler)
    print("srvmon dashboard on http://%s:%d/ (Ctrl+C to stop)" % (bind, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="srvmon", description="CPU, load, RAM and top-process history for Linux servers.")
    parser.add_argument("--config", help="config file (default %s)" % CONFIG_PATH)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("collect", help="take one sample (run every minute)")
    sub.add_parser("prune", help="delete data older than RETENTION_DAYS")
    p = sub.add_parser("import-sysstat", help="backfill CPU/load/RAM from sysstat files")
    p.add_argument("--dir", action="append", help="sysstat directory (repeatable)")
    p = sub.add_parser("report", help="print a daily report")
    p.add_argument("--days", type=int, help="window in days (default RETENTION_DAYS)")
    p.add_argument("--day", help="hour-by-hour detail for one day, YYYY-MM-DD")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")
    p = sub.add_parser("html", help="write the HTML dashboard to a file")
    p.add_argument("--out", required=True, help="output file")
    p.add_argument("--days", type=int)
    p = sub.add_parser("serve", help="serve the dashboard over HTTP")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default=os.environ.get("SRVMON_TOKEN"),
                   help="require ?token=... on every request")
    sub.add_parser("status", help="show collector and database status")
    sub.add_parser("version")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "version":
        print("srvmon " + VERSION)
        return 0

    cfg = Config(args.config)
    if args.command == "collect":
        result = collect(cfg)
        print("ts=%d cpu=%.1f%% load1=%.2f ram=%.1f%% procs=%d" % (
            result["ts"], result["cpu"], result["load1"], result["mem_pct"], len(result["top"])))
        return 0

    conn = connect(cfg)
    if args.command == "prune":
        print("removed %d rows" % prune(conn, cfg.retention_days))
    elif args.command == "import-sysstat":
        result = import_sysstat(cfg, conn, dirs=args.dir)
        prune(conn, cfg.retention_days)
        print("sysstat: %(files)d files, %(inserted)d samples imported" % result)
    elif args.command == "report":
        data = summarize(cfg, conn, days=args.days)
        print(json.dumps(data, indent=2) if args.json else text_report(data, day=args.day))
    elif args.command == "html":
        with open(args.out, "w") as fh:
            fh.write(render_html(summarize(cfg, conn, days=args.days)))
        print("wrote " + args.out)
    elif args.command == "serve":
        if args.bind not in ("127.0.0.1", "localhost", "::1") and not args.token:
            print("warning: serving on %s without --token exposes server metrics" % args.bind,
                  file=sys.stderr)
        serve(cfg, args.bind, args.port, args.token)
    elif args.command == "status":
        info = status_info(cfg, conn)
        for key in ("first_ts", "last_ts", "last_native_ts"):
            if info[key]:
                info[key] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info[key]))
        for key, value in info.items():
            print("%-18s %s" % (key, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())

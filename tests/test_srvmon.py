import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "srvmon"))
import srvmon  # noqa: E402


def make_cfg(tmp, **extra):
    path = os.path.join(tmp, "srvmon.conf")
    lines = ["DB_PATH=%s" % os.path.join(tmp, "db.sqlite"), "LABEL=test-box"]
    lines += ["%s=%s" % kv for kv in extra.items()]
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return srvmon.Config(path)


def insert_sample(conn, ts, cpu, mem_pct, load1, interval=60, source="native"):
    conn.execute(
        "INSERT INTO samples (ts, interval, source, cpu, load1, load5, load15, mem_total_kb,"
        " mem_used_kb, mem_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, interval, source, cpu, load1, load1, load1, 4000000, int(40000 * mem_pct), mem_pct))


class ConfigTest(unittest.TestCase):
    def test_parse_and_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, RETENTION_DAYS="3", TOP_N="'8'")
            self.assertEqual(cfg.retention_days, 3)
            self.assertEqual(cfg.top_n, 8)
            os.environ["SRVMON_RETENTION_DAYS"] = "5"
            try:
                self.assertEqual(srvmon.Config(cfg.path).retention_days, 5)
            finally:
                del os.environ["SRVMON_RETENTION_DAYS"]

    def test_set_config_value_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            srvmon.set_config_value(cfg.path, "RETENTION_DAYS", "7")
            srvmon.set_config_value(cfg.path, "RETENTION_DAYS", "9")
            text = open(cfg.path).read()
            self.assertIn("LABEL=test-box", text)
            self.assertEqual(text.count("RETENTION_DAYS="), 1)
            self.assertEqual(srvmon.Config(cfg.path).retention_days, 9)


class ParsingTest(unittest.TestCase):
    def test_stat_line_with_spaces_and_parens_in_comm(self):
        line = ("1234 (my (odd) proc) S 1 1234 1234 0 -1 4194560 100 0 0 0 "
                "250 50 0 0 20 0 1 0 98765 123456789 2560 18446744073709551615")
        comm, ppid, ticks, start, rss = srvmon.parse_stat_line(line)
        self.assertEqual(comm, "my (odd) proc")
        self.assertEqual(ppid, 1)
        self.assertEqual(ticks, 300)
        self.assertEqual(start, 98765)
        self.assertEqual(rss, 2560 * srvmon.PAGE_KB)

    def test_kernel_threads_are_grouped(self):
        self.assertEqual(srvmon.process_name(77, "kworker/u8:1-events", 2), "kworker")

    def test_cpu_breakdown(self):
        prev = [100, 0, 50, 800, 50, 0, 0, 0]
        cur = [160, 0, 70, 900, 70, 0, 0, 0]  # +60 user +20 sys +100 idle +20 iowait
        b = srvmon.cpu_breakdown(prev, cur)
        self.assertAlmostEqual(b["cpu"], 40.0)
        self.assertAlmostEqual(b["user"], 30.0)
        self.assertAlmostEqual(b["iowait"], 10.0)


class ProcessUsageTest(unittest.TestCase):
    def test_groups_by_name_and_counts_new_processes(self):
        hz = srvmon.CLK_TCK
        prev = {"ts": 1000.0, "uptime": 500.0, "cpu": [0] * 8,
                "procs": {"10": [100, 0], "11": [100, 0], "12": [100, 0]}}
        cur = {"ts": 1060.0, "uptime": 560.0, "cpu": [0] * 8, "procs": {
            "10": ("php-fpm", 100, 30 * hz, 1000),
            "11": ("php-fpm", 100, 30 * hz, 1000),
            "12": ("mysqld", 100, 6 * hz, 5000),
            "13": ("backup.sh", 520 * hz, 12 * hz, 300),  # started inside the interval
        }}
        usage = {u[0]: u for u in srvmon.process_usage(prev, cur, cores=2)}
        self.assertAlmostEqual(usage["php-fpm"][1], 50.0)  # 60 cpu-s / 60 s / 2 cores
        self.assertEqual(usage["php-fpm"][2], 2000)
        self.assertEqual(usage["php-fpm"][3], 2)
        self.assertAlmostEqual(usage["backup.sh"][1], 10.0)
        top = {u[0] for u in srvmon.select_top(list(usage.values()), 1)}
        self.assertEqual(top, {"php-fpm", "mysqld"})


class StorageTest(unittest.TestCase):
    def test_prune_keeps_only_retention_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, RETENTION_DAYS="7")
            conn = srvmon.connect(cfg)
            now = 1_800_000_000
            for age_days in (0, 3, 6.9, 7.1, 10):
                ts = int(now - age_days * 86400)
                insert_sample(conn, ts, 10, 20, 0.5)
                conn.execute("INSERT INTO proc_samples VALUES (?,?,?,?,?)", (ts, "x", 1, 1, 1))
            conn.commit()
            srvmon.prune(conn, cfg.retention_days, now=now)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM proc_samples").fetchone()[0], 3)

    def test_collect_on_live_proc(self):
        if not os.path.exists("/proc/stat"):
            self.skipTest("needs Linux /proc")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            first = srvmon.collect(cfg)
            self.assertTrue(0 <= first["cpu"] <= 100)
            self.assertTrue(first["top"])
            conn = srvmon.connect(cfg)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 1)


class SummaryTest(unittest.TestCase):
    def test_daily_stats_and_top_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, RETENTION_DAYS="2")
            conn = srvmon.connect(cfg)
            today = srvmon.local_midnight(time.time())
            now = today + 12 * 3600
            for minute in range(0, 6 * 60):  # 06:00-12:00 today, one sample per minute
                ts = today + 6 * 3600 + minute * 60
                cpu = 90.0 if minute < 30 else 10.0
                insert_sample(conn, ts, cpu, 50.0, 1.0)
                conn.execute("INSERT INTO proc_samples VALUES (?,?,?,?,?)",
                             (ts, "worker", cpu / 2, 2048 * 1024, 3))
                if minute % 2 == 0:
                    conn.execute("INSERT INTO proc_samples VALUES (?,?,?,?,?)",
                                 (ts, "cache", 1.0, 4096 * 1024, 1))
            insert_sample(conn, today - 3600, 20.0, 30.0, 0.2, interval=600, source="sysstat")
            conn.commit()

            data = srvmon.summarize(cfg, conn, now=now)
            self.assertEqual([d["date"] for d in data["days"]][-1],
                             time.strftime("%Y-%m-%d", time.localtime(today)))
            day = data["days"][-1]
            self.assertAlmostEqual(day["cpu_avg"], (30 * 90 + 330 * 10) / 360.0, places=1)
            self.assertEqual(day["cpu_max"], 90.0)
            self.assertEqual(day["cpu_alert_min"], 30)
            self.assertEqual(day["coverage"], 50.0)
            self.assertEqual(day["top_cpu"][0]["name"], "worker")
            self.assertEqual(day["top_mem"][0]["name"], "cache")
            self.assertEqual(day["top_mem"][0]["seen_pct"], 50.0)
            self.assertEqual(data["counts"], {"native": 360, "sysstat": 1})
            self.assertIsNotNone(day["hourly"]["cpu"][6])
            self.assertIsNone(day["hourly"]["cpu"][3])

            report = srvmon.text_report(data)
            self.assertIn("worker", report)
            self.assertIn("Day", srvmon.text_report(data, day=day["date"]))

    def test_html_escapes_process_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            conn = srvmon.connect(cfg)
            now = int(time.time())
            insert_sample(conn, now - 60, 5, 5, 0.1)
            conn.execute("INSERT INTO proc_samples VALUES (?,?,?,?,?)",
                         (now - 60, "</script><b>x", 1, 1, 1))
            conn.commit()
            html = srvmon.render_html(srvmon.summarize(cfg, conn, now=now))
            self.assertNotIn("</script><b>", html)
            self.assertNotIn("/*__DATA__*/", html)


if __name__ == "__main__":
    unittest.main()

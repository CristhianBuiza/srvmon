#!/usr/bin/python
# coding: utf-8
# srvmon aaPanel plugin: shows the srvmon dashboard inside the panel.
# The collector itself is the standalone srvmon core in /opt/srvmon.
import importlib.util
import os
import sys

os.chdir("/www/server/panel")
sys.path.append("class/")
import public  # noqa: E402

CORE = "/opt/srvmon/srvmon.py"


def _core():
    if not os.path.exists(CORE):
        raise RuntimeError("srvmon core not found at %s. Run install.sh from the srvmon repository." % CORE)
    # Loaded by path on every call so upgrades apply without restarting the panel.
    spec = importlib.util.spec_from_file_location("srvmon_core", CORE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _int(get, name, default, lo, hi):
    try:
        value = int(getattr(get, name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


class srvmon_main:
    def get_dashboard(self, get):
        try:
            core = _core()
            cfg = core.Config()
            days = _int(get, "days", cfg.retention_days, 1, cfg.retention_days)
            conn = core.connect(cfg)
            try:
                html = core.render_html(core.summarize(cfg, conn, days=days))
                status = core.status_info(cfg, conn)
            finally:
                conn.close()
        except Exception as exc:
            return public.returnMsg(False, "srvmon: %s" % exc)
        return {"status": True, "html": html, "info": status}

    def get_summary(self, get):
        try:
            core = _core()
            cfg = core.Config()
            conn = core.connect(cfg)
            try:
                data = core.summarize(cfg, conn, days=_int(get, "days", cfg.retention_days, 1, cfg.retention_days))
            finally:
                conn.close()
        except Exception as exc:
            return public.returnMsg(False, "srvmon: %s" % exc)
        return {"status": True, "data": data}

    def get_export(self, get):
        try:
            core = _core()
            cfg = core.Config()
            detail = getattr(get, "detail", "hour")
            conn = core.connect(cfg)
            try:
                data = core.summarize(cfg, conn, days=_int(get, "days", cfg.retention_days, 1, cfg.retention_days))
            finally:
                conn.close()
            text = core.export_markdown(data, detail if detail in core.EXPORT_STEPS else "hour")
        except Exception as exc:
            return public.returnMsg(False, "srvmon: %s" % exc)
        return {"status": True, "text": text, "filename": core.export_filename(data)}

    def get_status(self, get):
        try:
            core = _core()
            cfg = core.Config()
            conn = core.connect(cfg)
            try:
                info = core.status_info(cfg, conn)
            finally:
                conn.close()
        except Exception as exc:
            return public.returnMsg(False, "srvmon: %s" % exc)
        return {"status": True, "info": info}

    def set_retention(self, get):
        try:
            days = int(get.days)
        except (AttributeError, TypeError, ValueError):
            return public.returnMsg(False, "days must be a whole number")
        if not 1 <= days <= 90:
            return public.returnMsg(False, "days must be between 1 and 90")
        try:
            core = _core()
            cfg = core.Config()
            core.set_config_value(cfg.path, "RETENTION_DAYS", str(days))
            conn = core.connect(cfg)
            try:
                removed = core.prune(conn, days)
            finally:
                conn.close()
        except Exception as exc:
            return public.returnMsg(False, "srvmon: %s" % exc)
        public.WriteLog("srvmon", "Retention set to %d days (%d old rows removed)" % (days, removed))
        return public.returnMsg(True, "Keeping %d days of history (%d old rows removed)" % (days, removed))

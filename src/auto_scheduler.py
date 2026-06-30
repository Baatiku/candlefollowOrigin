"""Automatic trading window scheduler.

Runs a background thread that checks the current Lagos time every 60 seconds
and starts/stops the bot according to user-configured windows.

Storage: kv_store (PostgreSQL) — key "auto_schedule".
Falls back to a JSON file when no DATABASE_URL is set.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SCHEDULE_KEY = "auto_schedule"
LAGOS_OFFSET = timedelta(hours=1)
_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "auto_schedule.json"
)

_scheduler_thread: threading.Thread | None = None
scheduler_started_bot: bool = False


# ── Persistence (kv_store + file fallback) ────────────────────────────────────

def _db_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("auto_scheduler DB connect failed: %s", e)
        return None


def kv_load(key: str):
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                return json.loads(row[0])
        except Exception as e:
            logger.warning("kv_load(%s) failed: %s", key, e)
            try:
                conn.close()
            except Exception:
                pass
    if os.path.exists(_FILE_PATH):
        try:
            with open(_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def kv_save(key: str, value: dict):
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO kv_store (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = now()""",
                (key, json.dumps(value))
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("kv_save(%s) failed: %s", key, e)
            try:
                conn.close()
            except Exception:
                pass
    try:
        os.makedirs(os.path.dirname(_FILE_PATH), exist_ok=True)
        with open(_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
    except Exception as e:
        logger.warning("auto_scheduler file save failed: %s", e)


# ── Schedule helpers ──────────────────────────────────────────────────────────

def in_schedule_window(windows: list, lagos_minutes: int) -> bool:
    """True if *lagos_minutes* (minutes since midnight Lagos) is inside any window."""
    for w in windows:
        try:
            sh, sm = map(int, w["start"].split(":"))
            eh, em = map(int, w["end"].split(":"))
        except (KeyError, ValueError, AttributeError):
            continue
        s = sh * 60 + sm
        e = eh * 60 + em
        if s < e:
            if s <= lagos_minutes < e:
                return True
        else:
            if lagos_minutes >= s or lagos_minutes < e:
                return True
    return False


def next_window_start(windows: list, lagos_minutes: int):
    """Return (window_dict, minutes_until) for the soonest upcoming window start."""
    candidates = []
    for w in windows:
        try:
            sh, sm = map(int, w["start"].split(":"))
        except (KeyError, ValueError, AttributeError):
            continue
        s = sh * 60 + sm
        diff = s - lagos_minutes if s > lagos_minutes else (24 * 60 - lagos_minutes + s)
        candidates.append((diff, w))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][0]


def fmt_12h(time_str: str) -> str:
    """'14:30' → '2:30 PM'"""
    try:
        h, m = map(int, time_str.split(":"))
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except (ValueError, AttributeError):
        return time_str


def validate_windows(raw: list) -> list:
    """Return only valid HH:MM window dicts."""
    cleaned = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        s = str(w.get("start", "")).strip()
        e = str(w.get("end", "")).strip()
        try:
            sh, sm = map(int, s.split(":"))
            eh, em = map(int, e.split(":"))
            assert 0 <= sh < 24 and 0 <= sm < 60
            assert 0 <= eh < 24 and 0 <= em < 60
        except (ValueError, AssertionError, AttributeError):
            continue
        cleaned.append({"start": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}"})
    return cleaned


def get_schedule_status() -> dict:
    now_lagos = datetime.utcnow() + LAGOS_OFFSET
    lagos_min = now_lagos.hour * 60 + now_lagos.minute

    cfg = kv_load(SCHEDULE_KEY) or {"enabled": False, "windows": []}
    windows = cfg.get("windows", [])
    in_win = in_schedule_window(windows, lagos_min)
    next_w, mins_until = next_window_start(windows, lagos_min)

    time_str = now_lagos.strftime("%I:%M %p")
    if time_str.startswith("0"):
        time_str = time_str[1:]

    return {
        "enabled": cfg.get("enabled", False),
        "windows": windows,
        "in_window": in_win,
        "scheduler_running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "scheduler_started_bot": scheduler_started_bot,
        "next_window": next_w,
        "minutes_until_next": mins_until,
        "next_start_label": fmt_12h(next_w["start"]) if next_w and not in_win else None,
        "current_time_lagos": time_str,
    }


def save_schedule_config(enabled: bool, windows: list) -> dict:
    cfg = {"enabled": enabled, "windows": validate_windows(windows)}
    kv_save(SCHEDULE_KEY, cfg)
    return cfg


# ── Scheduler loop ────────────────────────────────────────────────────────────

def start_scheduler(bot_ref, thread_alive_fn, start_trading_fn, license_valid_fn):
    """Launch the background scheduler thread (idempotent)."""
    global _scheduler_thread

    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    def _loop():
        global scheduler_started_bot
        import time
        logger.info("Auto-start scheduler thread running.")
        while True:
            try:
                cfg = kv_load(SCHEDULE_KEY)
                if cfg and cfg.get("enabled") and cfg.get("windows"):
                    windows = cfg["windows"]
                    now_lagos = datetime.utcnow() + LAGOS_OFFSET
                    lagos_min = now_lagos.hour * 60 + now_lagos.minute
                    in_win = in_schedule_window(windows, lagos_min)
                    is_running = bot_ref.running and thread_alive_fn()
                    is_connected = bot_ref.is_session_ready()
                    manual_stop = getattr(bot_ref, "manual_stop_requested", False)

                    if (
                        in_win
                        and not is_running
                        and is_connected
                        and license_valid_fn()
                        and not manual_stop
                    ):
                        logger.info("Scheduler: entering window — starting bot")
                        if start_trading_fn():
                            scheduler_started_bot = True
                    elif not in_win:
                        if is_running and scheduler_started_bot:
                            logger.info("Scheduler: window ended — stopping bot")
                            bot_ref.stop(manual=False)
                            scheduler_started_bot = False
                        # Outside all windows — allow auto-start when the next window opens
                        bot_ref.manual_stop_requested = False
            except Exception as e:
                logger.warning("Scheduler loop error: %s", e)
            time.sleep(60)

    _scheduler_thread = threading.Thread(
        target=_loop, name="auto-scheduler", daemon=True
    )
    _scheduler_thread.start()

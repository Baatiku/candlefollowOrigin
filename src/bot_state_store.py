"""Persist bot trading state across restarts.

Primary storage: PostgreSQL (DATABASE_URL) — survives all restarts and deploys.
Fallback: JSON file in data/ — used when DB is unavailable (local dev without DB).
"""
import json
import os
import shutil
import threading
import logging
from datetime import date

logger = logging.getLogger(__name__)

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "bot_state.json"
)

STORE_VERSION = 3

_store_lock = threading.Lock()


def state_file_path():
    return os.environ.get("BOT_STATE_PATH", DEFAULT_PATH)


def account_state_key(account_type, balance_id=None):
    """Unique key for persisted ladder/debt/statistics."""
    if account_type == "TOURNAMENT" and balance_id is not None:
        return f"TOURNAMENT_{balance_id}"
    return account_type or "PRACTICE"


def _db_conn():
    """Return a psycopg2 connection if DATABASE_URL is configured, else None."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("Bot state DB connection failed: %s", e)
        return None


def _ensure_db_table():
    conn = _db_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_states (
                account_key TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("Failed to ensure bot_states table: %s", e)
        try:
            conn.close()
        except Exception:
            pass


_ensure_db_table()


def _read_store_file():
    path = state_file_path()
    for candidate in (path, path + ".bak"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load bot state from {candidate}: {e}")
            continue

        if isinstance(data, dict) and "accounts" in data:
            return data

        if isinstance(data, dict) and "cumulative_debt" in data:
            key = data.get("account_type") or "PRACTICE"
            logger.info(f"Migrating legacy bot state into account bucket '{key}'")
            return {"version": STORE_VERSION, "accounts": {key: data}}

    return {"version": STORE_VERSION, "accounts": {}}


def _write_store_file(store):
    path = state_file_path()
    tmp_path = path + ".tmp"
    bak_path = path + ".bak"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        store["version"] = STORE_VERSION
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        if os.path.exists(path):
            shutil.copy2(path, bak_path)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Could not save bot state to {path}: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def load_state(account_key):
    with _store_lock:
        conn = _db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT state_json FROM bot_states WHERE account_key = %s",
                    (account_key,)
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    return json.loads(row[0])
            except Exception as e:
                logger.warning("DB load_state failed, falling back to file: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
        store = _read_store_file()
        accounts = store.get("accounts") or {}
        return accounts.get(account_key)


def save_state(account_key, data):
    with _store_lock:
        conn = _db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO bot_states (account_key, state_json, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (account_key) DO UPDATE
                        SET state_json = EXCLUDED.state_json,
                            updated_at = now()
                    """,
                    (account_key, json.dumps(data))
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logger.warning("DB save_state failed: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
        store = _read_store_file()
        if "accounts" not in store:
            store["accounts"] = {}
        store["accounts"][account_key] = data
        _write_store_file(store)


def clear_all_accounts():
    """Remove every persisted account bucket (full factory reset)."""
    with _store_lock:
        conn = _db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM bot_states")
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logger.warning("DB clear_all_accounts failed: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
        _write_store_file({"version": STORE_VERSION, "accounts": {}})
    logger.info("Cleared all persisted account state")


def snapshot_from_bot(bot):
    daily_date = bot.daily_start_time
    if isinstance(daily_date, date):
        daily_date = daily_date.isoformat()
    tier_date = getattr(bot, "tier_escalations_date", None)
    if isinstance(tier_date, date):
        tier_date = tier_date.isoformat()
    balance_id = getattr(bot, "active_balance_id", None)
    return {
        "version": 2,
        "account_type": bot.account_type,
        "balance_id": balance_id,
        "asset": bot.asset,
        "cumulative_debt": bot.cumulative_debt,
        "current_tier_index": bot.current_tier_index,
        "session_round_count": bot.session_round_count,
        "session_profit": bot.session_profit,
        "session_active": bot.session_active,
        "round_number": bot.round_number,
        "total_profit": bot.total_profit,
        "wins": bot.wins,
        "losses": bot.losses,
        "daily_start_balance": bot.daily_start_balance,
        "daily_profit": bot.daily_profit,
        "daily_start_time": daily_date,
        "paused": getattr(bot, "paused", False),
        "simulation_mode": getattr(bot, "simulation_mode", False),
        "auto_select_asset": getattr(bot, "auto_select_asset", True),
        "auto_select_manually_disabled": getattr(bot, "auto_select_manually_disabled", False),
        "tier_escalations_today": getattr(bot, "tier_escalations_today", 0),
        "tier_escalations_date": tier_date,
        "assigned_tier_index": getattr(bot, "assigned_tier_index", bot.current_tier_index),
        "tier_failure_streak": getattr(bot, "tier_failure_streak", 0),
        "tier_recovery_wins": getattr(bot, "tier_recovery_wins", 0),
        "window_profit": getattr(bot, "window_profit", 0.0),
        "evaluation_window_start": (
            bot.evaluation_window_start.isoformat() + "Z"
            if getattr(bot, "evaluation_window_start", None)
            else None
        ),
        "tier_exhaustion_cooldown_until": (
            bot.tier_exhaustion_cooldown_until.isoformat() + "Z"
            if getattr(bot, "tier_exhaustion_cooldown_until", None)
            else None
        ),
        "last_tier_exhaustion_at": (
            bot.last_tier_exhaustion_at.isoformat() + "Z"
            if getattr(bot, "last_tier_exhaustion_at", None)
            else None
        ),
        "window_had_tier_exhaustion": getattr(bot, "window_had_tier_exhaustion", False),
        "last_stop_reason": getattr(bot, "last_stop_reason", ""),
        "last_error": getattr(bot, "last_error", ""),
        "inflight_trade_ids": [int(x) for x in getattr(bot, "_inflight_trade_ids", [])],
        "session_peak_balance": float(getattr(bot, "session_peak_balance", 0.0) or 0.0),
        "locked_profit": float(getattr(bot, "locked_profit", 0.0) or 0.0),
        "risk_mode_until": (
            bot.risk_mode_until.isoformat() + "Z"
            if getattr(bot, "risk_mode_until", None)
            else None
        ),
        "mopup_initial_debt": float(getattr(bot, "mopup_initial_debt", 0.0)),
        "ladder_attempt_id": int(getattr(bot, "ladder_attempt_id", 0) or 0),
        "ladder_pair": getattr(bot, "ladder_pair", None),
        "ladder_loss_scores": list(getattr(bot, "ladder_loss_scores", []) or []),
        "saved_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }

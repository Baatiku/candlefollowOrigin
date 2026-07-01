"""Append-only trade history and simple analytics.

Primary storage: PostgreSQL (DATABASE_URL) — survives all restarts and deploys.
Fallback: JSONL file in data/ — used when DB is unavailable (local dev without DB).
"""
import csv
import io
import json
import os
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _db_conn():
    """Return a psycopg2 connection if DATABASE_URL is configured, else None."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.warning("Trade log DB connection failed: %s", e)
        return None


def _ensure_db_table():
    conn = _db_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                ts TEXT NOT NULL,
                account_key TEXT,
                data JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS trades_account_key_idx ON trades (account_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS trades_ts_idx ON trades (ts)")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("Failed to ensure trades table: %s", e)
        try:
            conn.close()
        except Exception:
            pass


_ensure_db_table()


def migrate_jsonl_to_db():
    """One-shot migration: imports file-based trade history into PostgreSQL.

    Called once at startup.  A marker in kv_store prevents re-running so
    subsequent restarts are instant.  Safe to call on every boot.
    """
    conn = _db_conn()
    if not conn:
        return

    path = log_path()
    if not os.path.exists(path):
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.commit()

        cur.execute("SELECT value FROM kv_store WHERE key = 'jsonl_trade_migrated_v1'")
        if cur.fetchone():
            cur.close()
            conn.close()
            return

        trades = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning("JSONL migration: could not read file: %s", e)
            cur.close()
            conn.close()
            return

        inserted = 0
        for t in trades:
            try:
                cur.execute(
                    "INSERT INTO trades (ts, account_key, data) VALUES (%s, %s, %s::jsonb)",
                    (t.get("ts"), t.get("account_key"), json.dumps(t))
                )
                inserted += 1
            except Exception as e:
                logger.warning("Migration: failed to insert trade: %s", e)
                try:
                    conn.rollback()
                except Exception:
                    pass

        cur.execute(
            """INSERT INTO kv_store (key, value)
               VALUES ('jsonl_trade_migrated_v1', %s)
               ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = now()""",
            (json.dumps({"migrated": inserted,
                         "ts": datetime.utcnow().isoformat()}),)
        )
        conn.commit()
        cur.close()
        conn.close()
        if inserted:
            logger.info("Migrated %d trades from trade_log.jsonl to PostgreSQL", inserted)
    except Exception as e:
        logger.warning("Trade log JSONL migration failed: %s", e)
        try:
            conn.close()
        except Exception:
            pass


# Flat CSV columns for analysis exports (stable order).
EVALUATION_CSV_FIELDS = [
    "ts",
    "account_key",
    "asset",
    "direction",
    "tier",
    "step",
    "bet",
    "round_profit",
    "outcome",
    "trading_mode",
    "strategy_mode",
    "bot_confidence",
    "entry_quality",
    "ensemble_combined_confidence",
    "ensemble_action",
    "entry_er",
    "entry_slope",
    "entry_slope_signed",
    "entry_straddle_score",
    "trend_aligned",
    "direction_flip_kind",
    "slope_override_flip",
    "rule_gate_reason",
    "ai_disabled",
    "ai_approved",
    "ai_confidence",
    "ai_skipped",
    "strike_profit_pct",
    "step_score_required",
    "debt",
    "session_profit",
    "simulation",
    "snap_efficiency_ratio",
    "snap_slope_signed",
    "snap_momentum_ratio",
    "snap_straddle_score",
    "snap_active_ratio",
    "snap_path_ratio",
]

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "trade_log.jsonl"
)


def log_path():
    return os.environ.get("TRADE_LOG_PATH", DEFAULT_PATH)


def copy_entry_snapshot(snapshot: dict | None) -> dict | None:
    """JSON-safe copy of live chart metrics captured at order placement."""
    if not snapshot or not isinstance(snapshot, dict):
        return None
    out = {}
    for key, val in snapshot.items():
        if val is None:
            continue
        if isinstance(val, (bool, int, float, str)):
            out[key] = val
        elif isinstance(val, (list, tuple)):
            try:
                out[key] = [float(x) if isinstance(x, (int, float)) else x for x in val]
            except (TypeError, ValueError):
                pass
    return out or None


def copy_bot_evaluation(evaluation: dict | None) -> dict | None:
    """JSON-safe copy of bot gate / confidence metrics at entry."""
    if not evaluation or not isinstance(evaluation, dict):
        return None
    out: Dict[str, Any] = {}
    for key, val in evaluation.items():
        if val is None:
            continue
        if isinstance(val, (bool, int, float, str)):
            out[key] = val
        elif isinstance(val, dict):
            nested = copy_bot_evaluation(val)
            if nested:
                out[key] = nested
        elif isinstance(val, (list, tuple)):
            try:
                out[key] = [
                    float(x) if isinstance(x, (int, float)) else x for x in val
                ]
            except (TypeError, ValueError):
                pass
    return out or None


def flatten_trade_for_export(trade: dict) -> Dict[str, Any]:
    """One row per trade with evaluation metrics flattened for CSV/Excel."""
    ev = trade.get("bot_evaluation") or {}
    snap = trade.get("entry_snapshot") or {}
    pnl = float(trade.get("round_profit", 0) or 0)
    direction = (
        ev.get("direction")
        or trade.get("bot_direction")
        or trade.get("direction")
        or ""
    )
    return {
        "ts": trade.get("ts"),
        "account_key": trade.get("account_key"),
        "asset": trade.get("asset"),
        "direction": direction,
        "tier": trade.get("tier"),
        "step": trade.get("step"),
        "bet": trade.get("bet"),
        "round_profit": round(pnl, 2),
        "outcome": "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven"),
        "trading_mode": ev.get("trading_mode") or trade.get("trading_mode"),
        "strategy_mode": ev.get("strategy_mode") or trade.get("strategy_mode"),
        "bot_confidence": ev.get("bot_confidence", trade.get("bot_confidence")),
        "entry_quality": ev.get("entry_quality", trade.get("entry_quality")),
        "ensemble_combined_confidence": ev.get(
            "ensemble_combined_confidence", trade.get("ensemble_combined_confidence")
        ),
        "ensemble_action": ev.get("ensemble_action", trade.get("ensemble_action")),
        "entry_er": ev.get("entry_er", trade.get("entry_er")),
        "entry_slope": ev.get("entry_slope", trade.get("entry_slope")),
        "entry_slope_signed": ev.get("entry_slope_signed"),
        "entry_straddle_score": ev.get(
            "entry_straddle_score", trade.get("entry_straddle_score")
        ),
        "trend_aligned": ev.get("trend_aligned"),
        "direction_flip_kind": ev.get("direction_flip_kind"),
        "slope_override_flip": ev.get("slope_override_flip"),
        "rule_gate_reason": ev.get("rule_gate_reason", trade.get("ai_reason")),
        "ai_disabled": ev.get("ai_disabled"),
        "ai_approved": ev.get("ai_approved", trade.get("ai_approved")),
        "ai_confidence": ev.get("ai_confidence", trade.get("ai_confidence")),
        "ai_skipped": ev.get("ai_skipped", trade.get("ai_skipped")),
        "strike_profit_pct": ev.get("strike_profit_pct"),
        "step_score_required": ev.get("step_score_required"),
        "debt": trade.get("debt"),
        "session_profit": trade.get("session_profit"),
        "simulation": trade.get("simulation"),
        "snap_efficiency_ratio": snap.get("efficiency_ratio"),
        "snap_slope_signed": snap.get("slope_signed"),
        "snap_momentum_ratio": snap.get("momentum_ratio"),
        "snap_straddle_score": snap.get("straddle_score"),
        "snap_active_ratio": snap.get("active_ratio"),
        "snap_path_ratio": snap.get("path_ratio"),
    }


def read_trades_for_export(
    limit: int = 5000,
    account_key: Optional[str] = None,
    include_all_accounts: bool = False,
) -> List[dict]:
    """Read trades oldest-first for export (newest at end)."""
    cap = min(max(int(limit), 1), 50000)
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            if include_all_accounts or not account_key:
                cur.execute(
                    "SELECT data FROM trades ORDER BY ts ASC, id ASC LIMIT %s",
                    (cap,)
                )
            else:
                cur.execute(
                    "SELECT data FROM trades WHERE account_key = %s ORDER BY ts ASC, id ASC LIMIT %s",
                    (account_key, cap)
                )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
        except Exception as e:
            logger.warning("DB read_trades_for_export failed, falling back to file: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    path = log_path()
    if not os.path.exists(path):
        return []
    rows_file: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not include_all_accounts and account_key:
                    if t.get("account_key") != account_key:
                        continue
                rows_file.append(t)
    except Exception as e:
        logger.warning(f"Trade log read failed: {e}")
        return []
    return rows_file[-cap:]


def export_trades_csv(
    limit: int = 5000,
    account_key: Optional[str] = None,
    include_all_accounts: bool = False,
) -> str:
    trades = read_trades_for_export(
        limit=limit,
        account_key=account_key,
        include_all_accounts=include_all_accounts,
    )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EVALUATION_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for trade in trades:
        writer.writerow(flatten_trade_for_export(trade))
    return buf.getvalue()


def append_trade(record: dict):
    record.setdefault("ts", datetime.utcnow().isoformat() + "Z")
    if record.get("entry_snapshot"):
        record["entry_snapshot"] = copy_entry_snapshot(record["entry_snapshot"])
    if record.get("bot_evaluation"):
        record["bot_evaluation"] = copy_bot_evaluation(record["bot_evaluation"])

    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO trades (ts, account_key, data) VALUES (%s, %s, %s::jsonb)",
                (record.get("ts"), record.get("account_key"), json.dumps(record))
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("Trade log DB write failed: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning(f"Trade log file write failed: {e}")


def _trade_matches_account(record, account_key, account_type=None):
    """Whether a log line belongs to the given account bucket."""
    key = record.get("account_key")
    if key:
        return key == account_key
    at = record.get("account_type")
    if at and account_type:
        return at == account_type
    # Legacy rows without tags are treated as practice-only.
    return account_key == "PRACTICE"


def purge_entire_trade_log():
    """Delete the full trade log (DB + file)."""
    count = 0
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trades")
            row = cur.fetchone()
            count = row[0] if row else 0
            cur.execute("DELETE FROM trades")
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("DB purge_entire_trade_log failed: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    path = log_path()
    if os.path.exists(path):
        try:
            if not count:
                with open(path, "r", encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip())
            os.remove(path)
        except Exception as e:
            logger.warning(f"Entire trade log file purge failed: {e}")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("")
            except Exception:
                pass

    logger.info("Purged entire trade log (%s rows)", count)
    return count


def purge_trades_for_account(account_key, account_type=None):
    """Remove trades for one account from DB + file; returns number of rows removed."""
    removed = 0
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM trades WHERE account_key = %s",
                (account_key,)
            )
            row = cur.fetchone()
            removed = row[0] if row else 0
            cur.execute("DELETE FROM trades WHERE account_key = %s", (account_key,))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.warning("DB purge_trades_for_account failed: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    import tempfile
    path = log_path()
    if not os.path.exists(path):
        return removed
    file_removed = 0
    tmp_fd, tmp_path = None, None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".tradelog_purge_"
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
            tmp_fd = None
            with open(path, "r", encoding="utf-8") as src:
                for line in src:
                    raw = line.rstrip("\n")
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        tmp_f.write(line if line.endswith("\n") else line + "\n")
                        continue
                    if _trade_matches_account(record, account_key, account_type):
                        file_removed += 1
                    else:
                        tmp_f.write(line if line.endswith("\n") else line + "\n")
        os.replace(tmp_path, path)
        tmp_path = None
        if not removed:
            removed = file_removed
    except Exception as e:
        logger.warning(f"Trade log file purge failed: {e}")
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return removed


def read_trades(limit=50, account_key=None):
    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            if account_key:
                cur.execute(
                    "SELECT data FROM trades WHERE account_key = %s ORDER BY ts DESC, id DESC LIMIT %s",
                    (account_key, limit)
                )
            else:
                cur.execute(
                    "SELECT data FROM trades ORDER BY ts DESC, id DESC LIMIT %s",
                    (limit,)
                )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]
        except Exception as e:
            logger.warning("DB read_trades failed, falling back to file: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    path = log_path()
    if not os.path.exists(path):
        return []
    lines = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning(f"Trade log read failed: {e}")
        return []
    trades = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
        except json.JSONDecodeError:
            continue
        if account_key and t.get("account_key") != account_key:
            continue
        trades.append(t)
    return list(reversed(trades[-limit:]))


def get_recent_trades(asset: str, count: int = 40) -> list:
    """Return the most recent `count` completed (non-partial) trades for `asset`,
    newest-first. Fetches a larger window and filters by asset so the caller
    always gets up to `count` asset-specific entries regardless of how many
    other assets appear in the log."""
    all_trades = read_trades(limit=max(count * 8, 200))
    return [
        t for t in all_trades
        if t.get("asset") == asset and not t.get("partial")
    ][:count]


def _compute_analytics(trades_iter, days, account_key):
    """Shared analytics computation over an iterable of trade dicts."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    total = wins = losses = 0
    by_asset = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    by_hour = defaultdict(lambda: {"w": 0, "l": 0})
    for t in trades_iter:
        ts = t.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        if account_key and t.get("account_key") != account_key:
            continue
        if t.get("partial"):
            continue
        total += 1
        pnl = float(t.get("round_profit", 0))
        asset = t.get("asset", "?")
        hour = dt.hour
        if pnl >= 0:
            wins += 1
            by_asset[asset]["w"] += 1
            by_hour[hour]["w"] += 1
        else:
            losses += 1
            by_asset[asset]["l"] += 1
            by_hour[hour]["l"] += 1
        by_asset[asset]["pnl"] += pnl
    win_rate = (wins / total * 100) if total else 0
    return {
        "days": days,
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "by_asset": dict(by_asset),
        "by_hour": {str(k): v for k, v in sorted(by_hour.items())},
    }


def analytics(days=7, account_key=None):
    cutoff_str = (datetime.utcnow() - timedelta(days=days)).isoformat()

    conn = _db_conn()
    if conn:
        try:
            cur = conn.cursor()
            if account_key:
                cur.execute(
                    "SELECT data FROM trades WHERE account_key = %s AND ts >= %s",
                    (account_key, cutoff_str)
                )
            else:
                cur.execute(
                    "SELECT data FROM trades WHERE ts >= %s",
                    (cutoff_str,)
                )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            trades_iter = (r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows)
            return _compute_analytics(trades_iter, days, account_key)
        except Exception as e:
            logger.warning("DB analytics failed, falling back to file: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    path = log_path()
    if not os.path.exists(path):
        return {"total": 0, "wins": 0, "losses": 0, "by_asset": {}, "by_hour": {}}
    try:
        def _file_iter():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        return _compute_analytics(_file_iter(), days, account_key)
    except Exception as e:
        logger.warning(f"Analytics failed: {e}")
        return {"total": 0, "wins": 0, "losses": 0, "by_asset": {}, "by_hour": {}}

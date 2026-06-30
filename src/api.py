import sys
import os
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel
import threading
import time
from datetime import datetime
from typing import Any, List, Optional
from strategies.double_martingale import (
    DoubleMartingaleBot,
    STANDARD_BUDGET_TIERS,
    balance_tier_brackets,
)
from trade_log import (
    read_trades,
    read_trades_for_export,
    export_trades_csv,
    analytics as trade_analytics,
)
from trade_pattern_analysis import analyze_trade_patterns
from pair_learning import pair_learning_summary, refresh_pair_learning
from standalone_iq import get_standalone_api, disconnect_standalone
from simulator import simulate_sessions
from bot_state_store import state_file_path
from deploy_fresh import should_wipe_on_deploy
from config import TRADING_MODE, IQ_ACCOUNT_TYPE, BOT_API_KEY, ALLOWED_ORIGINS
import config as app_config
from licensing import LicenseManager
import auto_scheduler as _sched

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Double Martingale Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = DoubleMartingaleBot(
    asset="GBPJPY-OTC",
    min_profit_pct=None,
    asset_candidates=[
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
        "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "EURNZD", "AUDCAD",
        "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "NZDUSD-OTC",
        "EURJPY-OTC", "GBPJPY-OTC", "EURGBP-OTC", "AUDJPY-OTC", "EURNZD-OTC", "AUDCAD-OTC",
        "BTCUSD", "ETHUSD", "APPLE", "XAUUSD", "AMAZON"
    ],
    auto_select_asset=True,
    account_type=IQ_ACCOUNT_TYPE,
    trading_mode=TRADING_MODE,
)

bot_thread = None
_lifecycle_lock = threading.Lock()
_start_time = time.time()

_license_manager = None
_license_valid = True
_license_message = "Licensing disabled"

_UNPROTECTED_PATHS = {
    "/api/health",
    "/api/status",
    "/api/trades",
    "/api/trades/export",
    "/api/analytics",
    "/api/accounts",
    "/api/assets",
    "/api/config",
    "/api/learned-pattern",
    "/api/backtest",
    "/api/trade-history-analytics",
    "/api/ai-comparison",
    "/api/ai-optimization-logs",
    "/api/ai-evaluator-logs",
    "/api/setup-status",
    "/api/license/validate",
    "/api/license/status",
    "/api/schedule",
}

_LICENSE_EXEMPT_PATHS = _UNPROTECTED_PATHS | {"/api/setup"}


def _require_api_key(x_api_key: str = Header(default="")):
    if BOT_API_KEY and x_api_key != BOT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@app.middleware("http")
async def license_gate(request: Request, call_next):
    return await call_next(request)


def _should_auto_start():
    return os.environ.get("AUTO_START", "true").lower() not in ("0", "false", "no")


def _wait_for_trading_thread_stop(timeout=120.0):
    global bot_thread
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _thread_alive():
            return True
        time.sleep(0.25)
    logger.error("Trading thread did not exit within %.0fs", timeout)
    return False


def _start_trading_thread():
    global bot_thread
    with _lifecycle_lock:
        if _thread_alive():
            if not bot.running:
                logger.info("Old trading thread still winding down — waiting up to 30s...")
            else:
                logger.warning("Refusing new trading thread — previous loop still running")
                return False

    if not bot.running:
        deadline = time.time() + 30
        while time.time() < deadline:
            if not _thread_alive():
                break
            time.sleep(0.5)
        if _thread_alive():
            logger.error("Old trading thread did not exit in 30s — cannot start new one")
            return False

    with _lifecycle_lock:
        if _thread_alive():
            logger.warning("Refusing new trading thread — previous loop still running")
            return False
        if bot._connecting or not bot.is_session_ready():
            return False
        bot.last_error = ""
        bot.last_stop_reason = ""
        bot.paused = False
        bot.running = True
        bot_thread = threading.Thread(target=_run_bot_wrapper, name="trading-loop")
        bot_thread.daemon = True
        bot_thread.start()
        logger.info("Trading thread launched.")
        return True


def _thread_alive():
    global bot_thread
    return bot_thread is not None and bot_thread.is_alive()


def _sync_running_flag():
    global bot_thread
    with _lifecycle_lock:
        alive = _thread_alive()
        if bot.running and not alive:
            logger.warning("Trading thread is not alive but running flag was set — resetting.")
            bot.running = False
            bot.session_active = False
            if not bot.last_stop_reason:
                bot.last_stop_reason = "Trading stopped unexpectedly (thread ended)"
            bot.persist_state(bot.last_stop_reason)
        return alive


def _run_bot_wrapper():
    try:
        logger.info("Trading thread started.")
        bot.run()
        logger.info("Trading thread finished normally.")
    except Exception as e:
        logger.exception("Trading thread crashed")
        bot.last_error = str(e)
        bot.last_stop_reason = "Crashed — check server logs"
    finally:
        bot.running = False
        bot.session_active = False
        if not bot.last_stop_reason:
            bot.last_stop_reason = "Trading thread stopped"
        bot.persist_state(bot.last_stop_reason or "thread exited")
        logger.info(f"Trading thread exit: {bot.last_stop_reason}")


@app.on_event("startup")
def startup_event():
    def _boot():
        logger.info("Licensing disabled — bot will trade freely.")

        if should_wipe_on_deploy():
            logger.info("Deploy fresh start — resetting bot state (trade history preserved)")
            bot.full_system_reset(
                clear_trade_log=False,
                reason="Fresh start on new deploy (history kept)",
            )

        from trade_log import migrate_jsonl_to_db
        migrate_jsonl_to_db()

        _sched.start_scheduler(
            bot_ref=bot,
            thread_alive_fn=_thread_alive,
            start_trading_fn=_start_trading_thread,
            license_valid_fn=lambda: _license_valid,
        )

        auto = _should_auto_start()
        logger.info(f"Boot sequence started (AUTO_START={auto}).")
        for attempt in range(1, 6):
            if bot.is_session_ready():
                break
            if bot.connect():
                bot.warm_up_market_feed()
                # Re-sync tier assignment now that real balance is known.
                # This prevents the dashboard from showing Tier 1 when the
                # bot restarts — the actual balance-floor tier is applied here.
                try:
                    bot._sync_assigned_tier_for_trading()
                    bet_info = bot._compute_round_bet()
                    bot.current_bet = bet_info["amount"]
                    bot.last_bet_breakdown = bet_info
                    logger.info(
                        f"Post-connect tier sync: Tier {bot.current_tier_index + 1}, "
                        f"debt=${bot.cumulative_debt:.2f}, "
                        f"balance=${bot.safe_get_balance():.2f}"
                    )
                except Exception as _e:
                    logger.warning(f"Post-connect tier sync failed: {_e}")
                bot.persist_state("API started — connected")
                break
            logger.warning(f"Boot connect attempt {attempt}/5 failed.")
            bot.last_error = "Initial IQ Option connection failed — retrying"
            time.sleep(min(10 * attempt, 30))
        else:
            bot.persist_state("API started — not connected")
            return

        if auto and _license_valid:
            if _start_trading_thread():
                logger.info("Auto-start: trading loop launched after deploy.")
            else:
                logger.warning("Auto-start skipped (session not ready or already running).")

    threading.Thread(target=_boot, daemon=True, name="iq-boot").start()


@app.get("/api/health")
def health():
    return {"status": "ok", "uptime_seconds": int(time.time() - _start_time)}


@app.get("/api/setup-status")
def get_setup_status():
    """Returns whether initial IQ Option configuration is complete."""
    return {
        "needs_setup": not bool(os.environ.get("IQ_EMAIL")),
        "has_license": bool(os.environ.get("LICENSE_KEY")),
        "account_type": os.environ.get("IQ_ACCOUNT_TYPE", "PRACTICE"),
        "is_railway": bool(os.environ.get("RAILWAY_ENVIRONMENT")),
        "version": "1.1.0",
    }


class SetupRequest(BaseModel):
    iq_email: str
    iq_password: str
    iq_account_type: str = "PRACTICE"


@app.post("/api/setup")
def complete_setup(body: SetupRequest):
    """
    Validates IQ Option credentials by attempting a test connection.
    On Railway: returns instructions to set env vars in the Railway dashboard.
    On local/Docker: writes a .env file and returns success.
    """
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        return {
            "mode": "railway",
            "message": "You are running on Railway. Set IQ_EMAIL and IQ_PASSWORD in your Railway project Variables tab, then redeploy.",
        }

    if not body.iq_email or not body.iq_password:
        raise HTTPException(status_code=400, detail="Email and password are required")

    os.environ["IQ_EMAIL"] = body.iq_email
    os.environ["IQ_PASSWORD"] = body.iq_password
    os.environ["IQ_ACCOUNT_TYPE"] = body.iq_account_type

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
        keys_to_set = {
            "IQ_EMAIL": body.iq_email,
            "IQ_PASSWORD": body.iq_password,
            "IQ_ACCOUNT_TYPE": body.iq_account_type,
        }
        if body.license_key:
            keys_to_set["LICENSE_KEY"] = body.license_key.strip()
        updated_keys = set()
        new_lines = []
        for line in lines:
            key = line.split("=")[0].strip()
            if key in keys_to_set:
                new_lines.append(f"{key}={keys_to_set[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)
        for key, val in keys_to_set.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={val}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        logger.warning(f"Could not write .env file: {e}")

    threading.Thread(target=lambda: bot.connect(force_reconnect=True), daemon=True).start()
    return {"ok": True, "message": "Configuration saved. Connecting to IQ Option..."}


class LicenseRequest(BaseModel):
    token: str


@app.post("/api/license/validate")
def validate_license(body: LicenseRequest):
    return {"valid": True, "message": "Licensing disabled"}


@app.get("/api/license/status")
def license_status():
    return {"valid": True, "message": "Licensing disabled", "key_configured": True}


@app.get("/api/status")
def get_status():
    alive = _sync_running_flag()
    state = bot.get_state(thread_alive=alive)
    state["license_valid"] = _license_valid
    state["license_message"] = _license_message
    return state


@app.get("/api/trades")
def get_trades(limit: int = 30):
    account_key = bot._state_account_key()
    return {
        "trades": read_trades(limit=min(limit, 500), account_key=account_key),
        "account_key": account_key,
    }


@app.get("/api/trades/export")
def export_trades(
    format: str = "json",
    limit: int = 5000,
    all_accounts: bool = False,
):
    account_key = None if all_accounts else bot._state_account_key()
    cap = min(max(limit, 1), 50000)
    fmt = (format or "json").lower()
    if fmt == "csv":
        csv_body = export_trades_csv(
            limit=cap,
            account_key=account_key,
            include_all_accounts=all_accounts,
        )
        filename = f"trade_history_{account_key or 'all'}_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        return Response(
            content=csv_body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    trades = read_trades_for_export(
        limit=cap,
        account_key=account_key,
        include_all_accounts=all_accounts,
    )
    return {
        "trades": trades,
        "count": len(trades),
        "account_key": account_key or "all",
        "exported_at": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/api/analytics")
def get_analytics(days: int = 7):
    account_key = bot._state_account_key()
    stats = trade_analytics(days=min(max(days, 1), 90), account_key=account_key)
    stats["account_key"] = account_key
    return stats


@app.get("/api/pattern-analysis")
def get_pattern_analysis(limit: int = 40, days: int = 14, include_iq: bool = True):
    if not bot.is_session_ready():
        raise HTTPException(status_code=503, detail="Connect to IQ Option first")
    account_key = bot._state_account_key()

    def _run():
        return analyze_trade_patterns(
            bot.api,
            account_key=account_key,
            balance_id=_current_balance_id(),
            limit=min(max(limit, 5), 100),
            include_iq_history=include_iq,
            days_back=min(max(days, 1), 90),
        )

    result = {}
    err = None

    def _worker():
        nonlocal result, err
        try:
            result = _run()
        except Exception as e:
            err = str(e)
            logger.exception("pattern-analysis failed")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=45.0)
    if t.is_alive():
        raise HTTPException(status_code=504, detail="Pattern analysis timed out (45s)")
    if err:
        raise HTTPException(status_code=500, detail=err)
    result["account_key"] = account_key
    result["active_asset"] = bot.asset
    return result


def _current_balance_id() -> Optional[int]:
    bid = bot.active_balance_id
    if bid is not None:
        return int(bid)
    if bot.api:
        try:
            from iqoptionapi.stable_api import global_value
            if global_value.balance_id is not None:
                return int(global_value.balance_id)
        except Exception:
            pass
    return None


@app.post("/api/learn-pattern", dependencies=[Depends(_require_api_key)])
def refresh_pair_learning_now():
    refresh_pair_learning(force=True)
    if bot.is_session_ready():
        bot.reload_pair_learning()
    summary = pair_learning_summary()
    return {
        "pair_learning": summary,
        "note": "Learning is automatic after each trade; this only forces a rebuild.",
    }


@app.get("/api/learned-pattern")
def get_learned_pattern():
    return {"pair_learning": pair_learning_summary()}


@app.get("/api/backtest")
def get_backtest(asset: Optional[str] = None, lookback: int = 30):
    if not bot.is_session_ready():
        raise HTTPException(status_code=503, detail="Connect to IQ Option first")
    target = asset or bot.asset
    return bot.backtest_pair_readiness(target, lookback_candles=min(max(lookback, 10), 120))


class SimulateRequest(BaseModel):
    win_rate: float = 0.55
    sessions: int = 1000


@app.post("/api/simulate")
def run_simulation(body: SimulateRequest):
    return simulate_sessions(
        win_rate=body.win_rate,
        sessions=min(body.sessions, 50000),
        budget_tiers=[tuple(t) for t in STANDARD_BUDGET_TIERS],
    )


@app.get("/api/config")
def get_config():
    tiers = bot.budget_tiers if getattr(bot, "budget_tiers", None) else STANDARD_BUDGET_TIERS
    return {
        "asset": bot.asset,
        "budget_tiers": tiers,
        "balance_tier_brackets": balance_tier_brackets(),
        "auto_bracket_enabled": getattr(bot, "auto_bracket_enabled", True),
        "account_type": bot.account_type,
        "avoid_markets": bot.avoid_markets,
        "asset_candidates": bot.asset_candidates,
        "auto_select_asset": bot.auto_select_asset,
        "simulation_mode": bot.simulation_mode,
        "sim_win_rate": bot.sim_win_rate,
        "auto_start": _should_auto_start(),
        "strategy_mode": bot.strategy_mode,
        "ai_shadow_mode": bot.ai_shadow_mode,
        "ai_ensemble_enabled": getattr(bot, "ai_ensemble_enabled", True),
        "blocked_hours": getattr(bot, "blocked_hours", []),
        "hour_boundary_block_minutes": getattr(bot, "hour_boundary_block_minutes", 5),
        "hour_boundary_block_end_minutes": getattr(bot, "hour_boundary_block_end_minutes", 10),
        "market_open_blocks": [
            f"{oh:02d}:{om:02d}:{before}:{after}"
            for oh, om, before, after in getattr(bot, "market_open_blocks", [])
        ],
        "blocked_time_windows": [
            f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
            for sh, sm, eh, em in getattr(bot, "blocked_time_windows", [])
        ],
        "trading_timezone": getattr(bot, "trading_timezone", "Africa/Lagos"),
        "baseline_balance_thresholds": [
            {"min_balance": mb, "tier": ti + 1}
            for mb, ti in getattr(bot, "baseline_balance_thresholds", [])
        ],
        "sequential_steps_mode": getattr(bot, "sequential_steps_mode", True),
        "sequential_amounts": getattr(bot, "sequential_amounts", [
            [5.0, 10.0, 30.0], [10.0, 20.0, 60.0], [20.0, 40.0, 120.0],
            [40.0, 80.0, 240.0], [80.0, 160.0, 480.0], [160.0, 320.0, 960.0],
        ]),
        "rule_gate_enabled": getattr(bot, "rule_gate_enabled", True),
        "rule_gate_min_bot_conf": getattr(bot, "rule_gate_min_bot_conf", 0.35),
        "rule_gate_min_er": getattr(bot, "rule_gate_min_er", 0.30),
        "rule_gate_slope_override_min_bot_conf": getattr(bot, "rule_gate_slope_override_min_bot_conf", 0.70),
        "rule_gate_misaligned_min_bot_conf": getattr(bot, "rule_gate_misaligned_min_bot_conf", 0.42),
        "override_blocked_windows": getattr(bot, "override_blocked_windows", False),
        "ai_active": bot.ai_assessor is not None,
        "ai_error_msg": getattr(bot, "ai_error_msg", ""),
        "ai_key_count": len([
            k.strip() for k in
            getattr(app_config, "GEMINI_API_KEYS", "").split(",")
            if k.strip()
        ]),
    }


@app.get("/api/trade-history-analytics")
def get_trade_history_analytics(limit: int = 5000):
    if not bot.is_session_ready():
        return {"trades": [], "account_key": "unknown"}
    account_key = bot._state_account_key()
    return {
        "trades": read_trades(limit=min(limit, 10000), account_key=account_key),
        "account_key": account_key,
    }


@app.get("/api/ai-optimization-logs")
async def get_ai_opt_logs():
    log_path = os.path.join(os.path.dirname(__file__), "..", "data", "ai_opt_log.json")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="No AI optimization logs available yet.")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai-evaluator-logs")
async def get_ai_evaluator_logs():
    log_path = os.path.join(os.path.dirname(__file__), "..", "data", "ai_evaluator_log.json")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="No AI evaluator logs available yet.")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/trigger-optimization", dependencies=[Depends(_require_api_key)])
async def trigger_ai_opt():
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    if not bot.ai_assessor:
        raise HTTPException(status_code=400, detail="AI Assessor not configured. Provide Gemini API keys.")
    bot.run_ai_agent_pipeline()
    return {"status": "success", "message": "Multi-Agent Optimization Pipeline triggered in the background."}


@app.post("/api/config", dependencies=[Depends(_require_api_key)])
def update_config(config: "ConfigUpdate"):
    config_dict = config.model_dump(exclude_unset=True)
    with _lifecycle_lock:
        bot.update_config(config_dict)
    bot.persist_state("config updated")
    return {"message": "Configuration updated successfully", "config": config_dict}


@app.post("/api/reconnect", dependencies=[Depends(_require_api_key)])
def reconnect():
    def _do():
        bot.connected = False
        ok = bot.connect(force_reconnect=True)
        if ok:
            bot.warm_up_market_feed()
            bot.last_error = ""
            bot.persist_state("reconnected")
            if _should_auto_start() and not (bot.running and _thread_alive()):
                _start_trading_thread()
        else:
            bot.last_error = "Reconnect failed"

    threading.Thread(target=_do, daemon=True).start()
    return {"message": "Reconnect started — refresh status in a few seconds"}


class StartRequest(BaseModel):
    confirm_real: bool = False


@app.post("/api/start", dependencies=[Depends(_require_api_key)])
def start_bot(body: StartRequest = StartRequest()):
    global bot_thread
    if bot.account_type == "REAL" and not body.confirm_real:
        raise HTTPException(
            status_code=400,
            detail="REAL account requires confirm_real=true in request body",
        )
    if bot._connecting:
        raise HTTPException(
            status_code=503,
            detail="Still connecting to IQ Option — wait until dashboard shows Connected.",
        )
    if not bot.is_session_ready():
        raise HTTPException(
            status_code=503,
            detail="Not connected to IQ Option. Click Reconnect, wait for balances to load, then Start.",
        )
    if not _start_trading_thread():
        raise HTTPException(status_code=400, detail="Bot is already running or session not ready")

    return {
        "message": "Bot started on existing IQ session",
        "running": True,
        "connected": True,
        "simulation_mode": bot.simulation_mode,
        "resumed": {
            "debt": bot.cumulative_debt,
            "tier": bot.current_tier_index + 1,
            "step": bot.session_round_count + 1,
            "asset": bot.asset,
        },
    }


@app.post("/api/stop", dependencies=[Depends(_require_api_key)])
def stop_bot():
    with _lifecycle_lock:
        was_alive = _thread_alive()
        if was_alive:
            bot.stop()
        else:
            _sync_running_flag()
    if was_alive:
        _wait_for_trading_thread_stop(timeout=120.0)
    return {
        "message": "Stop signal sent" if was_alive else "Bot was already stopped",
        "running": False,
    }


@app.post("/api/pause", dependencies=[Depends(_require_api_key)])
def pause_bot():
    with _lifecycle_lock:
        if not bot.running or not _thread_alive():
            raise HTTPException(status_code=400, detail="Bot is not running")
        bot.pause()
    return {"message": "Bot paused — stays connected", "paused": True}


@app.post("/api/resume", dependencies=[Depends(_require_api_key)])
def resume_bot():
    with _lifecycle_lock:
        if not bot.running or not _thread_alive():
            raise HTTPException(status_code=400, detail="Bot is not running")
        bot.resume()
    return {"message": "Bot resumed", "paused": False}


class ResetRequest(BaseModel):
    clear_trade_log: bool = True
    confirm: bool = False


@app.post("/api/reset", dependencies=[Depends(_require_api_key)])
def reset_progress(body: ResetRequest = ResetRequest()):
    _sync_running_flag()
    if bot.running or _thread_alive():
        bot.stop()
        if not _wait_for_trading_thread_stop(timeout=90.0):
            raise HTTPException(
                status_code=409,
                detail="Bot is still stopping — wait a few seconds and try reset again",
            )
    if _thread_alive():
        raise HTTPException(
            status_code=400,
            detail="Stop the bot and wait until it is idle before resetting progress",
        )
    if bot.account_type == "REAL" and not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="REAL account reset requires confirm=true in request body",
        )
    result = bot.full_system_reset(
        clear_trade_log=body.clear_trade_log,
        reason="Dashboard reset — Tier 1 Step 1",
    )
    return {
        "message": "Full reset — Tier 1, zero debt, all accounts cleared",
        "account_key": result["account_key"],
        "account_type": bot.account_type,
        "trades_removed": result["trades_removed"],
        "pair_learning_cleared": result.get("pair_learning_cleared", False),
        "penalties_cleared": result.get("penalties_cleared", True),
        "note": (
            "Resets saved ladder on this account, in-memory penalties, and optionally "
            "trade history. Does not clear Railway/hosting deploy logs."
        ),
        "state": {
            "current_tier_index": bot.current_tier_index,
            "cumulative_debt": bot.cumulative_debt,
            "wins": bot.wins,
            "losses": bot.losses,
            "round_number": bot.round_number,
            "current_bet": bot.current_bet,
        },
    }


def _format_accounts(raw_balances, active_type, active_balance_id=None):
    accounts = []
    for b in raw_balances:
        b_type = b.get("type")
        if b_type == 1:
            acc_type = "REAL"
            label = "Real Account"
        elif b_type == 4:
            acc_type = "PRACTICE"
            label = "Practice Account"
        elif b_type == 2:
            acc_type = "TOURNAMENT"
            label = b.get("tournament_name") or f"Tournament #{b.get('tournament_id')}"
        else:
            continue
        balance_id = b.get("id")
        if acc_type != active_type:
            is_active = False
        elif acc_type == "TOURNAMENT":
            is_active = (
                active_balance_id is not None
                and balance_id is not None
                and int(balance_id) == int(active_balance_id)
            )
        else:
            is_active = True
        accounts.append({
            "id": balance_id,
            "type": acc_type,
            "label": label,
            "amount": b.get("amount", 0.0),
            "currency": b.get("currency", "USD"),
            "tournament_id": b.get("tournament_id"),
            "tournament_name": b.get("tournament_name"),
            "is_active": is_active,
        })
    active_id = active_balance_id
    if active_id is None and active_type in ("REAL", "PRACTICE"):
        for acc in accounts:
            if acc["type"] == active_type:
                active_id = acc["id"]
                acc["is_active"] = True
                break
    return {
        "active_account": active_type,
        "active_balance_id": active_id,
        "accounts": accounts,
    }


@app.post("/api/balance/refresh", dependencies=[Depends(_require_api_key)])
def refresh_balance():
    if not bot.is_session_ready():
        raise HTTPException(
            status_code=503,
            detail="Not connected to IQ Option. Click Reconnect first.",
        )
    result = {}
    err = None

    def _worker():
        nonlocal result, err
        try:
            result = bot.force_refresh_balance()
        except Exception as e:
            err = str(e)
            logger.exception("balance refresh failed")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=10.0)
    if t.is_alive():
        raise HTTPException(status_code=504, detail="Balance refresh timed out (10s)")
    if err:
        raise HTTPException(status_code=500, detail=err)
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "Balance refresh failed",
        )
    formatted = _format_accounts(
        result.get("accounts") or bot.get_all_balances(),
        bot.account_type,
        bot.active_balance_id,
    )
    return {"message": "Balance refreshed", "balance": result["balance"], **formatted}


@app.get("/api/accounts")
def get_accounts():
    if not bot.is_session_ready():
        return {"active_account": bot.account_type, "accounts": []}
    try:
        raw_balances = bot.get_all_balances()
        if raw_balances:
            return _format_accounts(raw_balances, bot.account_type, bot.active_balance_id)
        result = []

        def _do():
            nonlocal result
            try:
                raw = bot.api.get_balances()
                result = raw.get("msg", []) if isinstance(raw, dict) else []
            except Exception as e:
                logger.warning(f"get_balances failed: {e}")

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=3.0)
        if result:
            bot._refresh_balance_cache(allow_blocking=False)
            return _format_accounts(result, bot.account_type, bot.active_balance_id)
        return {
            "active_account": bot.account_type,
            "active_balance_id": bot.active_balance_id,
            "accounts": [],
        }
    except Exception as e:
        return {"error": str(e), "active_account": bot.account_type, "accounts": []}


class AccountSwitch(BaseModel):
    account_type: str
    balance_id: Optional[int] = None


class ConfigUpdate(BaseModel):
    account_type: Optional[str] = None
    asset: Optional[str] = None
    avoid_markets: Optional[List[str]] = None
    asset_candidates: Optional[List[str]] = None
    auto_select_asset: Optional[bool] = None
    simulation_mode: Optional[bool] = None
    sim_win_rate: Optional[float] = None
    strategy_mode: Optional[str] = None
    budget_tiers: Optional[List[List[float]]] = None
    auto_bracket_enabled: Optional[bool] = None
    ai_shadow_mode: Optional[bool] = None
    blocked_hours: Optional[List[int]] = None
    hour_boundary_block_minutes: Optional[int] = None
    hour_boundary_block_end_minutes: Optional[int] = None
    market_open_blocks: Optional[List[str]] = None
    blocked_time_windows: Optional[List[str]] = None
    trading_timezone: Optional[str] = None
    sequential_steps_mode: Optional[bool] = None
    sequential_amounts: Optional[Any] = None
    rule_gate_enabled: Optional[bool] = None
    rule_gate_min_bot_conf: Optional[float] = None
    rule_gate_min_er: Optional[float] = None
    rule_gate_slope_override_min_bot_conf: Optional[float] = None
    rule_gate_misaligned_min_bot_conf: Optional[float] = None
    override_blocked_windows: Optional[bool] = None
    gemini_api_keys: Optional[str] = None
    ai_enabled: Optional[bool] = None


@app.post("/api/account", dependencies=[Depends(_require_api_key)])
def set_account(body: AccountSwitch):
    if not bot.connected:
        raise HTTPException(status_code=400, detail="Bot not connected")
    if body.account_type == "TOURNAMENT" and body.balance_id:
        if not bot.switch_trading_account("TOURNAMENT", balance_id=body.balance_id):
            raise HTTPException(status_code=500, detail="Failed to switch to tournament account")
        return {"message": f"Switched to tournament account (ID: {body.balance_id})"}
    elif body.account_type in ["REAL", "PRACTICE"]:
        if not bot.switch_trading_account(body.account_type):
            raise HTTPException(status_code=500, detail=f"Failed to switch to {body.account_type}")
        return {"message": f"Account switched to {body.account_type}"}
    else:
        raise HTTPException(status_code=400, detail=f"Invalid account type: {body.account_type}")


@app.get("/api/assets")
def get_assets():
    if not bot.connected or not bot.api:
        return {"open_assets": [], "active_asset": bot.asset}
    try:
        open_assets = bot.list_tradeable_asset_symbols()
        return {
            "open_assets": open_assets,
            "active_asset": bot.asset,
            "auto_select_asset": bot.auto_select_asset,
        }
    except Exception as e:
        logger.warning("get_assets failed: %s", e)
        return {"error": str(e), "open_assets": [], "active_asset": bot.asset}


@app.get("/api/ai-comparison")
def get_ai_comparison():
    from trade_log import read_trades as _read_all
    all_trades = _read_all(limit=500)
    comparison = []
    bot_wins = bot_losses = ai_would_win = ai_would_lose = ai_agreed = ai_no_data = 0
    bot_pnl = 0.0
    ai_pnl = 0.0

    for t in all_trades:
        if t.get("partial"):
            continue
        profit = float(t.get("round_profit", 0))
        bet = float(t.get("bet", 1))
        ai_approved = t.get("ai_approved")
        bot_won = profit >= 0
        bot_pnl += profit
        if bot_won:
            bot_wins += 1
        else:
            bot_losses += 1
        if ai_approved is None:
            ai_no_data += 1
            ai_pnl += profit
            ai_result = "no_data"
        elif ai_approved:
            ai_agreed += 1
            ai_pnl += profit
            ai_result = "agreed_win" if bot_won else "agreed_loss"
            if bot_won:
                ai_would_win += 1
            else:
                ai_would_lose += 1
        else:
            ai_result = "saved_loss" if not bot_won else "missed_win"
            if not bot_won:
                ai_would_win += 1
            else:
                ai_would_lose += 1
        comparison.append({
            "ts": t.get("ts"),
            "asset": t.get("asset"),
            "tier": t.get("tier"),
            "step": t.get("step"),
            "bet": bet,
            "bot_direction": t.get("bot_direction"),
            "bot_profit": profit,
            "bot_won": bot_won,
            "ai_approved": ai_approved,
            "ai_confidence": t.get("ai_confidence"),
            "ai_reason": t.get("ai_reason"),
            "ai_direction": t.get("ai_direction"),
            "ai_result": ai_result,
        })

    total = bot_wins + bot_losses
    return {
        "total_trades": total,
        "bot": {
            "wins": bot_wins,
            "losses": bot_losses,
            "win_rate": round(bot_wins / total * 100, 1) if total else 0,
            "pnl": round(bot_pnl, 2),
        },
        "ai": {
            "correct_calls": ai_would_win,
            "wrong_calls": ai_would_lose,
            "no_data": ai_no_data,
            "agreed_with_bot": ai_agreed,
            "pnl": round(ai_pnl, 2),
        },
        "trades": comparison,
    }


# ── Admin: License Token Management ────────────────────────────────────────

def _admin_db_conn():
    import psycopg2
    return psycopg2.connect(os.environ["DATABASE_URL"])


@app.get("/api/admin/tokens", dependencies=[Depends(_require_api_key)])
def admin_list_tokens():
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT token_key, status, duration_days, activated_at, expires_at, "
            "hwid, is_trial, created_at FROM tokens ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return {"tokens": [
            {
                "token_key": r[0], "status": r[1], "duration_days": r[2],
                "activated_at": r[3].isoformat() if r[3] else None,
                "expires_at": r[4].isoformat() if r[4] else None,
                "hwid": r[5], "is_trial": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CreateTokenRequest(BaseModel):
    duration_days: int = 30
    token_key: str = ""


@app.post("/api/admin/tokens", dependencies=[Depends(_require_api_key)])
def admin_create_token(body: CreateTokenRequest = CreateTokenRequest()):
    import uuid as _uuid
    days = max(1, min(int(body.duration_days), 3650))
    key = (body.token_key or "").strip().upper()
    if not key:
        key = f"BESTA-{_uuid.uuid4().hex[:4].upper()}-{_uuid.uuid4().hex[:4].upper()}"
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tokens (token_key, status, duration_days) VALUES (%s, 'unclaimed', %s) "
            "ON CONFLICT (token_key) DO NOTHING RETURNING token_key",
            (key, days)
        )
        result = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not result:
            raise HTTPException(status_code=409, detail="Token key already exists")
        return {"token_key": key, "duration_days": days, "status": "unclaimed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/tokens/{token_key}/revoke", dependencies=[Depends(_require_api_key)])
def admin_revoke_token(token_key: str):
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE tokens SET status='revoked' WHERE token_key=%s RETURNING token_key",
            (token_key,)
        )
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
        return {"token_key": token_key, "status": "revoked"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/tokens/{token_key}", dependencies=[Depends(_require_api_key)])
def admin_delete_token(token_key: str):
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM tokens WHERE token_key=%s RETURNING token_key", (token_key,))
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found")
        return {"deleted": token_key}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/tokens/{token_key}/unrevoke", dependencies=[Depends(_require_api_key)])
def admin_unrevoke_token(token_key: str):
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE tokens SET status='active' WHERE token_key=%s AND status='revoked' RETURNING token_key",
            (token_key,)
        )
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found or not revoked")
        return {"token_key": token_key, "status": "active"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/stats", dependencies=[Depends(_require_api_key)])
def admin_stats():
    try:
        conn = _admin_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades")
        trade_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tokens")
        token_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM tokens WHERE status='active'")
        active_tokens = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM bot_states")
        state_count = cur.fetchone()[0]
        cur.execute("SELECT updated_at FROM kv_store WHERE key='pair_learning'")
        pl_row = cur.fetchone()
        cur.close(); conn.close()
        return {
            "trade_count": trade_count,
            "token_count": token_count,
            "active_tokens": active_tokens,
            "bot_state_count": state_count,
            "pair_learning_updated_at": pl_row[0].isoformat() if pl_row else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Session Heatmap ─────────────────────────────────────────────────────────

@app.get("/api/session-heatmap")
def get_session_heatmap():
    """Return per-hour loss-rate heatmap in Africa/Lagos time (UTC+1) plus blocked windows."""
    from datetime import datetime, timedelta
    from config import UTC_BAN_WINDOWS, UTC_SOFT_BAN_WINDOWS, UTC_SOFT_BAN_ASSETS

    LAGOS_OFFSET = timedelta(hours=1)
    now_utc = datetime.utcnow()
    now_lagos = now_utc + LAGOS_OFFSET

    account_key = bot._state_account_key()
    stats = trade_analytics(days=30, account_key=account_key)
    by_hour_utc = stats.get("by_hour", {})

    def mins_in_window(m, start, end):
        if start <= end:
            return start <= m < end
        return m >= start or m < end

    current_lagos_min = now_lagos.hour * 60 + now_lagos.minute

    blocked_windows = []

    def _fmt_labels(lsh, lsm, leh, lem):
        sh12 = lsh % 12 or 12
        eh12 = leh % 12 or 12
        sa = "AM" if lsh < 12 else "PM"
        ea = "AM" if leh < 12 else "PM"
        return (
            f"{sh12}:{lsm:02d} {sa}–{eh12}:{lem:02d} {ea}",
            f"{lsh:02d}:{lsm:02d}–{leh:02d}:{lem:02d}",
        )

    for sh, sm, eh, em in getattr(bot, "blocked_time_windows", []):
        s = sh * 60 + sm
        e = eh * 60 + em
        label_12, label_24 = _fmt_labels(sh, sm, eh, em)
        active = mins_in_window(current_lagos_min, s, e)
        blocked_windows.append({
            "label": label_12,
            "label_24": label_24,
            "start_min": s,
            "end_min": e,
            "timezone": "Lagos",
            "type": "static",
            "description": "Static time block",
            "active": active,
        })

    for w in UTC_BAN_WINDOWS:
        parts = w.strip().split("-")
        if len(parts) != 2:
            continue
        try:
            sh, sm = map(int, parts[0].split(":"))
            eh, em = map(int, parts[1].split(":"))
        except ValueError:
            continue
        s_lag = (sh * 60 + sm + 60) % (24 * 60)
        e_lag = (eh * 60 + em + 60) % (24 * 60)
        lsh, lsm = divmod(s_lag, 60)
        leh, lem = divmod(e_lag, 60)
        label_12, label_24 = _fmt_labels(lsh, lsm, leh, lem)
        active = mins_in_window(current_lagos_min, s_lag, e_lag)
        blocked_windows.append({
            "label": label_12,
            "label_24": label_24,
            "label_utc": f"{sh:02d}:{sm:02d}–{eh:02d}:{em:02d} UTC",
            "start_min": s_lag,
            "end_min": e_lag,
            "timezone": "UTC ban → Lagos",
            "type": "utc_ban",
            "description": "UTC hard ban (all assets)",
            "active": active,
        })

    for h in getattr(bot, "blocked_hours", []):
        next_h = (h + 1) % 24
        s = h * 60
        e = next_h * 60
        label_12, label_24 = _fmt_labels(h, 0, next_h, 0)
        active = mins_in_window(current_lagos_min, s, e)
        blocked_windows.append({
            "label": label_12,
            "label_24": label_24,
            "start_min": s,
            "end_min": e,
            "timezone": "Lagos",
            "type": "blocked_hour",
            "description": "Blocked hour",
            "active": active,
        })

    _OPEN_NAMES = {
        (2, 0): "Tokyo / Sydney open",
        (9, 0): "London open",
        (14, 30): "New York open",
        (22, 0): "NY close / overnight",
    }
    for oh, om, before, after in getattr(bot, "market_open_blocks", []):
        s_lag = (oh * 60 + om - before) % (24 * 60)
        e_lag = (oh * 60 + om + after) % (24 * 60)
        lsh, lsm = divmod(s_lag, 60)
        leh, lem = divmod(e_lag, 60)
        label_12, label_24 = _fmt_labels(lsh, lsm, leh, lem)
        open_name = _OPEN_NAMES.get((oh, om), f"{oh:02d}:{om:02d} market open")
        active = mins_in_window(current_lagos_min, s_lag, e_lag)
        blocked_windows.append({
            "label": label_12,
            "label_24": label_24,
            "open_time": f"{oh:02d}:{om:02d} Lagos",
            "start_min": s_lag,
            "end_min": e_lag,
            "timezone": "Lagos",
            "type": "market_open",
            "description": f"{open_name} buffer",
            "active": active,
        })

    soft_ban_windows = []
    for w in UTC_SOFT_BAN_WINDOWS:
        parts = w.strip().split("-")
        if len(parts) != 2:
            continue
        try:
            sh, sm = map(int, parts[0].split(":"))
            eh, em = map(int, parts[1].split(":"))
        except ValueError:
            continue
        s_lag = (sh * 60 + sm + 60) % (24 * 60)
        e_lag = (eh * 60 + em + 60) % (24 * 60)
        lsh, lsm = divmod(s_lag, 60)
        leh, lem = divmod(e_lag, 60)
        label_12, label_24 = _fmt_labels(lsh, lsm, leh, lem)
        active = mins_in_window(current_lagos_min, s_lag, e_lag)
        soft_ban_windows.append({
            "label": label_12,
            "label_24": label_24,
            "label_utc": f"{sh:02d}:{sm:02d}–{eh:02d}:{em:02d} UTC",
            "assets": list(UTC_SOFT_BAN_ASSETS),
            "active": active,
        })

    is_currently_blocked = any(w["active"] for w in blocked_windows)

    hours = []
    for lagos_hour in range(24):
        utc_hour = (lagos_hour - 1) % 24
        data = by_hour_utc.get(str(utc_hour), {"w": 0, "l": 0})
        total = data["w"] + data["l"]
        loss_rate = round(data["l"] / total * 100, 1) if total >= 3 else None

        hour_mid = lagos_hour * 60 + 30
        is_blocked = any(
            mins_in_window(lagos_hour * 60, w["start_min"], w["end_min"]) or
            mins_in_window(hour_mid, w["start_min"], w["end_min"])
            for w in blocked_windows
        )

        h12 = lagos_hour % 12 or 12
        ampm = "AM" if lagos_hour < 12 else "PM"

        hours.append({
            "hour": lagos_hour,
            "label": f"{h12} {ampm}",
            "wins": data["w"],
            "losses": data["l"],
            "trades": total,
            "loss_rate_pct": loss_rate,
            "is_blocked": is_blocked,
            "is_current": lagos_hour == now_lagos.hour,
        })

    hour12 = now_lagos.hour % 12 or 12
    ampm = "AM" if now_lagos.hour < 12 else "PM"
    current_time_12h = f"{hour12}:{now_lagos.minute:02d} {ampm}"

    return {
        "current_time_lagos": current_time_12h,
        "current_hour_lagos": now_lagos.hour,
        "hours": hours,
        "blocked_windows": blocked_windows,
        "soft_ban_windows": soft_ban_windows,
        "is_currently_blocked": is_currently_blocked,
        "total_trades_analyzed": stats.get("total", 0),
        "days_analyzed": 30,
    }


# ── Gate Rejection Log ───────────────────────────────────────────────────────

@app.get("/api/gate-log")
def get_gate_log():
    """Return the rolling list of gate rejections recorded this session."""
    entries = list(getattr(bot, "_gate_rejection_log", []))
    return {"entries": entries, "total": len(entries)}


# ── Auto-Start Scheduler ─────────────────────────────────────────────────────

@app.get("/api/schedule")
def get_schedule():
    return _sched.get_schedule_status()


class ScheduleConfig(BaseModel):
    enabled: bool = False
    windows: list = []


@app.post("/api/schedule", dependencies=[Depends(_require_api_key)])
def save_schedule(body: ScheduleConfig):
    cfg = _sched.save_schedule_config(body.enabled, body.windows)
    return {"message": "Schedule saved", "config": cfg}


# ── Daily P&L ────────────────────────────────────────────────────────────────

@app.get("/api/daily-pnl")
def get_daily_pnl():
    """Return per-day profit/loss for the last 30 days (Lagos time = UTC+1)."""
    from datetime import datetime, timedelta
    import json as _json

    LAGOS_OFFSET = timedelta(hours=1)
    DAYS = 30
    cutoff_utc = datetime.utcnow() - timedelta(days=DAYS)
    cutoff_str = cutoff_utc.isoformat()

    account_key = bot._state_account_key()

    conn = None
    trades_raw = []
    try:
        from trade_log import _db_conn
        conn = _db_conn()
        if conn:
            cur = conn.cursor()
            if account_key:
                cur.execute(
                    "SELECT data FROM trades WHERE account_key = %s AND ts >= %s ORDER BY ts ASC",
                    (account_key, cutoff_str)
                )
            else:
                cur.execute(
                    "SELECT data FROM trades WHERE ts >= %s ORDER BY ts ASC",
                    (cutoff_str,)
                )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            trades_raw = [r[0] if isinstance(r[0], dict) else _json.loads(r[0]) for r in rows]
    except Exception as e:
        logger.warning("daily-pnl DB query failed: %s", e)
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    from collections import defaultdict
    day_data: dict = defaultdict(lambda: {"pnl": 0.0, "wins": 0, "losses": 0})

    for t in trades_raw:
        if t.get("partial"):
            continue
        ts = t.get("ts", "")
        try:
            dt_utc = datetime.fromisoformat(ts.replace("Z", ""))
        except ValueError:
            continue
        dt_lagos = dt_utc + LAGOS_OFFSET
        day_key = dt_lagos.date().isoformat()
        pnl = float(t.get("round_profit", 0) or 0)
        day_data[day_key]["pnl"] += pnl
        if pnl >= 0:
            day_data[day_key]["wins"] += 1
        else:
            day_data[day_key]["losses"] += 1

    today_lagos = (datetime.utcnow() + LAGOS_OFFSET).date()
    days_list = []
    cumulative = 0.0
    for i in range(DAYS - 1, -1, -1):
        d = (today_lagos - timedelta(days=i)).isoformat()
        dd = day_data.get(d, {"pnl": 0.0, "wins": 0, "losses": 0})
        daily_pnl = round(dd["pnl"], 2)
        cumulative = round(cumulative + daily_pnl, 2)
        days_list.append({
            "date": d,
            "daily_pnl": daily_pnl,
            "wins": dd["wins"],
            "losses": dd["losses"],
            "total_trades": dd["wins"] + dd["losses"],
            "cumulative_pnl": cumulative,
        })

    total_pnl = round(sum(d["daily_pnl"] for d in days_list), 2)
    profit_days = sum(1 for d in days_list if d["daily_pnl"] > 0)
    loss_days = sum(1 for d in days_list if d["daily_pnl"] < 0)
    best_day = max(days_list, key=lambda x: x["daily_pnl"], default=None)
    worst_day = min(days_list, key=lambda x: x["daily_pnl"], default=None)

    return {
        "days": days_list,
        "total_pnl": total_pnl,
        "profit_days": profit_days,
        "loss_days": loss_days,
        "best_day": best_day,
        "worst_day": worst_day,
        "total_trades": sum(d["total_trades"] for d in days_list),
    }


# ── Static / Frontend ───────────────────────────────────────────────────────

dist_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
assets_path = os.path.join(dist_path, "assets")
if os.path.exists(dist_path):
    if os.path.exists(assets_path):
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

    # ── Per-Asset Risk Breakdown ─────────────────────────────────────────────

    @app.get("/api/asset-breakdown")
    def get_asset_breakdown():
        """Per-asset win/loss breakdown from the last 30 days of trade history."""
        account_key = bot._state_account_key()
        stats = trade_analytics(days=30, account_key=account_key)
        by_asset_raw = stats.get("by_asset", {})

        assets = []
        for asset, d in by_asset_raw.items():
            w = d.get("w", 0)
            l = d.get("l", 0)
            total = w + l
            if total == 0:
                continue
            pnl = d.get("pnl", 0.0)
            assets.append({
                "asset": asset,
                "total": total,
                "wins": w,
                "losses": l,
                "win_rate_pct": round(w / total * 100, 1),
                "loss_rate_pct": round(l / total * 100, 1),
                "total_pnl": round(float(pnl), 2),
                "avg_pnl": round(float(pnl) / total, 2),
            })

        assets.sort(key=lambda x: x["loss_rate_pct"], reverse=True)

        return {
            "days_analyzed": stats.get("days", 30),
            "total_trades": stats.get("total", 0),
            "assets": assets,
        }

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if os.path.exists(os.path.join(dist_path, full_path)) and full_path != "":
            return FileResponse(os.path.join(dist_path, full_path))
        return FileResponse(os.path.join(dist_path, "index.html"))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

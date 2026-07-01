"""
Candle Follow directional turbo bot with martingale ladder recovery.

Each minute: read last closed 1m candle color → place CALL or PUT turbo option.
Martingale ladder advances on loss, resets on win; balance-based tier brackets.
"""

import time
import json
import logging
import math
import threading
from collections import deque
import datetime
import sys
import os
import concurrent.futures
# Ensure src directory is in path when imported or run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from connection import connect_to_iqoption
import config as app_config
from bot_state_store import (
    account_state_key,
    load_state,
    save_state,
    snapshot_from_bot,
)
from notifier import notify as send_alert
from trade_log import append_trade, copy_bot_evaluation, copy_entry_snapshot, get_recent_trades
from market_metrics import entry_snapshot_from_candles
from pair_health import pattern_quality_factor, adjusted_score as ph_adjusted_score
from pair_learning import (
    clear_pair_learning_store,
    effective_gates_for_asset,
    load_pair_learning,
    pair_learning_summary,
    refresh_pair_learning,
    schedule_refresh,
)
import iqoptionapi.constants as OP_code
import random
from risk_governor import compute_risk_limits

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DoubleMartingale")

# Assets confirmed to NOT support IQ Option digital options endpoint —
# orders are always rejected with {'message': 'rejected'}.
DIGITAL_UNSUPPORTED_ASSETS = {
    "NZDUSD-OTC",
    "NZDUSD",
}

# Recovery-ladder tiers — cumulative-recovery chain at 85% payout.
# Each step of Tn recovers ALL prior tiers combined (T0..T(n-1)) in 3/2/1 wins at 85%.
# Formula: target=L_cum+P; step1=target/(3×0.85); step2=(target+step1)/(2×0.85); step3=(target+step1+step2)/0.85
# Tier is determined solely by current balance. On exhaustion: clear debt, reset to step 1.
STANDARD_BUDGET_TIERS = [
    [1, 3, 6, 16, 39, 98, 244, 610, 1526],
]

# Balance-proportional tier table (highest matching min_balance wins).
# 2.5× multiplier, 9 steps per tier.
# Ranges: $1–$1,999 | $2k–$9,999 | $10k–$19,999 | $20k–$49,999 | $50k–$149,999 | $150k+
BALANCE_TIER_TABLE = [
    (1,      [1, 3, 6, 16, 39, 98, 244, 610, 1526],                   [1, 3, 6, 16, 39, 98, 244, 610, 1526]),
    (2000,   [3, 8, 19, 47, 117, 293, 732, 1831, 4578],                [3, 8, 19, 47, 117, 293, 732, 1831, 4578]),
    (10000,  [9, 23, 56, 141, 352, 879, 2197, 5493, 13733],            [9, 23, 56, 141, 352, 879, 2197, 5493, 13733]),
    (20000,  [20, 50, 125, 313, 781, 1953, 4883, 12207, 30518],        [20, 50, 125, 313, 781, 1953, 4883, 12207, 30518]),
    (50000,  [45, 113, 281, 703, 1758, 4395, 10986, 27466, 68665],     [45, 113, 281, 703, 1758, 4395, 10986, 27466, 68665]),
    (150000, [100, 250, 625, 1563, 3906, 9766, 24414, 61035, 152588],  [100, 250, 625, 1563, 3906, 9766, 24414, 61035, 152588]),
]

# Step-4 asset rotation has been removed. The bot plays steps in order
# regardless of which step it is on; no pair switch is triggered at step 4.


def balance_tier_brackets():
    """Balance ranges and ladder amounts for UI / API (highest matching row wins)."""
    rows = []
    for i, (min_bal, t0, _t1) in enumerate(BALANCE_TIER_TABLE):
        next_min = BALANCE_TIER_TABLE[i + 1][0] if i + 1 < len(BALANCE_TIER_TABLE) else None
        if next_min is not None:
            range_label = f"${min_bal:,}–${next_min - 1:,}"
        else:
            range_label = f"${min_bal:,}+"
        rows.append(
            {
                "min_balance": min_bal,
                "max_balance": (next_min - 1) if next_min is not None else None,
                "range_label": range_label,
                "base_amount": t0[0],
                "amounts": list(t0),
            }
        )
    return rows

EVALUATION_WINDOW_MINUTES = 15
TIER_EXHAUSTION_COOLDOWN_MINUTES = 5
TIER_SECOND_EXHAUSTION_COOLDOWN_MINUTES = 5
TIER_FAILURES_BEFORE_ESCALATE = 1
TIER_1_FAILURES_BEFORE_ESCALATE = TIER_FAILURES_BEFORE_ESCALATE
TIER_HIGHER_FAILURES_BEFORE_ESCALATE = TIER_FAILURES_BEFORE_ESCALATE
LADDER_MAX_STEP_INDEX = 6       # 0-based; 7 steps, no tiers
RECOVERY_TIER_CEILING = 0      # No tier escalation — single flat sequence only
# No reserve tiers; the bot never escalates beyond T0.
ROUND_RESERVE_TIERS = set()   # empty — no reserve tiers

# Sentinel value used internally to distinguish a genuine $0 profit from a timeout
_TIMEOUT_SENTINEL = float("-inf")

# Straddle suitability thresholds (used for pair ranking AND pre-trade gates)
MIN_STRADDLE_EFFICIENCY_RATIO = 0.45
MIN_STRADDLE_DIRECTIONAL_SLOPE = 35.0
PAIR_UNTRADEABLE_SKIP_STREAK = 4
PAIR_PENALTY_MIN_MINUTES = 5
PAIR_PENALTY_MAX_MINUTES = 5
ORDER_REJECTION_PENALTY_MINUTES = 5
PAIR_UNTRADEABLE_COOLDOWN_MINUTES = 5
TIER_EXHAUSTED_PENALTY_MINUTES = TIER_EXHAUSTION_COOLDOWN_MINUTES
# Max strike ladder steps from ATM (0=ATM); step 4+ is too wide to cover with a straddle.
MAX_STRIKE_LADDER_STEPS_FROM_ATM = 3


def _clamp_penalty_minutes(minutes):
    return max(
        PAIR_PENALTY_MIN_MINUTES,
        min(PAIR_PENALTY_MAX_MINUTES, int(minutes)),
    )


class DoubleMartingaleBot:
    """
    Public Interface:
        __init__(asset, min_profit_pct, account_type)
        run() -> None  # Main loop
        stop() -> None  # Graceful shutdown
    """

    def __init__(
        self,
        asset="GBPJPY-OTC",
        min_profit_pct=None,
        max_profit_pct=None,
        account_type="PRACTICE",  # "REAL", "PRACTICE", or "TOURNAMENT"
        avoid_markets=None,
        asset_candidates=None,
        auto_select_asset=True,
        min_candle_body_pct=0.00006,
        min_session_range_pct=0.00025,
        asset_analysis_candles=20,
        min_asset_score=20.0,
        entry_window_start=None,
        entry_window_end=None,
        entry_hard_abort_sec=None,
        purchase_deadline_sec=None,
        min_seconds_to_expiry=None,
        max_seconds_to_expiry=None,
        doji_streak_max=4,
        tight_range_candles=10,
        tight_range_pct=0.00015,
        simulation_mode=False,
        sim_win_rate=0.55,
        news_blackout_utc_hours=None,
        stale_trade_alert_minutes=30,
        order_place_retries=1,
        strategy_mode="directional_trend",  # "directional_trend" or "straddle"
        trading_mode="digital", # "digital" or "turbo"
    ):
        self.strategy_mode = strategy_mode
        self.trading_mode = trading_mode
        self.rule_gate_enabled = getattr(app_config, "RULE_GATE_ENABLED", True)
        self.rule_gate_min_bot_conf = getattr(app_config, "RULE_GATE_MIN_BOT_CONF", 0.35)
        self.rule_gate_min_er = getattr(app_config, "RULE_GATE_MIN_ER", 0.30)
        self.rule_gate_slope_override_min_bot_conf = getattr(app_config, "RULE_GATE_SLOPE_OVERRIDE_MIN_BOT_CONF", 0.70)
        self.rule_gate_misaligned_min_bot_conf = getattr(app_config, "RULE_GATE_MISALIGNED_MIN_BOT_CONF", 0.42)
        self.chop_filter_enabled = getattr(app_config, "CHOP_FILTER_ENABLED", True)
        self.chop_ci_period = getattr(app_config, "CHOP_CI_PERIOD", 14)
        self.chop_ci_threshold = getattr(app_config, "CHOP_CI_THRESHOLD", 61.8)
        self.chop_min_er = getattr(app_config, "CHOP_MIN_EFFICIENCY_RATIO", 0.15)
        
        # Load Config History
        self.config_history_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "config_history.json")
        self.config_history = self._load_config_history()

        self.asset = asset
        self.asset_id = OP_code.ACTIVES.get(asset, 0)
        self.min_profit_pct = (
            float(min_profit_pct)
            if min_profit_pct is not None
            else float(app_config.MIN_PROFIT_PCT)
        )
        self.max_profit_pct = (
            float(max_profit_pct)
            if max_profit_pct is not None
            else float(app_config.MAX_PROFIT_PCT)
        )
        self.auto_evaluate = True
        self.auto_bracket_enabled = True
        self.budget_tiers = self._enforce_standard_budget_tiers()
        self.account_type = account_type
        self.active_balance_id = None
        _config_avoid = list(getattr(app_config, "AVOID_MARKETS", []))
        _passed_avoid = list(avoid_markets) if avoid_markets else []
        self.avoid_markets = list(dict.fromkeys(_config_avoid + _passed_avoid))
        self.asset_candidates = asset_candidates or [
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
            "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "EURNZD", "AUDCAD",
            "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCAD-OTC", "NZDUSD-OTC",
            "EURJPY-OTC", "GBPJPY-OTC", "EURGBP-OTC", "AUDJPY-OTC", "EURNZD-OTC", "AUDCAD-OTC",
            "ETHUSD", "ETHUSD-OTC", "APPLE", "APPLE-OTC", "AMAZON", "AMAZON-OTC",
        ]
        self.preferred_utc_hours = [(0, 5), (7, 10), (13, 18)]
        self.auto_select_manually_disabled = False
        self.auto_select_asset = bool(auto_select_asset)
        self.min_candle_body_pct = min_candle_body_pct
        self.min_session_range_pct = min_session_range_pct
        self.asset_analysis_candles = asset_analysis_candles
        self.min_asset_score = min_asset_score
        self.asset_scores = {}
        self.last_asset_selection_note = "Starting up"
        self.blocked_hours = []
        self.trading_timezone = getattr(app_config, "TRADING_TIMEZONE", "Africa/Lagos")
        self.hour_boundary_block_minutes = int(
            getattr(app_config, "HOUR_BOUNDARY_BLOCK_MINUTES", 5)
        )
        self.hour_boundary_block_end_minutes = int(
            getattr(app_config, "HOUR_BOUNDARY_BLOCK_END_MINUTES", 10)
        )
        self.market_open_blocks = self._parse_market_open_blocks(
            getattr(app_config, "MARKET_OPEN_BLOCKS", [])
        )
        self.blocked_time_windows = self._parse_blocked_time_windows(
            getattr(app_config, "BLOCKED_TIME_WINDOWS", [])
        )
        self.override_blocked_windows = False
        self.baseline_balance_thresholds = list(
            getattr(app_config, "BASELINE_BALANCE_THRESHOLDS", [(0, 0)])
        )
        self.tier_ceiling_thresholds = list(
            getattr(app_config, "TIER_CEILING_THRESHOLDS", [(0, 0)])
        )
        self.profit_lock_enabled = getattr(app_config, "PROFIT_LOCK_ENABLED", True)
        self.profit_lock_ratio = float(
            getattr(app_config, "PROFIT_LOCK_RATIO", 0.40)
        )
        self.profit_lock_min_reserve = float(
            getattr(app_config, "PROFIT_LOCK_MIN_RESERVE", 80.0)
        )
        self.drawdown_breaker_enabled = getattr(
            app_config, "DRAWDOWN_BREAKER_ENABLED", True
        )
        self.drawdown_pct = float(getattr(app_config, "DRAWDOWN_PCT", 0.20))
        self.drawdown_fast_usd = float(
            getattr(app_config, "DRAWDOWN_FAST_USD", 80.0)
        )
        self.drawdown_fast_minutes = float(
            getattr(app_config, "DRAWDOWN_FAST_MINUTES", 30.0)
        )
        self.drawdown_recovery_pct = float(
            getattr(app_config, "DRAWDOWN_RECOVERY_PCT", 0.10)
        )
        self.drawdown_risk_mode_minutes = float(
            getattr(app_config, "DRAWDOWN_RISK_MODE_MINUTES", 45.0)
        )
        self.drawdown_risk_pause_sec = float(
            getattr(app_config, "DRAWDOWN_RISK_PAUSE_SEC", 120.0)
        )
        self.step_score_escalation_enabled = getattr(
            app_config, "STEP_SCORE_ESCALATION_ENABLED", False
        )
        self.step_score_min_improvement = float(
            getattr(app_config, "STEP_SCORE_MIN_IMPROVEMENT", 0.05)
        )
        self.step_score_max_skips = int(
            getattr(app_config, "STEP_SCORE_MAX_SKIPS_BEFORE_PAIR_SWITCH", 2)
        )
        self.last_stop_reason = ""
        self.last_error = ""
        self.status_note = ""
        self.ai_error_msg = ""
        self._ai_fail_count = 0
        self.last_bet_breakdown = {}
        self.entry_window_start = (
            entry_window_start
            if entry_window_start is not None
            else app_config.ENTRY_WINDOW_START
        )
        self.entry_window_end = (
            entry_window_end
            if entry_window_end is not None
            else app_config.ENTRY_WINDOW_END
        )
        self.entry_hard_abort_sec = (
            entry_hard_abort_sec
            if entry_hard_abort_sec is not None
            else app_config.ENTRY_HARD_ABORT_SEC
        )
        self.purchase_deadline_sec = (
            purchase_deadline_sec
            if purchase_deadline_sec is not None
            else app_config.PURCHASE_DEADLINE_SEC
        )
        self.min_seconds_to_expiry = (
            min_seconds_to_expiry
            if min_seconds_to_expiry is not None
            else app_config.MIN_SECONDS_TO_EXPIRY
        )
        self.max_seconds_to_expiry = (
            max_seconds_to_expiry
            if max_seconds_to_expiry is not None
            else app_config.MAX_SECONDS_TO_EXPIRY
        )
        self.momentum_min_ratio = float(
            getattr(app_config, "MOMENTUM_MIN_RATIO", 0.50)
        )
        self.doji_streak_max = doji_streak_max
        self.tight_range_candles = tight_range_candles
        self.tight_range_pct = tight_range_pct
        self.simulation_mode = simulation_mode
        self.sim_win_rate = sim_win_rate
        self.sequential_steps_mode = getattr(app_config, "SEQUENTIAL_STEPS_MODE", True)
        self.sequential_amounts = list(getattr(app_config, "SEQUENTIAL_AMOUNTS", [
            [5.0, 10.0, 30.0], [10.0, 20.0, 60.0], [20.0, 40.0, 120.0],
            [40.0, 80.0, 240.0], [80.0, 160.0, 480.0], [160.0, 320.0, 960.0],
        ]))
        self.news_blackout_utc_hours = news_blackout_utc_hours or []
        self.stale_trade_alert_minutes = stale_trade_alert_minutes
        self.order_place_retries = max(1, int(order_place_retries))


        # State
        self.api = None
        self._trading_loop_lock = threading.Lock()
        self._round_placement_lock = threading.Lock()
        self._round_in_flight = False
        self.connected = False
        self.running = False
        self.paused = False
        self.tier_escalations_today = 0
        self.tier_escalations_date = None
        self.last_trade_time = None
        self.current_bet = self.budget_tiers[0][0]
        self.round_number = 0
        self.total_profit = 0.0
        self.wins = 0
        self.losses = 0
        self.daily_start_time = None
        self.daily_start_balance = 0.0
        self.daily_profit = 0.0

        # Session State
        self.session_profit = 0.0
        self.session_total_profit = 0.0
        self.session_round_count = 0
        self.current_tier_index = 0
        self.session_max_rounds = len(self.budget_tiers[0]) if self.budget_tiers else 0
        self.session_active = False
        self._trading_bootstrapped = False
        self.cumulative_debt = 0.0
        self.assigned_tier_index = 0
        self.tier_failure_streak = 0
        self.tier_recovery_wins = 0
        self.window_profit = 0.0
        self.evaluation_window_start = None
        self.tier_exhaustion_cooldown_until = None
        self.last_tier_exhaustion_at = None
        self.window_had_tier_exhaustion = False
        self.reserve_wins_needed = 0  # wins left for current reserve tier (T1/T3/T5) to recover
        self.mopup_initial_debt = 0.0  # prior-round debt set at start of T2/T4 mop-up phase
        self._inflight_trade_ids = []
        self.last_trend_direction = None
        self._last_direction_flip_kind = None

        # Advanced Strategy State
        self.asset_penalty_box = {}
        self._asset_flip_blocked: dict = {}  # asset → unblock unix timestamp (slope-flip rule)
        self._gate_rejection_log: deque = deque(maxlen=50)
        self._pair_filter_skip_streak = {}
        # Hot-pair loyalty: track consecutive wins on the same pair this session.
        # After HOT_PAIR_MIN_WINS wins the bot stays on that pair as long as it
        # is still tradeable — it only switches when the pair loses or goes flat.
        self._hot_pair: str = ""
        self._hot_pair_consecutive_wins: int = 0
        self._pending_recovery_rescan: bool = False  # set after T2/T4 debt-chip win to force fresh scan at next S1
        # Pair quality degradation: rolling history of winning ERs per pair.
        # Used to skip a pair whose current ER has dropped well below its
        # recent winning average (the market is no longer as directional).
        self._pair_win_er_history: dict = {}   # asset -> [er, er, ...]
        self._pair_recent_results: dict = {}  # asset -> [True/False, ...] recent win/loss window
        # Sliding-window full-ladder-loss tracker: per-pair list of UTC datetimes
        # when that pair exhausted all ladder steps. 2+ exhaustions in any 15-minute
        # window → 5-minute penalty (applied only between ladders, never mid-trade).
        self._pair_ladder_loss_times: dict = {}  # asset -> [datetime, ...]
        self._last_gate_er: float = 0.0        # ER captured at the moment of entry
        self.last_pair_quality = {}
        self.last_entry_snapshot = None
        self.last_entry_capture_ts = None
        self.pair_learning_store = load_pair_learning()
        self._last_ladder_prep_key = None
        self._last_ai_decision = None
        self._pending_trade_context = None
        self._persist_blocked = False

        # Live price data (updated by WS)
        self._price_data = {}
        self._price_lock = threading.Lock()
        self._price_event = threading.Event()
        self._original_on_message = None

        self._cached_balance = 0.0
        self._balance_lock = threading.Lock()
        self._trades_since_balance_refresh = 0
        self._last_full_balance_refresh = None
        self._connect_lock = threading.Lock()
        self._session_ready = threading.Event()
        self._connecting = False
        self._graceful_stop = False
        self.manual_stop_requested = False
        self._market_feed_active = False

        self._enforce_standard_budget_tiers()
        self._init_evaluation_window_state()
        self._init_risk_state()
        self._restore_persisted_state()

    @staticmethod
    def _enforce_standard_budget_tiers():
        """Fallback to fixed tier ladders if custom amounts are not provided."""
        return [list(t) for t in STANDARD_BUDGET_TIERS]

    def _apply_standard_budget_tiers(self):
        if not hasattr(self, 'budget_tiers') or not self.budget_tiers:
            self.budget_tiers = self._enforce_standard_budget_tiers()

    def _update_budget_tiers_for_balance(self, balance=None, force=False):
        """
        Build tier list from balance bracket table.
        Skipped mid-ladder unless force=True (e.g. periodic balance sync).
        """
        if not getattr(self, 'auto_bracket_enabled', True):
            return
        if not force and getattr(self, 'current_tier_index', 0) > 0:
            return
        if not force and getattr(self, 'session_round_count', 0) > 0:
            return
        if balance is None:
            balance = self.safe_get_balance()
        matched = None
        for min_bal, t0, t1 in reversed(BALANCE_TIER_TABLE):
            if balance >= min_bal:
                matched = (min_bal, t0, t1)
                break
        if matched is None:
            matched = (BALANCE_TIER_TABLE[0][0], BALANCE_TIER_TABLE[0][1], BALANCE_TIER_TABLE[0][2])
        min_bal, t0, _t1 = matched
        new_tiers = [list(t0)]
        if new_tiers != getattr(self, 'budget_tiers', None):
            self.budget_tiers = new_tiers
            bracket = next(
                (b for b in balance_tier_brackets() if b["min_balance"] == min_bal),
                None,
            )
            range_label = bracket["range_label"] if bracket else f"≥${min_bal:,}"
            logger.info(
                f"📊 Tier bracket updated for balance ${balance:.2f} "
                f"({range_label}): T0={t0}"
            )

    def _iq_balance_id(self):
        if self.active_balance_id is not None:
            return int(self.active_balance_id)
        if self.api:
            try:
                from iqoptionapi.stable_api import global_value

                if global_value.balance_id is not None:
                    return int(global_value.balance_id)
            except Exception:
                pass
        return None

    def _state_account_key(self):
        balance_id = self.active_balance_id
        if self.account_type == "TOURNAMENT" and balance_id is None and self.api:
            try:
                from iqoptionapi.stable_api import global_value
                balance_id = global_value.balance_id
            except Exception:
                balance_id = None
        return account_state_key(self.account_type, balance_id)

    def _clear_ephemeral_session_state(self):
        """In-memory session data not stored in bot_state.json."""
        self.asset_penalty_box.clear()
        self._pair_filter_skip_streak.clear()
        self._hot_pair = ""
        self._hot_pair_consecutive_wins = 0
        self._pair_win_er_history.clear()
        self._pair_recent_results.clear()
        self._pair_ladder_loss_times.clear()
        self._last_gate_er = 0.0
        self._inflight_trade_ids = []
        self._last_ladder_prep_key = None
        self._trading_bootstrapped = False
        self._resuming_mid_ladder = False
        with self._round_placement_lock:
            self._round_in_flight = False
        self._graceful_stop = False

    def _default_trading_state(self):
        """Fresh ladder and statistics when an account has no saved state."""
        self._clear_ephemeral_session_state()
        tier0 = self.budget_tiers[0]
        self.cumulative_debt = 0.0
        self.current_tier_index = 0
        self.session_round_count = 0
        self.session_profit = 0.0
        self.session_total_profit = 0.0
        self.session_active = False
        self._init_evaluation_window_state()
        self._init_risk_state()
        self.round_number = 0
        self.total_profit = 0.0
        self.wins = 0
        self.losses = 0
        self.daily_start_balance = 0.0
        self.daily_profit = 0.0
        self.daily_start_time = None
        self.tier_escalations_today = 0
        self.tier_escalations_date = None
        self.session_max_rounds = len(tier0)
        self.reserve_wins_needed = 0
        bet_info = self._compute_round_bet()
        self.current_bet = bet_info["amount"]
        self.last_bet_breakdown = bet_info

    def _apply_persisted_state(self, data):
        self._apply_standard_budget_tiers()
        self.cumulative_debt = float(data.get("cumulative_debt", 0))
        self.current_tier_index = int(data.get("current_tier_index", 0))
        self.session_round_count = int(data.get("session_round_count", 0))
        self.session_profit = float(data.get("session_profit", 0))
        self.session_active = bool(data.get("session_active", False))
        self.round_number = int(data.get("round_number", 0))
        self.total_profit = float(data.get("total_profit", 0))
        self.wins = int(data.get("wins", 0))
        self.losses = int(data.get("losses", 0))
        self.daily_start_balance = float(data.get("daily_start_balance", 0))
        self.daily_profit = float(data.get("daily_profit", 0))
        saved_asset = data.get("asset")
        if saved_asset and OP_code.ACTIVES.get(saved_asset):
            self.asset = saved_asset
            self.asset_id = OP_code.ACTIVES.get(saved_asset, 0)
        daily_raw = data.get("daily_start_time")
        if daily_raw:
            self.daily_start_time = datetime.datetime.strptime(
                daily_raw[:10], "%Y-%m-%d"
            ).date()
        else:
            self.daily_start_time = None
        self.last_stop_reason = data.get("last_stop_reason", "") or ""
        self.last_error = data.get("last_error", "") or ""
        self.paused = bool(data.get("paused", False))
        self.simulation_mode = bool(data.get("simulation_mode", False))
        if data.get("auto_select_manually_disabled"):
            self.auto_select_manually_disabled = True
            self.auto_select_asset = False
        else:
            self.auto_select_manually_disabled = False
            self.auto_select_asset = True
        self.tier_escalations_today = int(data.get("tier_escalations_today", 0))
        tier_raw = data.get("tier_escalations_date")
        if tier_raw:
            self.tier_escalations_date = datetime.datetime.strptime(
                tier_raw[:10], "%Y-%m-%d"
            ).date()
        else:
            self.tier_escalations_date = None
        self.reserve_wins_needed = int(data.get("reserve_wins_needed", 0))
        self.mopup_initial_debt = float(data.get("mopup_initial_debt", 0.0))
        self._apply_evaluation_window_persisted(data)
        self._restore_risk_state(data)
        if self.current_tier_index >= len(self.budget_tiers):
            self.current_tier_index = max(0, len(self.budget_tiers) - 1)
        tier = self.budget_tiers[self.current_tier_index]
        self.session_max_rounds = len(tier)
        if self.session_round_count >= len(tier):
            # session_round_count >= tier length means all steps were exhausted
            # but the escalation was not yet written to state (e.g. crash/stop
            # between the final-step loss and the tier escalation save).
            # Escalate now to avoid replaying the already-lost final step.
            if self.current_tier_index < len(self.budget_tiers) - 1:
                logger.warning(
                    f"Resuming after final-step loss on Tier {self.current_tier_index + 1} "
                    f"(saved step={self.session_round_count}, tier size={len(tier)}) — "
                    f"escalating to Tier {self.current_tier_index + 2} to avoid re-play"
                )
                self.current_tier_index += 1
                tier = self.budget_tiers[self.current_tier_index]
            else:
                logger.warning(
                    f"Saved step {self.session_round_count} exceeds top tier length "
                    f"({len(tier)}); clamping to last step"
                )
            self.session_round_count = 0
            self.session_max_rounds = len(tier)
        if self.cumulative_debt <= 0:
            # Balance is not known yet at init time (IQ Option not connected).
            # Only raise current_tier if the balance-floor can be determined;
            # since safe_get_balance() returns 0 here, floor=0 so this is a no-op.
            # assigned_tier_index was already restored by _apply_evaluation_window_persisted.
            # The full floor sync happens in _sync_assigned_tier_for_trading() once
            # the real balance is known (called after IQ Option connects).
            floor = self._balance_baseline_tier_index()
            if self.current_tier_index < floor:
                self.current_tier_index = floor
                self.session_round_count = 0
            # Only override assigned_tier if balance is actually known (> 0).
            if self.safe_get_balance() > 0:
                self.assigned_tier_index = floor
        bet_info = self._compute_round_bet()
        self.current_bet = bet_info["amount"]
        self.last_bet_breakdown = bet_info
        self._inflight_trade_ids = data.get("inflight_trade_ids", [])
        self._resuming_mid_ladder = self.session_round_count > 0
        self.session_active = False

    def _restore_persisted_state(self):
        """Load debt, tier, and ladder for the active account only."""
        key = self._state_account_key()
        data = load_state(key)
        if not data:
            logger.info(f"No saved state for {key}; using fresh ladder defaults")
            self._default_trading_state()
            return
        try:
            self._apply_persisted_state(data)
            logger.info(
                f"Restored [{key}]: debt=${self.cumulative_debt:.2f}, "
                f"tier={self.current_tier_index + 1}, "
                f"step={self.session_round_count + 1}/{self.session_max_rounds}, "
                f"asset={self.asset}"
            )
        except Exception as e:
            logger.warning(f"Failed to restore persisted state for {key}: {e}")
            self._default_trading_state()

    def _reload_account_state(self):
        """Swap in-memory ladder/stats after changing PRACTICE ↔ REAL (or tournament)."""
        key = self._state_account_key()
        data = load_state(key)
        if data:
            try:
                self._apply_persisted_state(data)
                logger.info(
                    f"Switched to [{key}]: tier={self.current_tier_index + 1}, "
                    f"debt=${self.cumulative_debt:.2f}, "
                    f"step={self.session_round_count + 1}/{self.session_max_rounds}"
                )
                return
            except Exception as e:
                logger.warning(f"Could not load state for {key}: {e}")
        logger.info(f"No saved state for {key}; starting fresh on this account")
        self._default_trading_state()

    def switch_trading_account(self, account_type, balance_id=None):
        if account_type == self.account_type:
            if account_type != "TOURNAMENT":
                return True
            if balance_id is None or balance_id == self.active_balance_id:
                return True

        if self.running:
            logger.warning(
                "Account switch while bot is running — stop trading first for a clean handoff"
            )

        self.persist_state()

        if account_type == "TOURNAMENT":
            if balance_id is None:
                logger.error("Tournament account requires balance_id")
                return False
            if not self.switch_balance_by_id(balance_id):
                return False
            self.account_type = "TOURNAMENT"
            self.active_balance_id = balance_id
        elif account_type in ("REAL", "PRACTICE"):
            self.account_type = account_type
            self.active_balance_id = None
            if self.api:
                self.api.change_balance(self.account_type)
                self._wait_for_profile(timeout=5.0)
                self._refresh_balance_cache(allow_blocking=True)
        else:
            logger.error(f"Unsupported account type: {account_type}")
            return False

        self._reload_account_state()
        self.persist_state("account switched")
        return True

    def persist_state(self, reason=""):
        if getattr(self, "_persist_blocked", False):
            return
        if reason:
            self.last_stop_reason = reason
        save_state(self._state_account_key(), snapshot_from_bot(self))

    def full_system_reset(self, clear_trade_log=True, reason="Full system reset"):
        """Wipe all accounts, optional trade log + pair learning, and in-memory session."""
        from bot_state_store import clear_all_accounts
        from trade_log import purge_entire_trade_log

        key = self._state_account_key()
        was_running = self.running
        if was_running:
            logger.info("Reset: stopping bot before wiping state…")
            self.stop()
        self.running = False
        self.session_active = False

        self._persist_blocked = True
        try:
            self._default_trading_state()
            self.paused = False
            self.last_error = ""
            self.auto_select_manually_disabled = False
            self.auto_select_asset = True
            self.last_stop_reason = reason

            removed = 0
            learning_cleared = False
            if clear_trade_log:
                try:
                    removed = purge_entire_trade_log()
                except Exception as e:
                    logger.warning(f"Could not purge trade log: {e}")
                try:
                    self.pair_learning_store = clear_pair_learning_store(reason)
                    learning_cleared = True
                except Exception as e:
                    logger.warning(f"Could not clear pair learning: {e}")
            else:
                try:
                    self.pair_learning_store = refresh_pair_learning(force=True)
                except Exception as e:
                    logger.warning(f"Pair learning refresh after reset failed: {e}")

            try:
                clear_all_accounts()
            except Exception as e:
                logger.warning(f"Could not clear persisted accounts: {e}")

            self.persist_state(self.last_stop_reason)

            logger.info(
                f"Full reset [{key}]: tier=1, step=1/{self.session_max_rounds}, "
                f"debt=$0.00, all account buckets cleared, "
                f"trade log rows removed={removed}, "
                f"pair learning cleared={learning_cleared}"
            )
        finally:
            self._persist_blocked = False

        return {
            "account_key": key,
            "trades_removed": removed,
            "pair_learning_cleared": learning_cleared,
            "penalties_cleared": True,
            "all_accounts_cleared": True,
        }

    def reset_trading_progress(self, clear_trade_log=True, restart=False):
        was_running = self.running
        result = self.full_system_reset(
            clear_trade_log=clear_trade_log,
            reason="Progress reset — starting at Tier 1 Step 1",
        )
        if restart and was_running:
            import threading as _threading
            t = _threading.Thread(target=self.run, daemon=True, name="dm-post-reset")
            t.start()
            logger.info("Bot restarted after reset.")
        return result

    def get_all_balances(self):
        if not self.api:
            return []
        try:
            inner = getattr(self.api, "api", None)
            profile = getattr(inner, "profile", None) if inner else None
            if profile and getattr(profile, "balances", None):
                return list(profile.balances)
            if profile:
                msg = getattr(profile, "msg", None)
                if isinstance(msg, dict) and msg.get("balances"):
                    return list(msg["balances"])
        except Exception as e:
            logger.debug(f"Profile cache balances unavailable: {e}")
        return []

    def _wait_for_profile(self, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.get_all_balances() or self._read_balance_from_profile() is not None:
                return True
            time.sleep(0.2)
        return False

    def _interruptible_sleep(self, seconds, step=0.25):
        """Sleep in small steps so stop requests exit the trading loop quickly."""
        if seconds <= 0:
            return not self.running
        end_t = time.time() + float(seconds)
        while time.time() < end_t:
            if not self.running:
                return False
            time.sleep(min(step, end_t - time.time()))
        return True

    def _read_balance_from_profile(self):
        if not self.api or not self.api.api:
            return None
        try:
            from iqoptionapi.stable_api import global_value
            for source in (self.get_all_balances(),):
                for b in source:
                    if b.get("id") == global_value.balance_id:
                        return float(b.get("amount", 0.0))
            profile = self.api.api.profile
            balances = profile.balances if profile else None
            if balances:
                for b in balances:
                    if b.get("id") == global_value.balance_id:
                        return float(b.get("amount", 0.0))
        except Exception:
            pass
        return None

    def _refresh_balance_cache(self, allow_blocking=False):
        balance = self._read_balance_from_profile()
        if balance is None and allow_blocking and self.api:
            try:
                if self.api.check_connect():
                    balance = float(self.api.get_balance())
            except Exception as e:
                logger.warning(f"Error refreshing balance cache: {e}")
                return
        if balance is not None:
            with self._balance_lock:
                self._cached_balance = balance

    def safe_get_balance(self):
        balance = self._read_balance_from_profile()
        if balance is not None:
            with self._balance_lock:
                self._cached_balance = balance
            return balance
        with self._balance_lock:
            return self._cached_balance

    def force_refresh_balance(self):
        """Fetch live balances from IQ Option and update the local cache."""
        if not self.api:
            return {
                "ok": False,
                "error": "Not connected",
                "balance": self.safe_get_balance(),
                "accounts": [],
            }
        if not self._api_alive():
            return {
                "ok": False,
                "error": "IQ connection is down — use Reconnect",
                "balance": self.safe_get_balance(),
                "accounts": self.get_all_balances(),
            }
        try:
            raw = self.api.get_balances()
            balances = raw.get("msg", []) if isinstance(raw, dict) else []
            if balances:
                inner = getattr(self.api, "api", None)
                profile = getattr(inner, "profile", None) if inner else None
                if profile is not None:
                    profile.balances = list(balances)

            balance = float(self.api.get_balance())
            with self._balance_lock:
                self._cached_balance = balance

            if balances:
                from iqoptionapi.stable_api import global_value

                bid = global_value.balance_id
                for entry in balances:
                    if entry.get("id") == bid:
                        entry["amount"] = balance
                        break

            logger.info(f"Balance refreshed from IQ Option: ${balance:.2f}")
            return {
                "ok": True,
                "balance": balance,
                "accounts": balances or self.get_all_balances(),
            }
        except Exception as e:
            logger.warning(f"force_refresh_balance failed: {e}")
            return {
                "ok": False,
                "error": str(e),
                "balance": self.safe_get_balance(),
                "accounts": self.get_all_balances(),
            }

    def _realign_tier_after_balance(self, balance):
        """Re-sync ladder bracket and next bet from the latest balance."""
        self._sync_assigned_tier_for_trading(balance=balance)
        bet_info = self._compute_round_bet(balance=balance)
        self.current_bet = bet_info["amount"]
        self.last_bet_breakdown = bet_info

    def _run_scheduled_balance_sync(self, reason="scheduled"):
        """Fetch live balance from IQ Option and re-align tier brackets."""
        if not self.api or not self._api_alive():
            self._refresh_balance_cache(allow_blocking=True)
            return False
        result = self.force_refresh_balance()
        if not result.get("ok"):
            self._refresh_balance_cache(allow_blocking=True)
            logger.warning(
                f"Balance sync failed ({reason}): {result.get('error', 'unknown')}"
            )
            return False
        balance = float(result["balance"])
        self._trades_since_balance_refresh = 0
        self._last_full_balance_refresh = datetime.datetime.utcnow()
        self._update_budget_tiers_for_balance(balance=balance, force=True)
        self._realign_tier_after_balance(balance)
        logger.info(
            f"Balance sync ({reason}): ${balance:.2f} — ladder bracket re-aligned"
        )
        return True

    def _maybe_sync_balance_after_trade(self):
        """Light refresh every trade; full IQ API sync every N trades or hours."""
        self._trades_since_balance_refresh += 1
        every_n = max(1, int(getattr(app_config, "BALANCE_REFRESH_EVERY_N_TRADES", 3)))
        min_hours = float(getattr(app_config, "BALANCE_REFRESH_MIN_HOURS", 2.0))
        last = getattr(self, "_last_full_balance_refresh", None)
        hours_elapsed = (
            (datetime.datetime.utcnow() - last).total_seconds() / 3600.0
            if last
            else float("inf")
        )
        if (
            self._trades_since_balance_refresh >= every_n
            or hours_elapsed >= min_hours
        ):
            self._run_scheduled_balance_sync(reason="post-trade")
        else:
            self._refresh_balance_cache(allow_blocking=True)
            self._realign_tier_after_balance(self.safe_get_balance())

    def _maybe_sync_balance_idle(self):
        """Time-based sync while the bot is waiting between rounds."""
        min_hours = float(getattr(app_config, "BALANCE_REFRESH_MIN_HOURS", 2.0))
        last = getattr(self, "_last_full_balance_refresh", None)
        if last is None:
            return self._run_scheduled_balance_sync(reason="idle-first")
        hours_elapsed = (datetime.datetime.utcnow() - last).total_seconds() / 3600.0
        if hours_elapsed >= min_hours:
            return self._run_scheduled_balance_sync(reason="idle-timer")
        return False

    # ── Connection ───────────────────────────────────────────────────────────

    def _api_alive(self):
        if not self.api:
            return False
        try:
            return bool(self.api.check_connect())
        except Exception:
            return False

    def _get_candles_safe(self, asset_name, interval, count, end_time=None, timeout=15.0):
        """Bounded get_candles — avoids hanging the trading loop on a dead socket."""
        if not self.api:
            return None
        if end_time is None:
            end_time = time.time()

        def _fetch():
            return self.api.get_candles(asset_name, interval, count, end_time)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(_fetch).result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"get_candles timed out ({timeout}s) for {asset_name} "
                f"(interval={interval}, count={count})"
            )
            return None
        except Exception as e:
            logger.warning(f"get_candles failed for {asset_name}: {e}")
            return None

    def _ensure_api_connection(self, force=False):
        """Verify IQ websocket is alive; reconnect if needed."""
        if self._connecting:
            return False
        alive = False
        if self.api:
            try:
                alive = bool(self.api.check_connect())
            except Exception:
                alive = False
        if alive and not force:
            return True
        if not alive:
            logger.warning("IQ API connection lost — reconnecting…")
            self._session_ready.clear()
            self._market_feed_active = False
        return self.connect(force_reconnect=True)

    def is_session_ready(self):
        return self._session_ready.is_set() and self._api_alive()

    def connect(self, force_reconnect=False):
        with self._connect_lock:
            if not force_reconnect and self.is_session_ready():
                logger.info(
                    f"IQ session already active ({self.account_type}). "
                    f"Balance: ${self.safe_get_balance():.2f}"
                )
                return True

            self._connecting = True
            self._session_ready.clear()
            self._market_feed_active = False
            try:
                logger.info("Opening new IQ Option session...")
                self.connected = False
                self.api = connect_to_iqoption()
                if not self.api:
                    self.connected = False
                    logger.error("Failed to connect to IQ Option.")
                    return False

                self.connected = True
                try:
                    self.api.change_balance(self.account_type)
                except Exception as e:
                    logger.warning(f"change_balance({self.account_type}): {e}")

                self._wait_for_profile(timeout=15.0)
                self._run_scheduled_balance_sync(reason="connect")
                self._start_balance_refresh_thread()
                self._session_ready.set()
                logger.info(
                    f"IQ session ready — {self.account_type}. "
                    f"Balance: ${self.safe_get_balance():.2f} | "
                    f"Accounts: {len(self.get_all_balances())}"
                )
                return True
            finally:
                self._connecting = False

    def warm_up_market_feed(self):
        if not self._api_alive():
            return False
        try:
            self._install_price_sniffer()
            self._subscribe()
            self._market_feed_active = True
            logger.info(f"Market feed warmed up for {self.asset} (id={self.asset_id})")
            return True
        except Exception as e:
            logger.warning(f"Market feed warm-up failed: {e}")
            return False

    def _start_idle_keepalive(self):
        if getattr(self, "_idle_keepalive_thread", None) and self._idle_keepalive_thread.is_alive():
            return

        def _loop():
            while not self.running and self._session_ready.is_set():
                if not self._api_alive():
                    logger.warning("IQ session idle timeout — use Reconnect before Start.")
                    self._session_ready.clear()
                    self._market_feed_active = False
                    break
                self._refresh_balance_cache(allow_blocking=False)
                time.sleep(30)

        self._idle_keepalive_thread = threading.Thread(
            target=_loop, daemon=True, name="iq-idle-keepalive"
        )
        self._idle_keepalive_thread.start()

    def _start_balance_refresh_thread(self):
        if getattr(self, "_balance_refresh_thread", None) and self._balance_refresh_thread.is_alive():
            return

        def _loop():
            tick = 0
            while self.connected:
                # Profile websocket updates are fast; periodic API refresh catches post-trade balance.
                self._refresh_balance_cache(allow_blocking=(tick % 12 == 0))
                tick += 1
                time.sleep(5)

        self._balance_refresh_thread = threading.Thread(target=_loop, daemon=True)
        self._balance_refresh_thread.start()

    def switch_balance_by_id(self, balance_id):
        if not self.api:
            return False
        try:
            from iqoptionapi.stable_api import global_value
            if global_value.balance_id is not None:
                self.api.position_change_all("unsubscribeMessage", global_value.balance_id)
            global_value.balance_id = balance_id
            self.api.position_change_all("subscribeMessage", balance_id)
            self._wait_for_profile(timeout=5.0)
            with self._balance_lock:
                self._cached_balance = 0.0
            self._refresh_balance_cache(allow_blocking=True)
            balance = self._read_balance_from_profile()
            if balance is None:
                if self.api.check_connect():
                    balance = float(self.api.get_balance())
                    with self._balance_lock:
                        self._cached_balance = balance
            logger.info(f"Switched to balance ID {balance_id}. Balance: ${self.safe_get_balance():.2f}")
            return True
        except Exception as e:
            logger.error(f"Failed to switch balance by ID: {e}")
            return False

    # ── Price Subscription ───────────────────────────────────────────────────

    def _install_price_sniffer(self):
        ws_client = self.api.api.websocket_client
        if getattr(self, "_original_on_message", None) is not None:
            logger.info("Price sniffer already installed.")
            return

        self._original_on_message = ws_client.on_message

        def patched_on_message(raw_message):
            try:
                msg = json.loads(str(raw_message))
                name = msg.get("name")
                if name == "client-price-generated":
                    payload = msg.get("msg", {})
                    if payload.get("asset_id") == self.asset_id:
                        period = payload.get("period")
                        prices = payload.get("prices", [])
                        with self._price_lock:
                            self._price_data[period] = prices
                        self._price_event.set()
                elif name in ("profile", "balances", "balance-changed"):
                    balance = self._read_balance_from_profile()
                    if balance is not None:
                        with self._balance_lock:
                            self._cached_balance = balance
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
            self._original_on_message(raw_message)

        ws_client.on_message = patched_on_message
        ws_client.wss.on_message = lambda ws, msg: patched_on_message(msg)
        logger.info("Price sniffer installed.")

    def _subscribe(self):
        try:
            if hasattr(self.api.api, 'subscribe_digital_price_splitter'):
                self.api.api.subscribe_digital_price_splitter(self.asset_id)
            else:
                data = {
                    "name": "price-splitter.client-price-generated",
                    "version": "1.0",
                    "params": {
                        "routingFilters": {
                            "instrument_type": "digital-option",
                            "asset_id": int(self.asset_id)
                        }
                    }
                }
                self.api.api.send_websocket_request("subscribeMessage", msg=data)
            logger.info(f"Subscribed to price-splitter for asset_id={self.asset_id}")
        except Exception as e:
            logger.error(f"Failed to subscribe to price-splitter: {e}")

    def _unsubscribe(self, asset_id=None):
        target_id = asset_id if asset_id is not None else self.asset_id
        try:
            if hasattr(self.api.api, 'unsubscribe_digital_price_splitter'):
                self.api.api.unsubscribe_digital_price_splitter(target_id)
            else:
                data = {
                    "name": "price-splitter.client-price-generated",
                    "version": "1.0",
                    "params": {
                        "routingFilters": {
                            "instrument_type": "digital-option",
                            "asset_id": int(target_id)
                        }
                    }
                }
                self.api.api.send_websocket_request("unsubscribeMessage", msg=data)
        except Exception:
            pass

    # ── Asset movement analysis ──────────────────────────────────────────────

    def _map_to_active_symbol(self, name):
        """Map IQ underlying name to an OP_code.ACTIVES key (e.g. EURUSD → EURUSD-OTC)."""
        if not name:
            return None
        if name in OP_code.ACTIVES:
            return name
        for candidate in (f"{name}-OTC", name.replace("-OTC", "")):
            if candidate in OP_code.ACTIVES:
                return candidate
        upper = str(name).upper()
        for key in OP_code.ACTIVES:
            ku = key.upper()
            if ku == upper or ku == f"{upper}-OTC" or ku.replace("-OTC", "") == upper.replace("-OTC", ""):
                return key
        return None

    def _build_actives_otc_fallback_pool(self):
        """OTC forex names from OP_code when IQ digital schedule API is empty."""
        now_utc = datetime.datetime.utcnow()
        is_weekend = now_utc.weekday() >= 5
        pool = []
        for name in OP_code.ACTIVES.keys():
            if name in self.avoid_markets or name in DIGITAL_UNSUPPORTED_ASSETS:
                continue
            if is_weekend:
                if "-OTC" in name:
                    pool.append(name)
            else:
                if "-OTC" in name and any(
                    curr in name
                    for curr in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")
                ):
                    pool.append(name)
        if not pool and self.asset in OP_code.ACTIVES:
            pool = [self.asset]
        return sorted(set(pool))

    def _get_open_digital_from_all_open_time(self):
        """Secondary source: get_all_open_time() digital section."""
        if not self.api:
            return []
        try:
            open_time = self.api.get_all_open_time()
            digital = (open_time or {}).get("digital") or {}
            names = []
            for base, info in digital.items():
                if not isinstance(info, dict):
                    continue
                if not info.get("open"):
                    continue
                mapped = self._map_to_active_symbol(base)
                if mapped and mapped not in names:
                    names.append(mapped)
            if names:
                logger.info(
                    f"Open digital pairs from get_all_open_time: {len(names)}"
                )
            return names
        except Exception as e:
            logger.warning(f"get_all_open_time digital section failed: {e}")
            return []

    def _get_open_digital_asset_names(self):
        """
        List open digital pairs from IQ underlying schedule, with fallbacks.
        """
        if not self.api:
            return self._build_actives_otc_fallback_pool()

        names = []
        relaxed_names = []
        try:
            raw = self.api.get_digital_underlying_list_data()
            if raw is None:
                logger.warning(
                    "Digital underlying API returned no data (timeout) — trying fallbacks"
                )
            else:
                underlyings = raw.get("underlying") if isinstance(raw, dict) else None
                if not underlyings:
                    logger.warning(
                        "Digital underlying list empty in API response — trying fallbacks"
                    )
                else:
                    now = time.time()
                    for item in underlyings:
                        if not isinstance(item, dict):
                            continue
                        base = item.get("underlying") or item.get("name")
                        mapped = self._map_to_active_symbol(base)
                        if not mapped:
                            continue
                        if mapped not in relaxed_names:
                            relaxed_names.append(mapped)
                        schedule = item.get("schedule") or []
                        is_open = not schedule
                        for slot in schedule:
                            if not isinstance(slot, dict):
                                continue
                            start = float(slot.get("open", 0) or 0)
                            end = float(slot.get("close", 0) or 0)
                            if start < now < end:
                                is_open = True
                                break
                        if is_open and mapped not in names:
                            names.append(mapped)
                    if names:
                        logger.info(
                            f"Open digital pairs from IQ schedule: {len(names)}"
                        )
                    elif relaxed_names:
                        logger.warning(
                            f"Schedule filter left 0 open pairs ({len(relaxed_names)} "
                            f"mapped) — using mapped underlyings without schedule filter"
                        )
                        names = relaxed_names
        except KeyError as e:
            logger.warning(f"Digital underlying API missing key ({e}) — trying fallbacks")
        except Exception as e:
            logger.warning(f"Could not fetch open digital assets: {e}")

        if not names:
            names = self._get_open_digital_from_all_open_time()
        if not names:
            names = self._build_actives_otc_fallback_pool()
            logger.info(f"Using ACTIVES OTC fallback pool: {len(names)} pairs")
        return names

    def list_tradeable_asset_symbols(self):
        """Symbols for UI dropdown: open digitals + configured candidates."""
        open_names = set(self._get_open_digital_asset_names())
        for name in self.asset_candidates or []:
            if self._map_to_active_symbol(name):
                open_names.add(self._map_to_active_symbol(name))
        if self.asset and self._map_to_active_symbol(self.asset):
            open_names.add(self.asset)
        return sorted(open_names)

    @staticmethod
    def _candle_ohlc(candle):
        close = float(candle.get("close", 0) or 0)
        open_ = float(candle.get("open", close) or close)
        high = float(candle.get("max", candle.get("high", close)) or close)
        low = float(candle.get("min", candle.get("low", close)) or close)
        return open_, high, low, close

    def _refresh_pair_learning_cache_later(self):
        """Debounced cache refresh."""
        def _run():
            time.sleep(2.0)
            self._cached_pair_learning_summary = pair_learning_summary()
        threading.Thread(target=_run, daemon=True, name="pair-cache-refresh").start()

    def _load_config_history(self):
        if not os.path.exists(self.config_history_path):
            return []
        try:
            with open(self.config_history_path, "r") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_config_history(self):
        try:
            os.makedirs(os.path.dirname(self.config_history_path), exist_ok=True)
            with open(self.config_history_path, "w") as f:
                json.dump(self.config_history, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save config history: {e}")

    def reload_pair_learning(self):
        """Reload auto per-pair rules from the bot trade log (all accounts)."""
        self.pair_learning_store = refresh_pair_learning(force=True)
        n = len(self.pair_learning_store.get("pairs") or {})
        logger.info("Per-pair learning loaded: %s pairs with rules", n)

    def _straddle_gate_thresholds(self, asset_name=None) -> dict:
        asset = asset_name or self.asset
        return effective_gates_for_asset(asset, self.pair_learning_store)

    def _is_asset_penalty_blocked(self, asset=None):
        name = asset or self.asset
        until = self.asset_penalty_box.get(name)
        if not until:
            return False
        if datetime.datetime.utcnow() >= until:
            del self.asset_penalty_box[name]
            return False
        return True

    def _apply_pair_penalty(self, minutes, reason):
        mins = _clamp_penalty_minutes(minutes)
        self.asset_penalty_box[self.asset] = (
            datetime.datetime.utcnow() + datetime.timedelta(minutes=mins)
        )
        logger.warning(f"{self.asset} penalized {mins}m — {reason}")

    def _apply_asset_penalty_uncapped(self, asset, minutes, reason):
        """Penalty box entry without the short 5-minute clamp used for gate skips."""
        mins = max(1, int(minutes))
        until = datetime.datetime.utcnow() + datetime.timedelta(minutes=mins)
        self.asset_penalty_box[asset] = until
        logger.warning(
            f"{asset} penalized {mins}m — {reason} "
            f"(until {until.strftime('%H:%M')} UTC)"
        )

    # _rotate_asset_after_step_four removed — step-4 pair rotation is disabled.
    # The bot plays all ladder steps in order without switching pairs at step 4.


    def _effective_min_er(self) -> float:
        """
        Return the highest applicable ER floor for the current asset:
          1. Global base floor (RULE_GATE_MIN_ER, default 0.30)
          2. Per-pair historical floor (ASSET_MIN_ER) — e.g. ETHUSD-OTC=0.40
          3. Off-peak window floor (RULE_GATE_OFFPEAK_MIN_ER=0.37) when the pair
             is being called outside its known-good UTC window.
        The highest of all applicable values wins.
        """
        base = self.rule_gate_min_er  # global floor (0.30)

        # Per-pair hard floor from historical CSV analysis
        pair_floors = getattr(app_config, "ASSET_MIN_ER", {})
        pair_floor = pair_floors.get(self.asset, base)
        effective = max(base, pair_floor)

        # Off-peak window check — preferred windows are advisory by default.
        # Only raises the ER floor when OFFPEAK_HARD_BLOCK=true; otherwise the
        # pair is logged as off-peak but the gate does not block it.
        windows = getattr(app_config, "ASSET_PREFERRED_WINDOWS", {})
        if windows and self.asset in windows:
            hour_utc = datetime.datetime.utcnow().hour
            in_window = any(start <= hour_utc < end for start, end in windows[self.asset])
            if not in_window:
                hard_block = getattr(app_config, "OFFPEAK_HARD_BLOCK", False)
                if hard_block:
                    off_peak = getattr(app_config, "RULE_GATE_OFFPEAK_MIN_ER", 0.37)
                    effective = max(effective, off_peak)
                else:
                    logger.debug(
                        f"📅 {self.asset} is off preferred window at UTC {hour_utc}:00 "
                        f"(advisory only — set OFFPEAK_HARD_BLOCK=true to enforce)"
                    )

        # Dynamic quality degradation floor — if the pair's current conditions have
        # dropped well below what it was doing when it was winning, tighten the gate.
        # The floor is capped at PAIR_QUALITY_MAX_FLOOR (0.40) so pairs like APPLE-OTC
        # that legitimately win at low ER (0.25-0.40) are never over-filtered.
        _min_wins = getattr(app_config, "PAIR_QUALITY_MIN_WINS", 3)
        _drop_ratio = getattr(app_config, "PAIR_QUALITY_DROP_RATIO", 0.60)
        _max_floor = getattr(app_config, "PAIR_QUALITY_MAX_FLOOR", 0.40)
        _hist = self._pair_win_er_history.get(self.asset, [])
        if len(_hist) >= _min_wins:
            avg_win_er = sum(_hist) / len(_hist)
            # Cap: dynamic floor can never exceed PAIR_QUALITY_MAX_FLOOR regardless
            # of how high the winning ER average was.
            quality_floor = min(avg_win_er * _drop_ratio, _max_floor)
            if quality_floor > effective:
                logger.debug(
                    f"📊 {self.asset} quality floor {quality_floor:.2f} "
                    f"(avg_win_er={avg_win_er:.2f} × {_drop_ratio}, "
                    f"capped at {_max_floor}, based on {len(_hist)} wins)"
                )
                effective = quality_floor

        # Deep-step ER boost — step 3+ bets are 6x the S1 bet; require a cleaner signal.
        # session_round_count is 0-indexed (0 = S1, 1 = S2, 2 = S3).
        _deep_start = getattr(app_config, "DEEP_STEP_START", 3)
        _deep_er_boost = getattr(app_config, "DEEP_STEP_MIN_ER_BOOST", 0.08)
        _current_step = self.session_round_count + 1
        if _current_step >= _deep_start:
            effective += _deep_er_boost
            logger.debug(
                f"📊 {self.asset} step {_current_step} deep-step ER boost "
                f"+{_deep_er_boost:.2f} → floor {effective:.2f}"
            )

        if effective > base:
            logger.debug(
                f"📊 {self.asset} ER floor raised to {effective:.2f} "
                f"(pair_floor={pair_floor:.2f}, base={base:.2f})"
            )
        return effective

    def _placement_deadline_second(self):
        return self.purchase_deadline_sec

    def _too_late_to_place(self):
        # For dynamic timeframe, deadline applies to seconds_past_candle
        return self._seconds_past_candle() > self._placement_deadline_second()

    def _handle_penalty_box_block(self):
        until = self.asset_penalty_box.get(self.asset)
        if not until or datetime.datetime.utcnow() >= until:
            # Penalty already expired between the outer check and here — nothing to do.
            self.asset_penalty_box.pop(self.asset, None)
            return
        remaining = max(0.0, (until - datetime.datetime.utcnow()).total_seconds())
        mins = max(1, int(remaining / 60))
        penalized = self.asset
        mid_ladder = self._in_active_ladder()
        step_label = f" (step {self.session_round_count + 1})" if mid_ladder else ""

        self.status_note = (
            f"🚫 {penalized} penalized (~{mins}m left){step_label} — scanning for another pair"
        )
        # Rate-limit this log since it runs inside the tight polling loop.
        _pb_log_times = getattr(self, "_penalty_box_log_times", {})
        _last_log = _pb_log_times.get(penalized, 0)
        if time.time() - _last_log >= 30:
            logger.warning(
                f"{penalized} in penalty box (~{mins}m left){step_label} — hunting for another pair"
            )
            _pb_log_times[penalized] = time.time()
            self._penalty_box_log_times = _pb_log_times

        if self.auto_select_asset:
            self._apply_auto_asset_selection(
                reason="penalty box", relaxed=True
            )
            if self.asset != penalized and not self._is_asset_penalty_blocked():
                self._wait_for_price_data()
                logger.info(
                    f"Penalty escape{step_label}: left {penalized}, now on {self.asset}"
                )
                return
            if self._switch_to_next_tradeable_pair(
                "penalty box", relaxed=True
            ):
                self._wait_for_price_data()
                return
            logger.warning(
                f"No alternate pair available while {penalized} is penalized "
                f"({len(self.asset_penalty_box)} in penalty box)"
            )
        self._skip_to_next_entry_window("penalty box")

    def _ladder_prep_key(self, bet_info):
        return (
            self.current_tier_index,
            self.session_round_count,
            bet_info.get("tier_number"),
            bet_info.get("step_number"),
        )

    def _log_ladder_prep(self, bet_info):
        key = self._ladder_prep_key(bet_info)
        if key == self._last_ladder_prep_key:
            return
        self._last_ladder_prep_key = key
        logger.info(f"\n{'-'*50}")
        logger.info(
            f"Ladder prep — Tier {bet_info['tier_number']} "
            f"step {bet_info['step_number']}/{self.session_max_rounds}"
        )
        logger.info(
            f"Bet: ${self.current_bet:.2f} per leg | "
            f"Session P/L: ${self.session_profit:.2f} | "
            f"Debt: ${self.cumulative_debt:.2f} | "
            f"Total P/L: ${self.total_profit:.2f}"
        )

    def _calculate_trend_metrics(self, asset_name, spot, count=15):
        """
        Calculate normalized slope and Efficiency Ratio of recent closes.
        FIX: Guard against spot being None or <= 0 to prevent NoneType crash.
        """
        # FIXED: was crashing with "NoneType <= int" when spot=None
        if not self.api or spot is None or spot <= 0:
            return 0.0, 0.0
        try:
            candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, count, time.time())
            if not candles or len(candles) < count:
                return 0.0, 0.0

            closes = [float(c.get("close", 0)) for c in candles]

            x = list(range(len(closes)))
            x_mean = sum(x) / len(x)
            y_mean = sum(closes) / len(closes)
            numerator = sum((x[i] - x_mean) * (closes[i] - y_mean) for i in range(len(x)))
            denominator = sum((x[i] - x_mean) ** 2 for i in range(len(x)))
            raw_slope = numerator / denominator if denominator != 0 else 0.0

            normalized_slope = (raw_slope / spot) * 1000000.0

            net_change = abs(closes[-1] - closes[0])
            total_movement = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
            er = net_change / total_movement if total_movement > 0 else 0.0

            return normalized_slope, er
        except Exception as e:
            logger.warning(f"Failed to calculate trend metrics for {asset_name}: {e}")
            return 0.0, 0.0

    def _long_term_trend_blocks_direction(self, direction, spot):
        """
        Block short-term flips against the higher-timeframe trend.
        Uses a longer candle window so weak bounces do not trigger CALLs in downtrends.

        Exception: if short-term momentum is strong enough (slope >= 20, ER >= 0.28),
        the recovery/reversal has sufficient force to override the LT bias.  This
        prevents the 35-candle lookback from blocking a clearly visible trend change.
        """
        if not direction or spot is None or spot <= 0:
            return False
        lt_slope, lt_er = self._calculate_trend_metrics(self.asset, spot, count=35)
        direction = direction.lower()
        if direction == "call" and lt_slope <= -12.0 and lt_er >= 0.20:
            st_slope, st_er = self._calculate_trend_metrics(self.asset, spot, count=7)
            if st_slope >= 20.0 and st_er >= 0.28:
                logger.info(
                    f"📈 LT bearish (slope={lt_slope:.1f}, ER={lt_er:.3f}) overridden by "
                    f"strong ST recovery (ST slope={st_slope:.1f}, ER={st_er:.3f}). Allowing CALL."
                )
                return False
            logger.warning(
                f"🛑 Long-term bearish trend blocks CALL "
                f"(LT slope={lt_slope:.1f}, ER={lt_er:.3f}). Keeping PUT bias."
            )
            return True
        if direction == "put" and lt_slope >= 12.0 and lt_er >= 0.20:
            st_slope, st_er = self._calculate_trend_metrics(self.asset, spot, count=7)
            if st_slope <= -20.0 and st_er >= 0.28:
                logger.info(
                    f"📉 LT bullish (slope={lt_slope:.1f}, ER={lt_er:.3f}) overridden by "
                    f"strong ST drop (ST slope={st_slope:.1f}, ER={st_er:.3f}). Allowing PUT."
                )
                return False
            logger.warning(
                f"🛑 Long-term bullish trend blocks PUT "
                f"(LT slope={lt_slope:.1f}, ER={lt_er:.3f}). Keeping CALL bias."
            )
            return True
        return False

    @staticmethod
    def _sticky_direction_disagrees_with_short_term(
        direction, med_slope, short_slope, short_er,
    ):
        """
        Break sticky direction only when short-term momentum clearly disagrees
        and the medium trend is not overwhelmingly in the sticky direction.
        Avoids fading live recoveries without flipping on every micro-tick.

        Special case: if short-term slope is very strong (>= 28 / <= -28), break
        sticky direction even when the medium slope is still against us — a large
        visible recovery should not be suppressed by a lagging medium slope.
        """
        if short_er < 0.12:
            return False
        direction = (direction or "").lower()
        if direction == "put":
            return (short_slope >= 18.0 and med_slope > -12.0) or short_slope >= 28.0
        if direction == "call":
            return (short_slope <= -18.0 and med_slope < 12.0) or short_slope <= -28.0
        return False

    def _should_hold_sticky_direction(self, direction, med_slope, spot):
        """Whether to keep last_direction through a classified pullback/correction."""
        if self._long_term_trend_blocks_direction(direction, spot):
            return False
        short_slope, short_er = self._calculate_trend_metrics(self.asset, spot, count=5)
        if self._sticky_direction_disagrees_with_short_term(
            direction, med_slope, short_slope, short_er
        ):
            logger.warning(
                f"Short-term momentum vs sticky {direction.upper()} "
                f"(med={med_slope:.1f}, short={short_slope:.1f}, ER={short_er:.3f}) "
                f"— re-reading direction."
            )
            return False
        return True

    def _calculate_atr(self, asset_name, count=5):
        if not self.api:
            return 0.0
        try:
            candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, count, time.time())
            if not candles or len(candles) < 2:
                return 0.0

            trs = []
            for i in range(1, len(candles)):
                open_, high, low, close = self._candle_ohlc(candles[i])
                _, _, _, prev_close = self._candle_ohlc(candles[i-1])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)

            return sum(trs) / len(trs) if trs else 0.0
        except Exception as e:
            logger.warning(f"Failed to calculate ATR for {asset_name}: {e}")
            return 0.0

    def _is_momentum_accelerating(self, asset_name):
        if not self.api:
            return True, 0.0, 0.0
        try:
            candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, 7, time.time())
            if not candles or len(candles) < 6:
                return True, 0.0, 0.0

            trs = []
            for i in range(1, len(candles)):
                open_, high, low, close = self._candle_ohlc(candles[i])
                _, _, _, prev_close = self._candle_ohlc(candles[i - 1])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)

            older_trs = trs[:3]
            recent_trs = trs[-2:]

            older_atr = sum(older_trs) / len(older_trs) if older_trs else 0.0
            recent_atr = sum(recent_trs) / len(recent_trs) if recent_trs else 0.0

            ratio = self.momentum_min_ratio
            is_accelerating = older_atr <= 0 or recent_atr >= (older_atr * ratio)
            return is_accelerating, recent_atr, older_atr
        except Exception as e:
            logger.warning(f"Failed momentum check for {asset_name}: {e}")
            return True, 0.0, 0.0

    @staticmethod
    def _count_candle_alternations(candles, lookback=None):
        """Count bullish/bearish color flips over the last `lookback` closed candles."""
        lookback = lookback or getattr(app_config, "RANGING_LOOKBACK_CANDLES", 6)
        if not candles:
            return 0
        recent = candles[-lookback:] if len(candles) >= lookback else candles
        colors = []
        for c in recent:
            open_p = float(c.get("open", 0) or 0)
            close_p = float(c.get("close", 0) or 0)
            colors.append(close_p >= open_p)
        alternations = 0
        for i in range(1, len(colors)):
            if colors[i] != colors[i - 1]:
                alternations += 1
        return alternations

    def _closed_candle_direction(self, asset_name):
        """Direction implied by the last fully closed candle: call=green, put=red."""
        tf = int(getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60))
        lookback = getattr(app_config, "RANGING_LOOKBACK_CANDLES", 6)
        candles = self._get_candles_safe(
            asset_name, tf, lookback + 2, time.time()
        )
        if not candles or len(candles) < 2:
            return None
        closed = candles[:-1]
        last = closed[-1]
        open_p = float(last.get("open", 0) or 0)
        close_p = float(last.get("close", 0) or 0)
        return "call" if close_p >= open_p else "put"

    def _record_ladder_exhaustion_and_check_penalty(self):
        """
        Sliding-window performance rule (applied only between ladders, never mid-trade).

        Tracks per-pair timestamps of full-ladder exhaustions (all steps lost).
        If a pair exhausts the ladder 2 or more times within any rolling 15-minute
        window, it earns a 5-minute penalty box entry so the bot rescans for a
        better pair before starting the next ladder.
        """
        asset = self.asset
        now = datetime.datetime.utcnow()
        cutoff = now - datetime.timedelta(minutes=15)

        times = self._pair_ladder_loss_times.get(asset, [])
        times = [t for t in times if t > cutoff]
        times.append(now)
        self._pair_ladder_loss_times[asset] = times

        if len(times) >= 2:
            penalty_until = now + datetime.timedelta(minutes=5)
            self.asset_penalty_box[asset] = penalty_until
            self._pair_ladder_loss_times[asset] = []
            logger.warning(
                f"📉 {asset}: {len(times)} full-ladder losses in 15 min — "
                f"5-min penalty applied (until {penalty_until.strftime('%H:%M')} UTC)"
            )

    def _score_asset_movement(self, asset_name):
        if not self.api:
            return None
        candles = self._get_candles_safe(
            asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, self.asset_analysis_candles, time.time()
        )
        if not candles or len(candles) < 8:
            return None

        body_pcts = []
        closes = []
        highs = []
        lows = []

        for candle in candles:
            open_, high, low, close = self._candle_ohlc(candle)
            if close <= 0:
                continue
            body_pcts.append(abs(close - open_) / close)
            closes.append(close)
            highs.append(high)
            lows.append(low)

        if len(closes) < 8:
            return None

        path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
        total_range = max(highs) - min(lows)
        avg_close = sum(closes) / len(closes)
        if avg_close <= 0:
            return None

        range_pct = total_range / avg_close
        active_ratio = sum(
            1 for b in body_pcts if b >= self.min_candle_body_pct
        ) / len(body_pcts)
        path_ratio = path / max(total_range, avg_close * 1e-8)

        flat_penalty = 0.0
        if range_pct < self.min_session_range_pct:
            flat_penalty = 35.0 * (1.0 - range_pct / self.min_session_range_pct)

        doji_ratio = sum(body_pcts) / len(body_pcts)
        doji_penalty = max(0.0, (self.min_candle_body_pct * 2 - doji_ratio)) * 2000

        score = (
            active_ratio * 45.0
            + min(path_ratio, 4.0) * 12.0
            + range_pct * 8000.0
            - flat_penalty
            - doji_penalty
        )

        hour_utc = datetime.datetime.utcnow().hour
        if any(start <= hour_utc < end for start, end in self.preferred_utc_hours):
            score += 8.0

        spot = closes[-1] if closes else avg_close
        normalized_slope, er = self._calculate_trend_metrics(asset_name, spot, count=15)
        abs_slope = abs(normalized_slope)

        gates = self._straddle_gate_thresholds(asset_name)
        min_er = gates["min_efficiency_ratio"]
        min_slope = gates["min_directional_slope"]

        atr = self._calculate_atr(asset_name, count=5) or 0.0

        straddle_penalty = 0.0
        if er < min_er:
            straddle_penalty += 100.0
        if abs_slope < min_slope:
            straddle_penalty += 50.0
        if range_pct < self.min_session_range_pct:
            straddle_penalty += 30.0

        # ATR-normalized slope: slope should represent meaningful movement vs noise.
        # Soft penalty only — avoids blocking trades on lower-volatility pairs entirely.
        min_atr_slope = float(getattr(app_config, "MIN_SLOPE_ATR_RATIO", 0.30))
        atr_pips = (atr / spot) * 1_000_000.0 if (atr > 0 and spot > 0) else 0.0
        slope_atr_ratio = abs_slope / atr_pips if atr_pips > 0 else 1.0
        if atr_pips > 0 and slope_atr_ratio < min_atr_slope:
            straddle_penalty += 20.0

        # EMA20 over-extension penalty: avoid chasing price already far from mean.
        if len(closes) >= 5 and atr > 0:
            k = 2.0 / (min(len(closes), 20) + 1)
            ema = closes[0]
            for c in closes[1:]:
                ema = c * k + ema * (1.0 - k)
            ema_distance_atr = abs(spot - ema) / atr
            if ema_distance_atr > 2.5:
                extension_penalty = min(40.0, (ema_distance_atr - 2.5) * 15.0)
                straddle_penalty += extension_penalty

        straddle_score = max(0.0, score - straddle_penalty)
        lookback = getattr(app_config, "RANGING_LOOKBACK_CANDLES", 6)
        closed_for_chop = candles[:-1] if len(candles) > 1 else candles
        alternations = self._count_candle_alternations(closed_for_chop, lookback)
        max_alt = int(getattr(app_config, "RANGING_MAX_ALTERNATIONS", 3))
        whipsaw_path_thr = float(getattr(app_config, "WHIIPSAW_PATH_RATIO_THRESHOLD", 2.8))
        whipsaw_max_er = float(getattr(app_config, "WHIIPSAW_MAX_ER", 0.32))
        chop_reason = ""
        if alternations > max_alt:
            chop_reason = f"whipsaw alternations {alternations}>{max_alt}"
        elif path_ratio >= whipsaw_path_thr and er <= whipsaw_max_er:
            chop_reason = f"choppy path={path_ratio:.1f} ER={er:.2f}"

        tradeable = (
            er >= min_er
            and abs_slope >= min_slope
            and score >= self.min_asset_score
            and not chop_reason
        )

        # Body-to-range ratio for the most recent candles (feature 3: wick quality).
        # Low value = recent candles dominated by wicks → direction being rejected.
        _bq_ratios = []
        for _c in candles[-4:]:
            _o, _h, _l, _cl = self._candle_ohlc(_c)
            if _cl > 0:
                _rng = _h - _l
                _bq_ratios.append(abs(_cl - _o) / _rng if _rng > 0 else 1.0)
        body_quality = round(sum(_bq_ratios) / len(_bq_ratios), 3) if _bq_ratios else 0.5

        # ── Choppiness Index ─────────────────────────────────────────────────
        # Standard Dreiss CI: 100 * log10(sum_ATR1 / total_range) / log10(N)
        # Ranges 0–100: >61.8 = choppy/consolidating, <38.2 = trending.
        # First bar TR = high-low (no prior close); subsequent bars use full ATR.
        try:
            _sum_tr1 = 0.0
            for _ci_i in range(len(candles)):
                _oc, _hc, _lc, _cc = self._candle_ohlc(candles[_ci_i])
                if _ci_i == 0:
                    _tr = _hc - _lc  # first bar: no prior close available
                else:
                    _prev_close = self._candle_ohlc(candles[_ci_i - 1])[3]
                    _tr = max(_hc - _lc, abs(_hc - _prev_close), abs(_lc - _prev_close))
                _sum_tr1 += _tr
            _ci_range = total_range
            _ci_n = len(candles)  # full period count per Dreiss formula
            if _ci_range > 0 and _ci_n > 1:
                choppiness_index = round(
                    100.0 * math.log10(_sum_tr1 / _ci_range) / math.log10(_ci_n),
                    2,
                )
                choppiness_index = max(0.0, min(100.0, choppiness_index))
            else:
                choppiness_index = 50.0  # neutral fallback
        except Exception:
            choppiness_index = 50.0

        # ── Spike rejection ratio ────────────────────────────────────────────
        # Fraction of candles where the body is smaller than SPIKE_BODY_THRESHOLD
        # of the full high-low range.  These are candles where price moved but was
        # batted back by wicks — directional rejection.  A high ratio means the
        # market is resisting movement in both directions: bad for straddle entries.
        _spike_thr = float(getattr(app_config, "SPIKE_BODY_THRESHOLD", 0.35))
        _spike_count = 0
        _spike_total = 0
        for _c in candles:
            _o, _h, _l, _cl = self._candle_ohlc(_c)
            _rng = _h - _l
            if _rng > 0 and _cl > 0:
                _body_ratio = abs(_cl - _o) / _rng
                _spike_total += 1
                if _body_ratio < _spike_thr:
                    _spike_count += 1
        spike_rejection_ratio = round(_spike_count / _spike_total, 3) if _spike_total > 0 else 0.0

        # ── Score reweighting: CI/ER/spike quality penalty multiplier ────────
        # Geometric-mean factor so ANY one bad dimension (chop, low ER, heavy
        # spikes) crushes the whole score — chop/spike dominate the ranking.
        quality_factor = 1.0
        adj_straddle_score = straddle_score
        if getattr(app_config, "SCORE_REWEIGHT_ENABLED", False):
            quality_factor = pattern_quality_factor(
                choppiness_index=choppiness_index,
                efficiency_ratio=er,
                spike_rejection_ratio=spike_rejection_ratio,
                er_target=getattr(app_config, "SCORE_REWEIGHT_ER_TARGET", 0.5),
            )
            adj_straddle_score = ph_adjusted_score(straddle_score, quality_factor)

        return {
            "score": round(max(0.0, score), 1),
            "straddle_score": round(straddle_score, 1),
            "adj_straddle_score": round(adj_straddle_score, 2),
            "quality_factor": round(quality_factor, 3),
            "choppiness_index": choppiness_index,
            "tradeable": tradeable,
            "efficiency_ratio": round(er, 3),
            "abs_slope": round(abs_slope, 1),
            "slope_signed": round(normalized_slope, 1),
            "slope_atr_ratio": round(slope_atr_ratio, 3),
            "active_ratio": round(active_ratio, 2),
            "range_pct": round(range_pct * 100, 4),
            "path_ratio": round(path_ratio, 2),
            "alternations": alternations,
            "chop_reason": chop_reason,
            "candles": len(closes),
            "atr": round(atr, 6),
            "body_quality": body_quality,
            "spike_rejection_ratio": spike_rejection_ratio,
        }

    def _live_entry_snapshot(self, asset_name, count=20):
        if not self.api:
            return None
        try:
            candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, count, time.time())
            return entry_snapshot_from_candles(
                candles,
                min_candle_body_pct=self.min_candle_body_pct,
                min_session_range_pct=self.min_session_range_pct,
            )
        except Exception:
            return None

    def _capture_entry_snapshot_at_placement(self):
        """Record chart state at the moment CALL+PUT are sent (used in trade log)."""
        snap = self._live_entry_snapshot(self.asset)
        self.last_entry_snapshot = copy_entry_snapshot(snap)
        self.last_entry_capture_ts = time.time() if self.last_entry_snapshot else None

    def _calculate_adx(self, candles, period=14):
        if len(candles) <= period:
            return 25.0
        
        tr_list, p_dm, n_dm = [], [], []
        for i in range(1, len(candles)):
            _, h1, l1, c1 = self._candle_ohlc(candles[i-1])
            _, h2, l2, c2 = self._candle_ohlc(candles[i])
            
            tr = max(h2 - l2, abs(h2 - c1), abs(l2 - c1))
            up_m = h2 - h1
            dn_m = l1 - l2
            
            pdm = up_m if up_m > dn_m and up_m > 0 else 0
            ndm = dn_m if dn_m > up_m and dn_m > 0 else 0
            
            tr_list.append(tr)
            p_dm.append(pdm)
            n_dm.append(ndm)
            
        # Wilder's Smoothing approximation
        atr = sum(tr_list[:period])
        spdm = sum(p_dm[:period])
        sndm = sum(n_dm[:period])
        
        if atr == 0:
            return 0.0
            
        dx_list = []
        for i in range(period, len(tr_list)):
            atr = atr - (atr / period) + tr_list[i]
            spdm = spdm - (spdm / period) + p_dm[i]
            sndm = sndm - (sndm / period) + n_dm[i]
            
            if atr == 0:
                dx = 0
            else:
                pdi = 100 * spdm / atr
                ndi = 100 * sndm / atr
                dx = 100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0
            dx_list.append(dx)
            
        if not dx_list:
            return 0.0
            
        adx = sum(dx_list[:period]) / len(dx_list[:period]) if len(dx_list) >= period else sum(dx_list)/len(dx_list)
        for i in range(period, len(dx_list)):
            adx = ((adx * (period - 1)) + dx_list[i]) / period
            
        return adx

    def _evaluate_candle_follow(self, asset_name):
        result = {"tradeable": False, "reason": "", "direction": None}
        lookback = getattr(app_config, "RANGING_LOOKBACK_CANDLES", 6)
        max_alt = getattr(app_config, "RANGING_MAX_ALTERNATIONS", 3)
        adx_period = 14
        
        try:
            tf = int(getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60))
            candles = self._get_candles_safe(asset_name, tf, adx_period * 2 + lookback, time.time())
            if not candles or len(candles) < lookback + 1:
                result["reason"] = f"Not enough {tf}s candles"
                return result
                
            closed_candles = candles[:-1] if len(candles) > 1 else candles
            if not closed_candles:
                result["reason"] = "No closed candles"
                return result

            # ── Choppiness / whipsaw filter ──────────────────────────────────
            if self.chop_filter_enabled:
                from market_metrics import is_choppy_market
                chop = is_choppy_market(
                    closed_candles,
                    ci_period=self.chop_ci_period,
                    ci_threshold=self.chop_ci_threshold,
                    min_efficiency_ratio=self.chop_min_er,
                )
                if chop["choppy"]:
                    self.status_note = (
                        f"🌊 {asset_name}: choppy market — "
                        f"CI={chop['choppiness_index']} ({'ER' if chop['er_flag'] else 'CI'} triggered) "
                        f"ER={chop['efficiency_ratio_recent']} — skipping this candle"
                    )
                    logger.info(
                        f"⏭️ Skipping {asset_name}: choppy market "
                        f"(CI={chop['choppiness_index']}, ER={chop['efficiency_ratio_recent']})"
                    )
                    self._log_gate_rejection(
                        "chop_filter",
                        "choppy_market",
                        ci=chop["choppiness_index"],
                        er=chop["efficiency_ratio_recent"],
                    )
                    result["tradeable"] = False
                    result["reason"] = "choppy_market"
                    result["choppiness_index"] = chop["choppiness_index"]
                    result["efficiency_ratio_recent"] = chop["efficiency_ratio_recent"]
                    return result

            adx_value = self._calculate_adx(closed_candles, adx_period)

            recent_candles = closed_candles[-lookback:]
            colors = []
            for c in recent_candles:
                _, _, _, close_p = self._candle_ohlc(c)
                open_p = float(c.get("open", 0))
                colors.append(close_p >= open_p)
                
            alternations = 0
            for i in range(1, len(colors)):
                if colors[i] != colors[i-1]:
                    alternations += 1
                    
            last_color = colors[-1]
            candle_dir = "call" if last_color else "put"
            result["direction"] = candle_dir
            result["candle_color"] = "green" if last_color else "red"
            result["alternations"] = alternations
            result["adx"] = round(adx_value, 1)
            # Active ladder: always trade — candle follow is mandatory once a pair is chosen.
            result["tradeable"] = True
            result["reason"] = "Passed"
            if alternations > max_alt:
                result["chop_advisory"] = (
                    f"whipsaw advisory: {alternations}/{lookback - 1} alternations (scan max {max_alt})"
                )
            
            base = self._score_asset_movement(asset_name)
            if base:
                scan_tradeable = base.get("tradeable")
                chop_reason = base.get("chop_reason") or ""
                result.update(base)
                # Fix: re-assert candle direction AFTER update — _score_asset_movement
                # does not return a direction key today, but if it ever does this
                # prevents it from silently overriding the closed-candle direction.
                result["direction"] = candle_dir
                result["tradeable"] = True
                result["reason"] = "Passed"
                if chop_reason and not result.get("chop_advisory"):
                    result["chop_advisory"] = f"scan chop: {chop_reason}"
                result["scan_would_skip"] = not scan_tradeable

                # ── Slope-alignment guard ────────────────────────────────────────
                # If the 15-candle regression slope is strongly directional AND
                # disagrees with the single closed candle, trust the slope.
                # Threshold defaults: slope ≥ 20 pips AND ER ≥ 0.45.
                # This catches the "single green bounce candle inside a heavy
                # downtrend" pattern that candle-color alone cannot see.
                _sa_min_slope = float(getattr(app_config, "SLOPE_ALIGN_MIN_SLOPE", 20.0))
                _sa_min_er = float(getattr(app_config, "SLOPE_ALIGN_MIN_ER", 0.45))
                _slope_signed = base.get("slope_signed", 0.0)
                _er = base.get("efficiency_ratio", 0.0)
                _slope_dir = "call" if _slope_signed > 0 else "put"
                if (
                    abs(_slope_signed) >= _sa_min_slope
                    and _er >= _sa_min_er
                    and _slope_dir != candle_dir
                ):
                    logger.warning(
                        f"⚠️ Slope-align override on {asset_name}: "
                        f"candle={candle_dir.upper()} but slope={_slope_signed:.1f} "
                        f"ER={_er:.3f} → using {_slope_dir.upper()} "
                        f"(threshold slope≥{_sa_min_slope} ER≥{_sa_min_er})"
                    )
                    result["direction"] = _slope_dir
                    result["candle_dir_overridden"] = True
                    result["slope_override_reason"] = (
                        f"slope={_slope_signed:.1f} er={_er:.3f} "
                        f"overrides candle={candle_dir.upper()}"
                    )
            else:
                result.update({
                    "straddle_score": 0.0,
                    "efficiency_ratio": 0.0,
                    "slope": 0.0,
                    "abs_slope": 0.0
                })
            
            chop_note = f" | {result['chop_advisory']}" if result.get("chop_advisory") else ""
            final_dir = result.get("direction", candle_dir)
            override_note = (
                f" → SLOPE-OVERRIDE→{final_dir.upper()}"
                if result.get("candle_dir_overridden") else ""
            )
            logger.info(
                f"Candle Follow {asset_name}: last closed {result['candle_color'].upper()}→"
                f"{candle_dir.upper()}{override_note}, ADX {adx_value:.1f}, "
                f"Alts: {alternations}/{lookback - 1}{chop_note}"
            )
            return result
        except Exception as e:
            logger.error(f"Candle Follow evaluation failed: {e}")
            result["reason"] = "Evaluation error"
            return result

    def _assess_straddle_suitability(self, asset_name, call_info=None, put_info=None, check_momentum=False):
        """
        Single gate for straddle trading: chop (ER), direction, liquidity, strikes, momentum.
        Used when ranking pairs and immediately before placing orders.
        """
        base = self._score_asset_movement(asset_name)
        if not base:
            return {
                "asset": asset_name,
                "tradeable": False,
                "reason": "no candle data",
                "straddle_score": 0.0,
                "efficiency_ratio": 0.0,
                "abs_slope": 0.0,
            }

        er = base["efficiency_ratio"]
        abs_slope = base["abs_slope"]
        atr = base.get("atr", 0.0)
        gates = self._straddle_gate_thresholds(asset_name)
        min_er = gates["min_efficiency_ratio"]
        min_slope = gates["min_directional_slope"]
        failures = []

        chop_reason = base.get("chop_reason") or ""
        if chop_reason:
            failures.append(chop_reason)

        if er < min_er:
            failures.append(f"choppy market (ER {er:.3f} < {min_er})")
        if abs_slope < min_slope:
            failures.append(f"too flat (slope {abs_slope:.1f} < {min_slope})")
        min_move = gates.get("min_movement_score")
        if min_move is not None and base["score"] < min_move:
            failures.append(f"movement score {base['score']:.0f} < learned {min_move:.0f}")
        elif base["score"] < self.min_asset_score:
            failures.append(f"low movement score {base['score']:.0f}")

        snap = self._live_entry_snapshot(asset_name)
        min_mom = gates.get("min_momentum_ratio")
        if min_mom is not None and snap is not None:
            mom = snap.get("momentum_ratio", 0)
            if mom < min_mom:
                failures.append(f"momentum weak ({mom:.2f} < {min_mom})")
        max_doji = gates.get("max_doji_streak")
        if max_doji is not None and snap is not None:
            doji = snap.get("doji_streak", 0)
            if doji > max_doji:
                failures.append(f"{doji} doji candles (max {max_doji})")

        spot = None
        if asset_name == self.asset:
            with self._price_lock:
                prices = self._price_data.get(60, [])
            spot = self._estimate_spot_price(prices)
        if not spot and self.api:
            candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, max(5, 3), time.time())
            if candles:
                spot = float(candles[-1].get("close", 0) or 0)
        if spot and spot > 0 and atr > 0:
            if atr < spot * 0.00020:
                failures.append(f"dead market (ATR {atr:.6f})")
            if call_info and put_info:
                call_distance = abs(call_info["strike"] - spot)
                put_distance = abs(spot - put_info["strike"])
                nearest = min(call_distance, put_distance)
                if nearest > atr * 3.5:
                    failures.append("strikes too far from spot for current ATR")

        if call_info and put_info and spot and spot > 0:
            try:
                candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, 5, time.time())
                if candles and len(candles) >= 3:
                    recent = candles[-3:]
                    highs = [float(c.get("max", c.get("high", 0))) for c in recent]
                    lows = [float(c.get("min", c.get("low", 0))) for c in recent]
                    if max(highs) < call_info["strike"] and min(lows) > put_info["strike"]:
                        failures.append("zigzag: price trapped between strikes")
            except Exception:
                pass

        # Horizontal Zigzag Channel Detection (Price trapped in a very tight flat channel)
        # Only blocks when: slope is very flat AND the channel is narrow (< 0.12% of price).
        # Wide channels are still tradeable even when the long-term slope is flat.
        if spot and spot > 0:
            try:
                long_slope, _ = self._calculate_trend_metrics(asset_name, spot, count=35)
                # If the overall 35-min trend is extremely flat (well below the min slope)
                if abs(long_slope) < max(5.0, min_slope * 0.35):
                    candles_35 = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, 35, time.time())
                    if candles_35 and len(candles_35) >= 30:
                        hist_candles = candles_35[:-3]
                        highs_hist = [float(c.get("max", c.get("high", c.get("close", 0)))) for c in hist_candles]
                        lows_hist = [float(c.get("min", c.get("low", c.get("close", 0)))) for c in hist_candles]
                        if highs_hist and lows_hist:
                            channel_high = max(highs_hist)
                            channel_low = min(lows_hist)
                            channel_range_pct = (channel_high - channel_low) / spot if spot > 0 else 1.0
                            # Only block if the channel is genuinely tight — wide flat channels are tradeable
                            if channel_low <= spot <= channel_high and channel_range_pct < 0.0012:
                                failures.append(f"horizontal zigzag (tight flat channel {channel_range_pct*100:.3f}% range)")
            except Exception:
                pass

        if check_momentum:
            accel, recent_atr, older_atr = self._is_momentum_accelerating(asset_name)
            if not accel:
                failures.append("momentum fading")

        # Spike Exhaustion Detection — block entry when the most recent
        # candle(s) are parabolic relative to the prior average.
        # This prevents buying at the top of a spike that's about to reverse.
        if self.api and spot and spot > 0:
            try:
                spike_candles = self._get_candles_safe(asset_name, app_config.FOLLOW_CANDLE_TIMEFRAME, 10, time.time())
                if spike_candles and len(spike_candles) >= 6:
                    bodies = []
                    for c in spike_candles:
                        o, h, l, cl = self._candle_ohlc(c)
                        if cl > 0:
                            bodies.append(abs(cl - o))
                    if len(bodies) >= 6:
                        # Compare the last candle body to the average of the prior candles
                        last_body = bodies[-1]
                        prior_avg = sum(bodies[:-2]) / len(bodies[:-2])  # exclude last 2
                        if prior_avg > 0 and last_body >= prior_avg * 4.0:
                            failures.append(
                                f"spike exhaustion (last candle body {last_body:.6f} "
                                f"is {last_body/prior_avg:.1f}x avg {prior_avg:.6f})"
                            )
                        # Also check if the last 2 candles combined are a parabolic run
                        if len(bodies) >= 3:
                            last_two_body = bodies[-1] + bodies[-2]
                            prior_avg_3 = sum(bodies[:-3]) / len(bodies[:-3]) if len(bodies) > 3 else prior_avg
                            if prior_avg_3 > 0 and last_two_body >= prior_avg_3 * 6.0:
                                failures.append(
                                    f"parabolic run (last 2 candles {last_two_body:.6f} "
                                    f"is {last_two_body/prior_avg_3:.1f}x avg {prior_avg_3:.6f})"
                                )
            except Exception:
                pass

        skip, skip_reason = self._check_market_skip_signals(asset_name)
        if skip:
            failures.append(skip_reason)

        tradeable = len(failures) == 0
        reason = "straddle OK" if tradeable else "; ".join(failures)
        return {
            "asset": asset_name,
            "tradeable": tradeable,
            "reason": reason,
            "straddle_score": base["straddle_score"],
            "efficiency_ratio": er,
            "abs_slope": abs_slope,
            "movement_score": base["score"],
            "atr": atr,
            "body_quality": base.get("body_quality", 0.5),
            "momentum_ratio": snap.get("momentum_ratio", 1.0) if snap else 1.0,
        }

    def _resolve_asset_candidates(self):
        now_utc = datetime.datetime.utcnow()
        expired_penalties = [a for a, t in self.asset_penalty_box.items() if now_utc >= t]
        for a in expired_penalties:
            del self.asset_penalty_box[a]

        try:
            profit_dict = self.api.get_all_profit()
        except:
            profit_dict = {}

        def resolve_name(name):
            if name in self.avoid_markets or name in self.asset_penalty_box:
                return None
            
            # Check if base name is open/has profit
            if profit_dict.get(name) and OP_code.ACTIVES.get(name):
                return name
                
            # Check fallbacks for closed assets
            for suffix in ["-OTC", "-op", "-OTC-op"]:
                alt_name = f"{name}{suffix}"
                if profit_dict.get(alt_name) and OP_code.ACTIVES.get(alt_name):
                    return alt_name
                    
            # Fallback if profit dict is missing but OP_code has it
            if OP_code.ACTIVES.get(name):
                return name
            return None

        # For turbo mode, use the configured asset_candidates directly
        if self.trading_mode == "turbo":
            if self.asset_candidates:
                pool = []
                for name in self.asset_candidates:
                    resolved = resolve_name(name)
                    if resolved and resolved not in pool:
                        pool.append(resolved)
                if pool:
                    return pool
            resolved_asset = resolve_name(self.asset)
            return [resolved_asset] if resolved_asset else []

        # Digital mode: use open digital pairs from IQ schedule
        open_digital = self._get_open_digital_asset_names()
        if open_digital:
            pool = []
            for name in open_digital:
                resolved = resolve_name(name)
                if resolved and resolved not in DIGITAL_UNSUPPORTED_ASSETS and resolved not in pool:
                    pool.append(resolved)
            if pool:
                return pool

        pool = []
        for name in self._build_actives_otc_fallback_pool():
            resolved = resolve_name(name)
            if resolved and resolved not in DIGITAL_UNSUPPORTED_ASSETS and resolved not in pool:
                pool.append(resolved)

        if not pool:
            resolved_asset = resolve_name(self.asset)
            if resolved_asset:
                pool = [resolved_asset]
        return pool

    def _asset_rank_score(self, data):
        """Primary asset rank.

        In directional_trend mode a cleanly-trending pair should beat a merely-
        volatile one.  Pure straddle_score cannot distinguish between a pair
        moving strongly *with* the predicted direction versus against it — ER
        captures that directional clarity.  We keep straddle as the base (it
        gates minimum tradeable movement) and multiply by (1 + ER) so a pair
        with ER=0.8 scores 80 % higher than the same straddle at ER=0.

        When SCORE_REWEIGHT_ENABLED, the quality-adjusted straddle score is used
        instead so chop/spike-heavy pairs are penalised at ranking time.
        """
        raw_straddle = float(data.get("straddle_score", data.get("score", 0)) or 0)
        if getattr(app_config, "SCORE_REWEIGHT_ENABLED", False):
            straddle = float(data.get("adj_straddle_score", raw_straddle) or raw_straddle)
        else:
            straddle = raw_straddle
        if getattr(self, "strategy_mode", "") == "directional_trend":
            er = float(data.get("efficiency_ratio", 0) or 0)
            return straddle * (1.0 + er)
        return straddle

    # ── Multi-asset helpers ──────────────────────────────────────────────────

    def _extract_currency_codes(self, asset_name: str) -> list:
        """Return the major currency codes found in an asset name."""
        MAJORS = {"EUR", "GBP", "USD", "JPY", "AUD", "CAD", "CHF", "NZD"}
        base = asset_name.upper().replace("-OTC", "").replace("-OP", "")
        return [c for c in MAJORS if c in base]

    def _assets_are_correlated(self, a: str, b: str) -> bool:
        """True if two assets share at least one major currency (likely correlated)."""
        return bool(set(self._extract_currency_codes(a)) & set(self._extract_currency_codes(b)))

    def _select_best_asset(self, relaxed=False):
        candidates = self._resolve_asset_candidates()
        if not candidates:
            return self.asset, {}

        mode_label = "turbo" if self.trading_mode == "turbo" else "digital"
        logger.info(
            f"Scanning {len(candidates)} {mode_label} pairs for asset suitability..."
        )
        scores = {}
        for name in candidates:
            if not self.running:
                return self.asset, scores
            result = self._score_asset_movement(name)
            if result:
                scores[name] = result
            else:
                scores[name] = {
                    "score": 0.0,
                    "straddle_score": 0.0,
                    "tradeable": False,
                    "efficiency_ratio": 0.0,
                    "abs_slope": 0.0,
                    "active_ratio": 0,
                    "range_pct": 0,
                    "path_ratio": 0,
                }

        self.asset_scores = scores
        if not scores:
            return self.asset, scores

        tradeable = {k: v for k, v in scores.items() if v.get("tradeable")}
        if not tradeable:
            if relaxed:
                ranked = sorted(
                    scores.items(),
                    key=lambda x: self._asset_rank_score(x[1]),
                    reverse=True,
                )
                if ranked:
                    best_relaxed, data = ranked[0]
                    logger.warning(
                        f"No pair fully tradeable — relaxed pick {best_relaxed} "
                        f"(straddle {data.get('straddle_score', 0):.0f}, "
                        f"ER {data.get('efficiency_ratio', 0):.2f})"
                    )
                    return best_relaxed, scores
            logger.warning("No pairs pass straddle gates (chop/flat/low movement).")
            if self._is_asset_penalty_blocked():
                return next(iter(scores.keys()), self.asset), scores
            return self.asset, scores

        ranked = sorted(
            tradeable.items(),
            key=lambda x: self._asset_rank_score(x[1]),
            reverse=True,
        )
        top5 = ranked[:5]
        _reweight_on = getattr(app_config, "SCORE_REWEIGHT_ENABLED", False)
        logger.info(
            "Top pairs (rank/straddle"
            + ("/adj" if _reweight_on else "")
            + "/ER): "
            + ", ".join(
                f"{name}={self._asset_rank_score(data):.0f}"
                f"/S{data.get('straddle_score', data['score']):.0f}"
                + (
                    f"/A{data.get('adj_straddle_score', data.get('straddle_score', data['score'])):.1f}"
                    f"/Q{data.get('quality_factor', 1.0):.2f}"
                    if _reweight_on else ""
                )
                + f"/ER{data.get('efficiency_ratio', 0):.2f}"
                for name, data in top5
            )
        )

        return ranked[0][0], scores

    def _switch_trading_asset(self, new_asset):
        if new_asset == self.asset or not OP_code.ACTIVES.get(new_asset):
            return False
        if self.api and self.connected:
            self._unsubscribe(self.asset_id)
        old = self.asset
        self.asset = new_asset
        self.asset_id = OP_code.ACTIVES.get(new_asset, 0)
        with self._price_lock:
            self._price_data.clear()
        self._subscribed = False
        
        if self.api and self.connected and app_config.USE_TRADER_MOOD:
            try:
                self.api.stop_mood_stream(old, instrument="turbo-option" if self.trading_mode == "turbo" else "digital-option")
                self.api.start_mood_stream(self.asset, instrument="turbo-option" if self.trading_mode == "turbo" else "digital-option")
            except Exception as e:
                logger.debug(f"Mood stream switch error: {e}")

        logger.info(f"Asset switched: {old} -> {new_asset} (id={self.asset_id})")

        if self.api and self.connected:
            if self.trading_mode == "turbo":
                # Turbo assets don't have digital-option price splitters.
                # Immediately seed price data from candles so the feed is
                # available without waiting for a websocket event.
                self._seed_price_data_from_candles(new_asset)
            else:
                self._subscribe()
                self._subscribed = True
        return True

    def _seed_price_data_from_candles(self, asset=None):
        """Populate _price_data[60] from REST candle history for turbo mode."""
        target = asset or self.asset
        try:
            candles = self.api.get_candles(target, app_config.FOLLOW_CANDLE_TIMEFRAME, 30, time.time())
            if candles and len(candles) >= 5:
                with self._price_lock:
                    self._price_data[app_config.FOLLOW_CANDLE_TIMEFRAME] = candles
                logger.info(f"Seeded price feed from {len(candles)} candles for {target}")
                return True
        except Exception as e:
            logger.warning(f"Could not seed candle data for {target}: {e}")
        return False

    def _wait_for_price_data(self, timeout=30):
        for _ in range(timeout):
            if not self.running:
                return False
            with self._price_lock:
                if 60 in self._price_data:
                    return True
            # In turbo mode, try seeding from candles if websocket hasn't arrived
            if self.trading_mode == "turbo":
                if self._seed_price_data_from_candles():
                    return True
            if not self._interruptible_sleep(1):
                return False
        return False

    def _has_price_feed(self, period=app_config.FOLLOW_CANDLE_TIMEFRAME):
        with self._price_lock:
            prices = self._price_data.get(period, [])
        if prices:
            return True
        # In turbo mode, fall back to seeding from candles
        if self.trading_mode == "turbo":
            return self._seed_price_data_from_candles()
        return False

    def _in_active_ladder(self):
        return self.session_active and self.session_round_count > 0

    def _apply_auto_asset_selection(self, reason="scheduled", relaxed=False):
        if not self.auto_select_asset:
            return

        # Hot-pair loyalty gate: if the current pair is on a winning streak and
        # still tradeable, stay on it — don't switch just because another pair
        # scored higher this candle.
        _HOT_PAIR_MIN_WINS = 2
        _loyalty_override_reasons = {
            "rejection penalty",
            "penalty box",
            "tier exhausted penalty",
            "recovery debt chipping",
            "pair performance degradation",
            # After every win the bot must rescan — "trading start" is that trigger.
            # Hot-pair loyalty must not suppress it; the scan may still land back on
            # the same pair if it genuinely ranks highest.
            "trading start",
        }
        if (
            self._hot_pair
            and self._hot_pair == self.asset
            and self._hot_pair_consecutive_wins >= _HOT_PAIR_MIN_WINS
            and reason not in _loyalty_override_reasons
            and self.session_round_count == 0
        ):
            self.last_asset_selection_note = (
                f"🔥 Hot pair loyalty — {self.asset} "
                f"({self._hot_pair_consecutive_wins}W streak)"
            )
            logger.info(self.last_asset_selection_note)
            return

        # STRICT CANDLE FOLLOW: Never switch pairs mid-ladder.
        # Exception: penalty box (sliding-window loss rule) and step-4 retry must switch.
        _mid_ladder_bypass = {"trading start", "step 4 rotation retry", "penalty box"}
        if self._in_active_ladder() and reason not in _mid_ladder_bypass:
            logger.info(f"🔒 Locked to {self.asset} for the entire tier (step {self.session_round_count+1}).")
            return

        best, scores = self._select_best_asset(relaxed=relaxed)
        if not scores:
            self.last_asset_selection_note = "No candle data; keeping current asset."
            logger.warning(self.last_asset_selection_note)
            return

        ranked = sorted(
            scores.items(),
            key=lambda x: self._asset_rank_score(x[1]),
            reverse=True,
        )
        summary = ", ".join(
            f"{k}=S{v.get('straddle_score', v['score']):.0f}/ER{v.get('efficiency_ratio', 0):.2f}"
            for k, v in ranked[:8]
        )
        logger.info(f"Asset analysis ({reason}): {summary}")

        best_data = scores.get(best, {})
        if not best_data.get("tradeable") and not relaxed:
            self.last_asset_selection_note = (
                f"No tradeable pairs (best {best} ER="
                f"{best_data.get('efficiency_ratio', 0):.2f}). Waiting."
            )
            logger.warning(self.last_asset_selection_note)
            return

        best_score = best_data.get("straddle_score", best_data["score"])
        if best != self.asset:
            self._switch_trading_asset(best)
            self._wait_for_price_data()
            self._pair_filter_skip_streak[self.asset] = 0
            self.last_asset_selection_note = (
                f"Selected {best} (straddle {best_score:.0f}, "
                f"ER {best_data.get('efficiency_ratio', 0):.2f}) — {summary}"
            )
        else:
            self.last_asset_selection_note = (
                f"Keeping {best} (straddle {best_score:.0f}) — {summary}"
            )
        logger.info(self.last_asset_selection_note)

    def _switch_to_next_tradeable_pair(self, reason, relaxed=False):
        _switch_bypass = {"step 4 rotation retry", "penalty box"}
        if self._in_active_ladder() and reason not in _switch_bypass:
            logger.info(
                f"Mid-ladder lock — cannot switch from {self.asset} "
                f"(step {self.session_round_count + 1}); reason was: {reason}"
            )
            return False
        candidates = self._resolve_asset_candidates()
        ranked = []
        for name in candidates:
            if name == self.asset:
                continue
            movement = self._score_asset_movement(name)
            if not movement:
                continue
            if movement.get("tradeable") or relaxed:
                ranked.append((name, movement["straddle_score"]))
        ranked.sort(key=lambda x: x[1], reverse=True)
        if not ranked:
            logger.warning(f"No alternative tradeable pair after: {reason}")
            return False
        new_asset = ranked[0][0]
        if self._switch_trading_asset(new_asset):
            self._pair_filter_skip_streak[new_asset] = 0
            logger.info(
                f"Switched to {new_asset} (straddle {ranked[0][1]:.0f}) — {reason}"
            )
            return True
        return False

    def _abandon_untradeable_pair(self, reason):
        # DEPRECATED — no longer called. Pair switching on quality failure was removed.
        # Pairs are committed at ladder start and held until a win triggers a fresh scan.
        # Kept to avoid breaking any persisted state or external references.
        logger.warning(
            f"_abandon_untradeable_pair called unexpectedly for {self.asset} ({reason}). "
            "This path should not be reached — quality failures now wait, not switch."
        )

    @staticmethod
    def _is_pair_condition_failure(reason):
        if not reason:
            return False
        r = reason.lower()
        markers = (
            "choppy",
            "chop",
            "efficiency",
            "too flat",
            "ranging",
            "dead market",
            "low movement",
            "zigzag",
            "strikes too far",
            "momentum fading",
            "doji",
            "tight",
            "straddle ok",
        )
        if "straddle ok" in r:
            return False
        return any(m in r for m in markers if m != "straddle ok")

    def _log_gate_rejection(self, category: str, reason: str, **metrics):
        """Append a gate rejection entry to the rolling in-memory log."""
        entry = {
            "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "asset": getattr(self, "asset", "?"),
            "category": category,
            "reason": reason,
        }
        for k, v in metrics.items():
            try:
                entry[k] = round(float(v), 3) if v is not None else None
            except (TypeError, ValueError):
                entry[k] = None
        self._gate_rejection_log.appendleft(entry)

    def _handle_trade_gate_failure(self, reason):
        """Pair committed — wait for conditions to improve, never switch mid-recovery.

        Pair switching happens only after a WIN (at the next 'trading start' rescan).
        If conditions are choppy or flat on the committed pair, wait for the next
        candle rather than abandoning it. The pair was already vetted by the initial
        scan before the ladder started.
        """
        label = (reason or "quality gate").replace("_", " ")
        self.status_note = f"⏳ {self.asset}: {label} — waiting for conditions to improve"

        if not self.auto_select_asset:
            self.last_asset_selection_note = (
                f"{self.asset}: {label}. Change pair or enable auto-select."
            )
            logger.warning(self.last_asset_selection_note)
            if not self._interruptible_sleep(90):
                return
            return

        logger.warning(
            f"{self.asset} unsuitable ({reason}) at step {self.session_round_count + 1} "
            f"— waiting for next candle (pair switch happens after a win)"
        )
        self._skip_to_next_entry_window(reason)

    def _ensure_tradeable_market(self):
        """Called only at step 1 — safe to auto-select since no trades are in play."""
        if not self.asset:
            if self.auto_select_asset:
                self._apply_auto_asset_selection(reason="initial selection", relaxed=True)
            else:
                return False

        quality = self._assess_straddle_suitability(self.asset, check_momentum=False)
        self.last_pair_quality = quality
        if quality["tradeable"]:
            self._pair_filter_skip_streak[self.asset] = 0
            return True

        # Risky-pair ER floor at step 1 only (scan gate — does not block mid-ladder candle follow).
        pair_floors = getattr(app_config, "ASSET_MIN_ER", {})
        if self.asset in pair_floors:
            er_floor = max(self.rule_gate_min_er, pair_floors[self.asset])
            live_er = float(quality.get("efficiency_ratio", 0) or 0)
            if live_er < er_floor:
                logger.warning(
                    f"{self.asset} ER {live_er:.2f} below risky-pair floor {er_floor:.2f} at step 1"
                )
                if self.auto_select_asset:
                    self._apply_auto_asset_selection(
                        reason=f"ER {live_er:.2f} < floor {er_floor:.2f}", relaxed=True
                    )
                    quality = self._assess_straddle_suitability(self.asset, check_momentum=False)
                    self.last_pair_quality = quality
                    if quality.get("tradeable"):
                        self._pair_filter_skip_streak[self.asset] = 0
                        return True

        # Step 1: no trades placed on this tier yet, safe to switch.
        if self.auto_select_asset:
            self._apply_auto_asset_selection(
                reason="untradeable conditions", relaxed=True
            )
            quality = self._assess_straddle_suitability(self.asset, check_momentum=False)
            self.last_pair_quality = quality
            if quality["tradeable"]:
                self._pair_filter_skip_streak[self.asset] = 0
                return True

        self.last_asset_selection_note = (
            f"{self.asset} not tradeable: {quality.get('reason', 'unknown')}. "
            "Change pair or enable auto-select."
        )
        logger.warning(self.last_asset_selection_note)
        return False

    # ── Strike Selection ─────────────────────────────────────────────────────

    @staticmethod
    def _profit_pct_from_ask(ask):
        if ask is None or ask <= 0:
            return None
        return ((100 - ask) * 100) / ask

    def _estimate_spot_price(self, prices):
        if not prices:
            return None

        # Try candle OHLC format first (turbo mode seeded from get_candles)
        first = prices[0] if prices else {}
        if "close" in first or "open" in first:
            # It's candle data — use close price of the most recent candle
            try:
                spot = float(prices[-1].get("close", 0) or 0)
                return spot if spot > 0 else None
            except (TypeError, ValueError):
                return None

        # Digital-option price format: find ATM strike where call_ask ≈ put_ask
        best_strike = None
        best_diff = float("inf")
        for entry in prices:
            strike_raw = entry.get("strike", "0")
            if strike_raw == "SPT":
                continue
            try:
                strike_val = float(strike_raw)
            except ValueError:
                continue
            call_ask = entry.get("call", {}).get("ask")
            put_ask = entry.get("put", {}).get("ask")
            if call_ask is None or put_ask is None:
                continue
            diff = abs(call_ask - put_ask)
            if diff < best_diff:
                best_diff = diff
                best_strike = strike_val
        return best_strike


    def _strike_leg_candidate(self, entry, side, strike_val, for_entry_timing, now):
        data = entry.get(side, {})
        ask = data.get("ask")
        symbol = data.get("symbol", "")
        if ask is None or ask <= 0 or not symbol:
            return None
        if for_entry_timing and not self._strike_in_expiry_window(symbol, now=now):
            return None
        profit_pct = self._profit_pct_from_ask(ask)
        if profit_pct is None:
            return None
        if not (self.min_profit_pct <= profit_pct <= self.max_profit_pct):
            return None
        return {
            "symbol": symbol,
            "strike": strike_val,
            "ask": ask,
            "profit_pct": profit_pct,
        }

    def _sorted_strike_ladder(self, prices):
        """Unique strike levels from the live feed, sorted ascending."""
        levels = []
        for entry in prices or []:
            strike_raw = entry.get("strike", "0")
            if strike_raw == "SPT":
                continue
            try:
                levels.append(float(strike_raw))
            except ValueError:
                continue
        return sorted(set(levels))

    def _strike_entry_map(self, prices):
        by_strike = {}
        for entry in prices or []:
            strike_raw = entry.get("strike", "0")
            if strike_raw == "SPT":
                continue
            try:
                by_strike[float(strike_raw)] = entry
            except ValueError:
                continue
        return by_strike

    def _diagnose_expiry_in_feed(self, prices, now=None):
        if now is None:
            now = self._server_now()
        counts = {"under_min": 0, "in_window": 0, "over_max": 0, "no_symbol": 0, "parse_error": 0}
        samples = []
        for entry in prices or []:
            for side in ("call", "put"):
                sym = entry.get(side, {}).get("symbol", "")
                if not sym:
                    continue
                try:
                    secs = self._seconds_to_expiry(sym, now=now)
                except Exception:
                    counts["parse_error"] += 1
                    continue
                if secs < self.min_seconds_to_expiry:
                    counts["under_min"] += 1
                elif secs > self.max_seconds_to_expiry:
                    counts["over_max"] += 1
                else:
                    counts["in_window"] += 1
                    if len(samples) < 5:
                        samples.append(f"{secs:.0f}s")
        logger.warning(
            f"Expiry scan (server :{self._server_second():02d}): "
            f"in_window={counts['in_window']} "
            f"under_min={counts['under_min']} "
            f"over_max={counts['over_max']} "
            f"(need {self.min_seconds_to_expiry}-{self.max_seconds_to_expiry}s) "
            f"samples={samples}"
        )

    def _calculate_ema(values, period=15):
        if not values or len(values) < 2:
            return 0.0
        k = 2.0 / (period + 1.0)
        ema = float(values[0])
        for val in values[1:]:
            ema = float(val) * k + ema * (1.0 - k)
        return ema

    def _determine_trend_direction_UNUSED(self, last_direction=None):
        """
        DEAD CODE — never called. Retained for reference only; do not reconnect.
        Direction is now determined exclusively by _evaluate_candle_follow (closed-candle
        color + slope-alignment guard). All EMA/ATR/reversal logic below is orphaned.
        """
        with self._price_lock:
            prices = self._price_data.get(60, [])
        spot = self._estimate_spot_price(prices)
        
        # In simulation/fallback where no price array exists
        if not self.api or spot is None or spot <= 0:
            # Try to fetch candles to calculate slope/ema
            try:
                candles = self.api.get_candles(self.asset, app_config.FOLLOW_CANDLE_TIMEFRAME, 20, time.time())
                if candles:
                    spot = float(candles[-1].get("close", 0) or 0)
            except Exception:
                pass
            if not spot or spot <= 0:
                return last_direction or "call"

        slope, er = self._calculate_trend_metrics(self.asset, spot, count=15)
        logger.info(f"Trend metrics: slope={slope:.2f}, ER={er:.3f}")
        self._last_direction_flip_kind = None

        # Standard trend checks
        is_uptrend = slope >= 15.0 and er >= 0.25
        is_downtrend = slope <= -15.0 and er >= 0.25

        if last_direction:
            # We only flip if we have active indicators representing structural changes
            # 1. Slope validation — slope must have crossed to the opposite side
            slope_flip = (last_direction == "call" and is_downtrend) or (last_direction == "put" and is_uptrend)

            # ALSO flip if slope is strongly against us even without full ER threshold
            # (e.g., slope=-25 but ER=0.22 — clearly bearish, don't stay CALL)
            strong_opposing = (
                (last_direction == "call" and slope <= -20.0) or
                (last_direction == "put" and slope >= 20.0)
            )

            if not slope_flip and not strong_opposing:
                if self._should_hold_sticky_direction(last_direction, slope, spot):
                    return last_direction
            else:
                # 2. Volatility ATR and EMA Calculation
                try:
                    candles = self.api.get_candles(self.asset, app_config.FOLLOW_CANDLE_TIMEFRAME, 20, time.time())
                    if candles and len(candles) >= 15:
                        closes = [float(c.get("close", 0)) for c in candles]
                        ema15 = self._calculate_ema(closes, 15)
                        atr = self._calculate_atr(self.asset, count=5)

                        accel, recent_atr, older_atr = self._is_momentum_accelerating(self.asset)
                        momentum_ratio = (recent_atr / older_atr) if older_atr > 0 else 1.0

                        if last_direction == "call" and (is_downtrend or strong_opposing):
                            price_breach = (ema15 - spot) >= (0.5 * atr)
                            if price_breach and momentum_ratio >= 1.05:
                                logger.warning(
                                    f"📉 Reversal confirmed: Spot breached EMA15 ({ema15:.5f}) "
                                    f"by {(ema15-spot):.5f} (need >= {0.5*atr:.5f}) with momentum {momentum_ratio:.2f}. PUT."
                                )
                                self._last_direction_flip_kind = "reversal_confirmed"
                                return "put"
                            if strong_opposing and slope <= -25.0:
                                logger.warning(
                                    f"📉 Strong opposing slope ({slope:.1f}) overrides stale CALL. Flipping to PUT."
                                )
                                self._last_direction_flip_kind = "slope_override"
                                return "put"
                        elif last_direction == "put" and (is_uptrend or strong_opposing):
                            price_breach = (spot - ema15) >= (0.5 * atr)
                            if price_breach and momentum_ratio >= 1.05:
                                logger.warning(
                                    f"📈 Reversal confirmed: Spot breached EMA15 ({ema15:.5f}) "
                                    f"by {(spot-ema15):.5f} (need >= {0.5*atr:.5f}) with momentum {momentum_ratio:.2f}. CALL."
                                )
                                self._last_direction_flip_kind = "reversal_confirmed"
                                if self._long_term_trend_blocks_direction("call", spot):
                                    return "put"
                                return "call"
                            if strong_opposing and slope >= 25.0:
                                if self._long_term_trend_blocks_direction("call", spot):
                                    return "put"
                                logger.warning(
                                    f"📈 Strong opposing slope ({slope:.1f}) overrides stale PUT. Flipping to CALL."
                                )
                                self._last_direction_flip_kind = "slope_override"
                                return "call"
                except Exception as e:
                    logger.warning(f"Reversal confirmation filters encountered error: {e}")

                if self._should_hold_sticky_direction(last_direction, slope, spot):
                    logger.info(
                        f"🔄 Reversal filter rejected slope flip. Classified as Correction. "
                        f"Continuing {last_direction.upper()}."
                    )
                    return last_direction

        # Initial direction pick
        if is_uptrend:
            direction = "call"
        elif is_downtrend:
            direction = "put"
        else:
            direction = "call" if slope >= 0 else "put"

        if direction == "call" and self._long_term_trend_blocks_direction("call", spot):
            direction = "put"
        elif direction == "put" and self._long_term_trend_blocks_direction("put", spot):
            direction = "call"

        # Overbought/Oversold Exhaustion Guard — if price is stretched
        # far beyond the EMA in the direction we want to trade, the move
        # is likely exhausted and we should NOT chase it.
        try:
            candles = self.api.get_candles(self.asset, app_config.FOLLOW_CANDLE_TIMEFRAME, 20, time.time())
            if candles and len(candles) >= 15:
                closes = [float(c.get("close", 0)) for c in candles]
                opens = [float(c.get("open", 0)) for c in candles]
                ema15 = self._calculate_ema(closes, 15)
                atr = self._calculate_atr(self.asset, count=10)
                if ema15 > 0 and atr > 0:
                    distance_from_ema = spot - ema15  # positive = above EMA
                    
                    # Analyze the most recent completed candle (index -2)
                    last_completed_close = closes[-2]
                    last_completed_open = opens[-2]
                    last_body = abs(last_completed_close - last_completed_open)
                    is_last_bearish = last_completed_close < last_completed_open
                    is_last_bullish = last_completed_close > last_completed_open
                    # Only count a candle as a meaningful reversal signal if its body
                    # is at least 30% of ATR — tiny doji/consolidation candles should
                    # not flip direction (they are mid-recovery pauses, not reversals).
                    is_meaningful_bearish = is_last_bearish and (last_body >= 0.30 * atr)
                    is_meaningful_bullish = is_last_bullish and (last_body >= 0.30 * atr)

                    # If picking CALL but price is stretched
                    if direction == "call" and distance_from_ema > (1.5 * atr):
                        # If extremely stretched (>2.0x) OR (stretched >1.5x AND meaningful pullback started)
                        if distance_from_ema > (2.0 * atr) or is_meaningful_bearish:
                            logger.warning(
                                f"⚠️ Overbought exhaustion: price {spot:.5f} is "
                                f"{distance_from_ema:.5f} above EMA15 ({ema15:.5f}), "
                                f"ATR {atr:.5f}. Meaningful pullback={is_meaningful_bearish}. Refusing to chase CALL."
                            )
                            return "put"  # fade the exhausted spike
                            
                    # If picking PUT but price is stretched
                    if direction == "put" and distance_from_ema < -(1.5 * atr):
                        # If extremely stretched (>2.0x) OR (stretched >1.5x AND pullback already started)
                        if distance_from_ema < -(2.0 * atr) or is_meaningful_bullish:
                            logger.warning(
                                f"⚠️ Oversold exhaustion: price {spot:.5f} is "
                                f"{abs(distance_from_ema):.5f} below EMA15 ({ema15:.5f}), "
                                f"ATR {atr:.5f}. Meaningful pullback={is_meaningful_bullish}. Refusing to chase PUT."
                            )
                            return "call"  # fade the exhausted drop
        except Exception as e:
            logger.warning(f"Exhaustion guard error: {e}")

        return direction

    def _build_ai_context(self, target_dir, candles, spot, ema15, atr, er, slope, recent_atr, older_atr) -> dict:
        """Constructs the market data dictionary for the AI assessor."""
        distance = spot - ema15 if ema15 > 0 else 0
        distance_atr = (distance / atr) if atr > 0 else 0

        # Spike ratio: last candle body vs recent average body
        spike_ratio = 1.0
        try:
            if len(candles) >= 6:
                bodies = [abs(float(c.get("close", 0)) - float(c.get("open", 0))) for c in candles]
                last_body = bodies[-1]
                prior_avg = sum(bodies[:-2]) / len(bodies[:-2]) if len(bodies) > 2 else 0.0001
                if prior_avg > 0:
                    spike_ratio = last_body / prior_avg
        except Exception:
            pass

        # Trader mood
        mood_pct = 50.0
        try:
            raw_mood = self.api.get_traders_mood(self.asset)
            if raw_mood is not None:
                mood_pct = raw_mood * 100
        except Exception as e:
            logger.debug(f"Could not fetch trader mood for AI: {e}")

        # ── NEW: 5-minute candles for higher-timeframe context ──
        candles_5min = []
        try:
            raw_5 = self._get_candles_safe(self.asset, 300, 10, time.time())
            if raw_5 and len(raw_5) >= 5:
                candles_5min = [
                    {
                        "open": float(c.get("open", 0)),
                        "high": float(c.get("max", 0)),
                        "low": float(c.get("min", 0)),
                        "close": float(c.get("close", 0)),
                    }
                    for c in raw_5[-10:]
                ]
        except Exception as e:
            logger.debug(f"Could not fetch 5min candles for AI context: {e}")

        # ── NEW: Candle pattern summary (last 10 × 1-min candles) ──
        consecutive_same_dir = 0
        doji_count = 0
        try:
            last10_closes = [float(c.get("close", 0)) for c in candles[-10:]]
            last10_opens = [float(c.get("open", 0)) for c in candles[-10:]]
            last10_bodies = [abs(c - o) for c, o in zip(last10_closes, last10_opens)]
            avg_body = sum(last10_bodies) / len(last10_bodies) if last10_bodies else 0.0001
            doji_count = sum(1 for b in last10_bodies if b < avg_body * 0.3)
            for i in range(len(last10_closes) - 1, 0, -1):
                is_bullish = last10_closes[i] > last10_opens[i]
                if target_dir == "call" and is_bullish:
                    consecutive_same_dir += 1
                elif target_dir == "put" and not is_bullish:
                    consecutive_same_dir += 1
                else:
                    break
        except Exception:
            pass

        # ── NEW: Session / time-of-day context (Lagos = UTC+1) ──
        utc_now = datetime.datetime.utcnow()
        lagos_hour = (utc_now.hour + 1) % 24
        if 7 <= lagos_hour < 9:
            session = "London Open (high volatility, big moves expected)"
        elif 9 <= lagos_hour < 12:
            session = "London mid-session (moderate, good for trends)"
        elif 12 <= lagos_hour < 15:
            session = "London/NY overlap (peak liquidity, strongest trends)"
        elif 15 <= lagos_hour < 18:
            session = "NY session (active, directional)"
        elif 18 <= lagos_hour < 20:
            session = "NY close (fading liquidity, reversals common)"
        else:
            session = "Off-peak / Asian session (lower liquidity, choppy — be cautious)"

        # ── NEW: Volatility regime (recent vs older ATR) ──
        volatility_trend = "stable"
        if recent_atr and older_atr and older_atr > 0:
            vr = recent_atr / older_atr
            if vr > 1.35:
                volatility_trend = "expanding — volatility increasing, wider swings"
            elif vr < 0.75:
                volatility_trend = "contracting — market quieting down or becoming choppy"

        # ── NEW: Recent trade history on this pair ──
        recent_pair_trades = []
        streak_wins = 0
        streak_losses = 0
        try:
            from trade_log import read_trades as _read_trades_ctx
            _all_recent = _read_trades_ctx(limit=40, account_key=self._state_account_key())
            _pair_only = [t for t in _all_recent if t.get("asset") == self.asset][:8]
            for t in _pair_only:
                profit = float(t.get("round_profit", 0))
                outcome = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "PUSH")
                beval = t.get("bot_evaluation") or {}
                direction_taken = beval.get("direction") or "?"
                recent_pair_trades.append({
                    "outcome": outcome,
                    "direction": direction_taken,
                    "step": t.get("step", "?"),
                    "ai_approved": t.get("ai_approved"),
                    "ai_confidence": t.get("ai_confidence"),
                })
            # Streak: walk from most recent until direction changes
            for t in _pair_only:
                profit = float(t.get("round_profit", 0))
                if profit > 0:
                    if streak_losses > 0:
                        break
                    streak_wins += 1
                elif profit < 0:
                    if streak_wins > 0:
                        break
                    streak_losses += 1
        except Exception as e:
            logger.debug(f"Could not load recent pair trades for AI context: {e}")

        # Ladder position
        bd = self.last_bet_breakdown or {}
        step_num = bd.get("step_number") or (self.session_round_count + 1)
        tier_num = bd.get("tier_number") or (self.current_tier_index + 1)
        step_scale = bd.get("scale", 1.0)

        return {
            "asset": self.asset,
            "direction": target_dir,
            "tier": tier_num,
            "step": step_num,
            "step_scale": step_scale,
            "bet_amount": self.current_bet,
            "candles": [
                {
                    "open": float(c.get("open", 0)),
                    "high": float(c.get("max", 0)),
                    "low": float(c.get("min", 0)),
                    "close": float(c.get("close", 0)),
                }
                for c in candles[-20:]
            ],
            "candles_5min": candles_5min,
            "slope": slope,
            "er": er,
            "atr": atr,
            "ema15": ema15,
            "distance_from_ema": distance,
            "distance_atr": distance_atr,
            "spike_ratio": spike_ratio,
            "mood_pct": mood_pct,
            "consecutive_same_dir_candles": consecutive_same_dir,
            "doji_count_last_10": doji_count,
            "session": session,
            "lagos_hour": lagos_hour,
            "volatility_trend": volatility_trend,
            "recent_atr": recent_atr,
            "older_atr": older_atr,
            "recent_pair_trades": recent_pair_trades,
            "streak_wins": streak_wins,
            "streak_losses": streak_losses,
        }

    def _get_best_directional_strike(self, direction, for_entry_timing=False):
        """
        Select ATM/ITM strike for single leg (CALL or PUT) or return dummy for Turbo/Binary mode.
        """
        if self.trading_mode in ["turbo", "binary"]:
            try:
                profit_dict = self.api.get_all_profit()
            except Exception as e:
                logger.warning(f"get_all_profit failed: {e}")
                return None
            actual_asset_key = self.asset
            asset_profit = profit_dict.get(self.asset)
            if not asset_profit:
                # API keys sometimes have suffixes like -OTC, -op, -OTC-op
                for suffix in ["-OTC", "-op", "-OTC-op"]:
                    if f"{self.asset}{suffix}" in profit_dict:
                        asset_profit = profit_dict[f"{self.asset}{suffix}"]
                        actual_asset_key = f"{self.asset}{suffix}"
                        break
            if not asset_profit:
                asset_profit = {}
                
            profit_pct = asset_profit.get(self.trading_mode, 0.0) * 100.0

            # Turbo/Binary binary options pay 80-90% — always use 70% as the floor,
            # ignoring the digital-options min_profit_pct (which is 145%+).
            MIN_PROFIT = 70.0
            if profit_pct < MIN_PROFIT:
                if not for_entry_timing:
                    logger.warning(
                        f"{self.trading_mode.capitalize()} profit for {self.asset} is {profit_pct:.1f}% "
                        f"(< {MIN_PROFIT}%) — asset may be unavailable as {self.trading_mode} option. Skipping."
                    )
                return None

            return {
                "strike": None,
                "profit_pct": profit_pct,
                "symbol": self.trading_mode
            }


        with self._price_lock:
            prices = self._price_data.get(60, [])

        if not prices:
            return None

        spot = self._estimate_spot_price(prices)
        if spot is None:
            return None

        ladder = self._sorted_strike_ladder(prices)
        if len(ladder) < 1:
            return None

        by_strike = self._strike_entry_map(prices)
        atm_idx = min(range(len(ladder)), key=lambda i: abs(ladder[i] - spot))
        now = self._server_now()
        max_steps = MAX_STRIKE_LADDER_STEPS_FROM_ATM

        # Payout range target for ATM/ITM directional bets (70% - 105%)
        min_p = 70.0
        max_p = 105.0

        best_leg = None
        if direction == "call":
            # For CALL ATM/ITM, walk down from ATM (ITM/ATM) or slightly up
            for i in range(atm_idx + 1, -1, -1):
                steps_from_atm = abs(i - atm_idx)
                if steps_from_atm > max_steps:
                    continue
                strike_val = ladder[i]
                entry = by_strike.get(strike_val)
                if not entry:
                    continue
                # Temp override profit settings for strike candidate walk
                orig_min_p, orig_max_p = self.min_profit_pct, self.max_profit_pct
                self.min_profit_pct, self.max_profit_pct = min_p, max_p
                leg = self._strike_leg_candidate(entry, "call", strike_val, for_entry_timing, now)
                self.min_profit_pct, self.max_profit_pct = orig_min_p, orig_max_p
                if leg:
                    best_leg = leg
                    break
        else: # direction == "put"
            # For PUT ATM/ITM, walk up from ATM (ITM/ATM) or slightly down
            for i in range(atm_idx, len(ladder)):
                steps_from_atm = abs(i - atm_idx)
                if steps_from_atm > max_steps:
                    continue
                strike_val = ladder[i]
                entry = by_strike.get(strike_val)
                if not entry:
                    continue
                orig_min_p, orig_max_p = self.min_profit_pct, self.max_profit_pct
                self.min_profit_pct, self.max_profit_pct = min_p, max_p
                leg = self._strike_leg_candidate(entry, "put", strike_val, for_entry_timing, now)
                self.min_profit_pct, self.max_profit_pct = orig_min_p, orig_max_p
                if leg:
                    best_leg = leg
                    break

        if best_leg:
            secs = self._seconds_to_expiry(best_leg["symbol"], now=now)
            logger.info(
                f"Directional Strike pick (spot≈{spot:.6f}, ATM≈{ladder[atm_idx]:.6f}, "
                f"direction={direction}, expiry {secs:.0f}s): "
                f"STRIKE {best_leg['strike']:.6f} @ {best_leg['profit_pct']:.1f}%"
            )
            return best_leg

        return None

    def _notify(self, title, body=""):
        try:
            send_alert(title, body)
        except Exception as e:
            logger.warning(f"Alert failed: {e}")

    def _reset_daily_counters(self):
        today = datetime.date.today()
        if self.tier_escalations_date != today:
            self.tier_escalations_date = today
            self.tier_escalations_today = 0

    def _record_tier_escalation(self, new_tier_index):
        self._reset_daily_counters()
        self.tier_escalations_today += 1
        self._notify(
            "Tier escalated",
            f"Now Tier {new_tier_index + 1}. Escalations today: "
            f"{self.tier_escalations_today}",
        )


    def _balance_baseline_tier_index(self, balance=None):
        """
        Lowest tier index the bot may use as its floor given current capital.
        Thresholds are (min_balance, tier_index) highest match wins.
        """
        if balance is None:
            balance = self.safe_get_balance()
        floor = 0
        for min_balance, tier_idx in self.baseline_balance_thresholds:
            if balance >= min_balance:
                floor = tier_idx
                break
        max_tier = len(self.budget_tiers) - 1 if self.budget_tiers else 0
        return min(max(0, floor), max_tier)


    def _init_risk_state(self):
        self.session_peak_balance = 0.0
        self.locked_profit = 0.0
        self.risk_mode_until = None
        self._drawdown_window_start_balance = None
        self._drawdown_window_start_ts = None
        self._last_risk_limits = {}
        self.ladder_attempt_id = 0
        self.ladder_pair = None
        self.ladder_loss_scores = []
        self._step_score_skip_streak = 0
        self._pending_entry_quality = None

    def _restore_risk_state(self, data):
        self.session_peak_balance = float(
            data.get("session_peak_balance", 0.0) or 0.0
        )
        self.locked_profit = float(data.get("locked_profit", 0.0) or 0.0)
        self.ladder_attempt_id = int(data.get("ladder_attempt_id", 0) or 0)
        self.ladder_pair = data.get("ladder_pair")
        self.ladder_loss_scores = [
            float(x) for x in (data.get("ladder_loss_scores") or [])
        ]
        risk_raw = data.get("risk_mode_until")
        if risk_raw:
            try:
                self.risk_mode_until = datetime.datetime.fromisoformat(
                    risk_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                self.risk_mode_until = None
        else:
            self.risk_mode_until = None

    def _update_and_get_risk_limits(self, balance):
        now_ts = time.time()
        risk_until_ts = (
            self.risk_mode_until.timestamp()
            if self.risk_mode_until is not None
            else None
        )
        lock_ratio = self.profit_lock_ratio if self.profit_lock_enabled else 0.0
        dd_pct = self.drawdown_pct if self.drawdown_breaker_enabled else 2.0
        dd_fast = self.drawdown_fast_usd if self.drawdown_breaker_enabled else 1e9

        dd_enabled = getattr(app_config, "DRAWDOWN_BREAKER_ENABLED", True)
        limits = compute_risk_limits(
            float(balance),
            float(self.session_peak_balance or balance),
            float(self.locked_profit),
            budget_tiers=self.budget_tiers,
            ceiling_thresholds=self.tier_ceiling_thresholds,
            lock_ratio=lock_ratio,
            min_reserve_usd=self.profit_lock_min_reserve,
            drawdown_pct=dd_pct,
            drawdown_fast_usd=dd_fast,
            drawdown_fast_minutes=self.drawdown_fast_minutes,
            drawdown_window_start_balance=self._drawdown_window_start_balance,
            drawdown_window_start_ts=self._drawdown_window_start_ts,
            now_ts=now_ts,
            risk_mode_until_ts=risk_until_ts,
            drawdown_recovery_pct=self.drawdown_recovery_pct,
            drawdown_breaker_enabled=dd_enabled,
        )

        if not self.profit_lock_enabled:
            limits["locked_profit"] = 0.0
            limits["tradable_balance"] = float(balance)

        self.session_peak_balance = limits["session_peak_balance"]
        self.locked_profit = limits["locked_profit"]
        self._drawdown_window_start_balance = limits["drawdown_window_start_balance"]
        self._drawdown_window_start_ts = limits["drawdown_window_start_ts"]

        limits["max_step_index"] = LADDER_MAX_STEP_INDEX

        self._last_risk_limits = limits
        return limits

    def _apply_risk_tier_caps(self, limits):
        if limits.get("risk_mode"):
            cap = int(limits["risk_tier_cap"])
            # Never interrupt an active debt-recovery sequence — dropping the tier
            # mid-recovery abandons T2/T3/T4/T5 and causes "random" betting.
            # Log the would-be cap and hold position until the sequence completes.
            if self.cumulative_debt > 0 and self.current_tier_index > 0:
                logger.warning(
                    f"Drawdown risk mode — would cap to Tier {cap + 1} but holding "
                    f"Tier {self.current_tier_index + 1} mid-recovery "
                    f"(debt=${self.cumulative_debt:.2f})"
                )
                if limits.get("drawdown_fast_triggered"):
                    logger.warning(
                        f"Fast drawdown ${self.drawdown_fast_usd:.0f} in "
                        f"{self.drawdown_fast_minutes:.0f}m — risk mode active (recovery held)"
                    )
                elif limits.get("drawdown_from_peak_pct", 0) >= self.drawdown_pct * 100:
                    logger.warning(
                        f"Peak drawdown {limits['drawdown_from_peak_pct']:.1f}% — "
                        f"risk mode active (recovery held)"
                    )
                return
            if self.assigned_tier_index > cap:
                logger.warning(
                    f"Drawdown risk mode — assigned Tier "
                    f"{self.assigned_tier_index + 1} → {cap + 1}"
                )
                self.assigned_tier_index = cap
            if self.current_tier_index > cap:
                self.current_tier_index = cap
                self.session_round_count = min(
                    self.session_round_count, limits["max_step_index"]
                )
            if limits.get("drawdown_fast_triggered"):
                logger.warning(
                    f"Fast drawdown ${self.drawdown_fast_usd:.0f} in "
                    f"{self.drawdown_fast_minutes:.0f}m — risk mode active"
                )
            elif limits.get("drawdown_from_peak_pct", 0) >= self.drawdown_pct * 100:
                logger.warning(
                    f"Peak drawdown {limits['drawdown_from_peak_pct']:.1f}% — "
                    f"risk mode active"
                )

    def _check_risk_mode_step_allowed(self):
        limits = self._last_risk_limits or {}
        if not limits.get("risk_mode"):
            return True
        return self.session_round_count <= int(
            limits.get("max_step_index", LADDER_MAX_STEP_INDEX)
        )

    def _compute_entry_quality(self, bot_conf=None, ensemble_combined=None):
        if bot_conf is not None or ensemble_combined is not None:
            return max(float(bot_conf or 0), float(ensemble_combined or 0))
        assess = self.last_pair_quality or {}
        if self.strategy_mode == "directional_trend":
            slope = float(assess.get("abs_slope", 0.0) or 0.0)
            er = float(assess.get("efficiency_ratio", 0.0) or 0.0)
            direction = self.last_trend_direction or "call"
            if direction == "put":
                slope = -abs(slope)
            else:
                slope = abs(slope)
            return compute_bot_confidence(assess, direction, slope, er)
        return min(1.0, float(assess.get("straddle_score", 0) or 0) / 150.0)

    def _check_step_score_escalation(self, entry_quality):
        if not self.step_score_escalation_enabled:
            return True, ""
        if self.session_round_count == 0 or not self.ladder_loss_scores:
            return True, ""
        # Step-score only applies on the same pair — switching pairs resets the bar.
        if self.ladder_pair and self.asset != self.ladder_pair:
            return True, ""
        required = max(self.ladder_loss_scores) + self.step_score_min_improvement
        if float(entry_quality) >= required:
            self._step_score_skip_streak = 0
            return True, ""
        return False, (
            f"step score {float(entry_quality):.2f} < {required:.2f} required "
            f"(prior losses on {self.ladder_pair})"
        )

    def _on_ladder_step_start(self):
        if self.session_round_count == 0:
            self.ladder_pair = self.asset
            self.ladder_loss_scores = []
            self._step_score_skip_streak = 0

    def _record_ladder_step_loss(self, entry_quality):
        if entry_quality is None:
            return
        if self.ladder_pair and self.asset == self.ladder_pair:
            self.ladder_loss_scores.append(float(entry_quality))

    def _reset_ladder_tracking(self):
        self.ladder_attempt_id += 1
        self.ladder_pair = None
        self.ladder_loss_scores = []
        self._step_score_skip_streak = 0
        self._pending_entry_quality = None

    @staticmethod
    def _parse_market_open_blocks(raw_blocks):
        """Parse ['02:00:15:30', ...] into [(hour, minute, before, after), ...]."""
        parsed = []
        for entry in raw_blocks or []:
            try:
                parts = [p.strip() for p in str(entry).split(":")]
                if len(parts) != 4:
                    continue
                oh, om, before, after = (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
                parsed.append((oh, om, before, after))
            except Exception:
                continue
        return parsed

    @staticmethod
    def _parse_blocked_time_windows(raw_windows):
        """Parse ['02:00-02:45', ...] into [(2, 0, 2, 45), ...] local hour/min tuples."""
        parsed = []
        for entry in raw_windows or []:
            try:
                part = str(entry).strip()
                if "-" not in part:
                    continue
                start_s, end_s = part.split("-", 1)
                sh, sm = [int(x) for x in start_s.strip().split(":", 1)]
                eh, em = [int(x) for x in end_s.strip().split(":", 1)]
                parsed.append((sh, sm, eh, em))
            except Exception:
                continue
        return parsed

    def _trading_now(self):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.trading_timezone)
        except Exception:
            tz = None
        ts = self._server_timestamp()
        if tz:
            return datetime.datetime.fromtimestamp(ts, tz=tz)
        return datetime.datetime.utcfromtimestamp(ts).replace(
            tzinfo=datetime.timezone.utc
        )

    @staticmethod
    def _is_in_market_open_block(now, open_h, open_m, minutes_before, minutes_after):
        open_min = open_h * 60 + open_m
        start_min = (open_min - minutes_before) % (24 * 60)
        end_min = (open_min + minutes_after) % (24 * 60)
        now_min = now.hour * 60 + now.minute
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        return now_min >= start_min or now_min <= end_min

    def _stake_multiplier(self):
        """Per-leg stake multiplier: straddle places CALL+PUT; directional one leg."""
        return 1.0 if self.strategy_mode == "directional_trend" else 2.0

    def _find_affordable_ladder_bet(self, balance, sched_tier, sched_step):
        """
        When balance cannot cover the scheduled step, walk down the ladder:
        lower steps on the same tier, then step 1 on each lower tier.
        """
        mult = self._stake_multiplier()
        tier = self.budget_tiers[sched_tier]
        for step_idx in range(min(sched_step, len(tier) - 1), -1, -1):
            amount = float(tier[step_idx])
            if amount * mult <= balance:
                return sched_tier, step_idx, amount
        for tier_idx in range(sched_tier - 1, -1, -1):
            amount = float(self.budget_tiers[tier_idx][0])
            if amount * mult <= balance:
                return tier_idx, 0, amount
        return None

    def _apply_balance_ladder_downgrade(self, balance=None):
        """
        When balance cannot fund the scheduled step, retreat to the previous tier
        step 1 for recovery.  If already on Tier 1, cannot retreat further.
        During active debt recovery, only downgrade if even S1 of the current
        recovery tier is unaffordable — never retreat from a recovery tier just
        because a later step costs more than the current balance.
        """
        if balance is None:
            balance = self.safe_get_balance()
        if not self.budget_tiers:
            return False

        sched_tier_idx, sched_step_idx = self._clamp_tier_step_indices()
        sched_tier = self.budget_tiers[sched_tier_idx]
        required = float(sched_tier[sched_step_idx]) * self._stake_multiplier()
        if required <= balance:
            return False

        # During active debt recovery, check whether S1 of the current recovery
        # tier is still affordable. If it is, fall back to S1 of that same tier
        # rather than retreating to a lower tier and breaking the sequence.
        if self.cumulative_debt > 0 and sched_tier_idx > 0:
            s1_required = float(sched_tier[0]) * self._stake_multiplier()
            if s1_required <= balance:
                if sched_step_idx > 0:
                    logger.warning(
                        f"Balance ${balance:.2f} cannot cover recovery "
                        f"Tier {sched_tier_idx + 1} step {sched_step_idx + 1} "
                        f"(${required:.2f}) — resetting to S1 (${float(sched_tier[0]):.2f}) "
                        f"of same tier (debt=${self.cumulative_debt:.2f})"
                    )
                    self.session_round_count = 0
                return False

        if sched_tier_idx == 0:
            return False

        retreat_tier_idx = sched_tier_idx - 1
        play_amount = float(self.budget_tiers[retreat_tier_idx][0])

        floor = self._balance_baseline_tier_index(balance)
        new_assigned = max(retreat_tier_idx, floor)
        prev_assigned = self.assigned_tier_index
        prev_tier = self.current_tier_index
        prev_step = self.session_round_count

        self.current_tier_index = retreat_tier_idx
        self.session_round_count = 0
        self.session_max_rounds = len(self.budget_tiers[retreat_tier_idx])
        if self.assigned_tier_index > new_assigned:
            self.assigned_tier_index = new_assigned

        logger.warning(
            f"Balance ${balance:.2f} cannot cover scheduled "
            f"Tier {sched_tier_idx + 1} step {sched_step_idx + 1} "
            f"(${required:.2f}) — retreating to "
            f"Tier {retreat_tier_idx + 1} step 1 "
            f"(${play_amount:.2f}); assigned Tier {prev_assigned + 1} → "
            f"{self.assigned_tier_index + 1} "
            f"(was Tier {prev_tier + 1} step {prev_step + 1})"
        )
        return True

    def _is_blocked_time_window(self):
        """True during hour boundaries, market-open buffers, or extra static windows."""
        now = self._trading_now()
        hb_start = max(0, int(getattr(self, "hour_boundary_block_minutes", 0) or 0))
        hb_end = max(0, int(getattr(self, "hour_boundary_block_end_minutes", 0) or 0))
        if hb_start > 0 and now.minute < hb_start:
            return True
        if hb_end > 0 and now.minute >= (60 - hb_end):
            return True
        for oh, om, before, after in getattr(self, "market_open_blocks", []):
            if self._is_in_market_open_block(now, oh, om, before, after):
                return True
        if getattr(self, "override_blocked_windows", False):
            return False
        for sh, sm, eh, em in self.blocked_time_windows:
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end = now.replace(hour=eh, minute=em, second=59, microsecond=999999)
            if start <= now <= end:
                return True
        return False

    @staticmethod
    def _check_utc_window(now_min, window_str):
        """
        Return True if now_min (hour*60+minute in UTC) falls within 'HH:MM-HH:MM'.
        Handles midnight-crossing windows (e.g. '22:00-00:30').
        """
        try:
            start_s, end_s = str(window_str).strip().split("-", 1)
            sh, sm = [int(x) for x in start_s.strip().split(":", 1)]
            eh, em = [int(x) for x in end_s.strip().split(":", 1)]
            start_min = sh * 60 + sm
            end_min = eh * 60 + em
            if end_min < start_min:
                return now_min >= start_min or now_min <= end_min
            return start_min <= now_min <= end_min
        except Exception:
            return False

    def _is_utc_ban_window(self):
        """
        Returns (is_banned: bool, reason: str).
        Hard UTC ban windows block ALL trading regardless of asset.
        """
        now_utc = datetime.datetime.utcnow()
        now_min = now_utc.hour * 60 + now_utc.minute
        for window_str in getattr(app_config, "UTC_BAN_WINDOWS", []):
            if self._check_utc_window(now_min, window_str):
                return True, f"UTC hard ban {window_str} ({now_utc.strftime('%H:%M')} UTC)"
        return False, ""

    def _is_utc_soft_ban_for_asset(self, asset):
        """
        Returns (is_banned: bool, reason: str).
        Soft UTC ban blocks specific high-risk assets (e.g. AMAZON, APPLE) during
        volatile US-market-open hours without stopping all trading.
        """
        soft_assets = getattr(app_config, "UTC_SOFT_BAN_ASSETS", [])
        asset_upper = (asset or "").upper()
        is_soft_asset = any(
            asset_upper == a.upper() or asset_upper.startswith(a.upper().rstrip("-OTC"))
            for a in soft_assets
        )
        if not is_soft_asset:
            return False, ""
        now_utc = datetime.datetime.utcnow()
        now_min = now_utc.hour * 60 + now_utc.minute
        for window_str in getattr(app_config, "UTC_SOFT_BAN_WINDOWS", []):
            if self._check_utc_window(now_min, window_str):
                return True, f"UTC soft ban {window_str} for {asset} ({now_utc.strftime('%H:%M')} UTC)"
        return False, ""

    def _is_asset_flip_blocked(self, asset: str) -> bool:
        """True if the asset is in the slope-flip cooldown (20-minute block)."""
        until = self._asset_flip_blocked.get(asset, 0)
        if time.time() < until:
            mins_left = int((until - time.time()) / 60) + 1
            return True
        self._asset_flip_blocked.pop(asset, None)
        return False

    def _check_and_apply_slope_flip_block_UNUSED(
        self, asset: str, med_slope: float, short_slope: float
    ) -> bool:
        """
        DEAD CODE — never called. Retained for reference only; do not reconnect.
        Original: slope-flip hard block rule (London analysis, 2026-06).
        Condition: abs(short_slope) > 1.5 × abs(med_slope) AND opposite signs.
        When true the market has already reversed against the entry direction —
        block the asset for 20 minutes regardless of ER.
        Returns True when the block was just triggered.
        """
        if abs(med_slope) < 1.0:
            return False
        opposite = (med_slope > 0) != (short_slope > 0)
        if opposite and abs(short_slope) > 1.0 * abs(med_slope):
            until = time.time() + 12 * 60
            self._asset_flip_blocked[asset] = until
            logger.warning(
                f"⚡ Slope-flip block on {asset}: "
                f"med={med_slope:.1f}, short={short_slope:.1f} "
                f"(|short|={abs(short_slope):.1f} > 1.5×|med|={1.5 * abs(med_slope):.1f}). "
                f"Blocked 20 min."
            )
            return True
        return False

    def _is_legacy_blocked_hour(self):
        if not self.blocked_hours:
            return False
        return datetime.datetime.now().hour in self.blocked_hours

    def _clamp_tier_step_indices(self):
        if not self.budget_tiers:
            return 0, 0
        tier_idx = min(max(0, self.current_tier_index), len(self.budget_tiers) - 1)
        tier = self.budget_tiers[tier_idx]
        if not tier:
            return tier_idx, 0
        step_idx = min(max(0, self.session_round_count), len(tier) - 1)
        return tier_idx, step_idx

    def _sync_ladder_indices(self):
        """Persist clamped tier/step on the bot (keeps state aligned with the fixed ladder)."""
        tier_idx, step_idx = self._clamp_tier_step_indices()
        self.current_tier_index = tier_idx
        self.session_max_rounds = len(self.budget_tiers[tier_idx])
        return tier_idx, step_idx

    def _compute_round_bet(self, balance=None):
        """
        Exact bet from the fixed tier ladder only — no debt/dynamic scaling.
        Tier advances only after all steps in the current tier lose (never on a win).
        When balance is given and cannot cover the scheduled step, ladder state is
        downgraded first via _apply_balance_ladder_downgrade (called from sync).
        """
        if not hasattr(self, 'budget_tiers') or not self.budget_tiers:
            self._apply_standard_budget_tiers()
        bal = balance if balance is not None else self.safe_get_balance()
        self._sync_assigned_tier_for_trading(balance=bal)
        sched_tier_idx, sched_step_idx = self._sync_ladder_indices()
        sched_tier = self.budget_tiers[sched_tier_idx]
        tier_idx, step_idx = sched_tier_idx, sched_step_idx
        amount = float(sched_tier[sched_step_idx])
        if getattr(self, 'sequential_steps_mode', False) and getattr(self, 'sequential_amounts', None):
            seq = self.sequential_amounts
            if seq and isinstance(seq[0], list):
                tier_amounts = seq[min(sched_tier_idx, len(seq) - 1)]
                amount = float(tier_amounts[sched_step_idx % len(tier_amounts)])
            else:
                amount = float(seq[sched_step_idx % len(seq)])
        balance_downgrade = False

        if balance is not None:
            required = amount * self._stake_multiplier()
            if required > balance:
                affordable = self._find_affordable_ladder_bet(
                    balance, sched_tier_idx, sched_step_idx
                )
                if affordable:
                    tier_idx, step_idx, amount = affordable
                    balance_downgrade = (
                        tier_idx != sched_tier_idx or step_idx != sched_step_idx
                    )
                    if balance_downgrade:
                        logger.warning(
                            f"Balance ${balance:.2f} still below scheduled "
                            f"Tier {sched_tier_idx + 1} step {sched_step_idx + 1} "
                            f"(${required:.2f}) — playing affordable "
                            f"Tier {tier_idx + 1} step {step_idx + 1} (${amount:.2f})"
                        )

        play_tier = self.budget_tiers[tier_idx]
        return {
            "amount": amount,
            "tier_index": tier_idx,
            "tier_number": tier_idx + 1,
            "step_index": step_idx,
            "step_number": step_idx + 1,
            "scheduled_tier_index": sched_tier_idx,
            "scheduled_tier_number": sched_tier_idx + 1,
            "scheduled_step_index": sched_step_idx,
            "scheduled_step_number": sched_step_idx + 1,
            "balance_downgrade": balance_downgrade,
            "base_amount": amount,
            "scale": 1.0,
            "debt_scale_applied": False,
            "dynamic_scale_applied": False,
            "scheduled_ladder": [float(x) for x in sched_tier],
            "exact_ladder_value": True,
            "play_ladder": [float(x) for x in play_tier],
        }

    def _validate_bet_amount(self, amount):
        self._apply_standard_budget_tiers()
        allowed = {float(x) for tier in self.budget_tiers for x in tier}
        if float(amount) not in allowed:
            logger.error(f"Bet ${amount} is not on the fixed tier ladder: {allowed}")
            return False
        return True

    def _is_news_blackout(self, now_utc):
        return now_utc.hour in self.news_blackout_utc_hours

    def _check_market_skip_signals(self, asset_name=None):
        asset_name = asset_name or self.asset
        if not self.api:
            return False, ""
        try:
            candles = self._get_candles_safe(
                asset_name,
                60,
                max(self.tight_range_candles, self.doji_streak_max + 2),
                time.time(),
            )
        except Exception as e:
            logger.warning(f"Skip-rule candle fetch failed: {e}")
            return False, ""

        if not candles or len(candles) < 5:
            return False, ""

        doji_streak = 0
        for candle in reversed(candles):
            open_, _, _, close = self._candle_ohlc(candle)
            if close <= 0:
                break
            body_pct = abs(close - open_) / close
            if body_pct < self.min_candle_body_pct:
                doji_streak += 1
            else:
                break
        if doji_streak >= self.doji_streak_max:
            return True, f"{doji_streak} consecutive doji candles"

        sample = candles[-self.tight_range_candles :]
        highs, lows, closes = [], [], []
        for candle in sample:
            open_, high, low, close = self._candle_ohlc(candle)
            if close <= 0:
                continue
            highs.append(high)
            lows.append(low)
            closes.append(close)
        if len(closes) >= 5:
            avg_close = sum(closes) / len(closes)
            range_pct = (max(highs) - min(lows)) / avg_close if avg_close else 0
            if range_pct < self.tight_range_pct:
                return True, f"Tight {self.tight_range_candles}m range ({range_pct * 100:.4f}%)"

        return False, ""

    def _build_trade_evaluation_context(self, target_dir, leg_info, entry_quality):
        """Capture bot gate metrics at order placement for trade log / export."""
        ai_info = self._last_ai_decision or {}
        pq = self.last_pair_quality or {}
        abs_slope = float(pq.get("abs_slope", 0) or 0)
        er = float(pq.get("efficiency_ratio", 0) or 0)
        direction = (target_dir or self.last_trend_direction or "call").lower()
        if self.strategy_mode != "directional_trend":
            direction = "straddle"
        signed_slope = abs_slope if direction == "call" else -abs_slope
        if ai_info.get("gate_slope") is not None:
            signed_slope = float(ai_info["gate_slope"])
        elif ai_info.get("entry_slope_signed") is not None:
            signed_slope = float(ai_info["entry_slope_signed"])
        if ai_info.get("gate_er") is not None:
            er = float(ai_info["gate_er"])
        trend_aligned = None
        if direction in ("call", "put"):
            trend_aligned = (direction == "call" and signed_slope > 0) or (
                direction == "put" and signed_slope < 0
            )
        flip_kind = self._last_direction_flip_kind
        slope_override = flip_kind == "slope_override" or bool(
            ai_info.get("slope_override_flip")
        )
        step_required = None
        if (
            self.step_score_escalation_enabled
            and self.session_round_count > 0
            and self.ladder_loss_scores
        ):
            step_required = round(
                max(self.ladder_loss_scores) + self.step_score_min_improvement, 3
            )
        strike_pct = None
        if leg_info and leg_info.get("profit_pct") is not None:
            strike_pct = round(float(leg_info["profit_pct"]), 1)
        bot_conf = ai_info.get("bot_confidence")
        if bot_conf is None and entry_quality is not None:
            bot_conf = float(entry_quality)
        return {
            "direction": direction,
            "trading_mode": self.trading_mode,
            "strategy_mode": self.strategy_mode,
            "bot_confidence": bot_conf,
            "entry_quality": entry_quality,
            "ensemble_combined_confidence": ai_info.get("ensemble_combined_confidence"),
            "ensemble_action": ai_info.get("ensemble_action"),
            "entry_er": round(er, 3),
            "er_floor_used": round(self._effective_min_er(), 3),
            "entry_slope": round(abs(signed_slope), 1),
            "entry_slope_signed": round(signed_slope, 1),
            "entry_straddle_score": pq.get("straddle_score"),
            "trend_aligned": trend_aligned,
            "direction_flip_kind": flip_kind,
            "slope_override_flip": slope_override,
            "rule_gate_reason": ai_info.get("reason"),
            "ai_disabled": ai_info.get("ai_disabled", False),
            "ai_approved": ai_info.get("approve"),
            "ai_confidence": ai_info.get("confidence"),
            "ai_skipped": ai_info.get("ai_skipped", False),
            "ai_direction": ai_info.get("direction"),
            "strike_profit_pct": strike_pct,
            "step_score_required": step_required,
            "ladder_loss_scores": list(self.ladder_loss_scores or []),
            "pair_quality_reason": pq.get("reason"),
        }

    def _log_trade_round(self, round_profit, call_info, put_info, partial=False, both_legs=False):
        ai_info = self._last_ai_decision or {}
        bot_eval = copy_bot_evaluation(self._pending_trade_context) or {}
        if not bot_eval.get("direction"):
            if call_info and not put_info:
                bot_eval["direction"] = "call"
            elif put_info and not call_info:
                bot_eval["direction"] = "put"
            else:
                bot_eval["direction"] = self.last_trend_direction
        append_trade(
            {
                "account_type": self.account_type,
                "account_key": self._state_account_key(),
                "asset": self.asset,
                "tier": (
                    self.last_bet_breakdown.get("tier_number")
                    or self.current_tier_index + 1
                ),
                "step": (
                    self.last_bet_breakdown.get("step_number")
                    or self.session_round_count + 1
                ),
                "scheduled_tier": self.last_bet_breakdown.get("scheduled_tier_number"),
                "scheduled_step": self.last_bet_breakdown.get("scheduled_step_number"),
                "balance_downgrade": self.last_bet_breakdown.get("balance_downgrade", False),
                "bet": self.current_bet,
                "bet_base": self.last_bet_breakdown.get("base_amount"),
                "bet_scale": self.last_bet_breakdown.get("scale"),
                "round_profit": round_profit,
                "session_profit": self.session_profit,
                "debt": self.cumulative_debt,
                "partial": partial,
                "both_legs": both_legs,
                "simulation": self.simulation_mode,
                "trading_mode": self.trading_mode,
                "strategy_mode": self.strategy_mode,
                "call_strike": call_info.get("strike") if call_info else None,
                "put_strike": put_info.get("strike") if put_info else None,
                "entry_er": bot_eval.get("entry_er")
                or self.last_pair_quality.get("efficiency_ratio"),
                "entry_slope": bot_eval.get("entry_slope")
                or self.last_pair_quality.get("abs_slope"),
                "entry_straddle_score": bot_eval.get("entry_straddle_score")
                or self.last_pair_quality.get("straddle_score"),
                "entry_quality": bot_eval.get("entry_quality")
                or self._pending_entry_quality,
                "entry_snapshot": self.last_entry_snapshot,
                "entry_ts": self.last_entry_capture_ts,
                "bot_evaluation": bot_eval,
                "ai_approved": bot_eval.get("ai_approved", ai_info.get("approve")),
                "ai_confidence": bot_eval.get("ai_confidence", ai_info.get("confidence")),
                "ai_reason": bot_eval.get("rule_gate_reason", ai_info.get("reason")),
                "ai_direction": bot_eval.get("ai_direction", ai_info.get("direction")),
                "bot_direction": bot_eval.get("direction") or self.last_trend_direction,
                "bot_confidence": bot_eval.get("bot_confidence", ai_info.get("bot_confidence")),
                "ensemble_action": bot_eval.get("ensemble_action", ai_info.get("ensemble_action")),
                "ensemble_combined_confidence": bot_eval.get(
                    "ensemble_combined_confidence",
                    ai_info.get("ensemble_combined_confidence"),
                ),
                "ai_skipped": bot_eval.get("ai_skipped", ai_info.get("ai_skipped", False)),
            }
        )
        conf_pct = bot_eval.get("bot_confidence")
        conf_label = f"{float(conf_pct):.0%}" if conf_pct is not None else "—"
        logger.info(
            f"TRADE LOG {bot_eval.get('direction', '?').upper()} "
            f"{self.asset} bot={conf_label} ER={bot_eval.get('entry_er')} "
            f"slope={bot_eval.get('entry_slope_signed')} "
            f"straddle={bot_eval.get('entry_straddle_score')} "
            f"aligned={bot_eval.get('trend_aligned')} "
            f"P/L=${float(round_profit):.2f}"
        )
        self._last_ai_decision = None
        self._pending_trade_context = None
        self._pending_entry_quality = None
        self.last_trade_time = time.time()
        schedule_refresh()
        self._refresh_pair_learning_cache_later()

    def _place_trade(self, instrument_id, amount, direction=None, retries=None, skip_validation=False, asset_name=None):
        max_attempts = 1 if retries is None else max(1, int(retries))
        if not skip_validation and not self._validate_bet_amount(amount):
            return False, None
        if self.simulation_mode:
            sim_id = f"sim-{int(time.time() * 1000)}-{instrument_id[-6:]}"
            return True, sim_id

        last_err = None
        for attempt in range(max_attempts):
            try:
                if self.trading_mode == "turbo":
                    if not direction:
                        logger.error("Direction is required for turbo trades")
                        return False, None
                    result = self.api.buy(amount, asset_name or self.asset, direction.lower(), max(1, int(app_config.FOLLOW_CANDLE_TIMEFRAME / 60)))
                    if result is None:
                        last_err = "buy() returned None (asset may not be open for turbo)"
                        logger.warning(last_err)
                        time.sleep(2.0)
                        continue
                    ok, order_id = result
                    if ok:
                        logger.info(f"Turbo Order placed: id={order_id} amount=${amount} dir={direction}")
                        return True, str(order_id)
                    else:
                        last_err = order_id
                        logger.warning(f"Turbo Order not confirmed: {order_id}")
                        time.sleep(2.0)
                        continue

                if not isinstance(self.api.api.digital_option_placed_id, dict):
                    self.api.api.digital_option_placed_id = {}

                from iqoptionapi.stable_api import global_value
                request_id = f"{int(time.time() * 1000)}"

                data = {
                    "name": "digital-options.place-digital-option",
                    "version": "2.0",
                    "body": {
                        "amount": str(amount),
                        "asset_id": int(self.asset_id),
                        "instrument_id": str(instrument_id),
                        "instrument_index": 0,
                        "user_balance_id": int(global_value.balance_id)
                    }
                }

                self.api.api.send_websocket_request("sendMessage", data, request_id=request_id)

                start_t = time.time()
                order_id = None
                while time.time() - start_t < 15:
                    if not self.running:
                        logger.info("Stop requested — aborting order confirmation wait")
                        return False, None
                    if isinstance(self.api.api.digital_option_placed_id, int):
                        order_id = self.api.api.digital_option_placed_id
                        break
                    elif isinstance(self.api.api.digital_option_placed_id, dict):
                        if request_id in self.api.api.digital_option_placed_id:
                            order_id = self.api.api.digital_option_placed_id[request_id]
                            break
                        elif "message" in self.api.api.digital_option_placed_id:
                            order_id = self.api.api.digital_option_placed_id
                            break
                    time.sleep(0.1)

                if isinstance(order_id, int) or (isinstance(order_id, str) and str(order_id).isdigit()):
                    logger.info(f"Order placed: id={order_id} amount=${amount} req={request_id}")
                    return True, str(order_id)
                else:
                    last_err = order_id
                    logger.warning(
                        f"Order not confirmed (attempt {attempt + 1}/{max_attempts}): {order_id}"
                    )
                    if attempt + 1 < max_attempts:
                        time.sleep(2.0)
            except Exception as e:
                last_err = e
                logger.error(f"Error placing trade: {e}")
                if attempt + 1 < max_attempts:
                    time.sleep(2.0)

        logger.error(f"Order placement failed after {max_attempts} attempt(s): {last_err}")
        return False, None

    def _check_trade_result(self, order_id, call_info=None, put_info=None, polling_time=2, max_polls=60):
        """
        Wait for a digital option trade to settle and return the profit/loss.

        CRITICAL FIX: A timeout means we could NOT confirm the result.
        Returns _TIMEOUT_SENTINEL (not 0.0) so the caller can distinguish
        "timed out / unknown" from "genuinely settled at breakeven".
        The caller must treat a timeout as a LOSS for ladder advancement.

        FIX: Both CALL and PUT results are now fetched CONCURRENTLY using
        concurrent.futures so we don't wait up to 140s sequentially.
        """
        try:
            start_t = time.time()
            order_id_int = int(order_id)
            
            if self.trading_mode == "turbo":
                while True:
                    if not self.running:
                        logger.info(f"Stop requested — abandoning result wait for order {order_id_int}")
                        return _TIMEOUT_SENTINEL
                    
                    timeout_limit = getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60) + 70
                    if time.time() - start_t > timeout_limit:
                        logger.warning(f"Timeout waiting for turbo result on order {order_id_int} after {timeout_limit}s")
                        return _TIMEOUT_SENTINEL
                        
                    order_info = self.api.get_async_order(order_id_int)
                    
                    # Some iqoptionapi versions use "option-closed" for binary/turbo
                    opt_closed = order_info.get("option-closed", {})
                    if opt_closed:
                        msg = opt_closed.get("msg", {})
                        profit = float(msg.get("profit_amount", 0)) - float(msg.get("amount", 0))
                        return float(profit)
                        
                    # Fallback to "position-changed" if that's what the broker sent
                    pos_changed = order_info.get("position-changed", {})
                    if pos_changed:
                        msg = pos_changed.get("msg", {})
                        if msg.get("status") == "closed":
                            win_status = msg.get("win")
                            if win_status == "equal":
                                profit = 0.0
                            elif win_status == "loose":
                                profit = float(msg.get("sum", 0)) * -1
                            else:
                                profit = float(msg.get("win_amount", 0)) - float(msg.get("sum", 0))
                            return float(profit)
                    
                    time.sleep(0.5)

            while True:
                if not self.running:
                    logger.info(f"Stop requested — abandoning result wait for order {order_id_int}")
                    return _TIMEOUT_SENTINEL
                pos_changed = self.api.get_async_order(order_id_int).get("position-changed", {})
                if pos_changed != {}:
                    msg = pos_changed.get("msg", {})
                    if msg.get("status") == "closed":
                        break
                if time.time() - start_t > 70:
                    logger.warning(f"Timeout waiting for async result on order {order_id_int}")
                    return _TIMEOUT_SENTINEL   # ← FIXED: was returning 0.0 (false win)
                time.sleep(0.1)

            order_data = self.api.get_async_order(order_id_int)["position-changed"].get("msg")
            if order_data is not None:
                if order_data.get("close_reason") == "expired":
                    profit = float(order_data.get("close_profit", 0)) - float(order_data.get("invest", 0))
                    return profit
                elif order_data.get("close_reason") == "default":
                    return float(order_data.get("pnl_realized", 0))
            return _TIMEOUT_SENTINEL  # ← FIXED: unknown close reason → treat as loss
        except Exception as e:
            logger.error(f"Error checking async result for {order_id}: {e}", exc_info=True)
            return _TIMEOUT_SENTINEL  # ← FIXED: exception → treat as loss

    def _leg_lost(leg_result):
        if leg_result is _TIMEOUT_SENTINEL:
            return True
        if leg_result is None:
            return True
        return float(leg_result) <= 0

    def _resolve_round_outcome(self, call_result, put_result):
        """
        Straddle outcome: LOSS only when BOTH legs lose (or time out).
        Returns (round_profit, both_lost, timed_out).
        """
        call_timed_out = call_result is _TIMEOUT_SENTINEL
        put_timed_out = put_result is _TIMEOUT_SENTINEL
        timed_out = call_timed_out or put_timed_out

        call_pl = (-self.current_bet) if call_timed_out else float(call_result or 0)
        put_pl = (-self.current_bet) if put_timed_out else float(put_result or 0)
        round_profit = call_pl + put_pl
        both_lost = self._leg_lost(call_result) and self._leg_lost(put_result)

        if timed_out:
            logger.warning(
                f"Result timeout — treating timed leg(s) as full stake loss. "
                f"CALL={'timeout' if call_timed_out else f'${call_pl:.2f}'} | "
                f"PUT={'timeout' if put_timed_out else f'${put_pl:.2f}'}"
            )

        return round_profit, both_lost, timed_out

    def _required_balance_next_round(self, balance=None):
        bet_info = self._compute_round_bet(balance=balance)
        return float(bet_info["amount"]) * self._stake_multiplier()

    def _reconcile_inflight_trades(self):
        if not self._inflight_trade_ids:
            return

        logger.info(
            f"🔌 Reconciling {len(self._inflight_trade_ids)} in-flight trades from before restart..."
        )

        total_profit = 0.0
        resolved = 0

        for order_id in self._inflight_trade_ids:
            try:
                pos_changed = self.api.get_async_order(int(order_id)).get("position-changed", {})
                if pos_changed:
                    msg = pos_changed.get("msg", {})
                    if msg.get("status") == "closed":
                        if msg.get("close_reason") == "expired":
                            profit = float(msg.get("close_profit", 0)) - float(msg.get("invest", 0))
                        else:
                            profit = float(msg.get("pnl_realized", 0))
                        total_profit += profit
                        resolved += 1
                        logger.info(f"  Order {order_id}: P/L=${profit:.2f} (resolved)")
                        continue

                logger.info(f"  Order {order_id}: Not in cache. Waiting up to 15s...")
                start_t = time.time()
                while time.time() - start_t < 15:
                    pos_changed = self.api.get_async_order(int(order_id)).get("position-changed", {})
                    if pos_changed:
                        msg = pos_changed.get("msg", {})
                        if msg.get("status") == "closed":
                            if msg.get("close_reason") == "expired":
                                profit = float(msg.get("close_profit", 0)) - float(msg.get("invest", 0))
                            else:
                                profit = float(msg.get("pnl_realized", 0))
                            total_profit += profit
                            resolved += 1
                            logger.info(f"  Order {order_id}: P/L=${profit:.2f} (resolved after wait)")
                            break
                    time.sleep(1)
                else:
                    logger.warning(f"  Order {order_id}: Could not resolve. Treating as lost.")
                    total_profit -= self.current_bet  # conservative: assume loss
                    resolved += 1

            except Exception as e:
                logger.warning(f"  Order {order_id}: Error during reconciliation: {e}")

        self._inflight_trade_ids = []

        if resolved > 0:
            logger.info(f"📈 Reconciliation complete: {resolved} trades, net P/L=${total_profit:.2f}")

            if total_profit >= 0:
                logger.info("Previous round WON! Resetting ladder to step 1.")
                self.session_profit += total_profit
                self.total_profit += total_profit
                self.session_total_profit += total_profit
                self.cumulative_debt = max(0.0, self.cumulative_debt - total_profit)
                self._record_window_profit(total_profit)
                self._finalize_session("Round Won (reconciled)")
                self._resuming_mid_ladder = False
            else:
                next_step = self.session_round_count + 1
                tier = self.budget_tiers[self.current_tier_index]
                self.session_profit += total_profit
                self.total_profit += total_profit
                self.session_total_profit += total_profit
                self.cumulative_debt += abs(total_profit)
                self._record_window_profit(total_profit)
                self._resuming_mid_ladder = True
                if next_step >= len(tier):
                    if getattr(self, 'sequential_steps_mode', False):
                        self.session_round_count = 0
                        self.cumulative_debt = 0.0
                        self._reset_ladder_tracking()
                        logger.warning(
                            f"Sequential LOSE all steps (net ${total_profit:.2f}) → wrapping to step 1"
                        )
                    else:
                        self.session_round_count = next_step
                        logger.warning(
                            f"Previous round LOST — all {len(tier)} tier steps exhausted. Cooldown."
                        )
                        self._finalize_session("Tier exhausted")
                else:
                    self.session_round_count = next_step
                    logger.warning(
                        f"Previous round LOST (net ${total_profit:.2f}). "
                        f"Advancing ladder to step {self.session_round_count + 1}."
                    )

            self.persist_state("reconciled")
            self._maybe_sync_balance_after_trade()

    def _server_timestamp(self):
        try:
            if self.api and getattr(self.api, "api", None):
                ts = getattr(self.api.api, "timesync", None)
                if ts is not None:
                    return float(ts.server_timestamp)
        except Exception:
            pass
        return time.time()

    def _server_now(self):
        return datetime.datetime.utcfromtimestamp(self._server_timestamp())

    def _server_second(self):
        return int(self._server_timestamp()) % 60

    def _seconds_past_minute(self):
        return self._server_timestamp() % 60

    def _seconds_past_candle(self):
        # Time since the start of the current candle — use server UTC to match IQ Option boundaries
        ts = self._server_timestamp()
        tf = int(getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60))
        return ts % tf

    def _expiry_from_symbol(self, symbol):
        date_str = symbol.split("A")[1][:8]
        time_str = symbol.split("D")[1][:6]
        return datetime.datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")

    def _seconds_to_expiry(self, symbol, now=None):
        if now is None:
            now = self._server_now()
        return (self._expiry_from_symbol(symbol) - now).total_seconds()

    def _strike_in_expiry_window(self, symbol, now=None):
        try:
            secs = self._seconds_to_expiry(symbol, now=now)
            return self.min_seconds_to_expiry <= secs <= self.max_seconds_to_expiry
        except Exception:
            return False

    def _log_entry_timing(self, phase):
        sec = self._server_second()
        logger.info(
            f"⏱ Entry timing [{phase}]: IQ server :{sec:02d} "
            f"(target :{self.entry_window_start:02d}–:{self.entry_window_end:02d}, "
            f"deadline <:{self.purchase_deadline_sec:02d})"
        )

    def _inside_entry_window(self):
        sec = self._seconds_past_candle()
        return self.entry_window_start <= sec <= self.entry_window_end

    def _past_entry_hard_abort(self):
        return self._seconds_past_candle() >= self.entry_hard_abort_sec

    def _wait_for_next_entry(self):
        # Wait for the start of the next candle
        start = self.entry_window_start
        end = self.entry_window_end
        seconds_past = self._seconds_past_candle()
        tf = int(getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60))

        if seconds_past < start:
            wait = start - seconds_past
        elif seconds_past > end:
            wait = (tf - seconds_past) + start
        else:
            wait = 0

        if wait > 0:
            logger.info(
                f"⏳ Waiting {wait:.1f}s for entry window "
                f"(now {seconds_past}s past candle boundary)..."
            )
            if not self._interruptible_sleep(wait):
                return

        self._log_entry_timing("window ready")

    def _skip_to_next_entry_window(self, reason):
        tf = int(getattr(app_config, "FOLLOW_CANDLE_TIMEFRAME", 60))
        seconds_past = self._seconds_past_candle()
        wait = max(1.0, (tf - seconds_past) + self.entry_window_start)
        tier = self.current_tier_index + 1
        step = self.session_round_count + 1
        label = reason.replace("_", " ").replace("-", " ")
        self.status_note = f"⏳ Waiting {wait:.0f}s for next candle — {label}"
        logger.info(
            f"Still Tier {tier} step {step}/{self.session_max_rounds} — "
            f"skip ({reason}). Next candle boundary in {wait:.1f}s..."
        )
        if not self._interruptible_sleep(wait):
            return
        self.status_note = ""

    def _passes_volatility_filters(self, call_info, put_info, check_momentum=True):
        """Delegates to unified straddle suitability (same rules as pair ranking)."""
        assess = self._assess_straddle_suitability(
            self.asset,
            call_info=call_info,
            put_info=put_info,
            check_momentum=check_momentum,
        )
        self.last_pair_quality = assess
        if assess["tradeable"]:
            logger.info(
                f"Straddle gate OK: ER={assess['efficiency_ratio']:.3f}, "
                f"slope={assess['abs_slope']:.1f}, "
                f"straddle_score={assess['straddle_score']:.0f}"
            )
            return True, ""
        return False, assess["reason"]

    # ── Evaluation windows & debt ────────────────────────────────────────────

    def _evaluation_window_boundary(self, now=None):
        """Start of the current clock-aligned evaluation window (UTC)."""
        now = now or datetime.datetime.utcnow()
        block_minute = (now.minute // EVALUATION_WINDOW_MINUTES) * EVALUATION_WINDOW_MINUTES
        return now.replace(minute=block_minute, second=0, microsecond=0)

    def _next_evaluation_window_boundary(self, window_start=None):
        start = window_start or self._evaluation_window_boundary()
        return start + datetime.timedelta(minutes=EVALUATION_WINDOW_MINUTES)

    def _init_evaluation_window_state(self):
        """Fresh 15-minute evaluation window and tier-assignment counters."""
        self.assigned_tier_index = 0
        self.tier_failure_streak = 0
        self.window_profit = 0.0
        self.evaluation_window_start = self._evaluation_window_boundary()
        self.tier_exhaustion_cooldown_until = None
        self.last_tier_exhaustion_at = None
        self.window_had_tier_exhaustion = False

    def _apply_evaluation_window_persisted(self, data):
        """Restore evaluation-window fields (backward compatible with older saves)."""
        self.assigned_tier_index = int(
            data.get("assigned_tier_index", data.get("current_tier_index", 0))
        )
        self.tier_failure_streak = int(data.get("tier_failure_streak", 0))
        self.tier_recovery_wins = int(data.get("tier_recovery_wins", 0))
        self.window_profit = 0.0
        self.window_had_tier_exhaustion = bool(data.get("window_had_tier_exhaustion", False))

        window_raw = data.get("evaluation_window_start")
        if window_raw:
            try:
                self.evaluation_window_start = datetime.datetime.fromisoformat(
                    window_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                self.evaluation_window_start = self._evaluation_window_boundary()
        else:
            self.evaluation_window_start = self._evaluation_window_boundary()

        cooldown_raw = data.get("tier_exhaustion_cooldown_until")
        if cooldown_raw:
            try:
                self.tier_exhaustion_cooldown_until = datetime.datetime.fromisoformat(
                    cooldown_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                self.tier_exhaustion_cooldown_until = None
        else:
            self.tier_exhaustion_cooldown_until = None

        last_exhaust_raw = data.get("last_tier_exhaustion_at")
        if last_exhaust_raw:
            try:
                self.last_tier_exhaustion_at = datetime.datetime.fromisoformat(
                    last_exhaust_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except Exception:
                self.last_tier_exhaustion_at = None
        else:
            self.last_tier_exhaustion_at = None

        max_tier = len(self.budget_tiers) - 1
        self.assigned_tier_index = min(max(0, self.assigned_tier_index), max_tier)
        if self.cumulative_debt <= 0:
            self.tier_failure_streak = 0

    def _failures_before_escalation(self, tier_index):
        if tier_index <= 0:
            return TIER_1_FAILURES_BEFORE_ESCALATE
        return TIER_HIGHER_FAILURES_BEFORE_ESCALATE

    def _maybe_roll_evaluation_window(self):
        """Close the current window and assign the next tier when the boundary passes."""
        if not self.evaluation_window_start:
            self._init_evaluation_window_state()
            return False

        now = datetime.datetime.utcnow()
        if now < self._next_evaluation_window_boundary(self.evaluation_window_start):
            return False

        self._close_evaluation_window()
        return True

    def _close_evaluation_window(self):
        """End-of-window bookkeeping — tier assignment is NOT advanced on a timer."""
        window_pl = self.window_profit
        debt = self.cumulative_debt
        assigned = self.assigned_tier_index
        needed = self._failures_before_escalation(assigned)
        logger.info(
            f"⏱ Evaluation window closed — window P/L ${window_pl:.2f}, "
            f"debt ${debt:.2f}, assigned Tier {assigned + 1}, "
            f"exhaustion streak {self.tier_failure_streak}/{needed}"
        )

        if debt <= 0:
            # Guard: if we are above T0 (mid-round on any tier), do NOT reset to
            # T0 just because the evaluation window expired.  Tier transitions are
            # driven exclusively by exhaustion / reserve-win completion, never by
            # the timer.  This covers:
            #   • T1/T3/T5 reserve recovery with pending reserve_wins_needed
            #   • T2/T4 main-tier play (cumulative_debt held at 1.0 sentinel)
            if self.current_tier_index > 0:
                wins_left = getattr(self, 'reserve_wins_needed', 0)
                logger.info(
                    f"Window closed — debt cleared but mid-round at T{self.current_tier_index + 1}"
                    + (f", {wins_left} reserve win(s) still needed" if wins_left else "")
                    + ". Holding position (no timer-based tier reset)."
                )
                self.window_profit = 0.0
                self.window_had_tier_exhaustion = False
                self.evaluation_window_start = self._evaluation_window_boundary()
                return
            self.tier_failure_streak = 0
            floor = self._balance_baseline_tier_index()
            self.assigned_tier_index = floor
            self.current_tier_index = floor
            if self.session_round_count > 0:
                logger.info(
                    f"Window closed — debt cleared but mid-ladder at step "
                    f"{self.session_round_count + 1}; preserving step "
                    f"(no timer-based reset)."
                )
            else:
                logger.info(f"Window success — debt cleared, baseline Tier {floor + 1}.")
        else:
            # Do not escalate or reset step just because 15 minutes passed.
            # Stay on the assigned tier until it is fully exhausted N times.
            self._sync_assigned_tier_for_trading()
            logger.info(
                f"Window closed with debt ${debt:.2f} — "
                f"continue Tier {self.current_tier_index + 1} "
                f"step {self.session_round_count + 1} "
                f"(no timer-based tier escalation)"
            )

        self.window_profit = 0.0
        self.window_had_tier_exhaustion = False
        self.evaluation_window_start = self._evaluation_window_boundary()
        self._sync_ladder_indices()
        self.persist_state("evaluation window closed")

    def _is_tier_exhaustion_cooldown_active(self):
        if not self.tier_exhaustion_cooldown_until:
            return False
        now = datetime.datetime.utcnow()
        if now >= self.tier_exhaustion_cooldown_until:
            self.tier_exhaustion_cooldown_until = None
            return False
        return True

    def _wait_for_tier_exhaustion_cooldown(self):
        if not self._is_tier_exhaustion_cooldown_active():
            return
        remaining = (
            self.tier_exhaustion_cooldown_until - datetime.datetime.utcnow()
        ).total_seconds()
        logger.info(
            f"Tier exhaustion cooldown — waiting {max(0.0, remaining):.0f}s "
            f"before retrying Tier {self.assigned_tier_index + 1}"
        )
        end_t = time.time() + max(0.0, remaining)
        while time.time() < end_t and self.running and not self.paused:
            if not self._interruptible_sleep(min(1.0, end_t - time.time())):
                return

    def _sync_assigned_tier_for_trading(self, balance=None):
        """
        Enforce balance baseline floor and keep assigned/current tiers aligned.
        With no debt, baseline tier tracks capital (never play below floor).
        With debt, assigned tier may escalate above floor for recovery.
        Risk governor caps max tier from tradable balance and drawdown mode.
        In CRM mode, normal tier sync is bypassed — CRM manages its own ladder.
        """
        if balance is None:
            balance = self.safe_get_balance()
        self._update_budget_tiers_for_balance(balance)
        limits = self._update_and_get_risk_limits(balance)
        floor = self._balance_baseline_tier_index(balance)

        if self.cumulative_debt <= 0:
            # Do NOT reset to floor if we are above T0 (mid-round sequence in progress).
            # T1/T2/T3/T4/T5 manage their own tier transitions; the sync routine must
            # not pull us back to T0 between trades.
            if self.current_tier_index > 0:
                return
            self.assigned_tier_index = floor
            if self.current_tier_index < floor:
                logger.info(
                    f"Balance ${balance:.2f} → baseline Tier {floor + 1} "
                    f"(raised from Tier {self.current_tier_index + 1})"
                )
                self.current_tier_index = floor
                self.session_round_count = 0
            elif self.current_tier_index > floor:
                logger.info(
                    f"Balance ${balance:.2f} → baseline Tier {floor + 1} "
                    f"(capital drop from Tier {self.current_tier_index + 1})"
                )
                self.current_tier_index = floor
                self.session_round_count = 0
        else:
            if self.assigned_tier_index < floor:
                logger.warning(
                    f"Balance baseline Tier {floor + 1} — raising assigned "
                    f"from Tier {self.assigned_tier_index + 1}"
                )
                self.assigned_tier_index = floor
            if self.current_tier_index < floor:
                self.current_tier_index = floor
                self.session_round_count = 0
            elif self.current_tier_index > self.assigned_tier_index:
                # Mid-recovery: current_tier was set by escalation/win logic and is
                # ahead of assigned. Correct assigned UPWARD to match current — never
                # clamp current down, which abandons the recovery sequence.
                logger.warning(
                    f"Ladder drift: current Tier {self.current_tier_index + 1} ahead of "
                    f"assigned Tier {self.assigned_tier_index + 1} — correcting assigned "
                    f"upward (debt=${self.cumulative_debt:.2f})"
                )
                self.assigned_tier_index = self.current_tier_index
            elif self.current_tier_index != self.assigned_tier_index:
                self.current_tier_index = self.assigned_tier_index
                self.session_round_count = 0

        self._apply_risk_tier_caps(limits)
        # Ladder progression uses full balance — tradable (after profit lock) is for tier caps only.
        self._apply_balance_ladder_downgrade(balance=balance)

    def _record_window_profit(self, amount):
        if not hasattr(self, "window_profit"):
            self.window_profit = 0.0
        self.window_profit += float(amount)

    def _start_tier_exhaustion_cooldown(self):
        now = datetime.datetime.utcnow()
        cooldown_until = now + datetime.timedelta(
            minutes=TIER_EXHAUSTION_COOLDOWN_MINUTES
        )
        pause_mins = TIER_EXHAUSTION_COOLDOWN_MINUTES
        self.status_note = (
            f"⏰ Tier {self.assigned_tier_index + 1} exhausted — "
            f"cooling down {pause_mins}m before escalating to Tier {self.assigned_tier_index + 2}"
        )
        logger.warning(
            f"Tier exhaustion cooldown — {pause_mins}m pause before escalating "
            f"from Tier {self.assigned_tier_index + 1} to Tier {self.assigned_tier_index + 2}"
        )
        self.last_tier_exhaustion_at = now
        self.tier_exhaustion_cooldown_until = cooldown_until
        self.window_had_tier_exhaustion = True

    def _tier_max_loss(self, tier_index):
        if tier_index < 0 or tier_index >= len(self.budget_tiers):
            return 0.0
        return 2.0 * sum(self.budget_tiers[tier_index])

    def _current_tier_step_count(self):
        if not self.budget_tiers or self.current_tier_index >= len(self.budget_tiers):
            return 0
        return len(self.budget_tiers[self.current_tier_index])

    def _all_tier_steps_exhausted(self):
        """
        True only after losing on every step of the current tier (e.g. $1, $3, $9).
        session_round_count becomes equal to step count after the last loss.
        """
        steps = self._current_tier_step_count()
        return steps > 0 and self.session_round_count >= steps

    def _apply_round_loss_to_debt(self, round_profit):
        """Track debt on each lost round; cycle finalize only applies wins."""
        if round_profit >= 0:
            return
        loss_amt = abs(round_profit)
        self.cumulative_debt += loss_amt
        logger.info(f"Loss ${loss_amt:.2f} added to debt. Total debt: ${self.cumulative_debt:.2f}")

    def _apply_cycle_profit_to_debt(self):
        pass

    def _return_to_tier_one_step_one_if_debt_cleared(self):
        """When debt hits zero, reset to balance-appropriate baseline tier step 1."""
        if self.cumulative_debt <= 0:
            self.cumulative_debt = 0.0
            self.tier_failure_streak = 0
            floor = self._balance_baseline_tier_index()
            self.assigned_tier_index = floor
            self.current_tier_index = floor
            self.session_round_count = 0
            logger.info(
                f"All debt recovered — baseline Tier {floor + 1} step 1 "
                f"(balance ${self.safe_get_balance():.2f})."
            )
            return True
        return False

    def _maybe_escalate_assigned_tier_after_exhaustion(self):
        """
        Compartmentalised 2-tier active pair system.
        - T0 exhausted → escalate to T1 (recovery tier for this balance bracket).
        - T1 exhausted → enter Capital Recovery Mode (CRM) instead of T2/T3/T4.
        - CRM-T1 exhausted → advance to CRM-T2 (backup).
        - CRM-T2 exhausted → accept total loss, reset cleanly to balance-floor tier.
        Always returns False (no hard stop).
        """
        # ── Normal mode: sequential 6-tier escalation across 3 rounds ───────────
        # Structure:
        #   Round 1: T0 (main) → T1 (reserve)
        #   Round 2: T2 (main) → T3 (reserve)  triggered when Round 1 is fully lost
        #   Round 3: T4 (main) → T5 (reserve)  triggered when Round 2 is fully lost
        #   T5 exhausted → total loss, clean reset to T0
        current_tier = self.assigned_tier_index
        max_tier = len(self.budget_tiers) - 1  # = 5

        self.tier_failure_streak = 0
        self.tier_recovery_wins = 0
        self.session_round_count = 0
        self.last_tier_exhaustion_at = None

        if current_tier < max_tier:
            next_tier = current_tier + 1
            self.assigned_tier_index = next_tier
            self.current_tier_index = next_tier
            ladder = self.budget_tiers[next_tier]

            # Even index = new round's main tier (T2 or T4). Prior round fully lost.
            if next_tier % 2 == 0:
                round_num = next_tier // 2 + 1
                logger.warning(
                    f"💀 Round {round_num - 1} fully lost → starting Round {round_num} "
                    f"T{next_tier + 1} [{', '.join(f'${x:.0f}' for x in ladder)}]."
                )
                self._notify(
                    f"Round {round_num} started",
                    f"Round {round_num - 1} exhausted. "
                    f"Playing Round {round_num} (T{next_tier + 1}+T{next_tier + 2}).",
                )
            else:
                # T1/T3/T5: reserve tier within the same round (main tier exhausted).
                # Reset wins counter — the reserve tier needs exactly 3 wins (max) to
                # fully recover the main tier's loss, regardless of which steps are won.
                round_num = next_tier // 2 + 1
                self.reserve_wins_needed = 3
                logger.warning(
                    f"💀 T{current_tier + 1} exhausted → Round {round_num} reserve "
                    f"T{next_tier + 1} [{', '.join(f'${x:.0f}' for x in ladder)}]. "
                    f"Needs 3 reserve wins to recover."
                )
                self._notify(
                    f"T{current_tier + 1} exhausted",
                    f"Escalating to T{next_tier + 1} (Round {round_num} reserve). "
                    f"3 wins needed to recover.",
                )
        else:
            # T5 fully exhausted — accept total loss and reset cleanly to T0.
            logger.warning(
                f"❌ T5 (Round 3 reserve) exhausted — accepting total loss. "
                f"Resetting to T0 S1."
            )
            self._notify(
                "All rounds exhausted",
                "T5 lost. Accepting total loss and resetting to T0 S1.",
            )
            self.current_tier_index = 0
            self.assigned_tier_index = 0
            self.round_collected = 0.0
            self.round_target = 0.0
            self.cumulative_debt = 0.0

        return False

    def _apply_win_ladder_rules(self):
        """
        Win ladder rules (multi-round 6-tier system with wins-counter recovery):

        ANY win always resets to S1 of the CURRENT tier — steps are NEVER
        advanced on a win.  Tier only changes on exhaustion (all steps lost)
        or when a reserve tier's wins counter reaches zero.

        Reserve tier wins counter (reserve_wins_needed):
        ─────────────────────────────────────────────────
        When a main tier (T0/T2/T4) is exhausted, the reserve tier starts
        with reserve_wins_needed = 3.  Each win at step N (0-based) earns
        (N+1) wins toward the counter:
          S1 win → earns 1  (need 2 more at minimum)
          S2 win → earns 2  (covers the S1 loss + 1 recovery win)
          S3 win → earns 3  (covers S1+S2 losses + all 3 wins) → always done

        When counter reaches 0 → return to T0 S1 (recovery complete).
        If the reserve tier exhausts (all 3 steps lost) → escalate to next round.

        Main tier wins (T0/T2/T4):
          Reset to S1 of the same tier.  No counter change.
        """
        if getattr(self, 'sequential_steps_mode', False):
            tier = self.budget_tiers[self.current_tier_index]
            next_step = (self.session_round_count + 1) % len(tier)
            self.session_round_count = next_step
            self.cumulative_debt = 0.0
            logger.info(
                f"🔄 Sequential WIN → advancing to step {next_step + 1}/{len(tier)} "
                f"(no reset — sequential mode active)"
            )
            return

        tier_idx  = self.current_tier_index
        step_idx  = self.session_round_count          # 0-based step that just won
        step_label = step_idx + 1                     # 1-based for logs
        is_reserve = tier_idx in ROUND_RESERVE_TIERS  # {1, 3, 5}

        self.session_round_count = 0  # always back to S1 of current tier
        self._reset_ladder_tracking()
        self.tier_recovery_wins = 0

        if not is_reserve:
            # ── Main tier (T0/T2/T4) win ──────────────────────────────────────
            floor = self._balance_baseline_tier_index()
            if tier_idx == 0:
                # T0: normal win — zero debt, stay at baseline floor
                self.cumulative_debt = 0.0
                self.assigned_tier_index = floor
                self.current_tier_index  = floor
                logger.info(
                    f"🏆 WIN on T{tier_idx + 1} S{step_label} → T{tier_idx + 1} S1"
                )
            else:
                # T2/T4: recovery round — each win chips away at cumulative debt.
                # The win reconciler already reduced cumulative_debt before calling
                # here; we just need to decide whether debt is now cleared.
                if self.cumulative_debt <= 0:
                    # All prior-round debt cleared → return to T0.
                    self.cumulative_debt = 0.0
                    self.assigned_tier_index = floor
                    self.current_tier_index  = floor
                    logger.info(
                        f"🏆 WIN on T{tier_idx + 1} S{step_label} — all prior debt "
                        f"cleared. Returning to T{floor + 1} S1."
                    )
                    self._notify(
                        "Debt cleared — returning to Round 1",
                        f"T{tier_idx + 1} win cleared all accumulated losses. "
                        f"Resuming T{floor + 1}.",
                    )
                else:
                    # Debt still outstanding — stay on T2/T4 and keep recovering.
                    # Flag a fresh asset scan before the next S1 so the bot is not
                    # stuck on a pair that may have reversed since the sequence began.
                    self.assigned_tier_index = tier_idx
                    self._pending_recovery_rescan = True
                    logger.info(
                        f"🏆 WIN on T{tier_idx + 1} S{step_label} — "
                        f"${self.cumulative_debt:.2f} debt remains. "
                        f"Back to T{tier_idx + 1} S1 (will rescan for best asset)."
                    )
            return

        # ── Reserve tier (T1/T3/T5) — wins counter ───────────────────────────
        # Winning at step N earns (N+1) wins toward recovery.
        wins_earned = step_idx + 1
        self.reserve_wins_needed = max(
            0, getattr(self, 'reserve_wins_needed', 3) - wins_earned
        )
        round_num = tier_idx // 2 + 1

        if self.reserve_wins_needed == 0:
            if tier_idx == 1:
                # T1 done — Round 1 fully recovered, return to T0 S1.
                self.cumulative_debt = 0.0
                self.current_tier_index  = 0
                self.assigned_tier_index = 0
                logger.info(
                    f"✅ T{tier_idx + 1} S{step_label} win earned {wins_earned} — "
                    f"Round 1 recovery complete. Returning to T0 S1."
                )
                self._notify(
                    f"Round {round_num} recovery complete",
                    f"T{tier_idx + 1} S{step_label} win covered all losses. "
                    f"Resuming Round 1 (T0).",
                )
            else:
                # T3/T5 reserve done — the current round's main-tier loss is
                # covered by these 3 reserve wins.  But ALL accumulated debt
                # (prior rounds included) must reach zero before the bot can
                # return to T0.  The win reconciler has already reduced
                # cumulative_debt by this win's profit; check the remainder.
                main_tier = tier_idx - 1  # T3 → T2, T5 → T4
                if self.cumulative_debt <= 0:
                    # All prior-round debt cleared — return to T0.
                    self.cumulative_debt     = 0.0
                    self.current_tier_index  = 0
                    self.assigned_tier_index = 0
                    logger.info(
                        f"✅ T{tier_idx + 1} S{step_label} win earned {wins_earned} — "
                        f"all debt cleared. Returning to T0 S1."
                    )
                    self._notify(
                        f"Round {round_num} recovery complete",
                        f"T{tier_idx + 1} cleared all accumulated losses. "
                        f"Resuming Round 1 (T0).",
                    )
                else:
                    # Still debt to recover — cycle back to T2/T4 and keep going.
                    # T2/T4 wins will chip away further; when T2/T4 exhausts again
                    # it will re-enter T3/T5 with reserve_wins_needed reset to 3.
                    self.current_tier_index  = main_tier
                    self.assigned_tier_index = main_tier
                    logger.info(
                        f"✅ T{tier_idx + 1} S{step_label} win earned {wins_earned} — "
                        f"Round {round_num} main loss covered but ${self.cumulative_debt:.2f} "
                        f"prior-round debt remains. Back to T{main_tier + 1} S1."
                    )
                    self._notify(
                        f"Round {round_num} reserve done — continuing recovery",
                        f"T{tier_idx + 1} covered its round's losses. "
                        f"${self.cumulative_debt:.2f} prior debt remains — "
                        f"resuming T{main_tier + 1} to recover it.",
                    )
        else:
            # More reserve wins needed — stay on reserve tier S1.
            # Do NOT zero cumulative_debt here. The natural debt reduction (via
            # _finalize_session / round win reconciliation) already subtracted this
            # win's profit. Zeroing would cause _close_evaluation_window and
            # _return_to_tier_one_step_one_if_debt_cleared to wrongly see debt=0
            # and reset to T0 before all recovery wins are collected.
            self.assigned_tier_index = tier_idx
            logger.info(
                f"💰 T{tier_idx + 1} S{step_label} win earned {wins_earned} — "
                f"{self.reserve_wins_needed} more win(s) needed. "
                f"Debt remaining ${self.cumulative_debt:.2f}. Back to T{tier_idx + 1} S1."
            )

    def _finalize_session(self, reason):
        """Apply debt/tier rules after a win or after all steps on a tier are lost."""
        cycle_pl_before = self.session_profit
        logger.info(f"Ladder cycle ({reason}). Cycle P/L: ${cycle_pl_before:.2f}")

        self._apply_cycle_profit_to_debt()
        old_tier = self.current_tier_index
        hard_stop = False

        if reason == "Tier exhausted":
            # A tier exhaustion means the pair failed — clear the hot-pair streak
            if self.asset == self._hot_pair and self._hot_pair_consecutive_wins > 0:
                logger.info(
                    f"❄️ Hot pair {self._hot_pair} tier exhausted — clearing streak "
                    f"({self._hot_pair_consecutive_wins} wins lost)."
                )
                self._hot_pair = ""
                self._hot_pair_consecutive_wins = 0
            if not self._all_tier_steps_exhausted():
                logger.error(
                    f"Tier exhausted called prematurely "
                    f"(step {self.session_round_count}/{self._current_tier_step_count()}). "
                    f"Not applying exhaustion handling."
                )
            else:
                # Penalty box is managed solely by _record_ladder_exhaustion_and_check_penalty
                # (called just before _finalize_session). A pair is only blacklisted if it
                # exhausts the full ladder ≥2 times within 15 minutes — not on every exhaustion.
                self._start_tier_exhaustion_cooldown()
                if old_tier == self.assigned_tier_index:
                    hard_stop = self._maybe_escalate_assigned_tier_after_exhaustion()
                else:
                    logger.warning(
                        f"Exhaustion on Tier {old_tier + 1} but assigned recovery tier is "
                        f"Tier {self.assigned_tier_index + 1} — no escalation count"
                    )
                    self.current_tier_index = self.assigned_tier_index
                    self.session_round_count = 0
                cooldown_mins = TIER_EXHAUSTION_COOLDOWN_MINUTES
                logger.warning(
                    f"💀 TIER {old_tier + 1} EXHAUSTED — ALL STEPS LOST! "
                    f"Cooldown {cooldown_mins}m, then "
                    f"{'STOP' if hard_stop else f'retry Tier {self.assigned_tier_index + 1} step 1'} "
                    f"| Debt: ${self.cumulative_debt:.2f}"
                )
                self._notify(
                    "Tier exhausted",
                    f"Tier {old_tier + 1} all steps lost. "
                    f"{cooldown_mins}m cooldown, then "
                    f"{'hard stop' if hard_stop else f'Tier {self.assigned_tier_index + 1} step 1'}. "
                    f"Debt ${self.cumulative_debt:.2f}",
                )
            if not hard_stop:
                self.session_round_count = 0
                self._reset_ladder_tracking()
            if self.auto_select_asset:
                self._apply_auto_asset_selection(reason="tier exhausted penalty")
        elif reason.startswith("Round Won"):
            self._apply_win_ladder_rules()
            # Hot-pair loyalty: track consecutive wins on the same pair
            if self.asset == self._hot_pair:
                self._hot_pair_consecutive_wins += 1
            else:
                self._hot_pair = self.asset
                self._hot_pair_consecutive_wins = 1
            logger.info(
                f"🔥 Hot pair: {self._hot_pair} "
                f"({self._hot_pair_consecutive_wins} consecutive win(s) this session)"
            )
            logger.info(
                f"Round won — next bet Tier {self.current_tier_index + 1} "
                f"step {self.session_round_count + 1}."
            )
        else:
            logger.warning(f"Unknown finalize reason: {reason}")

        if not reason.startswith("Round Won"):
            self._return_to_tier_one_step_one_if_debt_cleared()
        self.session_profit = 0.0
        self._sync_ladder_indices()
        self._last_ladder_prep_key = None

        tier = self.budget_tiers[self.current_tier_index]
        logger.info(
            f"📊 STATE: Tier {self.current_tier_index + 1} "
            f"step {self.session_round_count + 1}/{len(tier)} "
            f"(${float(tier[min(self.session_round_count, len(tier)-1)]):.0f}) "
            f"| Debt: ${self.cumulative_debt:.2f} "
            f"| Ladder: {[float(x) for x in tier]}"
        )
        self.persist_state(reason)

        if hard_stop:
            self.running = False

    # ── Main Loop ────────────────────────────────────────────────────────────

    def run(self):
        if not self._trading_loop_lock.acquire(blocking=False):
            logger.error("Duplicate trading loop blocked — another run() is already active")
            return
        try:
            self._run_trading_loop()
        finally:
            self._trading_loop_lock.release()

    def _run_trading_loop(self):
        logger.info("=" * 60)
        logger.info("DOUBLE MARTINGALE BOT STARTING")
        logger.info(f"  Asset:        {self.asset}")
        logger.info(f"  Min Profit:   {self.min_profit_pct:.0f}%")
        logger.info(f"  Tier ladders: {STANDARD_BUDGET_TIERS}")
        logger.info(
            f"  Evaluation: {EVALUATION_WINDOW_MINUTES}m windows, "
            f"{TIER_EXHAUSTION_COOLDOWN_MINUTES}m / "
            f"{TIER_SECOND_EXHAUSTION_COOLDOWN_MINUTES}m exhaustion cooldown"
        )
        self._trading_bootstrapped = False
        if self.simulation_mode:
            logger.info("  *** SIMULATION MODE — no real orders ***")
        logger.info("=" * 60)

        if not self.running:
            logger.info("Stop received before trading loop started — exiting")
            self.last_stop_reason = self.last_stop_reason or "Stopped before loop entry"
            self.persist_state(self.last_stop_reason)
            return

        self.paused = False
        self.last_error = ""

        if not self.is_session_ready():
            self.running = False
            self.last_error = (
                "IQ Option not connected. Click Reconnect, wait for Connected + balances, then Start."
            )
            self.last_stop_reason = "Start failed — no IQ session"
            logger.error(self.last_error)
            self.persist_state(self.last_stop_reason)
            return

        logger.info("Trading on existing IQ Option session (no reconnect).")
        self.last_stop_reason = ""
        self.persist_state("started")

        feed_ready = False
        with self._price_lock:
            feed_ready = self._market_feed_active and 60 in self._price_data

        if feed_ready:
            logger.info("Resuming — warm market feed still active (no re-subscribe).")
        else:
            self._install_price_sniffer()
            self._subscribe()
            self._market_feed_active = True

        logger.info("Waiting for initial price data...")
        got_prices = feed_ready
        wait_secs = 15 if feed_ready else 90
        for i in range(wait_secs):
            if not self.running:
                self.last_stop_reason = "Stopped during startup"
                self.persist_state(self.last_stop_reason)
                return
            with self._price_lock:
                if 60 in self._price_data:
                    got_prices = True
                    break
            if i > 0 and i % 15 == 0:
                logger.info(f"Still waiting for price data... ({i}s)")
            if not self._interruptible_sleep(1):
                self.last_stop_reason = "Stopped during startup"
                self.persist_state(self.last_stop_reason)
                return

        if not got_prices:
            if self.auto_select_asset:
                logger.warning(f"No price data for {self.asset}, attempting auto asset selection...")
                self._apply_auto_asset_selection(reason="startup failure", relaxed=True)
                with self._price_lock:
                    if 60 in self._price_data:
                        got_prices = True

            if not got_prices:
                msg = (
                    f"No 1-minute price data for {self.asset} after {wait_secs}s. "
                    "Market may be closed or subscription failed."
                )
                logger.error(msg)
                self.last_error = msg
                self.last_stop_reason = "Aborted — no strike prices"
                self.running = False
                self._unsubscribe()
                self.persist_state(self.last_stop_reason)
                return

        logger.info("Price data received. Entering trading loop.")
        self._reconcile_inflight_trades()

        if self.api and app_config.USE_TRADER_MOOD:
            try:
                self.api.start_mood_stream(self.asset, instrument="turbo-option" if self.trading_mode == "turbo" else "digital-option")
            except Exception as e:
                logger.debug(f"Failed to start initial mood stream: {e}")

        try:
            while self.running:
                try:
                    while self.paused and self.running:
                        if not self._interruptible_sleep(2):
                            break

                    if not self.running:
                        break

                    if not self._ensure_api_connection():
                        if not self._interruptible_sleep(10):
                            break
                        continue

                    self._maybe_sync_balance_idle()
                    self._maybe_roll_evaluation_window()
                    if self._is_tier_exhaustion_cooldown_active():
                        self._wait_for_tier_exhaustion_cooldown()
                        continue

                    self._sync_assigned_tier_for_trading()

                    if self._is_blocked_time_window():
                        logger.info(
                            f"🚫 Blocked time window ({self.trading_timezone}) — "
                            "no new rounds"
                        )
                        self.last_asset_selection_note = "Blocked time window"
                        self.persist_state()
                        if not self._interruptible_sleep(30):
                            break
                        self._ensure_api_connection()
                        continue

                    _utc_banned, _utc_ban_reason = self._is_utc_ban_window()
                    if _utc_banned:
                        logger.info(f"🚫 {_utc_ban_reason} — no new rounds")
                        self.last_asset_selection_note = _utc_ban_reason
                        self.persist_state()
                        if not self._interruptible_sleep(30):
                            break
                        self._ensure_api_connection()
                        continue

                    _soft_banned, _soft_ban_reason = self._is_utc_soft_ban_for_asset(self.asset)
                    if _soft_banned:
                        logger.info(f"🚫 {_soft_ban_reason} — switching asset")
                        self.last_asset_selection_note = _soft_ban_reason
                        if self.auto_select_asset:
                            self._apply_auto_asset_selection(reason=_soft_ban_reason)
                        else:
                            if not self._interruptible_sleep(30):
                                break
                        continue

                    if self._is_legacy_blocked_hour():
                        current_hour = datetime.datetime.now().hour
                        logger.info(
                            f"🚫 Blocked hour ({current_hour}:00) — sleeping 60s"
                        )
                        self.last_asset_selection_note = f"Blocked hour ({current_hour}:00)"
                        self.persist_state()
                        if not self._interruptible_sleep(60):
                            break
                        self._ensure_api_connection()
                        continue


                    if (
                        self.last_trade_time
                        and (time.time() - self.last_trade_time)
                        > self.stale_trade_alert_minutes * 60
                    ):
                        mins = int((time.time() - self.last_trade_time) / 60)
                        self._notify(
                            "No trades recently",
                            f"No completed round in {mins} min on {self.asset}",
                        )
                        self.last_trade_time = time.time()

                    current_balance = self.safe_get_balance()
                    current_date = datetime.datetime.now().date()

                    if self.daily_start_time != current_date:
                        self.daily_start_time = current_date
                        self.daily_start_balance = current_balance
                        self.daily_profit = 0.0
                        self._reset_daily_counters()
                        logger.info("=== NEW TRADING DAY ===")
                        logger.info(f"Starting Balance: ${self.daily_start_balance:.2f}")

                    if self.daily_start_balance and self.daily_start_balance > 0:
                        self.daily_profit = current_balance - self.daily_start_balance

                    tier_idx, _ = self._clamp_tier_step_indices()
                    self.current_tier_index = tier_idx
                    current_tier = self.budget_tiers[tier_idx]
                    self.session_max_rounds = len(current_tier)

                    if not self._trading_bootstrapped:
                        self._trading_bootstrapped = True
                        self.session_active = True
                        if getattr(self, "_resuming_mid_ladder", False):
                            self._resuming_mid_ladder = False
                            bet_info = self._compute_round_bet(balance=current_balance)
                            logger.info(f"\n{'='*50}")
                            logger.info(
                                f"RESUMING (Tier {self.current_tier_index + 1}, "
                                f"step {self.session_round_count + 1}/{self.session_max_rounds})"
                            )
                            logger.info(f"Asset: {self.asset} | Next bet: ${bet_info['amount']:.2f}")
                            logger.info(f"Debt: ${self.cumulative_debt:.2f}")
                            logger.info(f"{'='*50}")
                        else:
                            self._sync_assigned_tier_for_trading(
                                balance=current_balance
                            )
                            if self.current_tier_index >= len(self.budget_tiers):
                                self.current_tier_index = len(self.budget_tiers) - 1
                            # Pair rescan is handled by the session_round_count==0
                            # block below — no duplicate call needed here.
                            preview = self._compute_round_bet(balance=current_balance)
                            logger.info(f"\n{'='*50}")
                            logger.info(
                                f"TRADING (Tier {self.current_tier_index + 1}, "
                                f"ladder {preview['scheduled_ladder']})"
                            )
                            logger.info(f"Asset: {self.asset} | Debt: ${self.cumulative_debt:.2f}")
                            logger.info(f"{'='*50}")

                    if self._all_tier_steps_exhausted():
                        if getattr(self, 'sequential_steps_mode', False):
                            self.session_round_count = 0
                            self._reset_ladder_tracking()
                            logger.warning("Sequential mode: all steps done → wrapping to step 1")
                        else:
                            self._finalize_session("Tier exhausted")
                        continue

                    if self.session_round_count == 0:
                        # Every new ladder start (after a win OR tier exhaustion reset)
                        # gets a fresh pair scan. This is the sole mechanism for switching
                        # pairs: we scan here, commit, then hold through any losses until
                        # the next win brings us back to step 0 and rescans again.
                        if self.auto_select_asset:
                            # Debt-chip recovery rescan takes priority (specific override);
                            # otherwise run the standard post-win / new-ladder rescan.
                            if self._pending_recovery_rescan:
                                self._pending_recovery_rescan = False
                                self._apply_auto_asset_selection(reason="recovery debt chipping")
                            else:
                                self._apply_auto_asset_selection(reason="trading start")
                        self._on_ladder_step_start()
                        if not self._ensure_tradeable_market():
                            wait_sec = 90 if not self.auto_select_asset else 30
                            if not self._interruptible_sleep(wait_sec):
                                break
                            continue

                        now_utc = datetime.datetime.utcnow()
                        if self._is_news_blackout(now_utc):
                            logger.info(
                                f"News blackout hour UTC {now_utc.hour:02d}:00 — skipping new round"
                            )
                            if not self._interruptible_sleep(30):
                                break
                            continue

                    bet_info = self._compute_round_bet(balance=current_balance)
                    required_balance = float(bet_info["amount"]) * self._stake_multiplier()

                    if current_balance < required_balance:
                        logger.warning(
                            f"Balance ${current_balance:.2f} below minimum affordable "
                            f"round ${required_balance:.2f} — waiting to retry."
                        )
                        if not self._interruptible_sleep(30):
                            break
                        continue

                    self.current_bet = bet_info["amount"]
                    self.last_bet_breakdown = bet_info

                    if not self._check_risk_mode_step_allowed():
                        logger.warning(
                            f"Drawdown risk mode — step "
                            f"{self.session_round_count + 1} blocked "
                            f"(max step {int((self._last_risk_limits or {}).get('max_step_index', LADDER_MAX_STEP_INDEX)) + 1})"
                        )
                        self._skip_to_next_entry_window("drawdown risk mode step cap")
                        continue

                    if self._is_asset_penalty_blocked():
                        self._handle_penalty_box_block()
                        continue

                    self._log_ladder_prep(bet_info)

                    if self.asset in self.avoid_markets:
                        logger.warning(f"Asset {self.asset} is in avoid_markets list. Pausing.")
                        if not self._interruptible_sleep(10):
                            break
                        continue

                    if not self._has_price_feed(period=app_config.FOLLOW_CANDLE_TIMEFRAME):
                        logger.warning("No strike price feed yet; waiting for websocket data.")
                        if not self._interruptible_sleep(5):
                            break
                        continue

                    self._wait_for_next_entry()

                    if self._past_entry_hard_abort():
                        logger.warning(
                            f"Past hard abort second :{self.entry_hard_abort_sec:02d} "
                            f"after wait — skipping."
                        )
                        self._skip_to_next_entry_window("past hard abort after wait")
                        continue

                    if self._too_late_to_place():
                        logger.warning(
                            f"Too late to place (deadline :{self._placement_deadline_second():02d}) — skipping."
                        )
                        self._skip_to_next_entry_window("past placement deadline")
                        continue

                    pair_quality = self._evaluate_candle_follow(self.asset)
                    self.last_pair_quality = pair_quality
                    if not pair_quality.get("tradeable"):
                        logger.warning(f"Pair not ready for Follow Candle: {pair_quality.get('reason')}")
                        self._handle_trade_gate_failure(pair_quality.get("reason"))
                        continue

                    # ── Asset suspension gate (empirical win-rate, shadow mode) ────
                    # Runs AFTER per-round gate so we don't add DB I/O on rounds that
                    # were already skipped for other reasons.
                    target_dir = pair_quality["direction"]
                    candle_dir = self._closed_candle_direction(self.asset)
                    if candle_dir and target_dir != candle_dir:
                        logger.warning(
                            f"Candle follow correction: evaluator={target_dir.upper()} "
                            f"last closed candle={candle_dir.upper()} — using candle"
                        )
                        target_dir = candle_dir
                    self.last_trend_direction = target_dir
                    
                    self._log_entry_timing("pre-strike refresh")
                    strikes = self._get_best_directional_strike(target_dir, for_entry_timing=True)
                    
                    if not strikes:
                        logger.warning("No qualifying strikes at fire time.")
                        self._skip_to_next_entry_window("no strikes at fire time")
                        continue
                        
                    leg_info = strikes
                    call_info = leg_info if target_dir == "call" else None
                    put_info = leg_info if target_dir == "put" else None

                    if self._past_entry_hard_abort():
                        logger.warning(
                            f"Past hard abort :{self.entry_hard_abort_sec:02d} after strike pick — skipping."
                        )
                        self._skip_to_next_entry_window("past hard abort after strike pick")
                        continue

                    if self.strategy_mode == "directional_trend":
                        if self.trading_mode == "turbo":
                            logger.info(
                                f"TURBO BET ({target_dir.upper()}): "
                                f"profit={leg_info['profit_pct']:.1f}% (Turbo Option)"
                            )
                        else:
                            strike_val = leg_info.get('strike')
                            logger.info(
                                f"DIRECTIONAL BET ({target_dir.upper()}): strike={strike_val:.6f} | "
                                f"ask={leg_info.get('ask', 0):.2f} | "
                                f"profit={leg_info['profit_pct']:.1f}%"
                            )
                            leg_secs = self._seconds_to_expiry(leg_info["symbol"])
                            logger.info(
                                f"Expiry check: {target_dir.upper()} in {leg_secs:.0f}s "
                                f"(max allowed {self.max_seconds_to_expiry}s)"
                            )
                            if leg_secs > self.max_seconds_to_expiry:
                                logger.warning(
                                    "Strike expires too far out (~1m30s bucket) — skipping minute."
                                )
                                self._skip_to_next_entry_window("expiry too far")
                                continue
                    else:
                        logger.info(
                            f"CALL: strike={call_info['strike']:.6f} | "
                            f"ask={call_info['ask']:.2f} | "
                            f"profit={call_info['profit_pct']:.1f}%"
                        )
                        logger.info(
                            f"PUT:  strike={put_info['strike']:.6f} | "
                            f"ask={put_info['ask']:.2f} | "
                            f"profit={put_info['profit_pct']:.1f}%"
                        )

                        call_secs = self._seconds_to_expiry(call_info["symbol"])
                        put_secs = self._seconds_to_expiry(put_info["symbol"])
                        logger.info(
                            f"Expiry check: CALL in {call_secs:.0f}s | PUT in {put_secs:.0f}s "
                            f"(max allowed {self.max_seconds_to_expiry}s)"
                        )
                        if call_secs > self.max_seconds_to_expiry or put_secs > self.max_seconds_to_expiry:
                            logger.warning(
                                "Strikes expire too far out (~1m30s bucket) — skipping minute."
                            )
                            self._skip_to_next_entry_window("expiry too far")
                            continue

                    if self._too_late_to_place():
                        logger.warning(
                            f"Placement deadline :{self._placement_deadline_second():02d} "
                            f"reached (server :{self._server_second():02d}) — skipping."
                        )
                        self._skip_to_next_entry_window("purchase deadline")
                        continue

                    # Candle follow: log live metrics but never block mid-ladder on ER/chop gates.
                    pq = self.last_pair_quality or pair_quality or {}
                    _gate_er = float(pq.get("efficiency_ratio", 0) or 0)
                    _gate_slope = float(pq.get("abs_slope", 0) or 0)
                    if target_dir == "put":
                        _gate_slope = -abs(_gate_slope)
                    self._last_gate_er = _gate_er
                    self._last_ai_decision = {
                        "approve": True,
                        "bot_confidence": 1.0,
                        "ensemble_combined_confidence": 1.0,
                        "reason": "strict candle follow",
                        "ai_disabled": True,
                        "gate_slope": _gate_slope,
                        "gate_er": _gate_er,
                    }


                    _entry_bot_conf = None
                    _entry_combined = None
                    if self._last_ai_decision:
                        _entry_bot_conf = self._last_ai_decision.get("bot_confidence")
                        _entry_combined = self._last_ai_decision.get(
                            "ensemble_combined_confidence"
                        )
                    entry_quality = self._compute_entry_quality(
                        _entry_bot_conf, _entry_combined
                    )
                    # Bypass Step Score gate for strict candle follow
                    score_ok = True
                    score_reason = "Bypassed for strict candle follow"

                    self._pending_entry_quality = entry_quality
                    self._pending_trade_context = self._build_trade_evaluation_context(
                        target_dir, leg_info, entry_quality
                    )

                    self._log_entry_timing("pre-placement")
                    self._capture_entry_snapshot_at_placement()

                    if not self.running:
                        break

                    with self._round_placement_lock:
                        if self._round_in_flight:
                            logger.warning(
                                "Round already in flight — skipping duplicate placement"
                            )
                            if not self._interruptible_sleep(2):
                                break
                            continue
                        self._round_in_flight = True
                    try:
                        self._log_entry_timing("send order")
                        bd = self.last_bet_breakdown or {}
                        logger.info(
                            f"LADDER ORDER — Tier {bd.get('tier_number', '?')} "
                            f"step {bd.get('step_number', '?')}: "
                            f"Placing single {target_dir.upper()} ${self.current_bet:.2f}..."
                        )
                        ok, order_id = self._place_trade(
                            leg_info["symbol"], self.current_bet, direction=target_dir
                        )
                        call_ok, call_id = (ok, order_id) if target_dir == "call" else (False, None)
                        put_ok, put_id = (ok, order_id) if target_dir == "put" else (False, None)
                        logger.info(
                            f"Trade placement complete: {target_dir.upper()}={call_ok or put_ok}"
                        )

                        self._inflight_trade_ids = []
                        if call_ok and call_id:
                            self._inflight_trade_ids.append(int(call_id))
                        if put_ok and put_id:
                            self._inflight_trade_ids.append(int(put_id))
                        if call_ok or put_ok:
                            self._pair_filter_skip_streak[self.asset] = 0
                        self.persist_state("trades placed")
                    finally:
                        with self._round_placement_lock:
                            self._round_in_flight = False

                    if not call_ok and not put_ok:
                        logger.error(
                            f"Trade REJECTED on {self.asset} — skipping this window."
                        )
                        self._notify(
                            "Order rejected",
                            f"Legs rejected on {self.asset} — skipping this candle.",
                        )
                        self._skip_to_next_entry_window("legs rejected")
                        continue

                    self.round_number += 1
                    logger.info(f"Order placed: {call_id or put_id}")
                    logger.info("Waiting for expiry...")

                    if self.simulation_mode:
                        round_profit = 0.0
                        both_lost = True
                        for _ in self._inflight_trade_ids:
                            if random.random() >= self.sim_win_rate:
                                round_profit -= self.current_bet
                            else:
                                payout_pct = leg_info["profit_pct"] / 100.0
                                round_profit += self.current_bet * payout_pct
                                both_lost = False
                        logger.info(f"SIM round P/L: ${round_profit:.2f}")
                    else:
                        resolved_profits = []
                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=max(1, len(self._inflight_trade_ids))
                        ) as executor:
                            futures = {
                                executor.submit(
                                    self._check_trade_result,
                                    oid,
                                    call_info=leg_info,
                                    put_info=leg_info,
                                ): oid
                                for oid in self._inflight_trade_ids
                            }
                            for fut in concurrent.futures.as_completed(futures):
                                resolved_profits.append(fut.result())

                        round_profit = 0.0
                        both_lost = True
                        for res in resolved_profits:
                            if res is _TIMEOUT_SENTINEL:
                                round_profit -= self.current_bet
                            else:
                                round_profit += res
                                if res > 0:
                                    both_lost = False
                        if round_profit <= 0:
                            both_lost = True

                    self.total_profit += round_profit
                    self.session_profit += round_profit
                    self.session_total_profit += round_profit
                    self._record_window_profit(round_profit)

                    if not both_lost:
                        self.wins += 1
                        if round_profit > 0:
                            self.cumulative_debt = max(0.0, self.cumulative_debt - round_profit)
                            logger.info(f"Win ${round_profit:.2f} applied to debt. Remaining: ${self.cumulative_debt:.2f}")
                        # Record the entry ER for the pair quality degradation filter
                        if self._last_gate_er > 0:
                            _win_window = getattr(app_config, "PAIR_QUALITY_WINDOW", 5)
                            _hist = self._pair_win_er_history.setdefault(self.asset, [])
                            _hist.append(self._last_gate_er)
                            if len(_hist) > _win_window:
                                _hist.pop(0)
                        # Win -> Reset last trend direction so we pick direction fresh
                        self.last_trend_direction = None
                        # Track per-asset result for conviction gate
                        _rr_window = getattr(app_config, "PAIR_RECENT_RESULT_WINDOW", 6)
                        _rr = self._pair_recent_results.setdefault(self.asset, [])
                        _rr.append(True)
                        if len(_rr) > _rr_window:
                            _rr.pop(0)
                    else:
                        self.losses += 1
                        self._record_ladder_step_loss(self._pending_entry_quality)
                        # Track per-asset result for conviction gate
                        _rr_loss_window = getattr(app_config, "PAIR_RECENT_RESULT_WINDOW", 6)
                        _rr_loss = self._pair_recent_results.setdefault(self.asset, [])
                        _rr_loss.append(False)
                        if len(_rr_loss) > _rr_loss_window:
                            _rr_loss.pop(0)
                        if round_profit < 0:
                            self._apply_round_loss_to_debt(round_profit)

                    logger.info(
                        f"Round P/L: ${round_profit:.2f} | "
                        f"Session P/L: ${self.session_profit:.2f} | "
                        f"Total P/L: ${self.total_profit:.2f} | "
                        f"W/L: {self.wins}/{self.losses}"
                    )
                    self._log_trade_round(
                        round_profit, call_info, put_info, partial=False, both_legs=(self.strategy_mode != "directional_trend")
                    )
                    self._maybe_sync_balance_after_trade()
                    logger.info(f"Balance: ${self.safe_get_balance():.2f}")

                    if not self.running:
                        logger.info("Stop requested — exiting after trade settled")
                        break

                    _balance_downgrade_round = self.last_bet_breakdown.get("balance_downgrade", False) if self.last_bet_breakdown else False

                    if not both_lost:
                        logger.info(
                            f"ROUND WON — at least one leg profitable (Tier {self.current_tier_index + 1})."
                        )
                        self._finalize_session("Round Won")
                    elif _balance_downgrade_round:
                        logger.warning(
                            f"Balance too low for scheduled step — played affordable fallback bet. "
                            f"Forcing session reset to step 1 (debt remains: ${self.cumulative_debt:.2f})."
                        )
                        self._notify(
                            "Balance downgrade reset",
                            f"Scheduled step unaffordable — played fallback bet. Resetting to step 1. Debt ${self.cumulative_debt:.2f}.",
                        )
                        self._finalize_session("Round Won")
                    else:
                        # One ladder step per round — no step-4 pair rotation.
                        steps_consumed = 1

                        next_step = self.session_round_count + steps_consumed
                        tier = self.budget_tiers[self.current_tier_index]
                        _tier_label = f"Tier {self.current_tier_index + 1}"
                        logger.warning(
                            f"ROUND LOST ({_tier_label} "
                            f"step {self.session_round_count + 1}). "
                            f"{'Tier exhausted — cooldown.' if next_step >= len(tier) else f'Advancing to step {next_step + 1}.'}"
                        )
                        self.session_round_count = next_step
                        self._last_ladder_prep_key = None
                        if self._all_tier_steps_exhausted():
                            self._record_ladder_exhaustion_and_check_penalty()
                            self._finalize_session("Tier exhausted")
                        else:
                            # Re-read direction next step — do not auto-flip, just drop sticky bias
                            self.last_trend_direction = None
                            self._sync_ladder_indices()

                    self.persist_state()
                    if (self._last_risk_limits or {}).get("risk_mode"):
                        pause = self.drawdown_risk_pause_sec
                        if pause > 0:
                            logger.info(
                                f"Risk mode — pausing {pause:.0f}s before next round"
                            )
                            if not self._interruptible_sleep(pause):
                                break
                    else:
                        if not self._interruptible_sleep(3):
                            break

                except Exception as inner_e:
                    logger.error(f"Error during bot iteration: {inner_e}")
                    self.last_error = str(inner_e)
                    self._notify("Bot error", str(inner_e)[:500])
                    logger.info("Attempting to reconnect and resume in 10 seconds...")
                    if not self._interruptible_sleep(10):
                        break
                    if not self._ensure_api_connection(force=True):
                        self._notify("Disconnected", "Reconnect failed — check IQ Option")

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            self.last_stop_reason = "Stopped by user"
            self._notify("Bot stopped", "Stopped by user")
        except Exception as e:
            logger.error(f"Unexpected fatal error: {e}", exc_info=True)
            self.last_error = str(e)
            self.last_stop_reason = "Crashed — see logs"
            self._notify("Bot crashed", str(e)[:500])
        finally:
            self.running = False
            self.session_active = False
            if self._graceful_stop:
                logger.info(
                    "Graceful stop — IQ session and market feed kept alive for fast resume."
                )
                self._start_idle_keepalive()
            else:
                self._unsubscribe()
                self._market_feed_active = False
            self._graceful_stop = False
            self._refresh_balance_cache(allow_blocking=True)
            self.persist_state(self.last_stop_reason or "stopped")
            logger.info(f"\n{'='*60}")
            logger.info("SESSION SUMMARY")
            logger.info(f"  Rounds: {self.round_number}")
            logger.info(f"  Wins:   {self.wins}")
            logger.info(f"  Losses: {self.losses}")
            logger.info(f"  Total P/L: ${self.total_profit:.2f}")
            logger.info(f"  Final Balance: ${self.safe_get_balance():.2f}")
            logger.info("=" * 60)

    def stop(self, manual=True):
        self._graceful_stop = True
        self.running = False
        self.paused = False
        if manual:
            self.manual_stop_requested = True
        with self._round_placement_lock:
            self._round_in_flight = False
        self.last_stop_reason = "Stop requested from dashboard"
        logger.info("Stop signal received (session stays connected).")
        self._notify("Bot stopped", self.last_stop_reason)
        self.persist_state(self.last_stop_reason)

    def pause(self):
        self.paused = True
        self.last_stop_reason = "Paused — no new rounds"
        logger.info("Pause signal received.")
        self.persist_state(self.last_stop_reason)

    def resume(self):
        self.paused = False
        self.last_stop_reason = ""
        logger.info("Resume signal received.")
        self.persist_state("resumed")

    def get_state(self, thread_alive=False):
        balance = self.safe_get_balance()

        effective_running = self.running and thread_alive
        api_up = self.is_session_ready()
        if self._connecting:
            phase = "connecting"
        elif effective_running:
            phase = "trading"
        elif api_up:
            phase = "connected"
        else:
            phase = "disconnected"
        return {
            "connected": api_up,
            "connecting": self._connecting,
            "session_ready": api_up,
            "bot_phase": phase,
            "connection_flag": self.connected,
            "running": effective_running,
            "paused": self.paused,
            "simulation_mode": self.simulation_mode,
            "running_flag": self.running,
            "thread_alive": thread_alive,
            "manual_stop_requested": getattr(self, "manual_stop_requested", False),
            "can_start": self.connected and not effective_running,
            "last_stop_reason": self.last_stop_reason,
            "last_error": self.last_error,
            "ai_error_msg": getattr(self, "ai_error_msg", ""),
            "asset": self.asset,
            "account_type": self.account_type,
            "account_key": self._state_account_key(),
            "balance_id": self._iq_balance_id(),
            "is_real_account": self.account_type == "REAL",
            "balance": balance,
            "current_bet": self.current_bet,
            "bet_breakdown": self.last_bet_breakdown,
            "scheduled_ladder": (
                self.last_bet_breakdown.get("scheduled_ladder")
                if self.last_bet_breakdown
                else (
                    self.budget_tiers[self.current_tier_index]
                    if self.budget_tiers
                    and self.current_tier_index < len(self.budget_tiers)
                    else []
                )
            ),
            "total_profit": self.total_profit,
            "session_profit": self.session_total_profit,
            "daily_profit": self.daily_profit,
            "wins": self.wins,
            "losses": self.losses,
            "round_number": self.round_number,
            "session_round_count": self.session_round_count,
            "avoid_markets": self.avoid_markets,
            "current_tier_index": self.current_tier_index,
            "current_step": self.session_round_count + 1,
            "ladder_steps": len(
                self.budget_tiers[self.current_tier_index]
                if self.budget_tiers and self.current_tier_index < len(self.budget_tiers)
                else []
            ),
            "cumulative_debt": self.cumulative_debt,
            "assigned_tier_index": getattr(self, "assigned_tier_index", self.current_tier_index),
            "assigned_tier": getattr(self, "assigned_tier_index", self.current_tier_index) + 1,
            "is_mopup_phase": (
                self.current_tier_index in (2, 4)
                and self.cumulative_debt > 0
            ),
            "mopup_initial_debt": float(getattr(self, "mopup_initial_debt", 0.0)),
            "mopup_tier": (
                self.current_tier_index + 1
                if self.current_tier_index in (2, 4) and self.cumulative_debt > 0
                else None
            ),
            "tier_failure_streak": getattr(self, "tier_failure_streak", 0),
            "window_profit": getattr(self, "window_profit", 0.0),
            "evaluation_window_minutes": EVALUATION_WINDOW_MINUTES,
            "evaluation_window_start": (
                self.evaluation_window_start.isoformat() + "Z"
                if getattr(self, "evaluation_window_start", None)
                else None
            ),
            "tier_exhaustion_cooldown_until": (
                self.tier_exhaustion_cooldown_until.isoformat() + "Z"
                if getattr(self, "tier_exhaustion_cooldown_until", None)
                else None
            ),
            "last_tier_exhaustion_at": (
                self.last_tier_exhaustion_at.isoformat() + "Z"
                if getattr(self, "last_tier_exhaustion_at", None)
                else None
            ),
            "window_had_tier_exhaustion": getattr(self, "window_had_tier_exhaustion", False),
            "budget_tiers": self.budget_tiers if hasattr(self, 'budget_tiers') and self.budget_tiers else STANDARD_BUDGET_TIERS,
            "reserve_wins_needed": getattr(self, "reserve_wins_needed", 0),
            "active_round": self.current_tier_index // 2 + 1,
            "is_reserve_tier": self.current_tier_index in ROUND_RESERVE_TIERS,
            "session_max_rounds": self.session_max_rounds,
            "inflight_trade_ids": self._inflight_trade_ids,
            "tier_escalations_today": self.tier_escalations_today,
            "entry_window_start": self.entry_window_start,
            "entry_window_end": self.entry_window_end,
            "entry_hard_abort_sec": self.entry_hard_abort_sec,
            "purchase_deadline_sec": self.purchase_deadline_sec,
            "min_seconds_to_expiry": self.min_seconds_to_expiry,
            "max_seconds_to_expiry": self.max_seconds_to_expiry,
            "server_second": self._server_second() if self.api else None,
            "daily_profit_pct": (
                ((self.safe_get_balance() - self.daily_start_balance) / self.daily_start_balance * 100.0)
                if self.daily_start_balance and self.daily_start_balance > 0
                else 0.0
            ),
            "asset_candidates": self.asset_candidates,
            "auto_select_asset": self.auto_select_asset,
            "override_blocked_windows": getattr(self, "override_blocked_windows", False),
            "penalty_box": {
                asset: until.isoformat() + "Z"
                for asset, until in self.asset_penalty_box.items()
                if until > datetime.datetime.utcnow()
            },
            "slope_flip_blocked": {
                asset: int(max(0, until - time.time()))
                for asset, until in self._asset_flip_blocked.items()
                if time.time() < until
            },
            "asset_scores": self.asset_scores,
            "status_note": self.status_note,
            "asset_selection_note": self.last_asset_selection_note,
            "pair_quality": self.last_pair_quality,
            "pair_learning": pair_learning_summary(),
            "gates_for_active_pair": self._straddle_gate_thresholds(self.asset),
            "auto_start": os.environ.get("AUTO_START", "true").lower() not in ("0", "false", "no"),
            "strategy_mode": self.strategy_mode,
            "hour_boundary_block_minutes": getattr(
                self, "hour_boundary_block_minutes", 5
            ),
            "hour_boundary_block_end_minutes": getattr(
                self, "hour_boundary_block_end_minutes", 10
            ),
            "market_open_blocks": [
                f"{oh:02d}:{om:02d}:{before}:{after}"
                for oh, om, before, after in getattr(self, "market_open_blocks", [])
            ],
            "blocked_time_windows": [
                f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
                for sh, sm, eh, em in getattr(self, "blocked_time_windows", [])
            ],
            "trading_timezone": getattr(self, "trading_timezone", "Africa/Lagos"),
            "balance_baseline_tier": self._balance_baseline_tier_index() + 1,
            "balance_baseline_tier_index": self._balance_baseline_tier_index(),
            "baseline_balance_thresholds": [
                {"min_balance": mb, "tier": ti + 1}
                for mb, ti in getattr(self, "baseline_balance_thresholds", [])
            ],
            "tier_ceiling_tier": (
                (self._last_risk_limits or {}).get("tier_ceiling_index", 0) + 1
            ),
            "tradable_balance": (self._last_risk_limits or {}).get(
                "tradable_balance", balance
            ),
            "locked_profit": getattr(self, "locked_profit", 0.0),
            "session_peak_balance": getattr(self, "session_peak_balance", 0.0),
            "risk_mode": bool((self._last_risk_limits or {}).get("risk_mode")),
            "drawdown_from_peak_pct": (self._last_risk_limits or {}).get(
                "drawdown_from_peak_pct", 0.0
            ),
            "ladder_pair": getattr(self, "ladder_pair", None),
            "ladder_loss_scores": list(getattr(self, "ladder_loss_scores", []) or []),
        }

    def update_config(self, new_config, skip_history=False, tag="UPDATE"):
        
        # Track history for AI Evaluator
        if not skip_history:
            self.config_history.append({
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "tag": tag,
                "config": {
                    "min_efficiency_ratio": getattr(self, "min_efficiency_ratio", 0.25),
                    "min_directional_slope": getattr(self, "min_directional_slope", 18.5),
                    "max_doji_streak": getattr(self, "doji_streak_max", 3),
                    "min_movement_score": getattr(self, "min_asset_score", 1.5)
                }
            })
            self._save_config_history()

        if "budget_tiers" in new_config:
            raw = new_config["budget_tiers"]
            if isinstance(raw, list) and len(raw) >= 1:
                valid = True
                cleaned = []
                for tier in raw:
                    if not isinstance(tier, list) or len(tier) < 3:
                        valid = False
                        break
                    cleaned.append([max(1, float(v)) for v in tier])
                if valid:
                    self.budget_tiers = cleaned
                    STANDARD_BUDGET_TIERS.clear()
                    STANDARD_BUDGET_TIERS.extend(cleaned)
                    # Recalculate current bet with new tiers
                    if self.current_tier_index >= len(self.budget_tiers):
                        self.current_tier_index = len(self.budget_tiers) - 1
                    tier = self.budget_tiers[self.current_tier_index]
                    self.session_max_rounds = len(tier)
                    if self.session_round_count >= len(tier):
                        self.session_round_count = len(tier) - 1
                    bet_info = self._compute_round_bet()
                    self.current_bet = bet_info["amount"]
                    self.last_bet_breakdown = bet_info
                    logger.info(f"Budget tiers updated: {self.budget_tiers}")
                else:
                    logger.warning(f"Invalid budget_tiers format: {raw}")

        if "auto_bracket_enabled" in new_config:
            self.auto_bracket_enabled = bool(new_config["auto_bracket_enabled"])
            logger.info(f"Config update: auto_bracket_enabled={self.auto_bracket_enabled}")
            if self.auto_bracket_enabled:
                self._update_budget_tiers_for_balance()

        if "strategy_mode" in new_config:
            self.strategy_mode = new_config["strategy_mode"]
        if "account_type" in new_config and new_config["account_type"] != self.account_type:
            self.switch_trading_account(new_config["account_type"])
        if "asset" in new_config:
            new_asset = new_config["asset"]
            if new_asset and OP_code.ACTIVES.get(new_asset):
                if new_asset != self.asset:
                    if self.api and self.connected:
                        old_id = self.asset_id
                        self.asset = new_asset
                        self.asset_id = OP_code.ACTIVES.get(new_asset, 0)
                        self._unsubscribe(old_id)
                        with self._price_lock:
                            self._price_data.clear()
                        self._subscribe()
                        logger.info(f"Manual pair set to {new_asset}")
                    else:
                        self.asset = new_asset
                        self.asset_id = OP_code.ACTIVES.get(new_asset, 0)
            else:
                logger.warning(f"Unknown asset in config: {new_asset}")
        if "avoid_markets" in new_config:
            self.avoid_markets = new_config["avoid_markets"]
        if "asset_candidates" in new_config:
            self.asset_candidates = new_config["asset_candidates"]
        if "auto_select_asset" in new_config:
            self.auto_select_asset = bool(new_config["auto_select_asset"])
            self.auto_select_manually_disabled = not self.auto_select_asset
        if "min_candle_body_pct" in new_config:
            self.min_candle_body_pct = new_config["min_candle_body_pct"]
        if "min_session_range_pct" in new_config:
            self.min_session_range_pct = new_config["min_session_range_pct"]
        if "min_asset_score" in new_config:
            self.min_asset_score = new_config["min_asset_score"]
        if "simulation_mode" in new_config:
            self.simulation_mode = new_config["simulation_mode"]
        if "entry_window_start" in new_config:
            self.entry_window_start = int(new_config["entry_window_start"])
        if "entry_window_end" in new_config:
            self.entry_window_end = int(new_config["entry_window_end"])
        if "entry_hard_abort_sec" in new_config:
            self.entry_hard_abort_sec = int(new_config["entry_hard_abort_sec"])
        if "purchase_deadline_sec" in new_config:
            self.purchase_deadline_sec = int(new_config["purchase_deadline_sec"])
        if "min_seconds_to_expiry" in new_config:
            self.min_seconds_to_expiry = int(new_config["min_seconds_to_expiry"])
        if "max_seconds_to_expiry" in new_config:
            self.max_seconds_to_expiry = int(new_config["max_seconds_to_expiry"])
        if "sim_win_rate" in new_config:
            self.sim_win_rate = new_config["sim_win_rate"]
        if "blocked_hours" in new_config:
            if isinstance(new_config["blocked_hours"], list):
                self.blocked_hours = [int(h) for h in new_config["blocked_hours"] if str(h).isdigit()]
        if "hour_boundary_block_minutes" in new_config:
            self.hour_boundary_block_minutes = int(
                new_config["hour_boundary_block_minutes"]
            )
        if "hour_boundary_block_end_minutes" in new_config:
            self.hour_boundary_block_end_minutes = int(
                new_config["hour_boundary_block_end_minutes"]
            )
        if "market_open_blocks" in new_config:
            if isinstance(new_config["market_open_blocks"], list):
                self.market_open_blocks = self._parse_market_open_blocks(
                    new_config["market_open_blocks"]
                )
        if "blocked_time_windows" in new_config:
            if isinstance(new_config["blocked_time_windows"], list):
                self.blocked_time_windows = self._parse_blocked_time_windows(
                    new_config["blocked_time_windows"]
                )
        if "trading_timezone" in new_config and new_config["trading_timezone"]:
            self.trading_timezone = str(new_config["trading_timezone"])
        if "sequential_steps_mode" in new_config:
            self.sequential_steps_mode = bool(new_config["sequential_steps_mode"])
            logger.info(f"Sequential steps mode {'ENABLED' if self.sequential_steps_mode else 'DISABLED'}")
        if "sequential_amounts" in new_config:
            raw = new_config["sequential_amounts"]
            if isinstance(raw, list) and len(raw) >= 1:
                if raw and isinstance(raw[0], list):
                    self.sequential_amounts = [[max(1.0, float(v)) for v in tier] for tier in raw]
                else:
                    self.sequential_amounts = [[max(1.0, float(v)) for v in raw]]
                logger.info(f"Sequential amounts updated: {self.sequential_amounts}")
        if "override_blocked_windows" in new_config:
            self.override_blocked_windows = bool(new_config["override_blocked_windows"])
            logger.info(f"override_blocked_windows set to {self.override_blocked_windows}")
        if "rule_gate_enabled" in new_config:
            self.rule_gate_enabled = bool(new_config["rule_gate_enabled"])
            logger.info(f"Rule gate {'ENABLED' if self.rule_gate_enabled else 'DISABLED'}")
        if "rule_gate_min_bot_conf" in new_config:
            self.rule_gate_min_bot_conf = float(new_config["rule_gate_min_bot_conf"])
        if "rule_gate_min_er" in new_config:
            self.rule_gate_min_er = float(new_config["rule_gate_min_er"])
        if "rule_gate_slope_override_min_bot_conf" in new_config:
            self.rule_gate_slope_override_min_bot_conf = float(new_config["rule_gate_slope_override_min_bot_conf"])
        if "rule_gate_misaligned_min_bot_conf" in new_config:
            self.rule_gate_misaligned_min_bot_conf = float(new_config["rule_gate_misaligned_min_bot_conf"])
        if "gemini_api_keys" in new_config or "ai_enabled" in new_config:
            logger.info("AI settings ignored — AI assessment removed from this build")
        # Re-clamp tier/step indices so any mid-session config change (e.g.
        # shorter budget_tiers list) never leaves current_tier_index out of bounds.
        self._sync_ladder_indices()
        logger.info(f"Bot config updated: {new_config}")


if __name__ == "__main__":
    bot = DoubleMartingaleBot(asset="GBPJPY-OTC")
    bot.run()

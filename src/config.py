import os
from dotenv import load_dotenv

load_dotenv()

IQ_EMAIL = os.getenv("IQ_EMAIL", "")
IQ_PASSWORD = os.getenv("IQ_PASSWORD", "")
IQ_ACCOUNT_TYPE = os.getenv("IQ_ACCOUNT_TYPE", "PRACTICE")
TRADING_MODE = os.getenv("TRADING_MODE", "binary") # Switched to binary for 5m expiries

# --- Follow Candle 5M Strategy Config ---
FOLLOW_CANDLE_TIMEFRAME = 300  # 5 minutes
RANGING_LOOKBACK_CANDLES = 6   # How many candles to look back for ranging filter
RANGING_MAX_ALTERNATIONS = 3   # Max color changes allowed in the lookback window
RANGING_MIN_ADX = float(os.getenv("RANGING_MIN_ADX", "25.0"))


if not IQ_EMAIL or not IQ_PASSWORD:
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)
    logger.warning("Missing IQ_EMAIL or IQ_PASSWORD in environment variables or .env file. The bot will not be able to connect until they are configured.")

# Security
BOT_API_KEY = os.getenv("BOT_API_KEY", "")
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()] or ["*"]

# Entry window for 5-minute options
ENTRY_WINDOW_START = int(os.getenv("ENTRY_WINDOW_START", "0"))  # Seconds into the candle to start looking
ENTRY_WINDOW_END = int(os.getenv("ENTRY_WINDOW_END", "60"))     # Seconds into the candle to stop looking
PURCHASE_DEADLINE_SEC = int(os.getenv("PURCHASE_DEADLINE_SEC", "60"))
ENTRY_HARD_ABORT_SEC = int(os.getenv("ENTRY_HARD_ABORT_SEC", "65"))
MIN_SECONDS_TO_EXPIRY = int(os.getenv("MIN_SECONDS_TO_EXPIRY", "240")) # 4 minutes minimum
MAX_SECONDS_TO_EXPIRY = int(os.getenv("MAX_SECONDS_TO_EXPIRY", "300")) # 5 minutes maximum
MOMENTUM_MIN_RATIO = float(os.getenv("MOMENTUM_MIN_RATIO", "0.65"))
MIN_SLOPE_ATR_RATIO = float(os.getenv("MIN_SLOPE_ATR_RATIO", "0.30"))
HIGH_TIER_MAX_RECOVERY_WINS = int(os.getenv("HIGH_TIER_MAX_RECOVERY_WINS", "2"))
MIN_PROFIT_PCT = float(os.getenv("MIN_PROFIT_PCT", "145"))
MAX_PROFIT_PCT = float(os.getenv("MAX_PROFIT_PCT", "277.5"))

# AI Assessment
GEMINI_API_KEYS = os.getenv("GEMINI_API_KEYS", "")
AI_ASSESSMENT_ENABLED = os.getenv("AI_ASSESSMENT_ENABLED", "false").lower() == "true"

# If no keys/enable flag came from env vars, try loading from the persisted
# dashboard settings file (written when the user saves keys via the UI).
_AI_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "ai_settings.json"
)
if not GEMINI_API_KEYS:
    try:
        import json as _json_cfg
        with open(_AI_SETTINGS_PATH, "r", encoding="utf-8") as _f_cfg:
            _ai_cfg = _json_cfg.load(_f_cfg)
            GEMINI_API_KEYS = _ai_cfg.get("gemini_api_keys", "")
            if not AI_ASSESSMENT_ENABLED:
                AI_ASSESSMENT_ENABLED = bool(_ai_cfg.get("ai_enabled", False))
    except Exception:
        pass
AI_SHADOW_MODE = os.getenv("AI_SHADOW_MODE", "false").lower() == "true"
AI_ENSEMBLE_ENABLED = os.getenv("AI_ENSEMBLE_ENABLED", "false").lower() == "true"
AI_MIN_TIER = int(os.getenv("AI_MIN_TIER", "0"))
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "3.0"))
AI_KEY_COOLDOWN_SECONDS = int(os.getenv("AI_KEY_COOLDOWN_SECONDS", "60"))
AI_MAX_CALLS_PER_MINUTE_PER_KEY = int(os.getenv("AI_MAX_CALLS_PER_MINUTE_PER_KEY", "4"))
AI_LIVE_MODEL = os.getenv("AI_LIVE_MODEL", "gemini-2.5-flash")
AI_MIN_TRADES_FOR_OPTIMIZATION = int(os.getenv("AI_MIN_TRADES_FOR_OPTIMIZATION", "50"))
AI_SKIP_BOT_CONFIDENCE = float(os.getenv("AI_SKIP_BOT_CONFIDENCE", "0.78"))
AI_SKIP_MIN_STRADDLE_SCORE = float(os.getenv("AI_SKIP_MIN_STRADDLE_SCORE", "115"))
AI_SKIP_MIN_ER = float(os.getenv("AI_SKIP_MIN_ER", "0.55"))
AI_ENSEMBLE_MIN_COMBINED_CONFIDENCE = float(os.getenv("AI_ENSEMBLE_MIN_COMBINED_CONFIDENCE", "0.55"))
AI_ENSEMBLE_AI_UNAVAILABLE_THRESHOLD = float(os.getenv("AI_ENSEMBLE_AI_UNAVAILABLE_THRESHOLD", "0.50"))

# Rule-based entry gates
RULE_GATE_ENABLED = os.getenv("RULE_GATE_ENABLED", "true").lower() == "true"
RULE_GATE_SLOPE_OVERRIDE_MIN_BOT_CONF = float(os.getenv("RULE_GATE_SLOPE_OVERRIDE_MIN_BOT_CONF", "0.70"))
RULE_GATE_SLOPE_FLIP_CALL_MIN_ER = float(os.getenv("RULE_GATE_SLOPE_FLIP_CALL_MIN_ER", "0.38"))
RULE_GATE_MISALIGNED_SLOPE = float(os.getenv("RULE_GATE_MISALIGNED_SLOPE", "50"))
RULE_GATE_MISALIGNED_MIN_BOT_CONF = float(os.getenv("RULE_GATE_MISALIGNED_MIN_BOT_CONF", "0.42"))
# Hard floor for all trades regardless of direction alignment
RULE_GATE_MIN_BOT_CONF = float(os.getenv("RULE_GATE_MIN_BOT_CONF", "0.35"))
# Minimum efficiency ratio for 1-minute binary options.
# 0.35 = global default — applies to any pair not listed in ASSET_MIN_ER.
# Per-asset floors in ASSET_MIN_ER override this upward for riskier pairs (up to 0.50 max).
# Deep-step boost adds +0.10 at step 3+ (so global step3+ floor = 0.45).
RULE_GATE_MIN_ER = float(os.getenv("RULE_GATE_MIN_ER", "0.35"))
# ER floor for pairs trading outside their known-good UTC windows (caution periods).
# Capped at 0.50 — matches the ceiling for the riskiest assets.
RULE_GATE_OFFPEAK_MIN_ER = float(os.getenv("RULE_GATE_OFFPEAK_MIN_ER", "0.50"))
OFFPEAK_HARD_BLOCK = os.getenv("OFFPEAK_HARD_BLOCK", "false").lower() == "true"

# Deep-step tighter gates — step 3+ has 6x the bet of step 1.
# Historical data: S3 WR is only 51% vs S1 56%. Losses at S3 average ER 0.455
# vs winners at 0.511. Raising the bar at step 3+ filters the marginal entries.
#
#   S3 ER floor  = effective_min_er + DEEP_STEP_MIN_ER_BOOST  (e.g. 0.55 → 0.65)
#   S3 conf floor = RULE_GATE_MIN_BOT_CONF + DEEP_STEP_MIN_CONF_BOOST (e.g. 0.35 → 0.60)
DEEP_STEP_START        = 3     # first step (1-indexed) that triggers deep-step mode
DEEP_STEP_MIN_ER_BOOST   = 0.10  # ER floor additive boost at step 3+  (0.35+0.10 = 0.45 floor)
DEEP_STEP_MIN_CONF_BOOST = 0.25  # confidence floor additive boost at step 3+  (0.35+0.25 = 0.60 floor)

# Enhanced conviction gate — runs after the basic rule/AI gate passes.
# Catches trades where every individual check clears its threshold but the signals
# collectively contradict each other (low coherence) or the asset is on a cold streak
# at a high-bet step.  Set ENHANCED_CONVICTION_ENABLED=false to disable entirely.
ENHANCED_CONVICTION_ENABLED  = os.getenv("ENHANCED_CONVICTION_ENABLED",  "true").lower() == "true"
MIN_CANDLE_BODY_QUALITY       = float(os.getenv("MIN_CANDLE_BODY_QUALITY",    "0.15"))  # body/range ratio floor
MIN_SIGNAL_COHERENCE          = float(os.getenv("MIN_SIGNAL_COHERENCE",       "0.22"))  # coherence floor steps 1-2
MIN_SIGNAL_COHERENCE_STEP3    = float(os.getenv("MIN_SIGNAL_COHERENCE_STEP3", "0.38"))  # higher floor at step 3+
MIN_ALIGNED_SIGNALS_STEP3     = int(  os.getenv("MIN_ALIGNED_SIGNALS_STEP3",  "2"))     # /3 directional signals required at step 3+
MIN_RECENT_WIN_RATE_STEP3     = float(os.getenv("MIN_RECENT_WIN_RATE_STEP3",  "0.25"))  # asset win-rate floor at step 3+
MIN_RECENT_TRADES_FOR_RATE    = int(  os.getenv("MIN_RECENT_TRADES_FOR_RATE", "4"))     # min trades before win-rate is used
PAIR_RECENT_RESULT_WINDOW     = int(  os.getenv("PAIR_RECENT_RESULT_WINDOW",  "6"))     # rolling window size for per-asset results

# Micro-pullback sniper
SNIPER_ENTRY_ENABLED = os.getenv("SNIPER_ENTRY_ENABLED", "true").lower() == "true"
SNIPER_PULLBACK_ATR = float(os.getenv("SNIPER_PULLBACK_ATR", "0.05"))
SNIPER_MAX_WAIT_SEC = float(os.getenv("SNIPER_MAX_WAIT_SEC", "12"))
SNIPER_POLL_INTERVAL_SEC = float(os.getenv("SNIPER_POLL_INTERVAL_SEC", "0.1"))
# If price moves against the target direction during the sniper pullback wait,
# skip the trade entirely rather than chasing a market already moving against us.
SNIPER_FOMO_SKIP_UNFAVORABLE = os.getenv("SNIPER_FOMO_SKIP_UNFAVORABLE", "true").lower() == "true"

# IQ Option Statistics
USE_TRADER_MOOD = os.getenv("USE_TRADER_MOOD", "true").lower() == "true"
USE_TECHNICAL_INDICATORS = os.getenv("USE_TECHNICAL_INDICATORS", "true").lower() == "true"

# Trading timezone and blocked windows
TRADING_TIMEZONE = os.getenv("TRADING_TIMEZONE", "Africa/Lagos")
HOUR_BOUNDARY_BLOCK_MINUTES = 0
HOUR_BOUNDARY_BLOCK_END_MINUTES = 0
_default_market_opens = os.getenv("MARKET_OPEN_BLOCKS", "")
MARKET_OPEN_BLOCKS = [p.strip() for p in _default_market_opens.split(",") if p.strip()]
_default_blocked = os.getenv("BLOCKED_TIME_WINDOWS", "")
BLOCKED_TIME_WINDOWS = [w.strip() for w in _default_blocked.split(",") if w.strip()]

# Permanently banned pairs — worst historical performers (BTCUSD-op ~37% win, XAUUSD-OTC ~38% win)
AVOID_MARKETS = ["BTCUSD-op", "XAUUSD-OTC", "BTCUSD", "XAUUSD"]

# Dynamic pair quality degradation filter.
# After a pair has at least PAIR_QUALITY_MIN_WINS recorded wins, the bot tracks
# the rolling average ER across the last PAIR_QUALITY_WINDOW wins. If the current
# candle ER drops below (avg_win_er * PAIR_QUALITY_DROP_RATIO) the trade is skipped —
# the market is no longer behaving the way it was when the pair was winning.
PAIR_QUALITY_MIN_WINS = 3      # wins needed before the filter engages
PAIR_QUALITY_WINDOW = 5        # rolling window of recent wins to average
PAIR_QUALITY_DROP_RATIO = 0.65 # current ER must be >= 65% of avg winning ER (35% drop triggers)
PAIR_QUALITY_MAX_FLOOR = 0.40  # hard cap — dynamic floor NEVER exceeds this, protecting
                                # pairs like APPLE-OTC that legitimately win at low ER

# Per-pair minimum ER floors — derived from CSV win/loss analysis.
# These are applied BEFORE the off-peak window check (the higher of the two wins).
# Per-asset ER floors — all values must be between 0.35 (global floor) and 0.50 (max).
# Pairs not listed here fall back to RULE_GATE_MIN_ER = 0.35.
#
# Risk tiers:
#   Low risk  (stable majors):  0.35 — uses global floor, no entry here needed
#   Medium risk (JPY crosses, USDCAD): 0.40–0.42
#   High risk (volatile/crypto): 0.45–0.50
#
# ETHUSD-OTC: crypto — very wide spreads, erratic moves; 0.50 hard ceiling
# USDCAD-OTC: 0.35-0.40 dead zone (33-42% WR); 0.40+ recovers to 67%
# GBPJPY-OTC: high-volatility cross — spikes on BOJ/BOE news
ASSET_MIN_ER: dict = {
    # --- High risk: 0.45–0.50 ---
    "ETHUSD-OTC": 0.50,
    "ETHUSD":     0.50,
    "GBPJPY-OTC": 0.45,
    "GBPJPY":     0.45,
    # --- Medium risk: 0.40–0.42 ---
    "USDCAD-OTC": 0.42,
    "USDCAD":     0.42,
    "USDJPY-OTC": 0.40,
    "USDJPY":     0.40,
    "EURJPY-OTC": 0.40,
    "EURJPY":     0.40,
    "CADJPY-OTC": 0.40,
    "CADJPY":     0.40,
    "AMAZON-OTC": 0.40,
    "AMAZON":     0.40,
    # Standard majors (EURUSD, GBPUSD, etc.) use the 0.35 global floor
}

# Per-asset minimum bot confidence floors — applied on top of the global floor.
# ETHUSD-OTC:  most-traded asset; Step 3 losses at conf 0.787/0.744/0.863 — the
#              only reliable filter is a high floor that removes marginal entries.
# GBPJPY-OTC:  Step 3 loss at conf 0.369 — right at bare global floor; both recorded
#              trades were borderline, setting floor above the loss confidence.
# AMAZON-OTC:  Only 1 filtered win at conf 0.396; all no-filter losses also low-conf.
# USDCAD-OTC:  Single filtered win at conf 0.443 — already has tighter ER floor;
#              adding matching confidence gate for consistency.
ASSET_MIN_CONF: dict = {
    # Floors are set above the global 0.35 but kept permissive enough that
    # Step 1/2 entries at typical confidence (0.55–0.70) still go through.
    # When this floor blocks a trade, the bot auto-switches to next candidate
    # instead of waiting out the entry window (see double_martingale.py gate).
    "ETHUSD-OTC":  0.62,  # was 0.75 — too many Step 1/2 rejections
    "ETHUSD":      0.62,
    "GBPJPY-OTC":  0.45,  # blocks the 0.369 loss; both data trades were borderline
    "GBPJPY":      0.45,
    "AMAZON-OTC":  0.42,  # softened from 0.48 — still above global 0.35 floor
    "AMAZON":      0.42,
    "USDCAD-OTC":  0.45,  # single filtered win at 0.443; matches tighter ER floor
    "USDCAD":      0.45,
    "USDJPY-OTC":  0.40,  # new — slightly above global; no historical data yet
    "USDJPY":      0.40,
    "EURJPY-OTC":  0.40,
    "EURJPY":      0.40,
    "CADJPY-OTC":  0.40,
    "CADJPY":      0.40,
}

# Preferred UTC trading windows per asset — derived from historical CSV analysis.
# Pairs trading OUTSIDE their window face a stricter ER floor (RULE_GATE_OFFPEAK_MIN_ER = 0.37).
# Pairs with no entry here are treated as always-preferred (base ER 0.30 applies at all hours).
ASSET_PREFERRED_WINDOWS: dict = {
    # NOTE: preferred windows are now ADVISORY only — they do not raise the ER floor
    # unless OFFPEAK_HARD_BLOCK=true. They are kept as reference data and logged.
    "EURNZD-OTC": [(12, 14), (15, 16)],
    "APPLE-OTC":  [(13, 16), (17, 19), (19, 22)],
    "APPLE":      [(13, 16), (17, 19), (19, 22)],
    "ETHUSD-OTC": [(8, 9),  (21, 22)],
    "ETHUSD":     [(8, 9),  (21, 22)],
    "AMAZON-OTC": [(13, 16), (17, 19)],
    "AMAZON":     [(13, 16), (17, 19)],
    "USDCAD-OTC": [(12, 15)],
    "USDCAD":     [(12, 15)],
    "AUDJPY-OTC": [(0, 8), (20, 22)],   # extended to cover Asian + early London
    "AUDJPY":     [(0, 8), (20, 22)],
    "GBPJPY-OTC": [(7, 11), (13, 17)],  # London session + NY overlap
    "GBPJPY":     [(7, 11), (13, 17)],
    "USDJPY-OTC": [(0, 9),  (13, 17)],  # Asian session + NY overlap
    "USDJPY":     [(0, 9),  (13, 17)],
    "EURJPY-OTC": [(7, 11), (13, 17)],  # London session + NY overlap
    "EURJPY":     [(7, 11), (13, 17)],
    "EURUSD-OTC": [(15, 16)],
}

# Balance → baseline tier floor (0-based index).
# Always T0 (index 0) for all balances. Bet AMOUNTS scale automatically via
# BALANCE_TIER_TABLE in double_martingale.py — T1 is only used for recovery
# after T0 is exhausted, never as a starting floor.
BASELINE_BALANCE_THRESHOLDS = [
    (0, 0),  # All balances start at T0; amounts scale via BALANCE_TIER_TABLE
]

# Balance → max tier ceiling (0-based).
# Flat-tier system: ceiling always equals floor — no recovery escalation.
TIER_CEILING_THRESHOLDS = [
    (0, 0),   # Ceiling = floor for all balances (recovery escalation disabled)
]

# UTC-based hard ban windows: no trading for any asset (midnight-crossing supported)
# 21:45-02:05 UTC = 10:45 PM–3:05 AM Lagos: midnight dead zone (low liquidity, erratic spreads).
# 06:00-06:59 UTC = 7:00–7:59 AM Lagos: historically weak (36% WR); hard-banned from 7am Lagos sharp.
_default_utc_bans = os.getenv("UTC_BAN_WINDOWS", "21:45-02:05,06:00-06:59")
UTC_BAN_WINDOWS = [w.strip() for w in _default_utc_bans.split(",") if w.strip()]

# ── Multi-asset simultaneous trading ──────────────────────────────────────────
# When enabled, the bot picks top N uncorrelated assets each minute and fires
# all trades simultaneously within the same entry window (:20–:35).
MULTI_ASSET_MODE = os.getenv("MULTI_ASSET_MODE", "false").lower() == "true"
# How many assets to trade at once (2 recommended at <$500 balance, 3 at $500+)
MULTI_ASSET_COUNT = int(os.getenv("MULTI_ASSET_COUNT", "2"))
# Bet-size scale factors per rank (rank 1 = best signal gets full amount).
# Default: rank-1 → 100%, rank-2 → 60%, rank-3 → 40% of the sequential step amount.
MULTI_ASSET_SCALE_FACTORS = [
    float(x) for x in os.getenv("MULTI_ASSET_SCALE_FACTORS", "1.0,0.6,0.4").split(",")
]
# Minimum straddle score for a secondary/tertiary asset to qualify.
MULTI_ASSET_MIN_SCORE = float(os.getenv("MULTI_ASSET_MIN_SCORE", "55.0"))
# Consecutive losses on a single asset before that asset's tier escalates.
MULTI_ASSET_TIER_ESCALATE_LOSSES = int(os.getenv("MULTI_ASSET_TIER_ESCALATE_LOSSES", "3"))
# Total session P/L across ALL assets that triggers the global hard stop.
MULTI_ASSET_GLOBAL_STOP_LOSS = float(os.getenv("MULTI_ASSET_GLOBAL_STOP_LOSS", "-150.0"))
# Pause duration (seconds) after the global hard stop fires before resuming.
MULTI_ASSET_GLOBAL_PAUSE_SEC = int(os.getenv("MULTI_ASSET_GLOBAL_PAUSE_SEC", "900"))

# UTC-based soft ban: these specific assets are blocked during UTC_SOFT_BAN_WINDOWS
_default_soft_ban_assets = os.getenv("UTC_SOFT_BAN_ASSETS", "AMAZON,AMAZON-OTC,APPLE,APPLE-OTC")
UTC_SOFT_BAN_ASSETS = [a.strip() for a in _default_soft_ban_assets.split(",") if a.strip()]

_default_utc_soft = os.getenv("UTC_SOFT_BAN_WINDOWS", "")
UTC_SOFT_BAN_WINDOWS = [w.strip() for w in _default_utc_soft.split(",") if w.strip()]

# Profit lock
PROFIT_LOCK_ENABLED = os.getenv("PROFIT_LOCK_ENABLED", "true").lower() == "true"
PROFIT_LOCK_RATIO = float(os.getenv("PROFIT_LOCK_RATIO", "0.40"))
PROFIT_LOCK_MIN_RESERVE = float(os.getenv("PROFIT_LOCK_MIN_RESERVE", "80"))

# Drawdown breaker
DRAWDOWN_BREAKER_ENABLED = os.getenv("DRAWDOWN_BREAKER_ENABLED", "true").lower() == "true"
DRAWDOWN_PCT = float(os.getenv("DRAWDOWN_PCT", "0.20"))
DRAWDOWN_FAST_USD = float(os.getenv("DRAWDOWN_FAST_USD", "80"))
DRAWDOWN_FAST_MINUTES = float(os.getenv("DRAWDOWN_FAST_MINUTES", "30"))
DRAWDOWN_RECOVERY_PCT = float(os.getenv("DRAWDOWN_RECOVERY_PCT", "0.10"))
DRAWDOWN_RISK_MODE_MINUTES = float(os.getenv("DRAWDOWN_RISK_MODE_MINUTES", "45"))
DRAWDOWN_RISK_PAUSE_SEC = float(os.getenv("DRAWDOWN_RISK_PAUSE_SEC", "120"))

# Consecutive full-ladder-loss pause
# If the bot loses all 3 steps back-to-back N times in a row, pause trading
# for a cooldown period before resuming. Resets on any round win.
CONSECUTIVE_LADDER_LOSS_LIMIT   = int(os.getenv("CONSECUTIVE_LADDER_LOSS_LIMIT", "3"))
CONSECUTIVE_LADDER_LOSS_PAUSE_SEC = float(os.getenv("CONSECUTIVE_LADDER_LOSS_PAUSE_SEC", "600"))

# Session-loss recovery escalation — DISABLED.
# Tier escalation is now driven entirely by ladder exhaustion:
# when all 3 steps of a tier are lost, the bot escalates to the next tier
# after the 5-minute cooldown, regardless of session P/L or balance.
# USD-based hard stops and session pauses have been removed.
RECOVERY_MODE_ENABLED = False

# Step score escalation
STEP_SCORE_ESCALATION_ENABLED = os.getenv("STEP_SCORE_ESCALATION_ENABLED", "true").lower() == "true"
# Require a more meaningful improvement in market quality before taking the next
# step in a ladder — 8% vs 5% reduces doubling-down on marginally better setups.
STEP_SCORE_MIN_IMPROVEMENT = float(os.getenv("STEP_SCORE_MIN_IMPROVEMENT", "0.02"))
STEP_SCORE_MAX_SKIPS_BEFORE_PAIR_SWITCH = int(os.getenv("STEP_SCORE_MAX_SKIPS_BEFORE_PAIR_SWITCH", "2"))

# Sequential Steps Mode: when true, advances steps by schedule regardless of outcome.
# When false (classic martingale): advance step only on loss, reset to S1 on any win.
# Classic martingale is active — each win immediately covers prior losses and returns
# to the minimum bet, maximising recovery speed and capital preservation.
SEQUENTIAL_STEPS_MODE = os.getenv("SEQUENTIAL_STEPS_MODE", "false").lower() == "true"
_seq_amounts_raw = os.getenv(
    "SEQUENTIAL_AMOUNTS",
    "1,3,9;6,15,42"
)
SEQUENTIAL_AMOUNTS = [
    [float(v.strip()) for v in tier.split(",") if v.strip()]
    for tier in _seq_amounts_raw.split(";") if tier.strip()
]

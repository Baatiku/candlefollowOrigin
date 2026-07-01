---
name: BestaBot architecture
description: Key files, patterns, and conventions for the BestaBot IQ Option trading bot.
---

# BestaBot Architecture

**Why:** Fast orientation across a large codebase (double_martingale.py alone is 6500+ lines).

## Core entry points
- `src/api.py` — FastAPI server, port 5000
- `src/strategies/double_martingale.py` — entire trading engine: `DoubleMartingale` class
- `src/config.py` — all config constants, env-var overrides via `os.getenv`

## Auth / lifecycle pattern
- License gate is checked at startup, not per-trade
- `_trading_loop_lock` (threading.Lock) prevents duplicate run() calls
- `self.running` / `self.paused` control the main loop
- `self.persist_state()` saves bot state to `bot_state_store`

## Asset scoring / selection
- `_score_asset_movement(asset)` → dict with score, straddle_score, adj_straddle_score, efficiency_ratio, choppiness_index, quality_factor, tradeable
- `_asset_rank_score(data)` → final rank value (uses adj_straddle_score when SCORE_REWEIGHT_ENABLED)
- `_select_best_asset()` → picks highest-rank tradeable pair
- `_apply_auto_asset_selection()` → wrapper with hot-pair loyalty logic

## Pre-trade gate pattern (in _run_trading_loop)
Checks run in order:
1. Time window blocks (UTC ban, soft ban, legacy hour block)
2. Balance / step checks
3. `_ensure_tradeable_market()` → candle/straddle gate
4. `_evaluate_candle_follow()` → direction + entry quality
5. Asset suspension gate (pair_health.py, shadow mode)
6. Strike selection + expiry check
7. Placement

## Gate rejection log
`self._gate_rejection_log` (deque maxlen=50) stores dicts with reason, asset, would_suspend, shadow_mode, ts etc. for all gate rejections including asset suspension checks.

## Trade log
- `src/trade_log.py`: `append_trade`, `read_trades(limit, account_key)`, `get_recent_trades(asset, count)`, `analytics`
- Storage: PostgreSQL when available, JSONL fallback at `data/` dir
- `get_recent_trades` fetches `count*8` trades and filters by asset (newest-first)

## Pair health / score reweighting (added)
- `src/pair_health.py`: `wilson_lower_bound`, `asset_health_check`, `pattern_quality_factor`, `adjusted_score`
- Config: ASSET_SUSPENSION_ENABLED, ASSET_SUSPENSION_SHADOW_MODE (hardcoded True), SCORE_REWEIGHT_ENABLED (default True)
- Shadow mode MUST default True for any new empirical gate — never flip without data review

## Never touch
- Staking / ladder / martingale math in double_martingale.py (STANDARD_BUDGET_TIERS, BALANCE_TIER_TABLE)
- risk_governor.py (separate scope)
- double_martingale.py budget/recovery math

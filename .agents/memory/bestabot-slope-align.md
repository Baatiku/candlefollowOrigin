---
name: BestaBot candle follow + slope alignment
description: Why single-candle follow fails in strong trends, and how slope_signed guard was added to _evaluate_candle_follow to balance them.
---

# Root cause of "green bounce candle in downtrend" loss pattern

`_evaluate_candle_follow` determines direction from the LAST CLOSED CANDLE's color (green=call, red=put).
`_closed_candle_direction` in the main loop (line ~5587) does the same check as a safety — so both agree.
Neither consults the 15-candle regression slope. In a strong downtrend, one green candle is noise; both
still say "call" → loss. `snap_slope_signed` in the trade log IS the real signed slope and was showing
-600 to -700 on those losing call trades. `entry_slope_signed` is NOT independent — it is `abs_slope × ±1`
based on trade direction (always agrees by construction, is cosmetic).

# Fix: slope_signed in _score_asset_movement + slope-alignment guard

`_score_asset_movement` now returns `"slope_signed": round(normalized_slope, 1)` (previously only `abs_slope`).

`_evaluate_candle_follow` now has a slope-alignment guard after `result.update(base)`:
- If `|slope_signed| ≥ SLOPE_ALIGN_MIN_SLOPE` (default 20.0) AND `ER ≥ SLOPE_ALIGN_MIN_ER` (default 0.45)
  AND slope direction ≠ candle color → override direction to slope direction, log "SLOPE-OVERRIDE"
- Config knobs: `SLOPE_ALIGN_MIN_SLOPE`, `SLOPE_ALIGN_MIN_ER` env vars
- The thresholds are conservative: ETHUSD slope was ~600 pips ER ~0.6, clearly over threshold

**Why:** Candle follow is the primary rule (FRAMEWORK.md), but one bounce candle in a strong trend
is noise not signal. The slope is computed over 15 candles — much more reliable for trend direction.

**How to apply:** If the threshold is too aggressive (overriding good candles), raise SLOPE_ALIGN_MIN_SLOPE
or SLOPE_ALIGN_MIN_ER. Default 20/0.45 is intentionally low to catch the ETHUSD pattern (~600/0.6).

# Problem 2 fix: fragile result.update(base)

`result["direction"] = candle_dir` is now set TWICE — once before update (for candle_color log), once
after `result.update(base)` to prevent silent overwrite if _score_asset_movement ever adds a direction key.
The slope-alignment guard then optionally overrides it a third time.

# Dead code renamed

`_determine_trend_direction` → `_determine_trend_direction_UNUSED` (never called, 167 lines)
`_check_and_apply_slope_flip_block` → `_check_and_apply_slope_flip_block_UNUSED` (never called, ~25 lines)
Both renamed, not deleted, for reference. Do not reconnect.

# Log spam fix

`_handle_penalty_box_block` mid-ladder branch now rate-limits to one log per 30 seconds per asset.
Previously ran at full polling speed producing 100s of identical lines per second.
Uses `self._penalty_box_log_times` dict (lazily initialised via getattr).

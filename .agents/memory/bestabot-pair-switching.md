---
name: BestaBot pair-switching rule
description: How and when the bot switches trading pairs — the agreed rule and where it is implemented.
---

## The Rule (agreed with user)

**Scan once per ladder, commit and hold through losses, rescan only after a win.**

1. **Start of every new ladder (session_round_count == 0):** Run `_apply_auto_asset_selection("trading start")` — picks the best-ranked pair across all candidates.
2. **After a LOSS (any step):** Keep the same pair. Wait for the next candle if quality check fails. Never switch mid-recovery.
3. **After a WIN:** `_finalize_session("Round Won")` resets `session_round_count` to 0 → main loop enters the step-0 block → fresh scan runs automatically.
4. **Tier exhaustion (all steps lost, sliding-window penalty applied):** also resets to step 0 → fresh scan.

## Penalty Box Rule (agreed with user)

A pair enters the penalty box **only** if it exhausts the full ladder (3 consecutive losses = all steps lost) **≥2 times within 15 minutes**.  
Nothing else triggers a penalty box entry.

Implemented in `_record_ladder_exhaustion_and_check_penalty()` — called just before `_finalize_session("Tier exhausted")` in the main loss path. All other penalty triggers were removed.

## Key Implementation Points

- `session_round_count == 0` block in the main loop (around line 5518) is the sole rescan trigger for post-win / new-ladder starts.
- Hot-pair loyalty gate: `"trading start"` added to `_loyalty_override_reasons` so winning streaks never suppress the post-win rescan.
- `_handle_trade_gate_failure`: quality/chop failures just call `_skip_to_next_entry_window` — no pair switch ever.
- `_abandon_untradeable_pair`: deprecated, logs a warning if reached, should never be called.
- `_mid_ladder_bypass` and `_switch_bypass`: only `{"step 4 rotation retry", "penalty box"}` bypass the mid-ladder lock. "quality escape" was removed.
- `_ensure_tradeable_market()`: still validates the chosen pair at step 0 and may reselect if it's truly untradeble — this is part of the initial commitment, not mid-ladder switching.

**Why:** Switching pairs on every quality hiccup causes churn and breaks recovery sequences. Holding a trusted pair through losses and rescanning after a win gives the strategy consistency.

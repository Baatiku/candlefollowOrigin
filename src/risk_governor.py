"""
Capital risk limits: tier ceiling, profit lock, drawdown mode.
Pure helpers — bot applies results to ladder state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def tier_index_for_balance(
    balance: float, thresholds: Sequence[Tuple[float, int]]
) -> int:
    """Highest tier index allowed for balance (thresholds sorted high→low)."""
    floor_idx = 0
    for min_balance, tier_idx in thresholds:
        if balance >= min_balance:
            return tier_idx
    return floor_idx


def tier_directional_ladder_cost(
    tier_index: int, budget_tiers: Sequence[Sequence[float]]
) -> float:
    if tier_index < 0 or tier_index >= len(budget_tiers):
        return 0.0
    return float(sum(budget_tiers[tier_index]))


def min_operating_reserve(
    ceiling_tier_index: int,
    budget_tiers: Sequence[Sequence[float]],
    min_reserve_usd: float,
    buffer_ratio: float = 1.15,
) -> float:
    ladder = tier_directional_ladder_cost(ceiling_tier_index, budget_tiers)
    return max(float(min_reserve_usd), ladder * buffer_ratio)


def clamp_locked_profit(
    balance: float, locked_profit: float, min_reserve: float
) -> float:
    locked = max(0.0, float(locked_profit))
    max_lock = max(0.0, float(balance) - float(min_reserve))
    return min(locked, max_lock)


def update_profit_lock_on_peak(
    balance: float,
    session_peak: float,
    locked_profit: float,
    lock_ratio: float,
    min_reserve: float,
) -> Tuple[float, float]:
    """
    Returns (new_peak, new_locked_profit).
    Ratchets lock upward on new balance highs.
    """
    bal = float(balance)
    peak = float(session_peak)
    locked = float(locked_profit)
    if bal > peak:
        gain = bal - peak
        locked += gain * float(lock_ratio)
        peak = bal
    locked = clamp_locked_profit(bal, locked, min_reserve)
    return peak, locked


def tradable_balance(balance: float, locked_profit: float, min_reserve: float) -> float:
    bal = float(balance)
    locked = clamp_locked_profit(bal, locked_profit, min_reserve)
    return max(float(min_reserve), bal - locked)


def compute_risk_limits(
    balance: float,
    session_peak: float,
    locked_profit: float,
    *,
    budget_tiers: Sequence[Sequence[float]],
    ceiling_thresholds: Sequence[Tuple[float, int]],
    lock_ratio: float,
    min_reserve_usd: float,
    drawdown_pct: float,
    drawdown_fast_usd: float,
    drawdown_fast_minutes: float,
    drawdown_window_start_balance: Optional[float],
    drawdown_window_start_ts: Optional[float],
    now_ts: float,
    risk_mode_until_ts: Optional[float],
    drawdown_recovery_pct: float,
    risk_mode_tier_delta: int = 1,
    drawdown_breaker_enabled: bool = True,
) -> Dict[str, Any]:
    """
    Compute tradable balance, tier ceiling, and whether drawdown risk mode is active.
    """
    bal = float(balance)
    peak = max(float(session_peak), bal)

    prelim_ceiling = tier_index_for_balance(bal, ceiling_thresholds)
    reserve = min_operating_reserve(
        prelim_ceiling, budget_tiers, min_reserve_usd
    )
    peak, locked = update_profit_lock_on_peak(
        bal, peak, locked_profit, lock_ratio, reserve
    )
    tradable = tradable_balance(bal, locked, reserve)
    ceiling = tier_index_for_balance(tradable, ceiling_thresholds)

    drawdown_from_peak = (peak - bal) / peak if peak > 0 else 0.0

    fast_trigger = False
    win_start = drawdown_window_start_balance
    win_ts = drawdown_window_start_ts
    window_sec = float(drawdown_fast_minutes) * 60.0
    if win_ts is None or (now_ts - float(win_ts)) > window_sec:
        win_start = bal
        win_ts = now_ts
    elif win_start is not None and bal <= float(win_start) - float(drawdown_fast_usd):
        fast_trigger = True

    risk_active = False
    if risk_mode_until_ts is not None and now_ts < float(risk_mode_until_ts):
        risk_active = True
    if drawdown_from_peak >= float(drawdown_pct) or fast_trigger:
        risk_active = True

    recovered = peak > 0 and bal >= peak * (1.0 - float(drawdown_recovery_pct))
    if risk_active and recovered and not fast_trigger:
        if risk_mode_until_ts is None or now_ts >= float(risk_mode_until_ts):
            risk_active = False

    risk_tier_cap = max(0, ceiling - int(risk_mode_tier_delta))
    if not drawdown_breaker_enabled:
        risk_active = False
    from strategies.double_martingale import LADDER_MAX_STEP_INDEX
    max_step_index = LADDER_MAX_STEP_INDEX  # 0-based; derived from ladder definition

    return {
        "balance": bal,
        "session_peak_balance": peak,
        "locked_profit": locked,
        "tradable_balance": tradable,
        "min_operating_reserve": reserve,
        "tier_ceiling_index": ceiling,
        "risk_mode": risk_active,
        "risk_tier_cap": risk_tier_cap,
        "max_step_index": max_step_index,
        "drawdown_from_peak_pct": round(drawdown_from_peak * 100.0, 2),
        "drawdown_fast_triggered": fast_trigger,
        "drawdown_window_start_balance": win_start,
        "drawdown_window_start_ts": win_ts,
        "enter_risk_mode": risk_active
        and (
            risk_mode_until_ts is None
            or now_ts >= float(risk_mode_until_ts)
            or drawdown_from_peak >= float(drawdown_pct)
            or fast_trigger
        ),
    }

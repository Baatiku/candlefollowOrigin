"""Per-asset empirical health tracking: statistical win-rate suspension
and pattern-quality score reweighting. Complements (does not replace) the
per-round CI/ER/spike gates in market_metrics.py."""
from __future__ import annotations
import math
import time


def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    """95% confidence LOWER bound on true win rate. Punishes small samples
    with appropriate uncertainty instead of trusting raw win rate directly."""
    if total == 0:
        return 0.0
    phat = wins / total
    denom = 1 + z**2 / total
    center = phat + z**2 / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z**2 / (4 * total)) / total)
    return max(0.0, (center - margin) / denom)


def asset_health_check(
    asset: str,
    trade_log_lookup_fn,       # inject: e.g. trade_log.get_recent_trades
    lookback: int = 40,
    min_wilson_winrate: float = 0.40,
) -> dict:
    """
    Read-only. Does NOT decide to skip anything — caller decides based on
    shadow_mode vs enforce config. Returns enough detail to log either way.
    """
    recent = trade_log_lookup_fn(asset, count=lookback)
    if len(recent) < lookback:
        return {
            "would_suspend": False,
            "reason": "insufficient_data",
            "sample_size": len(recent),
        }
    # Real trade records store `round_profit` (positive = win, negative = loss).
    # partial=True records are already excluded by get_recent_trades().
    wins = sum(1 for t in recent if float(t.get("round_profit", 0) or 0) > 0)
    lb = wilson_lower_bound(wins, len(recent))
    return {
        "would_suspend": lb < min_wilson_winrate,
        "wilson_lower_bound": round(lb, 3),
        "raw_winrate": round(wins / len(recent), 3),
        "sample_size": len(recent),
    }


def pattern_quality_factor(
    choppiness_index: float,
    efficiency_ratio: float,
    spike_rejection_ratio: float,
    er_target: float = 0.5,
) -> float:
    """
    0.0-1.0 multiplier applied to the raw movement score. Uses a geometric
    mean deliberately: ANY one bad dimension (heavy chop, poor efficiency,
    or heavy spike-rejection) crushes the whole factor toward zero rather
    than being averaged away by good numbers elsewhere. This is what makes
    chop/spike "the real killers" dominate the ranking, per design intent.
    """
    ci_factor = max(0.0, 1.0 - (choppiness_index / 100.0))
    er_factor = min(1.0, max(0.0, efficiency_ratio / er_target))
    spike_factor = max(0.0, 1.0 - spike_rejection_ratio)
    return (ci_factor * er_factor * spike_factor) ** (1 / 3)


def adjusted_score(raw_score: float, quality_factor: float) -> float:
    return round(raw_score * quality_factor, 2)

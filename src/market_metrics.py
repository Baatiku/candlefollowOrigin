"""Pure market metrics for straddle suitability (shared by bot and analysis)."""
from __future__ import annotations

import config as app_config


def candle_ohlc(candle: dict) -> tuple[float, float, float, float]:
    open_ = float(candle.get("open", 0) or 0)
    close = float(candle.get("close", 0) or 0)
    high = float(candle.get("max", candle.get("high", 0)) or 0)
    low = float(candle.get("min", candle.get("low", 0)) or 0)
    if high <= 0 and close > 0:
        high = close
    if low <= 0 and close > 0:
        low = close
    return open_, high, low, close


def efficiency_ratio(closes: list[float]) -> float:
    if len(closes) < 2:
        return 0.0
    net_change = abs(closes[-1] - closes[0])
    total_movement = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net_change / total_movement if total_movement > 0 else 0.0


def normalized_slope(closes: list[float], spot: float) -> float:
    if len(closes) < 2 or spot <= 0:
        return 0.0
    x = list(range(len(closes)))
    x_mean = sum(x) / len(x)
    y_mean = sum(closes) / len(closes)
    numerator = sum((x[i] - x_mean) * (closes[i] - y_mean) for i in range(len(x)))
    denominator = sum((x[i] - x_mean) ** 2 for i in range(len(x)))
    raw_slope = numerator / denominator if denominator != 0 else 0.0
    return (raw_slope / spot) * 1_000_000.0


def atr_from_candles(candles: list[dict], count: int = 5) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, min(len(candles), count + 1)):
        _, high, low, _ = candle_ohlc(candles[i])
        _, _, _, prev_close = candle_ohlc(candles[i - 1])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _momentum_ratio(candles: list[dict]) -> float:
    """Recent 2-bar ATR vs prior 3-bar ATR (same idea as live bot momentum gate)."""
    if len(candles) < 6:
        return 1.0
    trs = []
    for i in range(1, len(candles)):
        _, high, low, _ = candle_ohlc(candles[i])
        _, _, _, prev_close = candle_ohlc(candles[i - 1])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    older = sum(trs[-5:-2]) / 3 if len(trs) >= 5 else trs[0]
    recent = sum(trs[-2:]) / 2 if len(trs) >= 2 else trs[-1]
    return recent / older if older > 0 else 1.0


def _doji_streak(candles: list[dict], min_body_pct: float = 0.00008) -> int:
    streak = 0
    for candle in reversed(candles[-5:]):
        open_, _, _, close = candle_ohlc(candle)
        if close <= 0:
            break
        if abs(close - open_) / close < min_body_pct:
            streak += 1
        else:
            break
    return streak


def candle_body_quality(candles: list[dict], lookback: int = 4) -> float:
    """
    Body-to-range ratio for recent candles: 0.0 = all wicks (direction rejected),
    1.0 = full clean body (direction sustained with no rejection shadows).
    Low values flag markets where price is being pushed back — high wick candles
    signal indecision or active counter-pressure against the trade direction.
    """
    ratios = []
    for candle in candles[-lookback:]:
        o, h, l, c = candle_ohlc(candle)
        if c <= 0:
            continue
        full_range = h - l
        if full_range <= 0:
            ratios.append(1.0)
            continue
        ratios.append(abs(c - o) / full_range)
    return round(sum(ratios) / len(ratios), 3) if ratios else 0.5


def entry_snapshot_from_candles(
    candles: list[dict],
    min_candle_body_pct: float = 0.00008,
    min_session_range_pct: float = 0.00045,
) -> dict | None:
    """
  Full picture of the chart in the ~15 minutes before trade entry.
  Used to compare winners vs losers and derive bot rules.
    """
    base = movement_score_from_candles(
        candles, min_candle_body_pct, min_session_range_pct
    )
    if not base:
        return None

    closes = []
    for c in candles:
        _, _, _, close = candle_ohlc(c)
        if close > 0:
            closes.append(close)

    slope_signed = normalized_slope(
        closes[-15:] if len(closes) >= 15 else closes, base["spot"]
    )
    recent = candles[-3:]
    rh, rl, rc = [], [], []
    for c in recent:
        o, h, l, cl = candle_ohlc(c)
        if cl > 0:
            rh.append(h)
            rl.append(l)
            rc.append(cl)
    last3_range_pct = 0.0
    if rc:
        avg = sum(rc) / len(rc)
        last3_range_pct = (max(rh) - min(rl)) / avg * 100 if avg else 0.0

    spot = base["spot"]
    atr = base["atr"]
    atr_pct = (atr / spot * 100) if spot > 0 else 0.0

    return {
        **base,
        "slope_signed": round(slope_signed, 1),
        "momentum_ratio": round(_momentum_ratio(candles), 2),
        "doji_streak": _doji_streak(candles, min_candle_body_pct),
        "body_quality": candle_body_quality(candles),
        "last_3m_range_pct": round(last3_range_pct, 4),
        "atr_pct": round(atr_pct, 4),
        "path_ratio": round(
            sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            / max(max(closes) - min(closes), spot * 1e-8),
            2,
        )
        if len(closes) > 1
        else 0.0,
        "active_ratio": round(
            sum(
                1
                for c in candles[-15:]
                if candle_ohlc(c)[3] > 0
                and abs(candle_ohlc(c)[3] - candle_ohlc(c)[0]) / candle_ohlc(c)[3]
                >= min_candle_body_pct
            )
            / max(1, min(15, len(candles))),
            2,
        ),
    }


def movement_score_from_candles(
    candles: list[dict],
    min_candle_body_pct: float = 0.00008,
    min_session_range_pct: float = 0.00045,
) -> dict | None:
    if len(candles) < 8:
        return None
    body_pcts = []
    closes = []
    highs = []
    lows = []
    for candle in candles:
        open_, high, low, close = candle_ohlc(candle)
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
    active_ratio = sum(1 for b in body_pcts if b >= min_candle_body_pct) / len(body_pcts)
    path_ratio = path / max(total_range, avg_close * 1e-8)

    flat_penalty = 0.0
    if range_pct < min_session_range_pct:
        flat_penalty = 35.0 * (1.0 - range_pct / min_session_range_pct)

    doji_ratio = sum(body_pcts) / len(body_pcts)
    doji_penalty = max(0.0, (min_candle_body_pct * 2 - doji_ratio)) * 2000

    score = (
        active_ratio * 45.0
        + min(path_ratio, 4.0) * 12.0
        + range_pct * 8000.0
        - flat_penalty
        - doji_penalty
    )

    spot = closes[-1]
    slope = normalized_slope(closes[-15:] if len(closes) >= 15 else closes, spot)
    er = efficiency_ratio(closes[-15:] if len(closes) >= 15 else closes)
    atr = atr_from_candles(candles[-6:], count=5)

    return {
        "score": round(max(0.0, score), 1),
        "efficiency_ratio": round(er, 3),
        "abs_slope": round(abs(slope), 1),
        "range_pct": round(range_pct * 100, 4),
        "atr": round(atr, 6),
        "spot": spot,
    }


def metrics_at_timestamp(
    get_candles_fn,
    asset: str,
    end_ts: float,
    analysis_candles: int = 20,
) -> dict | None:
    try:
        candles = get_candles_fn(asset, app_config.FOLLOW_CANDLE_TIMEFRAME, analysis_candles, end_ts)
    except Exception:
        return None
    if not candles:
        return None
    return entry_snapshot_from_candles(candles)

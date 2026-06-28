"""Analyze winning vs losing rounds against historical candle patterns."""
from __future__ import annotations

import config as app_config

import logging
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from market_metrics import metrics_at_timestamp, entry_snapshot_from_candles
from pattern_profile import effective_gates, save_pattern_profile
from candle_fetch import build_entry_snapshot_cache, attach_snapshots_to_trades
from entry_pattern_learning import analyze_entry_patterns, profile_by_asset
from trade_log import read_trades
from iq_trade_history import (
    asset_matches_filter,
    fetch_digital_history_paginated,
    group_positions_into_rounds,
    positions_to_single_trades,
    temporary_balance,
)

logger = logging.getLogger(__name__)

MIN_STRADDLE_ER = 0.10
MIN_STRADDLE_SLOPE = 8.0


def _parse_trade_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 3)


def _profile(samples: list[dict]) -> dict:
    if not samples:
        return {}
    keys = ("efficiency_ratio", "abs_slope", "score", "range_pct", "atr")
    out = {"count": len(samples)}
    for key in keys:
        vals = [s[key] for s in samples if s.get(key) is not None]
        if vals:
            out[f"{key}_median"] = _median(vals)
            out[f"{key}_mean"] = round(sum(vals) / len(vals), 3)
    return out


def _rounds_from_local_log(account_key: str | None, limit: int) -> list[dict]:
    trades = read_trades(limit=limit, account_key=account_key)
    rounds = []
    for t in trades:
        if t.get("partial"):
            continue
        ts = _parse_trade_ts(t.get("ts"))
        if not ts:
            continue
        snap = t.get("entry_snapshot")
        row = {
            "asset": t.get("asset"),
            "close_ts": ts,
            "close_iso": t.get("ts"),
            "round_profit": float(t.get("round_profit", 0)),
            "tier": t.get("tier"),
            "step": t.get("step"),
            "source": "local_log",
        }
        if snap:
            row["entry_snapshot"] = snap
            row["metrics"] = snap
        rounds.append(row)
    return rounds


def enrich_rounds_with_metrics(api, rounds: list[dict], max_candle_lookups: int = 250) -> list[dict]:
    """Prefer entry_snapshot from trade log; fetch candles only for gaps."""
    with_snap = []
    need_api = []
    for r in rounds:
        snap = r.get("entry_snapshot") or r.get("metrics")
        if snap:
            with_snap.append({**r, "entry_snapshot": snap, "metrics": snap})
        else:
            need_api.append(r)

    if not need_api:
        logger.info(
            "Entry snapshots: %s/%s from trade log (no candle API)",
            len(with_snap),
            len(rounds),
        )
        return with_snap

    if not api:
        return with_snap + need_api

    cache, stats = build_entry_snapshot_cache(
        api, need_api, max_keys=max_candle_lookups, pause_sec=0.16
    )
    fetched = attach_snapshots_to_trades(need_api, cache)
    logger.info(
        "Entry snapshots: %s from log, %s/%s via API (%s calls)",
        len(with_snap),
        stats.get("cached"),
        stats.get("unique_minutes"),
        stats.get("api_calls"),
    )
    return with_snap + fetched


def _split_win_loss(rounds: list[dict]) -> tuple[list[dict], list[dict]]:
    wins, losses = [], []
    for r in rounds:
        pnl = float(r.get("round_profit", 0))
        if pnl >= 0:
            wins.append(r)
        else:
            losses.append(r)
    return wins, losses


def _metric_samples(rounds: list[dict]) -> list[dict]:
    return [r["metrics"] for r in rounds if r.get("metrics")]


def _would_pass_gates(metrics: dict, min_er: float, min_slope: float) -> bool:
    if not metrics:
        return False
    return (
        metrics.get("efficiency_ratio", 0) >= min_er
        and metrics.get("abs_slope", 0) >= min_slope
    )


def suggest_thresholds(win_metrics: list[dict], loss_metrics: list[dict]) -> dict:
    """
    Thresholds from your win distribution (~70% of past wins still pass),
    tightened using loss data when available.
    """
    win_er = [m["efficiency_ratio"] for m in win_metrics if m.get("efficiency_ratio") is not None]
    win_slope = [m["abs_slope"] for m in win_metrics if m.get("abs_slope") is not None]
    loss_er = [m["efficiency_ratio"] for m in loss_metrics if m.get("efficiency_ratio") is not None]
    loss_slope = [m["abs_slope"] for m in loss_metrics if m.get("abs_slope") is not None]

    rec_er = MIN_STRADDLE_ER
    rec_slope = MIN_STRADDLE_SLOPE

    if len(win_er) >= 5:
        sorted_er = sorted(win_er)
        p30 = sorted_er[max(0, int(len(sorted_er) * 0.30) - 1)]
        rec_er = round(max(0.12, p30 * 0.92), 2)
    if len(win_slope) >= 5:
        sorted_sl = sorted(win_slope)
        p30 = sorted_sl[max(0, int(len(sorted_sl) * 0.30) - 1)]
        rec_slope = round(max(12.0, p30 * 0.85), 1)

    if loss_er and win_er:
        gap_er = max(0.05, (min(win_er) + max(loss_er)) / 2.0)
        rec_er = round(max(rec_er * 0.85, min(rec_er, gap_er)), 2)
    if loss_slope and win_slope:
        gap_sl = max(10.0, (min(win_slope) + max(loss_slope)) / 2.0)
        rec_slope = round(max(rec_slope * 0.85, min(rec_slope, gap_sl)), 1)

    return {
        "min_efficiency_ratio": rec_er,
        "min_directional_slope": rec_slope,
    }


def build_bot_rules_from_trade_stats(by_asset: dict, min_trades: int = 15) -> dict:
    """Pair focus/caution from win rate when candle metrics are unavailable."""
    ranked = []
    for asset, stats in by_asset.items():
        total = stats.get("w", 0) + stats.get("l", 0)
        if total < min_trades:
            continue
        wr = stats["w"] / total * 100
        ranked.append((asset, wr, total))
    ranked.sort(key=lambda x: x[1], reverse=True)
    focus = [ranked[0][0]] if ranked and ranked[0][1] >= 33 else []
    caution = []
    if len(ranked) >= 2 and ranked[-1][1] < ranked[0][1] - 4:
        caution = [ranked[-1][0]]
    return {
        "min_efficiency_ratio": MIN_STRADDLE_ER,
        "min_directional_slope": MIN_STRADDLE_SLOPE,
        "focus_assets": focus,
        "caution_assets": caution,
        "skip_when_er_below": MIN_STRADDLE_ER,
        "notes": (
            f"Ranked by your history: best {ranked[0][0]} ({ranked[0][1]:.0f}% on {ranked[0][2]} trades)."
            if ranked
            else "Insufficient per-pair sample size."
        ),
    }


def build_bot_rules(
    thresholds: dict,
    by_asset: dict,
    win_profile: dict,
    loss_profile: dict,
    *,
    min_trades_per_asset: int = 25,
    min_win_rate_focus: float = 52.0,
) -> dict:
    """Translate statistics into rules the live bot can follow."""
    focus = []
    caution = []
    for asset, stats in by_asset.items():
        total = stats.get("w", 0) + stats.get("l", 0)
        if total < min_trades_per_asset:
            continue
        wr = stats["w"] / total * 100 if total else 0
        if wr >= min_win_rate_focus:
            focus.append(asset)
        elif wr < 40:
            caution.append(asset)

    focus.sort(
        key=lambda a: by_asset[a]["w"] / max(1, by_asset[a]["w"] + by_asset[a]["l"]),
        reverse=True,
    )

    return {
        "min_efficiency_ratio": thresholds["min_efficiency_ratio"],
        "min_directional_slope": thresholds["min_directional_slope"],
        "focus_assets": focus,
        "caution_assets": caution,
        "skip_when_er_below": thresholds["min_efficiency_ratio"],
        "notes": (
            f"Prefer {', '.join(focus[:3])} when tradeable."
            if focus
            else "Not enough per-asset data yet — keep global gates."
        ),
    }


def build_insights(
    win_profile: dict,
    loss_profile: dict,
    thresholds: dict,
    gate_accuracy: dict,
    *,
    emotional_summary: str | None = None,
    by_asset: dict | None = None,
) -> list[str]:
    insights = []
    if win_profile.get("efficiency_ratio_median") and loss_profile.get("efficiency_ratio_median"):
        w_er = win_profile["efficiency_ratio_median"]
        l_er = loss_profile["efficiency_ratio_median"]
        if l_er > 0:
            ratio = w_er / l_er
            insights.append(
                f"Winners had median ER {w_er:.2f} vs losers {l_er:.2f} ({ratio:.1f}×)."
            )
    if win_profile.get("abs_slope_median") and loss_profile.get("abs_slope_median"):
        insights.append(
            f"Winners had median slope {win_profile['abs_slope_median']:.1f} "
            f"vs losers {loss_profile.get('abs_slope_median', 0):.1f}."
        )
    insights.append(
        f"Suggested gates from your wins: ER ≥ {thresholds['min_efficiency_ratio']}, "
        f"slope ≥ {thresholds['min_directional_slope']}."
    )
    wins_pct = gate_accuracy.get("wins_pass_pct")
    loss_blk = gate_accuracy.get("losses_blocked_pct")
    if wins_pct is not None and loss_blk is not None:
        insights.append(
            f"Quality gates would have allowed {wins_pct:.0f}% of "
            f"your wins and blocked {loss_blk:.0f}% of losses."
        )
    elif wins_pct is not None:
        insights.append(f"Quality gates would have allowed {wins_pct:.0f}% of your wins.")
    if emotional_summary:
        insights.append(emotional_summary)
    if by_asset:
        best = sorted(
            by_asset.items(),
            key=lambda x: x[1]["w"] / max(1, x[1]["w"] + x[1]["l"]),
            reverse=True,
        )[:3]
        if best:
            parts = []
            for name, st in best:
                n = st["w"] + st["l"]
                if n >= 3:
                    wr = st["w"] / n * 100
                    parts.append(f"{name} {wr:.0f}% ({st['w']}W/{st['l']}L)")
            if parts:
                insights.append(f"Strongest pairs in this account: {', '.join(parts)}.")
    return insights


def _analyze_rounds_core(
    api,
    rounds: list[dict],
    *,
    use_learned_gates: bool = False,
    max_candle_lookups: int = 80,
) -> dict[str, Any]:
    gates = effective_gates() if use_learned_gates else {
        "min_efficiency_ratio": MIN_STRADDLE_ER,
        "min_directional_slope": MIN_STRADDLE_SLOPE,
    }
    min_er = gates["min_efficiency_ratio"]
    min_slope = gates["min_directional_slope"]

    enriched = enrich_rounds_with_metrics(
        api, rounds, max_candle_lookups=max_candle_lookups
    )
    wins, losses = _split_win_loss(enriched)
    win_m = _metric_samples(wins)
    loss_m = _metric_samples(losses)

    win_profile = _profile(win_m)
    loss_profile = _profile(loss_m)
    thresholds = suggest_thresholds(win_m, loss_m)

    wins_pass = sum(1 for m in win_m if _would_pass_gates(m, min_er, min_slope))
    losses_blocked = sum(
        1 for m in loss_m if not _would_pass_gates(m, min_er, min_slope)
    )
    gate_accuracy = {
        "wins_pass_pct": (wins_pass / len(win_m) * 100) if win_m else None,
        "losses_blocked_pct": (losses_blocked / len(loss_m) * 100) if loss_m else None,
    }

    by_asset = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for r in enriched:
        asset = r.get("asset") or "?"
        pnl = float(r.get("round_profit", 0))
        by_asset[asset]["pnl"] += pnl
        if pnl >= 0:
            by_asset[asset]["w"] += 1
        else:
            by_asset[asset]["l"] += 1

    losses_avoidable = sum(
        1
        for r in losses
        if r.get("metrics")
        and not _would_pass_gates(r["metrics"], thresholds["min_efficiency_ratio"], thresholds["min_directional_slope"])
    )
    emotional = None
    if losses and losses_avoidable:
        pct = losses_avoidable / len(losses) * 100
        emotional = (
            f"{losses_avoidable} of {len(losses)} losses ({pct:.0f}%) occurred in choppy/flat "
            f"conditions — the bot can skip those minutes so you trade less on bad setups."
        )

    bot_rules = build_bot_rules(thresholds, dict(by_asset), win_profile, loss_profile)
    insights = build_insights(
        win_profile,
        loss_profile,
        thresholds,
        gate_accuracy,
        emotional_summary=emotional,
        by_asset=dict(by_asset),
    )

    return {
        "rounds_analyzed": len(enriched),
        "wins": len(wins),
        "losses": len(losses),
        "winner_profile": win_profile,
        "loser_profile": loss_profile,
        "recommended_thresholds": thresholds,
        "bot_rules": bot_rules,
        "gate_accuracy": gate_accuracy,
        "insights": insights,
        "by_asset": dict(by_asset),
        "losses_avoidable_count": losses_avoidable,
        "rounds": [
            {
                "asset": r.get("asset"),
                "close_iso": r.get("close_iso"),
                "round_profit": r.get("round_profit"),
                "won": float(r.get("round_profit", 0)) >= 0,
                "source": r.get("source"),
                "metrics": r.get("metrics"),
                "would_pass_suggested_gates": _would_pass_gates(
                    r.get("metrics") or {},
                    thresholds["min_efficiency_ratio"],
                    thresholds["min_directional_slope"],
                ),
            }
            for r in enriched[:80]
        ],
    }


def learn_from_account_history(
    api,
    *,
    balance_id: int,
    account_label: str = "",
    source_account_type: str = "",
    asset_filters: list[str] | None = None,
    days_back: int = 90,
    max_positions: int = 500,
    mode: str = "legs",
    enrich_candles: bool = True,
    max_candle_lookups: int = 280,
) -> dict[str, Any]:
    """
    Deep analysis of any IQ balance (practice, real, or tournament):
    pull digital history, enrich wins/losses with chart metrics, output bot rules.
    """
    asset_filters = asset_filters or [
        "GBPJPY-OTC",
        "EURNZD-OTC",
        "AUDJPY-OTC",
        "GBPJPY",
        "EURNZD",
        "AUDJPY",
    ]

    with temporary_balance(api, balance_id):
        ok, positions, note = fetch_digital_history_paginated(
            api,
            balance_id=balance_id,
            days_back=days_back,
            max_positions=max_positions,
        )
    if not ok:
        return {"error": note, "balance_id": balance_id}

    filtered = [p for p in positions if asset_matches_filter(p.get("symbol"), asset_filters)]
    if mode == "rounds":
        rounds = group_positions_into_rounds(filtered)
    else:
        rounds = positions_to_single_trades(filtered, asset_filters=None)

    if not rounds:
        return {
            "error": "no trades found for selected pairs on this account",
            "balance_id": balance_id,
            "account_label": account_label,
            "iq_fetch_note": note,
            "positions_total": len(positions),
            "positions_filtered": len(filtered),
        }

    by_asset_raw = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for r in rounds:
        asset = r.get("asset") or "?"
        pnl = float(r.get("round_profit", 0))
        by_asset_raw[asset]["pnl"] += pnl
        if pnl > 0:
            by_asset_raw[asset]["w"] += 1
        else:
            by_asset_raw[asset]["l"] += 1

    if not enrich_candles:
        total_w = sum(1 for r in rounds if float(r.get("round_profit", 0)) > 0)
        total_l = len(rounds) - total_w
        by_asset = dict(by_asset_raw)
        thresholds = {
            "min_efficiency_ratio": MIN_STRADDLE_ER,
            "min_directional_slope": MIN_STRADDLE_SLOPE,
        }
        bot_rules = build_bot_rules_from_trade_stats(by_asset)
        ranked = sorted(
            by_asset.items(),
            key=lambda x: x[1]["w"] / max(1, x[1]["w"] + x[1]["l"]),
            reverse=True,
        )
        insights = [
            f"Full account history: {total_w}W / {total_l}L "
            f"({round(total_w / len(rounds) * 100, 1) if rounds else 0}% leg win rate).",
            "Per pair: "
            + ", ".join(
                f"{a} {s['w']}W/{s['l']}L ({s['w']/(s['w']+s['l'])*100:.0f}%)"
                for a, s in ranked
            ),
        ]
        if bot_rules.get("focus_assets"):
            insights.append(f"Prefer: {', '.join(bot_rules['focus_assets'])}.")
        if bot_rules.get("caution_assets"):
            insights.append(
                f"Use extra care or avoid: {', '.join(bot_rules['caution_assets'])}."
            )
        insights.append(
            "Run with candle enrichment later (bot stopped) for ER/slope tuning."
        )
        return {
            "source_balance_id": balance_id,
            "source_label": account_label,
            "asset_filters": asset_filters,
            "iq_fetch_note": note,
            "positions_total": len(positions),
            "positions_matched": len(filtered),
            "win_rate_pct": round(total_w / len(rounds) * 100, 1) if rounds else 0,
            "wins": total_w,
            "losses": total_l,
            "by_asset": by_asset,
            "recommended_thresholds": thresholds,
            "bot_rules": bot_rules,
            "insights": insights,
            "enrich_candles": False,
        }

    with temporary_balance(api, balance_id):
        enriched = enrich_rounds_with_metrics(
            api, rounds, max_candle_lookups=max_candle_lookups
        )
    pattern = analyze_entry_patterns(enriched)
    per_asset_chart = profile_by_asset(enriched)

    total_w = sum(1 for r in rounds if float(r.get("round_profit", 0)) > 0)
    total_l = len(rounds) - total_w
    pair_rules = build_bot_rules_from_trade_stats(dict(by_asset_raw))

    bot_rules = dict(pattern.get("bot_rules") or {})
    if pair_rules.get("focus_assets"):
        bot_rules["focus_assets"] = pair_rules["focus_assets"]
    if pair_rules.get("caution_assets"):
        bot_rules["caution_assets"] = pair_rules["caution_assets"]

    insights = list(pattern.get("insights") or [])
    insights.insert(
        0,
        f"Account: {total_w}W / {total_l}L on selected pairs "
        f"({round(total_w / len(rounds) * 100, 1) if rounds else 0}% leg win rate, "
        f"{pattern.get('trades_with_snapshots', 0)} trades with chart snapshots).",
    )

    return {
        "source_balance_id": balance_id,
        "source_label": account_label,
        "source_account_type": source_account_type,
        "asset_filters": asset_filters,
        "analysis_mode": mode,
        "iq_fetch_note": note,
        "positions_total": len(positions),
        "positions_matched": len(filtered),
        "win_rate_pct": round(total_w / len(rounds) * 100, 1) if rounds else 0,
        "wins": total_w,
        "losses": total_l,
        "by_asset": dict(by_asset_raw),
        "by_asset_chart": per_asset_chart,
        "before_trade_conditions": {
            "winner_profile": pattern.get("winner_profile"),
            "loser_profile": pattern.get("loser_profile"),
            "comparisons": pattern.get("comparisons"),
            "chart_summary": pattern.get("chart_summary"),
        },
        "recommended_thresholds": {
            "min_efficiency_ratio": bot_rules.get("min_efficiency_ratio", MIN_STRADDLE_ER),
            "min_directional_slope": bot_rules.get("min_directional_slope", MIN_STRADDLE_SLOPE),
        },
        "bot_rules": bot_rules,
        "learned_rules": pattern.get("learned_rules"),
        "gate_accuracy": pattern.get("gate_accuracy"),
        "insights": insights,
        "enrich_candles": True,
        "trades_with_snapshots": pattern.get("trades_with_snapshots"),
    }


def analyze_trade_patterns(
    api,
    *,
    account_key: str | None = None,
    balance_id: int | None = None,
    limit: int = 40,
    include_iq_history: bool = True,
    days_back: int = 14,
) -> dict[str, Any]:
    """
    Combine local bot trade log + IQ account history; compare candle metrics on wins vs losses.
    account_key filters the local log; balance_id selects which IQ balance history to pull.
    """
    local_rounds = _rounds_from_local_log(account_key, limit=limit)
    iq_note = ""
    iq_rounds: list[dict] = []

    if include_iq_history and api:
        ok, positions, iq_note = fetch_digital_history_paginated(
            api,
            balance_id=balance_id,
            days_back=days_back,
            max_positions=limit * 3,
        )
        if ok:
            iq_rounds = group_positions_into_rounds(positions)

    seen = {(r.get("asset"), int(r.get("close_ts", 0) // 60)) for r in local_rounds}
    merged = list(local_rounds)
    for r in iq_rounds:
        key = (r.get("asset"), int(r.get("close_ts", 0) // 60))
        if key not in seen:
            merged.append(r)
            seen.add(key)

    merged.sort(key=lambda x: x.get("close_ts") or 0, reverse=True)
    merged = merged[:limit]

    core = _analyze_rounds_core(api, merged)
    return {
        **core,
        "sources": {
            "local_log": len(local_rounds),
            "iq_history": len(iq_rounds),
            "iq_fetch_note": iq_note,
        },
        "current_gates": {
            "min_efficiency_ratio": MIN_STRADDLE_ER,
            "min_directional_slope": MIN_STRADDLE_SLOPE,
        },
    }


def backtest_pair_readiness(
    api,
    asset: str,
    *,
    lookback_candles: int = 30,
    min_er: float = MIN_STRADDLE_ER,
    min_slope: float = MIN_STRADDLE_SLOPE,
) -> dict[str, Any]:
    """
    Walk backward over recent 1m candles: how often would straddle gates pass?
    """
    import time

    if not api:
        return {"error": "not connected", "asset": asset}

    try:
        candles = api.get_candles(asset, app_config.FOLLOW_CANDLE_TIMEFRAME, lookback_candles + 15, time.time())
    except Exception as e:
        return {"error": str(e), "asset": asset}

    if not candles or len(candles) < 20:
        return {"error": "insufficient candles", "asset": asset}

    passes = 0
    samples = []
    # Evaluate at each minute using prior 15 candles
    for i in range(15, len(candles)):
        window = candles[i - 15 : i]
        from market_metrics import movement_score_from_candles

        m = movement_score_from_candles(window)
        if not m:
            continue
        ok = _would_pass_gates(m, min_er, min_slope)
        if ok:
            passes += 1
        samples.append(
            {
                "er": m["efficiency_ratio"],
                "slope": m["abs_slope"],
                "pass": ok,
            }
        )

    total = len(samples)
    pass_rate = (passes / total * 100) if total else 0
    return {
        "asset": asset,
        "lookback_minutes": total,
        "pass_count": passes,
        "pass_rate_pct": round(pass_rate, 1),
        "latest": samples[-1] if samples else None,
        "tradeable_now": bool(samples and samples[-1]["pass"]),
    }

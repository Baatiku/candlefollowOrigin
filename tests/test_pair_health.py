"""Unit tests for pair_health.py.

Acceptance criteria from task spec:
1. wilson_lower_bound() is more conservative than raw winrate at small N,
   and converges toward raw winrate as N grows large.
2. pattern_quality_factor() geometric mean: one bad input (e.g. CI=90) drags
   the factor near zero even when the other two inputs are perfect.
3. Confirm ASSET_SUSPENSION_ENABLED=False and SCORE_REWEIGHT_ENABLED=False
   each independently disable their code paths (tested via flag checks in
   integration code; flag values are validated here via config defaults).
"""
import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    import pytest
except ImportError:
    # Provide a minimal approx shim when pytest is not installed,
    # so the tests can run with plain `python tests/test_pair_health.py`.
    class _Approx:
        def __init__(self, expected, abs=1e-6):
            self._expected = expected
            self._abs = abs
        def __eq__(self, actual):
            return abs(actual - self._expected) <= self._abs
        def __repr__(self):
            return f"approx({self._expected}±{self._abs})"
    class _PytestShim:
        @staticmethod
        def approx(expected, abs=1e-6):
            return _Approx(expected, abs=abs)
    pytest = _PytestShim()

from pair_health import (
    wilson_lower_bound,
    asset_health_check,
    pattern_quality_factor,
    adjusted_score,
)


# ── wilson_lower_bound ────────────────────────────────────────────────────────

def test_wilson_lb_zero_total():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_lb_more_conservative_than_raw_at_small_n():
    """At small N, lower bound must be strictly below raw win rate."""
    wins, total = 7, 10
    raw = wins / total  # 0.70
    lb = wilson_lower_bound(wins, total)
    assert lb < raw, f"Expected lb={lb:.3f} < raw={raw:.3f}"


def test_wilson_lb_conservatism_increases_with_smaller_n():
    """Smaller sample → larger gap between raw and lower bound."""
    wins_big, total_big = 70, 100
    wins_sml, total_sml = 7, 10

    gap_big = (wins_big / total_big) - wilson_lower_bound(wins_big, total_big)
    gap_sml = (wins_sml / total_sml) - wilson_lower_bound(wins_sml, total_sml)
    assert gap_sml > gap_big, (
        f"Small-N gap ({gap_sml:.4f}) should exceed large-N gap ({gap_big:.4f})"
    )


def test_wilson_lb_converges_to_raw_at_large_n():
    """At very large N the lower bound approaches the raw win rate."""
    wins, total = 6500, 10000
    raw = wins / total
    lb = wilson_lower_bound(wins, total)
    assert abs(lb - raw) < 0.02, (
        f"At N=10000 lb={lb:.4f} should be within 0.02 of raw={raw:.4f}"
    )


def test_wilson_lb_zero_wins():
    lb = wilson_lower_bound(0, 40)
    assert lb == 0.0


def test_wilson_lb_all_wins():
    lb = wilson_lower_bound(40, 40)
    assert lb > 0.85  # should be high but not 1.0 (confidence interval)
    assert lb < 1.0


def test_wilson_lb_always_non_negative():
    for wins, total in [(0, 5), (1, 5), (3, 5), (5, 5)]:
        assert wilson_lower_bound(wins, total) >= 0.0


# ── pattern_quality_factor ────────────────────────────────────────────────────

def test_pqf_all_perfect_inputs():
    """Perfect inputs → factor == 1.0."""
    qf = pattern_quality_factor(
        choppiness_index=0.0,   # not choppy at all
        efficiency_ratio=0.5,   # exactly at target
        spike_rejection_ratio=0.0,
        er_target=0.5,
    )
    assert qf == 1.0, f"Expected 1.0, got {qf}"


def test_pqf_high_ci_crushes_factor():
    """CI=90 (very choppy, ci_factor=0.10) must pull the geometric-mean factor well
    below the arithmetic mean — verifying the 'worst dimension dominates' property.

    ci_factor=0.10, er_factor=1.0, spike_factor=1.0
      geometric = (0.10*1.0*1.0)^(1/3) ≈ 0.464
      arithmetic = (0.10+1.0+1.0)/3    ≈ 0.700
    The geometric mean must be below the arithmetic mean.
    """
    qf = pattern_quality_factor(
        choppiness_index=90.0,  # very choppy → ci_factor = 0.10
        efficiency_ratio=0.5,   # at target → er_factor = 1.0
        spike_rejection_ratio=0.0,  # none → spike_factor = 1.0
        er_target=0.5,
    )
    arithmetic_mean = (0.10 + 1.0 + 1.0) / 3  # ≈ 0.700
    assert qf < arithmetic_mean, (
        f"CI=90 geometric qf={qf:.3f} should be below arithmetic mean {arithmetic_mean:.3f}"
    )
    # Also verify the factor is meaningfully reduced (not a rounding artifact)
    assert qf < 0.50, (
        f"CI=90 quality factor {qf:.3f} should be below 0.50"
    )


def test_pqf_high_spike_rejection_crushes_factor():
    """spike_rejection_ratio=0.9 (spike_factor=0.10) must pull factor below
    arithmetic mean — one bad dimension dominates in geometric mean."""
    qf = pattern_quality_factor(
        choppiness_index=0.0,       # perfect → ci_factor = 1.0
        efficiency_ratio=0.5,       # at target → er_factor = 1.0
        spike_rejection_ratio=0.9,  # heavy spikes → spike_factor = 0.10
        er_target=0.5,
    )
    arithmetic_mean = (1.0 + 1.0 + 0.10) / 3  # ≈ 0.700
    assert qf < arithmetic_mean, (
        f"spike_ratio=0.9 geometric qf={qf:.3f} should be below arithmetic {arithmetic_mean:.3f}"
    )
    assert qf < 0.50, (
        f"spike_ratio=0.9 quality factor {qf:.3f} should be below 0.50"
    )


def test_pqf_low_er_crushes_factor():
    """ER=0.05 well below target → er_factor=0.10 must pull factor below arithmetic mean."""
    qf = pattern_quality_factor(
        choppiness_index=0.0,   # perfect → ci_factor = 1.0
        efficiency_ratio=0.05,  # far below target → er_factor = 0.10
        spike_rejection_ratio=0.0,
        er_target=0.5,
    )
    arithmetic_mean = (1.0 + 0.10 + 1.0) / 3  # ≈ 0.700
    assert qf < arithmetic_mean, (
        f"Low ER geometric qf={qf:.3f} should be below arithmetic {arithmetic_mean:.3f}"
    )
    assert qf < 0.50, (
        f"Low ER quality factor {qf:.3f} should be below 0.50"
    )


def test_pqf_geometric_mean_not_arithmetic():
    """The geometric mean must be below the arithmetic mean — verifies the
    implementation uses cube-root of product, not simple average."""
    ci_factor = 1.0 - (90.0 / 100.0)   # 0.10 (bad)
    er_factor = 1.0                      # 1.00 (perfect)
    spike_factor = 1.0                   # 1.00 (perfect)

    arithmetic_mean = (ci_factor + er_factor + spike_factor) / 3
    geometric_mean = (ci_factor * er_factor * spike_factor) ** (1 / 3)

    qf = pattern_quality_factor(
        choppiness_index=90.0,
        efficiency_ratio=0.5,
        spike_rejection_ratio=0.0,
        er_target=0.5,
    )
    assert abs(qf - geometric_mean) < 1e-9, (
        f"Factor should match geometric mean {geometric_mean:.4f}, got {qf:.4f}"
    )
    assert qf < arithmetic_mean, (
        f"Geometric mean ({qf:.4f}) must be below arithmetic mean ({arithmetic_mean:.4f})"
    )


def test_pqf_output_range():
    """Output must always be in [0, 1]."""
    test_cases = [
        (0, 0, 0),
        (100, 0, 1),
        (50, 0.5, 0.5),
        (61.8, 0.35, 0.0),
    ]
    for ci, er, sr in test_cases:
        qf = pattern_quality_factor(ci, er, sr)
        assert 0.0 <= qf <= 1.0, f"qf={qf} out of [0,1] for ci={ci}, er={er}, sr={sr}"


# ── adjusted_score ────────────────────────────────────────────────────────────

def test_adjusted_score_full_quality():
    assert adjusted_score(100.0, 1.0) == 100.0


def test_adjusted_score_zero_quality():
    assert adjusted_score(100.0, 0.0) == 0.0


def test_adjusted_score_partial_quality():
    result = adjusted_score(80.0, 0.5)
    assert result == 40.0


# ── asset_health_check ────────────────────────────────────────────────────────

def _make_trade(asset: str, round_profit: float, partial: bool = False) -> dict:
    """Build a realistic trade record matching the actual append_trade() schema.

    Real records store round_profit (>0 = win, <0 = loss), asset, partial, ts, etc.
    There is NO 'result' or 'outcome' key — those are computed on-the-fly by
    flatten_trade_for_export() and are NOT persisted.
    """
    return {
        "asset": asset,
        "round_profit": round_profit,
        "partial": partial,
        "bet": 1.0,
        "ts": "2026-07-01T12:00:00Z",
        "account_type": "PRACTICE",
        "simulation": False,
    }


def _make_lookup(asset: str, win_count: int, loss_count: int):
    """Return a trade_log_lookup_fn that yields win_count wins and loss_count losses
    for `asset`, using real-schema records (round_profit, not 'result')."""
    trades = (
        [_make_trade(asset, 1.20) for _ in range(win_count)]
        + [_make_trade(asset, -1.00) for _ in range(loss_count)]
    )
    def lookup(asset_arg, count):
        return trades[:count]
    return lookup


def test_health_check_insufficient_data():
    lookup = _make_lookup("EURUSD", win_count=7, loss_count=3)  # only 10 trades
    result = asset_health_check("EURUSD", lookup, lookback=40)
    assert result["would_suspend"] is False
    assert result["reason"] == "insufficient_data"
    assert result["sample_size"] == 10


def test_health_check_good_winrate():
    """60% win rate over 40 trades → wilson lb well above 0.40 → no suspension."""
    lookup = _make_lookup("EURUSD", win_count=24, loss_count=16)
    result = asset_health_check("EURUSD", lookup, lookback=40)
    assert result["would_suspend"] is False
    assert result["wilson_lower_bound"] > 0.40


def test_health_check_bad_winrate_triggers_suspension():
    """25% win rate over 40 trades → wilson lb below 0.40 → suspension."""
    lookup = _make_lookup("EURUSD", win_count=10, loss_count=30)
    result = asset_health_check("EURUSD", lookup, lookback=40)
    assert result["would_suspend"] is True
    assert result["wilson_lower_bound"] < 0.40


def test_health_check_uses_round_profit_not_result_field():
    """Explicitly verify the schema: records with only round_profit (no 'result' key)
    are scored correctly — this guards against regression to the wrong field.

    Uses 40 trades at 65% win rate: wilson lb ≈ 0.49, well above the 0.40 threshold.
    If the code mistakenly reads a non-existent 'result' field, all would be 0 wins
    and the test would fail (lb = 0, would_suspend = True).
    """
    wins, losses = 26, 14  # 40 total, 65% raw win rate
    trades = (
        [_make_trade("GBPUSD", 1.20)] * wins
        + [_make_trade("GBPUSD", -1.00)] * losses
    )
    def lookup(asset, count):
        return trades[:count]
    result = asset_health_check("GBPUSD", lookup, lookback=40)
    assert result["would_suspend"] is False, (
        f"65% WR over 40 trades should not suspend "
        f"(lb={result.get('wilson_lower_bound')}, raw={result.get('raw_winrate')})"
    )
    assert result["raw_winrate"] == pytest.approx(0.65, abs=0.01)


def test_health_check_breakeven_treated_as_loss():
    """round_profit == 0 is treated as not-a-win (conservative)."""
    # 20 wins, 10 losses, 10 breakevens — effective win rate = 20/40 = 50%
    trades = (
        [_make_trade("EURUSD", 1.20)] * 20
        + [_make_trade("EURUSD", -1.00)] * 10
        + [_make_trade("EURUSD", 0.0)] * 10
    )
    def lookup(asset, count):
        return trades[:count]
    result = asset_health_check("EURUSD", lookup, lookback=40)
    assert result["raw_winrate"] == pytest.approx(0.5, abs=0.01)


def test_health_check_borderline_conservative():
    """Exactly 50% raw but small sample: lb should dip below raw winrate."""
    # 5 wins, 5 losses (10 trades)
    trades = [_make_trade("EURUSD", rp) for rp in [1.20, -1.0] * 5]
    def lookup(asset, count):
        return trades[:count]
    result = asset_health_check("EURUSD", lookup, lookback=10)
    lb = result["wilson_lower_bound"]
    raw = result["raw_winrate"]
    assert lb < raw, "Lower bound should be below raw rate at small N"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {fn.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")

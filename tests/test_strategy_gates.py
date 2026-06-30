"""Invariant tests for double-martingale ladder rules and straddle gates."""
import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from strategies.double_martingale import (  # noqa: E402
    STANDARD_BUDGET_TIERS,
    STEP_FIVE_ROTATION_INDEX,
    TIER_EXHAUSTION_COOLDOWN_MINUTES,
    TIER_SECOND_EXHAUSTION_COOLDOWN_MINUTES,
    MIN_STRADDLE_EFFICIENCY_RATIO,
    MIN_STRADDLE_DIRECTIONAL_SLOPE,
    DoubleMartingaleBot,
    balance_tier_brackets,
    _TIMEOUT_SENTINEL,
)


class TestLadderInvariants(unittest.TestCase):
    def test_standard_tier_has_seven_steps(self):
        self.assertEqual(len(STANDARD_BUDGET_TIERS), 1)
        self.assertEqual(len(STANDARD_BUDGET_TIERS[0]), 7)

    def test_balance_baseline_tier_thresholds(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        # thresholds: <200 = 0, 200-999 = 1, 1000-2999 = 2, >=3000 = 3
        self.assertEqual(bot._balance_baseline_tier_index(150), 0)
        self.assertEqual(bot._balance_baseline_tier_index(500), 1)
        self.assertEqual(bot._balance_baseline_tier_index(1500), 2)
        self.assertEqual(bot._balance_baseline_tier_index(3500), 3)
        self.assertEqual(bot._balance_baseline_tier_index(9000), 3)

    def test_no_debt_uses_baseline_not_tier_one_when_balance_high(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.cumulative_debt = 0.0
        bot.current_tier_index = 0
        bot.assigned_tier_index = 0
        bot._sync_assigned_tier_for_trading(balance=3500.0)
        self.assertEqual(bot.assigned_tier_index, 3)
        self.assertEqual(bot.current_tier_index, 3)

    def test_compute_round_bet_matches_ladder(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.cumulative_debt = 0.0
        bot.current_tier_index = 0
        bot.session_round_count = 0
        bet = bot._compute_round_bet(balance=150.0)
        self.assertEqual(bet["amount"], 1.0)
        bot.session_round_count = 1
        bet = bot._compute_round_bet(balance=150.0)
        self.assertEqual(bet["amount"], 3.0)
        bot.cumulative_debt = 500.0
        bot.assigned_tier_index = 3
        bot.current_tier_index = 3
        bot.session_round_count = 2
        bet = bot._compute_round_bet(balance=50000.0)
        self.assertEqual(bet["amount"], 100.0)
        bot.cumulative_debt = 0.0
        bot.current_tier_index = 3
        bot.session_round_count = 2
        bet = bot._compute_round_bet(balance=150.0)
        self.assertEqual(bet["tier_number"], 1)
        self.assertEqual(bet["amount"], 1.0)

    def test_tier_exhausted_only_after_all_steps(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.current_tier_index = 0
        bot.session_round_count = 4
        self.assertFalse(bot._all_tier_steps_exhausted())
        bot.session_round_count = 5
        self.assertTrue(bot._all_tier_steps_exhausted())

    def test_win_with_debt_stays_on_assigned_tier_step1(self):
        """Win on assigned Tier 4 with debt → stay Tier 4 step 1 (no escalation on wins)."""
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 3
        bot.current_tier_index = 3
        bot.session_round_count = 2
        bot.cumulative_debt = 200.0
        bot.session_profit = 50.0
        bot._finalize_session("Round Won")
        self.assertEqual(bot.current_tier_index, 3)
        self.assertEqual(bot.assigned_tier_index, 3)
        self.assertEqual(bot.session_round_count, 0)
        self.assertGreater(bot.cumulative_debt, 0)

    def test_win_on_tier1_with_debt_stays_tier1_step1(self):
        """Win on Tier 1 with debt → Tier 1, step 1."""
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.current_tier_index = 0        # Tier 1
        bot.session_round_count = 2       # step 3
        bot.cumulative_debt = 30.0
        bot.session_profit = 10.0
        bot._finalize_session("Round Won")
        self.assertEqual(bot.current_tier_index, 0,  "should stay Tier 1")
        self.assertEqual(bot.session_round_count, 0, "step must reset to 1")
        self.assertGreater(bot.cumulative_debt, 0,   "debt still outstanding")

    def test_win_clears_debt_returns_tier1_step1(self):
        """Win that clears all debt → Tier 1, step 1 regardless of current tier."""
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.current_tier_index = 2        # Tier 3
        bot.session_round_count = 1       # step 2
        bot.cumulative_debt = 40.0
        bot.session_profit = 100.0        # profit > debt; debt will hit 0
        bot._finalize_session("Round Won")
        self.assertEqual(bot.current_tier_index, 0,  "debt cleared → must be Tier 1")
        self.assertEqual(bot.session_round_count, 0, "debt cleared → must be step 1")
        self.assertEqual(bot.cumulative_debt, 0.0,   "debt must be zero")

    def test_first_tier_exhaustion_retries_same_tier(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 0
        bot.current_tier_index = 0
        bot.session_round_count = 5
        bot.cumulative_debt = 100.0
        bot.session_profit = -30.0
        bot._finalize_session("Tier exhausted")
        self.assertEqual(bot.assigned_tier_index, 0)
        self.assertEqual(bot.current_tier_index, 0)
        self.assertEqual(bot.tier_failure_streak, 1)

    def test_second_tier_exhaustion_escalates_assigned_tier(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 0
        bot.current_tier_index = 0
        bot.tier_failure_streak = 1
        bot.session_round_count = 5
        bot.cumulative_debt = 100.0
        bot.session_profit = -30.0
        bot._finalize_session("Tier exhausted")
        self.assertEqual(bot.assigned_tier_index, 1)
        self.assertEqual(bot.current_tier_index, 1)
        self.assertEqual(bot.tier_failure_streak, 0)

    def test_first_exhaustion_uses_short_cooldown(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 0
        bot.tier_failure_streak = 0
        before = datetime.datetime.utcnow()
        bot._start_tier_exhaustion_cooldown()
        expected = before + datetime.timedelta(minutes=TIER_EXHAUSTION_COOLDOWN_MINUTES)
        self.assertAlmostEqual(
            bot.tier_exhaustion_cooldown_until.timestamp(),
            expected.timestamp(),
            delta=2.0,
        )

    def test_second_exhaustion_uses_long_cooldown(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 0
        bot.tier_failure_streak = 1
        bot.last_tier_exhaustion_at = datetime.datetime.utcnow() - datetime.timedelta(
            minutes=3
        )
        before = datetime.datetime.utcnow()
        bot._start_tier_exhaustion_cooldown()
        expected = before + datetime.timedelta(
            minutes=TIER_SECOND_EXHAUSTION_COOLDOWN_MINUTES
        )
        self.assertGreaterEqual(
            bot.tier_exhaustion_cooldown_until.timestamp(),
            expected.timestamp() - 1.0,
        )

    def test_win_on_tier2_does_not_escalate_to_tier3(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 1
        bot.current_tier_index = 1
        bot.session_round_count = 0
        bot.cumulative_debt = 80.0
        bot.session_profit = 15.0
        bot._finalize_session("Round Won")
        self.assertEqual(bot.assigned_tier_index, 1)
        self.assertEqual(bot.current_tier_index, 1)
        self.assertEqual(bot.session_round_count, 0)

    def test_evaluation_window_does_not_escalate_on_debt_alone(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.safe_get_balance = lambda: 600.0
        bot.session_peak_balance = 600.0
        bot.risk_mode_until = None
        bot.assigned_tier_index = 1
        bot.current_tier_index = 1
        bot.session_round_count = 1
        bot.cumulative_debt = 80.0
        bot.tier_failure_streak = 0
        bot.window_profit = 20.0
        bot._close_evaluation_window()
        self.assertEqual(bot.assigned_tier_index, 1)
        self.assertEqual(bot.current_tier_index, 1)
        self.assertEqual(bot.session_round_count, 1)
        self.assertEqual(bot.tier_failure_streak, 0)

    def test_balance_downgrade_uses_lower_affordable_step(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.strategy_mode = "directional_trend"
        bot.assigned_tier_index = 0
        bot.current_tier_index = 0
        bot.session_round_count = 3
        bot.cumulative_debt = 100.0
        bet = bot._compute_round_bet(balance=10.0)
        self.assertEqual(bet["tier_number"], 1)
        self.assertEqual(bet["step_number"], 3)
        self.assertEqual(bet["amount"], 7.0)
        self.assertLessEqual(bet["amount"], 10.0)
        self.assertEqual(bot.current_tier_index, 0)
        self.assertEqual(bot.session_round_count, 3)
        self.assertEqual(bet["scheduled_step_number"], 4)
        self.assertTrue(bet["balance_downgrade"])

    def test_balance_downgrade_falls_to_lower_affordable_step(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.strategy_mode = "directional_trend"
        bot.assigned_tier_index = 0
        bot.current_tier_index = 0
        bot.session_round_count = 4
        bot.cumulative_debt = 100.0
        bet = bot._compute_round_bet(balance=6.0)
        self.assertEqual(bet["tier_number"], 1)
        self.assertEqual(bet["step_number"], 2)
        self.assertEqual(bet["amount"], 3.0)
        self.assertEqual(bot.current_tier_index, 0)
        self.assertEqual(bot.assigned_tier_index, 0)
        self.assertEqual(bot.session_round_count, 4)

    def test_balance_downgrade_caps_assigned_tier_with_debt(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.strategy_mode = "directional_trend"
        bot.assigned_tier_index = 3
        bot.current_tier_index = 3
        bot.session_round_count = 0
        bot.cumulative_debt = 250.0
        bot._sync_assigned_tier_for_trading(balance=35.0)
        self.assertEqual(bot.assigned_tier_index, 0)
        self.assertEqual(bot.current_tier_index, 0)
        self.assertEqual(bot.session_round_count, 0)

    def test_sync_clamps_current_tier_down_to_assigned(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.safe_get_balance = lambda: 600.0
        bot.session_peak_balance = 600.0
        bot.risk_mode_until = None
        bot.assigned_tier_index = 1
        bot.current_tier_index = 2
        bot.session_round_count = 2
        bot.cumulative_debt = 50.0
        bot._sync_assigned_tier_for_trading()
        self.assertEqual(bot.current_tier_index, 1)
        self.assertEqual(bot.session_round_count, 0)


class TestStraddleOutcome(unittest.TestCase):
    def setUp(self):
        self.bot = DoubleMartingaleBot(simulation_mode=True)
        self.bot.current_bet = 10.0

    def test_loss_only_when_both_legs_lose(self):
        pl, both_lost, _ = self.bot._resolve_round_outcome(5.0, -10.0)
        self.assertFalse(both_lost)
        self.assertEqual(pl, -5.0)

        pl, both_lost, _ = self.bot._resolve_round_outcome(-10.0, -10.0)
        self.assertTrue(both_lost)
        self.assertEqual(pl, -20.0)

    def test_timeout_counts_as_leg_loss(self):
        pl, both_lost, timed_out = self.bot._resolve_round_outcome(
            _TIMEOUT_SENTINEL, _TIMEOUT_SENTINEL
        )
        self.assertTrue(timed_out)
        self.assertTrue(both_lost)
        self.assertEqual(pl, -20.0)

    def test_one_timeout_one_win_not_both_lost(self):
        pl, both_lost, _ = self.bot._resolve_round_outcome(_TIMEOUT_SENTINEL, 8.0)
        self.assertFalse(both_lost)
        self.assertEqual(pl, -2.0)


class TestDirectionalTrendMartingale(unittest.TestCase):
    def test_default_strategy_mode(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        self.assertEqual(bot.strategy_mode, "directional_trend")

    def test_determine_trend_direction_simple(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot._estimate_spot_price = lambda prices: 1.2000
        # Mock _calculate_trend_metrics directly
        bot._calculate_trend_metrics = lambda asset, spot, count: (20.0, 0.4) # strong uptrend
        direction = bot._determine_trend_direction()
        self.assertEqual(direction, "call")

        bot._calculate_trend_metrics = lambda asset, spot, count: (-20.0, 0.4) # strong downtrend
        direction = bot._determine_trend_direction()
        self.assertEqual(direction, "put")

    def test_determine_trend_direction_reversal_filter_blocks_minor_pullback(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot._estimate_spot_price = lambda prices: 1.2000
        # Slope flipped to Downtrend (-24.0), but long-term trend (count=35) is flat so it doesn't hard-block CALL
        bot._calculate_trend_metrics = lambda asset, spot, count: (-24.0, 0.4) if count == 15 else (0.0, 0.4)
        
        # Mock API calls to return a structure that FAILS reversal confirmation (e.g. no breach)
        class MockAPI:
            def get_candles(self, asset, timeframe, count, end_time):
                return [{"close": 1.2000} for _ in range(20)]
        bot.api = MockAPI()
        bot._calculate_atr = lambda asset, count: 0.0100
        bot._is_momentum_accelerating = lambda asset: (False, 0.0050, 0.0060) # decelerating (ratio < 1.2)
        
        direction = bot._determine_trend_direction(last_direction="call")
        self.assertEqual(direction, "call") # Blocks reversal -> Correction

    def test_determine_trend_direction_reversal_filter_allows_strong_break(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot._estimate_spot_price = lambda prices: 1.1500  # Spot is 1.1500 (lower)
        bot._calculate_trend_metrics = lambda asset, spot, count: (-25.0, 0.4)
        
        # Mock EMA15 = 1.2000. Breach = 1.2000 - 1.1500 = 0.0500. ATR = 0.0100. Breach (0.0500) > 1.2 * ATR (0.0120) -> CONFIRMED
        class MockAPI:
            def get_candles(self, asset, timeframe, count, end_time):
                return [{"close": 1.2000} for _ in range(20)]
        bot.api = MockAPI()
        bot._calculate_atr = lambda asset, count: 0.0100
        bot._is_momentum_accelerating = lambda asset: (True, 0.0200, 0.0100) # ratio = 2.0 (>= 1.2)
        
        direction = bot._determine_trend_direction(last_direction="call")
        self.assertEqual(direction, "put") # Reversal confirmed -> Switches to PUT


class TestTierExhaustionDirectionFlip(unittest.TestCase):
    """Tier exhaustion no longer flips direction — candle follow picks each minute."""

    def test_tier_exhaust_keeps_direction(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.assigned_tier_index = 0
        bot.tier_failure_streak = 0
        bot.current_tier_index = 0
        bot.session_round_count = 5
        bot.cumulative_debt = 100.0
        bot.session_profit = -13.0
        bot.last_trend_direction = "call"
        bot.auto_select_asset = False
        bot._finalize_session("Tier exhausted")
        self.assertEqual(bot.last_trend_direction, "call")
        self.assertEqual(bot.current_tier_index, 0)
        self.assertEqual(bot.session_round_count, 0)

    def test_straddle_mode_no_flip(self):
        """Tier exhaustion never flipped straddle mode either."""
        bot = DoubleMartingaleBot(simulation_mode=True, strategy_mode="straddle")
        bot.current_tier_index = 0
        bot.session_round_count = 5
        bot.cumulative_debt = 100.0
        bot.session_profit = -13.0
        bot.last_trend_direction = "call"
        bot._finalize_session("Tier exhausted")
        self.assertEqual(bot.last_trend_direction, "call")


class TestStepFiveAssetRotation(unittest.TestCase):
    def test_step_five_win_penalizes_and_switches(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.asset = "EURUSD-OTC"
        bot.auto_select_asset = True
        bot.session_round_count = STEP_FIVE_ROTATION_INDEX
        bot._apply_auto_asset_selection = MagicMock()
        bot._wait_for_price_data = MagicMock()
        bot._notify = MagicMock()
        bot._rotate_asset_after_step_five("win")
        self.assertIn("EURUSD-OTC", bot.asset_penalty_box)
        bot._apply_auto_asset_selection.assert_called_once()

    def test_step_four_does_not_rotate(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.asset = "EURUSD-OTC"
        bot.session_round_count = STEP_FIVE_ROTATION_INDEX - 1
        bot._apply_auto_asset_selection = MagicMock()
        bot._rotate_asset_after_step_five("loss")
        self.assertNotIn("EURUSD-OTC", bot.asset_penalty_box)
        bot._apply_auto_asset_selection.assert_not_called()


class TestBalanceTierBrackets(unittest.TestCase):
    def test_highest_matching_row_for_six_hundred(self):
        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.auto_bracket_enabled = True
        bot.current_tier_index = 0
        bot._update_budget_tiers_for_balance(600.0)
        self.assertEqual(bot.budget_tiers[0][0], 9)

    def test_bracket_labels_include_ranges(self):
        brackets = balance_tier_brackets()
        self.assertTrue(any(b["range_label"].startswith("$500") for b in brackets))
        self.assertEqual(brackets[0]["min_balance"], 0)


class TestPairGateHelpers(unittest.TestCase):
    def test_pair_condition_failure_detects_chop(self):
        self.assertTrue(
            DoubleMartingaleBot._is_pair_condition_failure(
                "choppy market (ER 0.021 < 0.25)"
            )
        )

    def test_pair_condition_failure_ignores_strike_timing(self):
        self.assertFalse(
            DoubleMartingaleBot._is_pair_condition_failure("no qualifying strikes")
        )

    def test_straddle_thresholds_documented(self):
        self.assertEqual(MIN_STRADDLE_EFFICIENCY_RATIO, 0.45)
        self.assertEqual(MIN_STRADDLE_DIRECTIONAL_SLOPE, 35.0)


if __name__ == "__main__":
    unittest.main()

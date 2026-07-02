"""Unit tests for capital risk governor helpers."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from risk_governor import (  # noqa: E402
    compute_risk_limits,
    tier_index_for_balance,
    update_profit_lock_on_peak,
)
from strategies.double_martingale import STANDARD_BUDGET_TIERS  # noqa: E402

CEILING = [(3000, 3), (1000, 2), (200, 1), (0, 0)]


class TestRiskGovernor(unittest.TestCase):
    def test_tier_ceiling_blocks_tier3_below_1000(self):
        self.assertEqual(tier_index_for_balance(999, CEILING), 1)
        self.assertEqual(tier_index_for_balance(1000, CEILING), 2)

    def test_profit_lock_keeps_operating_reserve(self):
        peak, locked = update_profit_lock_on_peak(
            345.0, 300.0, 0.0, lock_ratio=0.40, min_reserve=80.0
        )
        self.assertEqual(peak, 345.0)
        self.assertAlmostEqual(locked, 18.0, places=2)

    def test_drawdown_triggers_risk_mode(self):
        limits = compute_risk_limits(
            270.0,
            345.0,
            0.0,
            budget_tiers=STANDARD_BUDGET_TIERS,
            ceiling_thresholds=CEILING,
            lock_ratio=0.40,
            min_reserve_usd=80.0,
            drawdown_pct=0.20,
            drawdown_fast_usd=80.0,
            drawdown_fast_minutes=30.0,
            drawdown_window_start_balance=345.0,
            drawdown_window_start_ts=1000.0,
            now_ts=1100.0,
            risk_mode_until_ts=None,
            drawdown_recovery_pct=0.10,
        )
        self.assertFalse(limits["risk_mode"])
        self.assertEqual(limits["max_step_index"], 7)  # 0-based; 8 steps per tier

    def test_tradable_balance_used_for_ceiling(self):
        limits = compute_risk_limits(
            1200.0,
            1200.0,
            600.0,
            budget_tiers=STANDARD_BUDGET_TIERS,
            ceiling_thresholds=CEILING,
            lock_ratio=0.0,
            min_reserve_usd=80.0,
            drawdown_pct=0.99,
            drawdown_fast_usd=9999.0,
            drawdown_fast_minutes=30.0,
            drawdown_window_start_balance=1200.0,
            drawdown_window_start_ts=1000.0,
            now_ts=1001.0,
            risk_mode_until_ts=None,
            drawdown_recovery_pct=0.10,
        )
        self.assertLess(limits["tradable_balance"], 1000.0)
        self.assertEqual(limits["tier_ceiling_index"], 1)


class TestBotRiskIntegration(unittest.TestCase):
    def test_assigned_tier_capped_by_ceiling(self):
        from strategies.double_martingale import DoubleMartingaleBot

        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.strategy_mode = "directional_trend"
        bot.assigned_tier_index = 2
        bot.current_tier_index = 2
        bot.session_round_count = 0
        bot.cumulative_debt = 100.0
        bot._sync_assigned_tier_for_trading(balance=150.0)
        self.assertEqual(bot.assigned_tier_index, 0)
        self.assertEqual(bot.current_tier_index, 0)

    def test_step_score_blocks_weaker_second_step(self):
        from strategies.double_martingale import DoubleMartingaleBot

        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.step_score_escalation_enabled = True
        bot.ladder_pair = "EURUSD-OTC"
        bot.asset = "EURUSD-OTC"
        bot.session_round_count = 1
        bot.ladder_loss_scores = [0.62]
        ok, reason = bot._check_step_score_escalation(0.64)
        self.assertFalse(ok)
        self.assertIn("required", reason)
        ok2, _ = bot._check_step_score_escalation(0.70)
        self.assertTrue(ok2)

    def test_step_score_bypassed_after_pair_switch(self):
        from strategies.double_martingale import DoubleMartingaleBot

        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.ladder_pair = "EURNZD-OTC"
        bot.asset = "XAUUSD-OTC"
        bot.session_round_count = 1
        bot.ladder_loss_scores = [0.62]
        ok, reason = bot._check_step_score_escalation(0.40)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_ladder_step_advances_after_loss_not_rewound_by_sync(self):
        from strategies.double_martingale import DoubleMartingaleBot

        bot = DoubleMartingaleBot(simulation_mode=True)
        bot.strategy_mode = "directional_trend"
        bot.assigned_tier_index = 2
        bot.current_tier_index = 2
        bot.cumulative_debt = 100.0
        bot.session_round_count = 1
        bot.locked_profit = 1000.0
        bot.session_peak_balance = 2900.0
        bot._sync_assigned_tier_for_trading(balance=2900.0)
        self.assertEqual(bot.session_round_count, 1)


if __name__ == "__main__":
    unittest.main()

"""One-off: strip legacy methods from double_martingale.py."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "strategies" / "double_martingale.py"

REMOVE_METHODS = {
    "_get_or_init_asset_state",
    "_select_top_n_assets",
    "_multi_asset_bet_amount",
    "_get_asset_direction",
    "_run_multi_asset_cycle",
    "_compute_crm_tiers",
    "_trigger_crm",
    "_compute_crm_bet",
    "_apply_crm_win",
    "_exit_crm",
    "_sniper_blocked_by_recovery_momentum",
    "_seed_sniper_window_extremes",
    "_sniper_favorable_spot",
    "_wait_for_micro_pullback_entry",
    "run_ai_agent_pipeline",
    "_place_straddle_concurrent",
    "_get_best_strikes",
    "_simulate_round_outcome",
    "_fetch_both_results_concurrent",
}


def remove_class_methods(source: str, names: set[str]) -> str:
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # @staticmethod before method
        if line.strip() == "@staticmethod" and i + 1 < len(lines):
            m = re.match(r"^    def (\w+)\(", lines[i + 1])
            if m and m.group(1) in names:
                i += 2
                while i < len(lines) and not _is_class_level_def(lines[i]):
                    i += 1
                continue

        m = re.match(r"^    def (\w+)\(", line)
        if m and m.group(1) in names:
            i += 1
            while i < len(lines) and not _is_class_level_def(lines[i]):
                i += 1
            continue

        out.append(line)
        i += 1
    return "".join(out)


def _is_class_level_def(line: str) -> bool:
    if re.match(r"^    def ", line):
        return True
    if re.match(r"^class ", line):
        return True
    if line.startswith("def ") and not line.startswith("    "):
        return True
    return False


def main():
    text = TARGET.read_text(encoding="utf-8")

    # Update module docstring
    text = re.sub(
        r'"""[\s\S]*?"""',
        '"""\nCandle Follow directional turbo bot with martingale ladder recovery.\n\n'
        "Each minute: read last closed 1m candle color → place CALL or PUT turbo option.\n"
        "Martingale ladder advances on loss, resets on win; balance-based tier brackets.\n"
        '"""',
        text,
        count=1,
    )

    # Drop legacy imports
    for block in [
        "from ai_assessment import AITradeAssessor\n",
        "from ensemble import (\n    check_enhanced_conviction,\n    check_rule_based_entry_gate,\n    compute_bot_confidence,\n    compute_signal_coherence,\n    resolve_ensemble,\n    should_skip_ai_call,\n)\n",
        "from ai_agents import run_optimization_agents\n",
    ]:
        text = text.replace(block, "")

    text = remove_class_methods(text, REMOVE_METHODS)

    # Multi-asset init block
    text = re.sub(
        r"\n        # Multi-asset simultaneous trading\n"
        r"        self\.multi_asset_mode = .*?\n"
        r"        self\._multi_asset_session_profit: float = 0\.0\n",
        "\n",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"        self\.multi_asset_mode = .*?\n"
        r"        self\.multi_asset_count = .*?\n"
        r"        self\.multi_asset_scale_factors = .*?\n"
        r"        self\.multi_asset_min_score = .*?\n"
        r"        self\.multi_asset_tier_escalate_losses = .*?\n"
        r"        self\.multi_asset_global_stop_loss = .*?\n"
        r"        self\.multi_asset_global_pause_sec = .*?\n"
        r"        self\._multi_asset_states: dict = \{\}\n"
        r"        self\._multi_asset_session_profit: float = 0\.0\n",
        "",
        text,
    )

    # CRM state init in reset helpers
    for pat in [
        r"        self\.crm_mode = False\n        self\.crm_tiers = \[\]\n        self\.crm_tier_index = 0\n        self\.crm_target = 0\.0\n        self\.crm_collected = 0\.0\n",
        r"        self\.crm_mode = bool\(data\.get\(\"crm_mode\", False\)\)\n",
    ]:
        text = re.sub(pat, "", text)

    # CRM block in _maybe_escalate_assigned_tier_after_exhaustion
    text = re.sub(
        r"        # ── CRM mode: advance CRM tier or exit on full exhaustion ────────────────\n"
        r"        if getattr\(self, 'crm_mode', False\):[\s\S]*?            return False\n\n",
        "",
        text,
        count=1,
    )

    # CRM in _apply_win_ladder_rules
    text = re.sub(
        r"        if getattr\(self, 'crm_mode', False\):\n"
        r"            self\._apply_crm_win\(self\.session_profit\)\n"
        r"            return\n\n",
        "",
        text,
    )

    # CRM guards — unwrap single-line early returns
    text = re.sub(
        r"        if getattr\(self, 'crm_mode', False\):\n            return\n",
        "",
        text,
    )
    text = re.sub(
        r"        if getattr\(self, 'crm_mode', False\) and getattr\(self, 'crm_tiers', None\):[\s\S]*?\n        ",
        "        ",
        text,
    )

    # AI assessor init
    text = re.sub(
        r"        # Initialize AI Assessor[\s\S]*?"
        r'                "🤖 AI Assessment DISABLED — rule-based bot confidence gates only"\n'
        r"            \)\n\n",
        "",
        text,
        count=1,
    )

    # multi_asset branch in run loop
    text = re.sub(
        r"                    # ── Multi-asset simultaneous trading branch ───────────────\n"
        r"                    if self\.multi_asset_mode:\n"
        r"                        self\._run_multi_asset_cycle\(\)\n"
        r"                        continue\n\n",
        "",
        text,
    )

    # AI pipeline at end of run startup
    text = re.sub(
        r"        if self\.ai_assessor:[\s\S]*?                    self\.run_ai_agent_pipeline\(\)\n",
        "",
        text,
        count=1,
    )

    # update_config ai_assessor block
    text = re.sub(
        r"            if \"gemini_api_keys\" in new_config or \"ai_enabled\" in new_config:[\s\S]*?"
        r"                self\.ai_assessor = None\n",
        "",
        text,
        count=1,
    )

    TARGET.write_text(text, encoding="utf-8")
    print(f"Cleaned {TARGET} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()

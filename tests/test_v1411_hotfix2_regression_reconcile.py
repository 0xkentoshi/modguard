from pathlib import Path

from app.agent.report_intent import REPORT_INTENT_SYSTEM_PROMPT


def test_report_prompt_keeps_direct_hostility_out_of_report_flow():
    assert "NOT a report merely because it refers to that participant" in REPORT_INTENT_SYSTEM_PROMPT
    assert "я тебя найду и разъебу, это не шутка" in REPORT_INTENT_SYSTEM_PROMPT
    assert "report_target=false" in REPORT_INTENT_SYSTEM_PROMPT


def test_reporter_shield_and_core_truth_contracts_coexist():
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    assert handlers.count("apply_core_guard(") >= 2
    assert handlers.count("core_guard_applied=True") >= 2

    start = handlers.index("# REPORT-FIRST ROUTE")
    end = handlers.index("# Ordinary moderation path.", start)
    branch = handlers[start:end]
    assert "REPORTER SAFE BYPASS" in branch
    assert "apply_core_guard" not in branch
    assert "apply_overlay" not in branch
    assert "process_report_rereview" in branch

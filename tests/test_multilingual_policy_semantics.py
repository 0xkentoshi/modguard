from app.agent.prompts import SYSTEM_PROMPT
from app.community_policy.prompts import (
    POLICY_COMPILER_SYSTEM_PROMPT,
    POLICY_MATCH_SYSTEM_PROMPT,
    POLICY_TRIAGE_SYSTEM_PROMPT,
)


def test_custom_policy_is_cross_language_and_transliteration_aware():
    text = (
        POLICY_COMPILER_SYSTEM_PROMPT
        + POLICY_TRIAGE_SYSTEM_PROMPT
        + POLICY_MATCH_SYSTEM_PROMPT
    ).casefold()

    assert "transliteration" in text
    assert "phonetic" in text
    assert "фак" in text
    assert "fuck" in text
    assert "semantic" in text


def test_all_profanity_custom_rule_is_not_russian_only():
    text = POLICY_MATCH_SYSTEM_PROMPT.casefold()

    assert "all profanity" in text
    assert "languages" in text
    assert "scripts" in text


def test_low_risk_repeated_profanity_prefers_flood_warning():
    text = SYSTEM_PROMPT.casefold()

    assert "repetition / low-risk flood" in text
    assert "category=flood" in text
    assert "action=warn" in text
    assert "not profanity" in text


def test_prompts_require_decimal_confidence():
    text = (
        SYSTEM_PROMPT
        + POLICY_TRIAGE_SYSTEM_PROMPT
        + POLICY_MATCH_SYSTEM_PROMPT
    ).casefold()

    assert "never 100" in text
    assert "0.0 to 1.0" in text

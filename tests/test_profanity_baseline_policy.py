from app.agent.prompts import (
    DEFAULT_COMMUNITY_POLICY,
    SYSTEM_PROMPT,
)
from app.agent.triage_prompts import (
    TRIAGE_SYSTEM_PROMPT,
)
from app.community_policy.core import (
    CORE_DEFAULT_ALLOWED_ITEMS,
)


def test_core_profanity_is_language_neutral_and_allowed_by_default():
    lowered = SYSTEM_PROMPT.casefold()

    assert "non-targeted profanity" in lowered
    assert "same moderation standard across languages" in lowered
    assert '"бля"' in SYSTEM_PROMPT
    assert '"fuck"' in SYSTEM_PROMPT
    assert "do not delete/warn/mute/ban a message only for profanity" in lowered


def test_core_does_not_call_profanity_spam_by_itself():
    lowered = SYSTEM_PROMPT.casefold()

    assert "is not spam" in lowered or "not spam" in lowered
    assert "profanity" in lowered
    assert "category=spam" in lowered


def test_fast_triage_routes_harmless_profanity_as_safe_baseline():
    lowered = TRIAGE_SYSTEM_PROMPT.casefold()

    assert "core-safe profanity" in lowered
    assert '"бля"' in TRIAGE_SYSTEM_PROMPT
    assert '"fucking shit"' in TRIAGE_SYSTEM_PROMPT
    assert "do not route deep merely because" in lowered


def test_fast_triage_requires_compact_json_reasons():
    lowered = TRIAGE_SYSTEM_PROMPT.casefold()

    assert "output compactness" in lowered
    assert "<= 12 words" in TRIAGE_SYSTEM_PROMPT
    assert 'report_reason=""' in TRIAGE_SYSTEM_PROMPT


def test_default_policy_allows_non_targeted_profanity():
    lowered = DEFAULT_COMMUNITY_POLICY.casefold()

    assert "non-targeted profanity" in lowered
    assert "allowed" in lowered
    assert "custom community rules" in lowered


def test_policy_ui_explains_what_admin_can_make_stricter():
    joined = " ".join(
        CORE_DEFAULT_ALLOWED_ITEMS
    ).casefold()

    assert "profanity" in joined
    assert "custom" not in joined

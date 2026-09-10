from app.agent.prompts import SYSTEM_PROMPT
from app.agent.report_prompts import REPORT_REVIEW_SYSTEM_PROMPT


def _flat(text: str) -> str:
    return " ".join(text.casefold().split())


def test_exact_qa_aggression_is_not_silently_allowed():
    text = _flat(SYSTEM_PROMPT)
    assert "ты реально тупой, задолбал уже" in text
    assert "clear targeted harassment" in text
    assert "warn on a first/unknown offense" in text


def test_prompt_keeps_spam_light_and_scam_heavy():
    text = _flat(SYSTEM_PROMPT)
    assert "configurable light lane" in text
    assert "ordinary spam/flood" in text
    assert "protected heavy lane" in text
    assert "phishing / credential theft" in text


def test_reported_fight_can_escalate_with_conversation_context():
    text = _flat(REPORT_REVIEW_SYSTEM_PROMPT)
    assert "fight / harassment reports" in text
    assert "who started it" in text
    assert "conversation context" in text

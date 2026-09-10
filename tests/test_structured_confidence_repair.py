from app.agent.schemas import (
    ModerationDecision,
    TriageDecision,
)
from app.llm.ollama import (
    _normalize_confidence_value,
    _repair_structured_payload,
)


def test_confidence_100_is_repaired_to_one():
    assert _normalize_confidence_value(100) == 1.0
    assert _normalize_confidence_value(95) == 0.95
    assert _normalize_confidence_value("95%") == 0.95


def test_valid_fraction_confidence_is_untouched():
    assert _normalize_confidence_value(0.95) == 0.95
    assert _normalize_confidence_value(1.0) == 1.0


def test_deep_safe_payload_from_live_log_becomes_valid():
    raw = {
        "detected_language": "English",
        "current_message_violation": False,
        "category": "safe",
        "severity": "low",
        "confidence": 100,
        "action": "allow",
        "reason": "Ненаправленная ругань без нарушения.",
        "current_message_evidence": ["fucking shit"],
        "report_target": False,
        "report_reason": "",
    }

    repaired, changed = _repair_structured_payload(raw)

    assert changed is True
    assert repaired["confidence"] == 1.0

    decision = ModerationDecision.model_validate(
        repaired
    )

    assert decision.action == "allow"
    assert decision.confidence == 1.0


def test_triage_percentage_confidence_is_repaired():
    raw = {
        "route": "safe",
        "confidence": 95,
        "reason": "Harmless casual profanity.",
        "report_target": False,
        "report_confidence": 0,
        "report_reason": "",
    }

    repaired, changed = _repair_structured_payload(raw)

    assert changed is True

    triage = TriageDecision.model_validate(
        repaired
    )

    assert triage.confidence == 0.95

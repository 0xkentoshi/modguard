from pathlib import Path

from app.community_policy.core import PROTECTED_CORE_CATEGORIES
from app.community_policy.schemas import CommunityPolicyRule


def test_protected_core_categories_are_narrow_and_non_configurable():
    assert {"scam","phishing","malicious_link","threat"}.issubset(PROTECTED_CORE_CATEGORIES)
    assert "spam" not in PROTECTED_CORE_CATEGORIES
    assert "harassment" not in PROTECTED_CORE_CATEGORIES


def test_policy_schema_supports_light_medium_heavy_tiers():
    rule=CommunityPolicyRule(
        title="Spam", condition="ordinary spam", action="warn",
        enforcement_tier="light",
    )
    assert rule.enforcement_tier == "light"


def test_policy_compiler_documents_configurable_relaxation():
    source=Path("app/community_policy/prompts.py").read_text(encoding="utf-8").casefold()
    assert "allow ordinary spam" in source
    assert "protected core" in source
    assert "light" in source and "medium" in source and "heavy" in source


def test_policy_ui_explains_protected_and_configurable_layers():
    source=Path("app/admin/dashboard.py").read_text(encoding="utf-8")
    assert "PROTECTED CORE · CANNOT BE WEAKENED" in source
    assert "DEFAULT ENFORCEMENT · CUSTOMIZABLE" in source

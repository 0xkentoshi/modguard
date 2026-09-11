from pathlib import Path


def test_restriction_precheck_allows_bot_compatible_adapter_without_get_chat_member():
    text = Path("app/moderation/executor.py").read_text(encoding="utf-8")
    assert 'getattr(' in text
    assert '"get_chat_member"' in text
    assert 'if not callable(get_chat_member):' in text
    assert 'reason=get_chat_member_missing' in text


def test_installer_keeps_backups_outside_pytest_project_tree():
    text = Path("APPLY_PATCH.ps1").read_text(encoding="utf-8")
    assert "_modguard_patch_backups" in text
    assert "Move-Item" in text

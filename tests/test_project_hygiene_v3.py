from pathlib import Path


def test_pytest_is_scoped_to_canonical_tests_directory():
    text = Path("pytest.ini").read_text(encoding="utf-8").casefold()
    assert "testpaths = tests" in text
    assert "_backup_*" in text


def test_no_old_stabilization_backup_remains_in_project_root():
    backups = list(Path(".").glob("_backup_stabilization_v3_*"))
    assert backups == []

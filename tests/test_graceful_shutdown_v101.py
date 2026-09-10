from pathlib import Path


def test_module_entrypoint_catches_keyboard_interrupt():
    source = Path("app/main.py").read_text(encoding="utf-8")
    runner = Path("run.py").read_text(encoding="utf-8")
    assert "def run_main()" in source
    assert "except KeyboardInterrupt:" in source
    assert 'print("\\nModGuard stopped.")' in source
    assert "run_main()" in runner


def test_shutdown_closes_components_independently():
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert "_close_component_quietly" in source
    assert "except asyncio.CancelledError:" in source
    assert '"Telegram session"' in source
    assert '"Semantic cluster service"' in source
    assert '"Database"' in source

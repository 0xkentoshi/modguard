from pathlib import Path


def test_semantic_similarity_is_observational_not_per_message_enforcement():
    handlers = Path('app/bot/handlers.py').read_text(encoding='utf-8')
    service = Path('app/semantic_clustering/service.py').read_text(encoding='utf-8')

    assert 'semantic_force_deep' not in handlers
    assert 'SEMANTIC ROUTE | deep' not in handlers
    assert 'moderator_agent.analyze(context)' in handlers
    assert 'return False' in service.split('def should_force_deep', 1)[1].split('def enqueue', 1)[0]


def test_current_message_contamination_guard_is_explicit():
    prompt = Path('app/agent/prompts.py').read_text(encoding='utf-8')

    assert 'CURRENT-MESSAGE CONTAMINATION CHECK' in prompt
    assert 'Кто вечером будет играть?' in prompt
    assert 'Similarity to previous harmful messages is never proof of guilt.' in prompt


def test_truncated_structured_json_gets_one_bounded_retry():
    source = Path('app/llm/ollama.py').read_text(encoding='utf-8')

    assert 'json_decode_failed' in source
    assert 'STRUCTURED RETRY RECOVERED' in source
    assert 'int(self.num_predict) * 2' in source
    assert '640' in source


def test_autoban_off_uses_temporary_mute_fallback():
    source = Path('app/moderation/executor.py').read_text(encoding='utf-8')

    assert 'policy.final_action == "ban"' in source
    assert 'and not live_ban_enabled' in source
    assert 'LIVE BAN FALLBACK | MUTED' in source
    assert 'MUTE + DELETE · Auto-ban OFF' in source


def test_slow_admin_ticket_callback_cannot_crash_on_expired_query():
    source = Path('app/bot/admin_handlers.py').read_text(encoding='utf-8')

    assert 'safe_callback_answer' in source
    assert 'query is too old' in source
    assert 'response timeout expired' in source
    assert 'await safe_callback_answer(callback, "Processing…")' in source

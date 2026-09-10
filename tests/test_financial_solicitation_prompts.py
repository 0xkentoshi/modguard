from app.agent.prompts import SYSTEM_PROMPT
from app.agent.report_prompts import REPORT_REVIEW_SYSTEM_PROMPT
from app.agent.destructive_recheck_prompts import DESTRUCTIVE_OTHER_RECHECK_SYSTEM_PROMPT

def test_financial_dm_lure_is_heavy_scam_path():
    text=SYSTEM_PROMPT.casefold()
    assert "private investment group with good profit opportunities" in text
    assert "category=scam" in text
    assert "action=ban" in text

def test_seed_phrase_example_is_phishing_not_spam():
    text=SYSTEM_PROMPT.casefold()
    assert "seed phrase" in text and "must be phishing" in text

def test_report_rereview_financial_lure_is_not_harmless_ambiguity():
    text=REPORT_REVIEW_SYSTEM_PROMPT.casefold()
    assert "category=scam" in text and "action=ban" in text

def test_destructive_recheck_prefers_scam_for_private_profit_lure():
    text=DESTRUCTIVE_OTHER_RECHECK_SYSTEM_PROMPT.casefold()
    assert "prefer category=scam" in text and "action=ban" in text

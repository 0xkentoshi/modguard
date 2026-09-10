PROTECTED_CORE_CATEGORIES = {
    "scam",
    "phishing",
    "malicious_link",
    "threat",
}

PROTECTED_CORE_POLICY_ITEMS = [
    "Confirmed scam/fraud, fake rewards, and deceptive money/wallet schemes",
    "Phishing, credential theft, seed-phrase/account takeover attempts",
    "Malicious links and clearly dangerous redirects",
    "Credible direct physical threats",
]

# Kept as a compatibility export for existing UI/tests.
CORE_POLICY_ITEMS = PROTECTED_CORE_POLICY_ITEMS

DEFAULT_ENFORCEMENT_TIER_ITEMS = [
    "LIGHT · ordinary spam/flood and clearly excessive aggression → WARN → MUTE + DELETE → BAN + DELETE",
    "MEDIUM · clear unsolicited DM/promo solicitation and similar intrusive behavior → MUTE + DELETE → BAN + DELETE",
    "HEAVY · scam/phishing/malicious links/credible threats → BAN + DELETE immediately when confidence is high",
]

CORE_DEFAULT_ALLOWED_ITEMS = [
    "Casual profanity or emotional swearing without targeted abuse or threats",
    "Ordinary disagreement and heated discussion that is not clearly excessive harassment",
    "Ordinary links when they are not malicious and do not violate another protected rule",
    "Jokes, memes, slang, and friendly banter without an independent violation",
]

CORE_POLICY_NOTE = (
    "Protected Core threats cannot be weakened by custom rules. Community-level "
    "behavior such as ordinary spam, flood, harassment/aggression, advertising, "
    "and DM solicitation is configurable per chat. A custom policy may relax, "
    "strengthen, or change those community rules. Shadow and Auto-ban still "
    "control whether destructive actions are actually executed."
)

CORE_POLICY_PROMPT = "\n".join(
    f"- {item}"
    for item in PROTECTED_CORE_POLICY_ITEMS
)

DEFAULT_ENFORCEMENT_PROMPT = "\n".join(
    f"- {item}"
    for item in DEFAULT_ENFORCEMENT_TIER_ITEMS
)

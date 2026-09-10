# Changelog

All notable portfolio-release changes are documented here.

## [1.0.0] - 2026-09-10

### Added

- Fast / Deep local-LLM moderation pipeline
- structured moderation decisions
- deterministic PolicyGate
- real Telegram Warn / Delete / Mute / Ban / Unban actions
- Protected Core for high-risk security violations
- progressive spam and flood enforcement
- conservative human-conflict escalation
- report-intent preflight and target re-review
- moderator review tickets with conversation context
- natural-language per-community policies
- Shadow Mode and Auto-ban safety controls
- configurable per-chat mute duration
- moderator feedback memory
- known malicious-pattern memory
- semantic clustering with local embeddings
- Raid Guard for confirmed hostile campaigns
- multi-community dashboard and settings isolation
- ban pagination, search and unban
- Telegram basic-group → supergroup migration reconciliation
- Diagnostics for core dependencies
- Test Mode for safe moderation simulations
- 200+ automated regression tests

### Safety hardening

- harmless current messages cannot be punished solely because of prior history
- semantic similarity is observational and cannot independently punish a message
- ambiguous interpersonal fights are escalated to humans
- Community Policy cannot weaken protected scam/phishing safety
- destructive actions are guarded by Shadow Mode and Auto-ban controls

### Notes

`v1.0.0` is the frozen moderation baseline for the portfolio release.

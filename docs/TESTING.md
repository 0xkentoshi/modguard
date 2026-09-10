# Testing

ModGuard was developed with regression-driven stabilization.

The suite contains **200+ automated tests** covering the failure modes that matter most when an AI system is allowed to take real moderation actions.

## Run

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -v
```

## Major test areas

- false-positive containment
- current-message evidence vs history
- Fast / Deep routing
- structured-output repair
- scam and phishing enforcement
- spam / flood progression
- contextual harassment and human review
- report intent and target re-review
- Community Policy isolation
- moderator feedback memory
- Shadow Mode and Auto-ban
- live delete / mute / ban behavior
- ban search and unban
- semantic clustering
- Raid Guard
- edited messages and media captions
- Telegram reconnect/startup resilience
- group → supergroup migration handling
- multi-community settings isolation
- Test Mode safety
- dashboard and admin callbacks

## Testing philosophy

A bug discovered in live QA should become a regression test before or alongside the fix.

The goal is not to prove that an LLM is always right. The goal is to prove that the surrounding system contains predictable model mistakes and prevents known unsafe execution paths from returning.

## Live QA

Automated tests are necessary but not sufficient for Telegram integrations. Before enabling live moderation in a new environment:

1. start in `DRY_RUN=true` / Shadow Mode;
2. verify bot permissions;
3. test safe messages and harmless profanity;
4. test review-ticket creation;
5. test mute / ban / unban with a disposable account;
6. verify per-chat settings isolation;
7. verify Diagnostics;
8. only then enable destructive actions.

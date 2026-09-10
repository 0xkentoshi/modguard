# Security

## Secrets

Never commit:

- `.env`
- Telegram bot tokens
- private administrator IDs unless intentionally public
- SQLite databases
- moderation logs
- exported chat histories
- customer data

The repository includes `.env.example` with empty secret fields.

## Recommended rollout

For a new community, begin with:

```env
DRY_RUN=true
LIVE_DELETE_ENABLED=false
```

Use Shadow Mode and Test Mode before enabling destructive moderation.

## Telegram permissions

Grant only the permissions required for the desired features. Delete, mute and ban actions require corresponding administrator permissions in the target group.

## Local LLM availability

If local inference is unavailable, destructive decisions should not be guessed from incomplete model output. Diagnostics can be used to verify Telegram, database, Ollama and embedding availability.

## Reporting

This is a portfolio release. If you discover a security issue, open a GitHub issue without posting tokens, personal data, chat logs or other secrets.

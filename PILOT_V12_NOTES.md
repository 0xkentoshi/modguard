# ModGuard Pilot v1.2 — Safety & Reputation Update

This update hardens ModGuard for a live pilot without changing the core moderation architecture.

## Automatic safety circuit

ModGuard now fails closed **per community**. If ordinary autonomous moderation begins behaving abnormally, the affected chat is forced into persistent `SHADOW` mode and destructive actions stop until a human reviews the incident.

Default trip conditions are intentionally conservative and configurable through `.env`:

- 5 mute/ban actions within 60 seconds;
- 20 destructive actions (`delete`, `mute`, `ban`) within 60 seconds;
- 3 Telegram execution failures within 120 seconds;
- 3 unhandled moderation-pipeline failures within 120 seconds.

A trip refreshes the dashboard and sends an operational alert. There is **no automatic re-enable**. A moderator must inspect the community and manually disable `SHADOW`.

Raid Guard remains a separate explicit campaign subsystem, so a confirmed raid is not mistaken for an ordinary runaway moderation loop.

## LIGHT reputation decay

Minor moderation reputation now has a configurable memory window per community.

Default: **6 hours**.

The window applies only to LIGHT escalation history for:

- spam;
- flood;
- targeted harassment.

After the window expires, an old minor warning/mute no longer escalates a fresh incident days later. The next LIGHT incident starts from the first ladder step again. MEDIUM/HEAVY safety history is intentionally retained.

Available UI presets: `1h`, `3h`, `6h`, `12h`, `1d`, `3d`, `7d`, `Never expire`.

## Cleaner moderation UI

The community settings screen now contains only everyday controls:

- Shadow;
- Auto-ban;
- Mute duration;
- LIGHT memory;
- Raid Guard;
- Community Policy.

Operational and investigative controls moved into **Safety & tools**:

- Safety policy;
- Diagnostics;
- Banned users;
- Immunity list;
- Shadow-alert cleanup;
- latest Raid Guard incident when available.

## Public vs private code

Public agent improvements are safe to keep in the GitHub version: safety circuit, reputation decay, fake-admin protection, immunity list and compact community controls.

The owner-only rental/platform control plane remains isolated under `private_ops/` and is intentionally ignored by Git.

## Validation

Run the public regression suite with:

```powershell
python -m pytest -q
```

Run ModGuard from the project root with:

```powershell
python -m app.main
```

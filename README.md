<div align="center">

# 🛡️ ModGuard

### Local-first AI moderation agent for Telegram communities

![Version](https://img.shields.io/badge/version-1.2.0-111111)
![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-aiogram-26A5E4?logo=telegram&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Ollama-black)
![Tests](https://img.shields.io/badge/regression_tests-255-success)

**Real moderation actions · Local LLMs · Deterministic safety · Human review**

</div>

**ModGuard** is a production-oriented moderation agent that combines local LLM reasoning with deterministic safety controls. It analyzes Telegram messages, executes real moderation actions, detects coordinated campaigns, and escalates ambiguous human conflicts to a moderator instead of making unsafe guesses.

> **LLM reasoning → structured decision → deterministic policy → controlled execution**

ModGuard is not a keyword blacklist and not a chatbot that only gives advice. The model interprets behavior; deterministic code decides what is safe to execute.

---

## Product Tour

<table>
<tr>
<td width="50%" valign="top">

### Control Center

Per-community dashboard with live statistics, tickets, settings, Test Mode and multi-chat management.

<img src="assets/screenshots/01-dashboard.png" alt="ModGuard admin dashboard" width="100%">

</td>
<td width="50%" valign="top">

### Ban Management

Real ban state with newest-first ordering, search and one-click unban.

<img src="assets/screenshots/02-ban-management.png" alt="ModGuard banned users management" width="100%">

</td>
</tr>
<tr>
<td width="50%" valign="top">

### Human Review

Ambiguous interpersonal conflicts become review tickets with the surrounding conversation instead of blind autonomous punishment.

<img src="assets/screenshots/03-human-review-ticket.png" alt="ModGuard human review ticket with conversation context" width="100%">

</td>
<td width="50%" valign="top">

### Natural-language Policy

Administrators describe community rules in normal language; ModGuard compiles them into structured enforcement rules.

<img src="assets/screenshots/04-community-policy.png" alt="ModGuard natural-language community policy preview" width="100%">

</td>
</tr>
<tr>
<td width="50%" valign="top">

### System Diagnostics

Telegram, permissions, database, Ollama, Fast/Deep LLMs, embeddings and chat-registry health are checked from the admin interface.

<img src="assets/screenshots/05-diagnostics.png" alt="ModGuard system diagnostics" width="100%">

</td>
<td width="50%" valign="top">

### Safe Test Mode

Critical actions can be simulated before enabling them in a real community.

<img src="assets/screenshots/06-test-mode.png" alt="ModGuard safe test mode" width="100%">

</td>
</tr>
</table>

### Real warning enforcement

For a clear first-time targeted aggression case, ModGuard can issue a visible warning while leaving the triggering message in place. Repeated or ambiguous conflict is handled more conservatively and can be escalated to human review.

<img src="assets/screenshots/07-real-warning.png" alt="ModGuard real warning for targeted aggression" width="760">

---

## Live Demos

The demos below autoplay and loop directly inside the README.

### Project Overview
![Project Overview](assets/demo/gifs/01-project-overview.gif)

Architecture, safety model, product tour and regression coverage.

### Control Center
![Control Center](assets/demo/gifs/02-control-center.gif)

Per-community dashboard and moderation controls.

### Live Enforcement
![Live Enforcement](assets/demo/gifs/03-live-enforcement.gif)

Real moderation actions and ban-management workflow.

### Human Review
![Human Review](assets/demo/gifs/04-human-review.gif)

Ambiguous conflict escalates with conversation context.

### Community Policy
![Community Policy](assets/demo/gifs/05-community-policy.gif)

Natural-language community rules compiled into structured enforcement.

### System Diagnostics
![System Diagnostics](assets/demo/gifs/06-system-diagnostics.gif)

Telegram, database, Ollama, LLM and embedding health checks.

### Safe Test Mode
![Safe Test Mode](assets/demo/gifs/07-safe-test-mode.gif)

Safe simulations for critical moderation actions.


---

## Highlights

- **Fast → Deep AI routing** using separate local Qwen models
- **Real moderation actions:** Warn, Delete, Mute, Ban, Unban
- **Protected Core** for high-confidence scam, phishing, malicious links and credible threats
- **Progressive enforcement** for spam and flood
- **Human-review tickets** for ambiguous fights and contextual harassment
- **Natural-language Community Policy** with per-chat rules
- **Shadow Mode** for safe dry evaluation before live enforcement
- **Auto-ban kill switch** with temporary-mute fallback
- **Immunity List** for trusted bots and service accounts by Telegram username or user ID
- **Fake Admin / Moderator protection** using real Telegram roles, identity similarity and dangerous-behavior signals
- **Automatic Safety Circuit Breaker** that forces the affected community into Shadow Mode on abnormal destructive activity or repeated execution failures
- **Time-aware reputation decay** for minor spam, flood and harassment offenses with a configurable window
- **Semantic campaign detection** using local embeddings
- **Raid Guard** for confirmed multi-user hostile campaigns
- **Moderator Feedback Memory** for chat-scoped gray-area decisions
- **Known Pattern Memory** for repeated confirmed malicious payloads
- **Multi-community isolation** with independent settings and policies
- **Telegram group → supergroup migration recovery**
- **Stale-chat lifecycle cleanup** when the bot leaves or is removed
- **Per-user multi-party conflict state** for fair first-offense handling
- **Graceful Ctrl+C shutdown** with isolated resource cleanup
- **Ban registry** with newest-first pagination, search and unban
- **Diagnostics** for Telegram, database, Ollama, Fast/Deep LLMs and embeddings
- **255 automated regression tests**

---

## Architecture

```mermaid
flowchart TD
    TG[Telegram message / edit / caption] --> CTX[Context Builder]
    CTX --> FAST[Fast AI · qwen3:1.7b]
    FAST -->|clearly safe| CHALLENGE[Independent Safe Challenge]
    FAST -->|suspicious / uncertain| DEEP[Deep AI · qwen3:8b]
    CHALLENGE -->|safe| POLICY[Deterministic PolicyGate]
    CHALLENGE -->|needs deeper review| DEEP
    DEEP --> POLICY

    POLICY --> EXEC[Moderation Executor]
    EXEC --> ALLOW[Allow]
    EXEC --> WARN[Warn]
    EXEC --> DELETE[Delete]
    EXEC --> MUTE[Mute]
    EXEC --> BAN[Ban]
    EXEC --> REVIEW[Human Review Ticket]

    REVIEW --> FEEDBACK[Moderator Feedback Memory]

    EXEC --> AUDIT[(SQLite Audit / State)]
    AUDIT --> DASH[Admin Control Center]

    TG --> SEM[Semantic Clustering]
    SEM --> RAID[Raid Guard]
    RAID --> POLICY

    COMMUNITY[Natural-language Community Policy] --> POLICY
```

A deeper architecture breakdown is available in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Safety Model

The LLM does **not** directly call Telegram moderation methods.

Every model decision is converted into structured data and validated by deterministic code before anything destructive can happen.

### Protected Core

High-confidence security violations can be handled autonomously:

- phishing / credential theft
- confirmed scam or fraud
- malicious links
- credible direct threats
- obvious coordinated spam campaigns

### Contextual behavior

Human conflict is intentionally more conservative:

- first clear targeted aggression → warning
- repeated or ambiguous conflict → moderator review
- unclear multi-party fights → moderator review
- moderator sees conversation context before deciding

### Additional safety controls

- **Shadow Mode:** observe decisions without destructive actions
- **Auto-ban OFF:** a BAN verdict falls back to temporary mute + delete
- **Current-message evidence:** history cannot make a harmless current message guilty
- **Semantic clustering is observational:** similarity alone cannot punish a message
- **Per-chat memory:** one community's behavioral history does not become another community's reputation
- **Immunity List:** trusted bots and service accounts can bypass moderation entirely
- **Fake Admin / Moderator detection:** suspicious impersonation is combined with dangerous behavior before enforcement; a suspicious display name alone is not enough
- **Safety Circuit Breaker:** abnormal destructive-action bursts or repeated execution failures automatically force only the affected community into Shadow Mode
- **Minor-offense decay:** lightweight spam, flood and harassment history expires after a configurable period instead of escalating forever

---

## Admin Control Center

ModGuard exposes an inline Telegram admin interface for each managed community:

- Dashboard and 24h activity
- Open review tickets
- Shadow / Live mode
- Auto-ban switch
- Mute duration
- Safety Policy
- Safety & Tools section
- Immunity List management
- Community Policy
- Raid Guard
- Ban search / pagination / unban
- Test Mode
- Diagnostics
- Multi-chat switching

Settings are isolated per Telegram community.

---

## Local AI Stack

| Role | Default model |
|---|---|
| Fast semantic triage | `qwen3:1.7b` |
| Deep moderation reasoning | `qwen3:8b` |
| Semantic embeddings | `qwen3-embedding:0.6b` |

All inference is performed through a local Ollama instance.

---

## Quick Start

### 1. Install and prepare Ollama

```bash
ollama pull qwen3:1.7b
ollama pull qwen3:8b
ollama pull qwen3-embedding:0.6b
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Configure ModGuard

Copy the example configuration:

```powershell
Copy-Item .env.example .env
```

Set at minimum:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
ADMIN_IDS=your_telegram_user_id
```

Safe defaults ship with:

```env
DRY_RUN=true
LIVE_DELETE_ENABLED=false
```

Start in Shadow / dry-run mode before enabling live moderation.

### 4. Give the bot Telegram permissions

For full functionality the bot should be a group administrator with permission to:

- delete messages
- restrict members
- ban users

### 5. Run

```powershell
python run.py
```

`run.py` is the recommended launcher and exits cleanly on `Ctrl+C`.

---

## Testing

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run the regression suite:

```powershell
python -m pytest -v
```

The suite covers policy boundaries, false-positive guards, report re-review, community policies, moderation actions, semantic clustering, Raid Guard, feedback memory, chat migration, Test Mode, admin controls, immunity rules, fake-admin protection, safety-circuit behavior and reputation decay.

See [`docs/TESTING.md`](docs/TESTING.md) for the testing philosophy.

---

## Project Structure

```text
modguard/
├── app/
│   ├── admin/                  # Dashboard, settings, tickets, Test Mode
│   ├── agent/                  # Fast/Deep prompts, routing, structured decisions
│   ├── bot/                    # Telegram handlers
│   ├── community_policy/       # Natural-language per-chat policy overlay
│   ├── database/               # Async SQLite persistence
│   ├── feedback/               # Moderator feedback memory
│   ├── llm/                    # Ollama structured-output client
│   ├── moderation/             # PolicyGate and deterministic executor
│   ├── raid_guard/             # Multi-user campaign protection
│   ├── semantic_clustering/    # Embeddings and campaign observations
│   └── utils/                  # Caches, normalization, locks, throttling
├── docs/
│   ├── ARCHITECTURE.md
│   └── TESTING.md
├── tests/
├── .env.example
├── requirements.txt
├── requirements-dev.txt
└── run.py
```

---

## Engineering Focus

This project was built as a portfolio case study in practical AI automation. The interesting part is not just classification accuracy; it is designing a system where probabilistic model output can safely drive real-world actions.

Key engineering problems addressed:

- structured LLM output and repair
- fast/slow model routing
- false-positive containment
- current-message vs historical-context separation
- deterministic enforcement policy
- human-in-the-loop escalation
- async Telegram actions
- per-community state isolation
- semantic campaign detection without semantic overreach
- rollback-friendly ban management
- automated circuit breaking for abnormal moderation behavior
- time-aware escalation and reputation decay
- identity-aware fake-admin / fake-moderator detection
- regression testing for previously observed failures

---

## Releases

### v1.2.0 — Safety & Identity Protection

Current public release focused on real-world pilot safety:

- Immunity List for trusted bots and service accounts
- Fake Admin / Fake Moderator detection
- Automatic per-community Safety Circuit Breaker
- Configurable reputation decay for minor offenses
- Cleaner separation between everyday Settings and Safety & Tools
- Expanded public regression coverage: **255 tests**

### v1.0.1 — Portfolio Release

Established the tested moderation baseline with Fast → Deep local LLM routing, real Telegram enforcement, Human Review, Community Policy, Raid Guard, semantic campaign detection, diagnostics and multi-community controls.

The public repository contains the moderation agent itself. Private commercial operations tooling is intentionally kept outside the public codebase.

See [`CHANGELOG.md`](CHANGELOG.md) for the detailed history.

---

## Security

Never commit a real `.env`, Telegram token, database, moderation logs or customer chat data.

See [`SECURITY.md`](SECURITY.md).

---

## Author

Built by **0xkentoshi** as part of an AI automation portfolio.

Open to opportunities in **AI automation, AI agents, Python automation and workflow automation**.

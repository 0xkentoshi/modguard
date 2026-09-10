# Architecture

ModGuard uses a layered agent architecture designed around one constraint: **probabilistic model output must never be equivalent to direct execution authority**.

## Request path

```text
Telegram update
   ↓
Context Builder
   ├─ current message
   ├─ recent chat context
   ├─ recent user history
   └─ confirmed moderation history
   ↓
Fast AI
   ├─ clearly safe → Safe Challenge
   └─ suspicious / uncertain → Deep AI
                               ↓
                         Structured Decision
                               ↓
                         Deterministic PolicyGate
                               ↓
                         Moderation Executor
                         ├─ Allow
                         ├─ Warn
                         ├─ Delete
                         ├─ Mute
                         ├─ Ban
                         └─ Escalate
                               ↓
                            Telegram API
                               ↓
                          Audit / State DB
```

## Core components

### `app/agent`

Owns semantic reasoning and structured LLM decisions.

The fast model is optimized for inexpensive routing. The deep model handles ambiguous or high-risk content. Separate report-intent and re-review paths protect reporters from being mistaken for the violation they are describing.

### `app/moderation`

Owns deterministic safety policy and real Telegram actions.

This layer can downgrade, block or redirect an LLM recommendation. The executor is responsible for warnings, deletions, restrictions, bans, unbans and audit state.

### `app/community_policy`

Compiles an administrator's natural-language community policy into structured rules.

Custom rules can tune community behavior, but protected security categories cannot be disabled by an ordinary community rule.

### `app/feedback`

Stores chat-scoped examples from real moderator ticket resolutions and may use them for future gray-area cases.

Clear security decisions bypass feedback memory.

### `app/semantic_clustering`

Computes local embeddings and observes semantically similar message groups.

A crucial invariant is that **semantic similarity alone cannot punish an individual message**.

### `app/raid_guard`

Looks for confirmed multi-user hostile campaigns. It requires multiple independent hard-risk signals before campaign enforcement is considered.

### `app/admin`

Provides the Telegram control plane: dashboard, settings, tickets, activity, Test Mode, ban management and diagnostics.

## Important invariants

1. **Current message first.** Historical context cannot manufacture evidence that is absent from the current message.
2. **LLM ≠ executor.** A model's action suggestion must pass deterministic policy.
3. **Human conflict is conservative.** Ambiguous interpersonal disputes are escalated instead of blindly punished.
4. **Protected Core stays protected.** Community customization cannot turn clear phishing or scam into an allowed message.
5. **Semantic signals are supporting evidence.** They do not override a confident safe current-message verdict.
6. **State is chat-scoped.** A user's moderation history in one community is not automatically treated as reputation in another.
7. **Shadow blocks destructive execution.** It is the recommended evaluation mode for a new community.
8. **Auto-ban is a kill switch.** When disabled, a heavy BAN verdict falls back to a temporary restriction plus message cleanup.

## Persistence

The portfolio release uses async SQLAlchemy with SQLite.

Stored state includes moderation history, dashboard settings, community policies, review tickets, feedback examples, semantic observations, Raid Guard incidents, known malicious patterns and ban-management state.

## Local inference

The default stack is intentionally local-first:

```text
qwen3:1.7b              Fast triage
qwen3:8b                Deep moderation reasoning
qwen3-embedding:0.6b    Semantic embeddings
```

Ollama is accessed through HTTP, allowing the model layer to remain separate from Telegram and persistence logic.

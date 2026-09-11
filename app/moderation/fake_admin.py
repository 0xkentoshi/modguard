from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.agent.schemas import ModerationDecision, PolicyEvaluation


ROLE_MARKERS = {
    "admin",
    "administrator",
    "moderator",
    "mod",
    "support",
    "official",
    "админ",
    "администратор",
    "модератор",
    "поддержка",
    "саппорт",
}

DANGER_PATTERNS = (
    re.compile(r"https?://|www\.|t\.me/|(?:^|\s)[a-z0-9][a-z0-9.-]+\.(?:com|net|org|io|xyz|ru|app|finance)(?:/|\s|$)", re.I),
    re.compile(r"\b(?:dm|pm)\b|пиши(?:те)?\s+(?:в\s+)?лс|личн(?:ые|ку|ые сообщения)|direct message", re.I),
    re.compile(r"wallet|кошел|seed phrase|сид[- ]?фраз|recovery phrase|airdrop|connect wallet|подключи(?:те)?\s+кошел", re.I),
    re.compile(r"инвест|доход|прибыл|заработ|%\s*(?:в\s*)?(?:день|daily)|giveaway|розыгрыш|claim|bonus|бонус", re.I),
    re.compile(r"verify|verification|вериф|срочно|urgent|немедленно|подтверд(?:ить|ите)|reconnect", re.I),
)


def _norm(value: str | None) -> str:
    value = (value or "").casefold().strip()
    return "".join(ch for ch in value if ch.isalnum())


def _tokenize(value: str | None) -> set[str]:
    return {
        item
        for item in re.split(r"[^\wа-яё]+", (value or "").casefold())
        if item
    }


def _has_role_marker(value: str | None) -> bool:
    text = (value or "").casefold()
    tokens = _tokenize(text)
    if tokens.intersection(ROLE_MARKERS):
        return True
    # Product/community accounts often use compact names such as CryptoAdmin
    # or OfficialSupport. Treat long authority words as substrings, but keep
    # short "mod" token-only to avoid words such as "model".
    substring_markers = (
        "admin", "administrator", "moderator", "support", "official",
        "админ", "администратор", "модератор", "поддержка", "саппорт",
    )
    return any(marker in text for marker in substring_markers)


@dataclass(frozen=True)
class AdminIdentity:
    user_id: int
    username: str | None
    full_name: str


@dataclass(frozen=True)
class FakeAdminFinding:
    confidence: float
    reason: str
    evidence: list[str]
    matched_admin: str | None = None

    def decision(self) -> ModerationDecision:
        return ModerationDecision(
            detected_language="auto",
            current_message_violation=True,
            category="impersonation",
            severity="high",
            confidence=self.confidence,
            action="ban",
            delete_message=True,
            mute_minutes=None,
            needs_human_review=False,
            conflict_context="not_applicable",
            reason=self.reason,
            current_message_evidence=self.evidence[:5],
            context_evidence=[],
            report_target=False,
            report_confidence=0.0,
            report_reason="",
        )

    @staticmethod
    def policy() -> PolicyEvaluation:
        return PolicyEvaluation(
            original_action="ban",
            final_action="ban",
            final_delete_message=True,
            final_mute_minutes=None,
            autonomous=True,
            requires_human_review=False,
            policy_reason=(
                "Verified fake-admin/fake-moderator pattern with dangerous current-message signals. "
                "Permanent BAN still respects the per-chat Auto-ban kill switch in the executor."
            ),
            source="core",
        )


class FakeAdminDetector:
    """
    Conservative deterministic fake-admin detector.

    It only fires when BOTH are true:
      1. the sender impersonates an actual chat admin/moderator OR uses an
         explicit authority marker such as Admin/Moderator/Support; and
      2. the current message contains dangerous operational/scam signals.

    Real Telegram administrators are always excluded by Telegram user ID.
    """

    def __init__(self, *, bot, cache_ttl_seconds: int = 300):
        self.bot = bot
        self.cache_ttl_seconds = max(30, int(cache_ttl_seconds))
        self._cache: dict[int, tuple[float, list[AdminIdentity]]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _admins(self, chat_id: int) -> list[AdminIdentity]:
        now = time.monotonic()
        cached = self._cache.get(int(chat_id))
        if cached and cached[0] > now:
            return cached[1]

        lock = self._locks.setdefault(int(chat_id), asyncio.Lock())
        async with lock:
            cached = self._cache.get(int(chat_id))
            if cached and cached[0] > time.monotonic():
                return cached[1]

            identities: list[AdminIdentity] = []
            members = await self.bot.get_chat_administrators(int(chat_id))
            for member in members:
                user = getattr(member, "user", None)
                if user is None:
                    continue
                full_name = " ".join(
                    part
                    for part in (
                        getattr(user, "first_name", None),
                        getattr(user, "last_name", None),
                    )
                    if part
                ).strip()
                identities.append(
                    AdminIdentity(
                        user_id=int(user.id),
                        username=getattr(user, "username", None),
                        full_name=full_name,
                    )
                )

            self._cache[int(chat_id)] = (
                time.monotonic() + self.cache_ttl_seconds,
                identities,
            )
            return identities

    @staticmethod
    def _danger_score(text: str) -> tuple[int, list[str]]:
        score = 0
        evidence: list[str] = []
        for index, pattern in enumerate(DANGER_PATTERNS, start=1):
            if pattern.search(text or ""):
                score += 1
                labels = {
                    1: "external link or Telegram destination",
                    2: "request to continue in private messages",
                    3: "wallet/seed/airdrop instruction",
                    4: "financial or reward solicitation",
                    5: "urgent verification/confirmation language",
                }
                evidence.append(labels[index])
        return score, evidence

    async def inspect(self, message) -> FakeAdminFinding | None:
        sender = getattr(message, "from_user", None)
        if sender is None:
            return None

        text = (
            getattr(message, "text", None)
            or getattr(message, "caption", None)
            or ""
        ).strip()
        if not text:
            return None

        admins = await self._admins(int(message.chat.id))
        if not admins:
            return None

        sender_id = int(sender.id)
        if any(item.user_id == sender_id for item in admins):
            return None

        username = getattr(sender, "username", None)
        full_name = " ".join(
            part
            for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if part
        ).strip()

        candidate_values = [
            value
            for value in (_norm(username), _norm(full_name))
            if len(value) >= 3
        ]

        role_marker = bool(
            _has_role_marker(username)
            or _has_role_marker(full_name)
        )

        best_similarity = 0.0
        matched_admin: AdminIdentity | None = None
        matched_label: str | None = None

        for admin in admins:
            admin_values = [
                value
                for value in (_norm(admin.username), _norm(admin.full_name))
                if len(value) >= 3
            ]
            for candidate in candidate_values:
                for actual in admin_values:
                    if min(len(candidate), len(actual)) < 4:
                        continue
                    similarity = SequenceMatcher(None, candidate, actual).ratio()
                    if similarity > best_similarity:
                        best_similarity = similarity
                        matched_admin = admin
                        matched_label = admin.username or admin.full_name

        danger_score, danger_evidence = self._danger_score(text)
        near_copy = best_similarity >= 0.84
        exact_copy = best_similarity >= 0.97

        # Authority words alone are common. Require at least two independent
        # dangerous signals. A near/exact copy of a real admin requires one.
        required_danger = 1 if near_copy else 2
        if not (near_copy or role_marker) or danger_score < required_danger:
            return None

        # Cache is only a performance hint. Before a deterministic BAN path,
        # verify the sender's CURRENT Telegram role so a newly promoted admin
        # can never be punished because the admin cache is stale. If Telegram
        # cannot confirm the role, fail safe and leave the message to the
        # ordinary AI moderation pipeline.
        try:
            current_member = await self.bot.get_chat_member(
                int(message.chat.id),
                sender_id,
            )
            current_status = str(
                getattr(
                    getattr(current_member, "status", ""),
                    "value",
                    getattr(current_member, "status", ""),
                )
            ).casefold()
            if current_status in {"administrator", "creator", "owner"}:
                return None
        except Exception:
            return None

        evidence: list[str] = []
        if near_copy:
            evidence.append(
                f"sender identity closely resembles a real administrator ({best_similarity:.0%})"
            )
        if role_marker:
            evidence.append("sender name claims an administrator/moderator/support role")
        evidence.extend(danger_evidence)

        confidence = 0.94
        if exact_copy and danger_score >= 1:
            confidence = 0.995
        elif near_copy and danger_score >= 2:
            confidence = 0.98
        elif role_marker and danger_score >= 3:
            confidence = 0.97

        target = f"@{matched_label}" if matched_label and not matched_label.startswith("@") else matched_label
        reason = (
            "Potential fake administrator/moderator account: the sender is not a real Telegram "
            "administrator, but its identity suggests authority and the current message contains "
            "dangerous scam/redirect/verification signals."
        )
        if target:
            reason += f" Closest real administrator identity: {target}."

        return FakeAdminFinding(
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            matched_admin=matched_label,
        )

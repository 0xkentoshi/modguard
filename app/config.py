from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    telegram_bot_token: str
    admin_ids: str = ""

    llm_provider: str = "ollama"
    ollama_base_url: str = (
        "http://localhost:11434"
    )

    ollama_fast_model: str = (
        "qwen3:1.7b"
    )

    ollama_model: str = (
        "qwen3:8b"
    )

    llm_timeout_seconds: int = 90
    ollama_think: bool = False
    ollama_keep_alive: str = "24h"
    warmup_models_on_start: bool = True

    fast_num_ctx: int = 2048
    fast_num_predict: int = 120
    deep_num_ctx: int = 4096
    deep_num_predict: int = 320

    database_url: str = (
        "sqlite+aiosqlite:///modguard.db"
    )

    chat_history_limit: int = 20
    user_history_limit: int = 10
    moderation_history_limit: int = 10

    decision_cache_ttl_seconds: int = 45

    dry_run: bool = True

    live_delete_enabled: bool = False

    live_delete_categories: str = (
        "spam,scam,phishing,"
        "malicious_link,"
        "unsolicited_advertising,"
        "flood"
    )

    live_delete_chat_ids: str = ""

    admin_alert_cooldown_seconds: int = 60

    notify_autonomous_actions: bool = False

    # Rare ambiguous cases deserve attention.
    # One compact alert is sent only for a newly created ticket.
    notify_new_tickets: bool = True

    autonomous_ban_threshold: float = 0.92
    autonomous_mute_threshold: float = 0.85
    autonomous_delete_threshold: float = 0.78
    autonomous_warn_threshold: float = 0.70

    # Pilot safety circuit: fail closed to per-chat SHADOW on abnormal bursts
    # or repeated pipeline/action failures. Tunable through .env, intentionally
    # not exposed as everyday renter UI controls.
    safety_circuit_enabled: bool = True
    safety_action_window_seconds: int = 60
    safety_max_destructive_actions: int = 20
    safety_max_punitive_actions: int = 5
    safety_failure_window_seconds: int = 120
    safety_max_execution_failures: int = 3
    safety_max_pipeline_failures: int = 3

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def admin_id_list(
        self,
    ) -> list[int]:
        if not self.admin_ids.strip():
            return []

        return [
            int(
                admin_id.strip()
            )
            for admin_id
            in self.admin_ids.split(",")
            if admin_id.strip()
        ]

    @property
    def live_delete_category_set(
        self,
    ) -> set[str]:
        return {
            item.strip()
            for item
            in self.live_delete_categories
            .split(",")
            if item.strip()
        }

    @property
    def live_delete_chat_id_set(
        self,
    ) -> set[int]:
        if (
            not self.live_delete_chat_ids
            .strip()
        ):
            return set()

        return {
            int(
                item.strip()
            )
            for item
            in self.live_delete_chat_ids
            .split(",")
            if item.strip()
        }


settings = Settings()

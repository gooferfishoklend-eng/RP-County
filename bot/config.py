from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    ai_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    ai_model: str = "claude-opus-5-5"
    db_path: str = "geopolitics.db"
    start_year: int = 2026
    max_actions_per_turn: int = 5
    advisor_cooldown_sec: int = 20
    conference_npc_cooldown_sec: int = 20


def load_settings() -> Settings:
    return Settings()
